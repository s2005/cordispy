"""The calculator, composed of cordis components.

One component per operation. Each one declares what it needs, installs itself as
a revertible effect, and provides its own key so that anything built on top of
it can declare *that*.

The two lines that carry the demonstration:

* ``install_as_effect`` writes the inverse next to the thing it inverts, so an
  operation going away removes it from all four tables without anyone
  maintaining a teardown;
* ``mod`` declares ``inject=["op.sub", "op.mul", "op.div"]``, because a
  remainder is *defined* as ``a - floor(a / b) * b``. That is the whole of its
  dependency management. Retire ``div`` and the runtime deactivates ``mod``,
  which withdraws its catalog entry -- so the calculator stops offering a
  remainder the moment it stops being able to compute one.

The memo cache is opened inside the operator function, which is to say *while
evaluating an expression*, long after ``apply`` returned. It is an ordinary
``ctx.effect`` and therefore on the fiber's accumulator the instant it exists.
"""

from __future__ import annotations

import asyncio
from typing import Any

from cordispy import Component, Context, Fiber, plugin
from cordispy.effect import Disposer

from .engine import (
    FACTORIAL,
    Calculator,
    Operation,
    evict_later,
    format_result,
    install,
    memoized,
    operation_for,
    requirements_for,
    uninstall,
    with_fn,
)

__all__ = [
    "CordisCalculator",
    "broken_factorial",
    "calculator_plugin",
    "install_as_effect",
    "operation_component",
]


@plugin(name="calculator", provide=["calculator"])
def calculator_plugin(ctx: Context, config: Any) -> None:
    """Provide the calculator every operation installs itself into."""
    ctx.set("calculator", config if isinstance(config, Calculator) else Calculator())


def install_as_effect(ctx: Context, calculator: Calculator, operation: Operation) -> None:
    """Put an operation into service as a revertible effect of ``ctx``'s fiber.

    The inverse is one line, written beside the thing it inverts. Nobody has to
    remember to call it, which is the entire difference between this and a
    hand-written ``teardown``.
    """

    def callback() -> Disposer:
        install(calculator, operation)
        return lambda: uninstall(calculator, operation)

    ctx.effect(callback)


def operation_component(operation: Operation, requires: tuple[str, ...] = ()) -> Component:
    """Build the component that puts ``operation`` into service.

    ``requires`` is a tuple of other operation keys. It is the only dependency
    management in this file: declaring ``op.div`` is how ``mod`` says that it
    cannot work without division, and the runtime does the rest.
    """
    key = f"op.{operation.name}"

    @plugin(name=key, inject={"required": ["calculator", *requires]}, provide=[key])
    def apply(ctx: Context, config: Any) -> None:
        calculator: Calculator = ctx.calculator

        def allocate() -> dict[tuple[float, float], float]:
            """Open this operation's memo cache, mid-evaluation, as an effect."""

            def callback() -> Disposer:
                cache: dict[tuple[float, float], float] = {}
                calculator.caches[operation.name] = cache
                task = asyncio.ensure_future(evict_later(calculator, operation.name))
                calculator.evictions[operation.name] = task

                def release() -> None:
                    calculator.caches.pop(operation.name, None)
                    pending = calculator.evictions.pop(operation.name, None)
                    if pending is not None:
                        pending.cancel()

                return release

            ctx.effect(callback)
            return calculator.caches[operation.name]

        install_as_effect(ctx, calculator, memoized(operation, calculator, allocate))
        ctx.set(key, operation)

    return apply


@plugin(name="op.factorial", inject={"required": ["calculator"]}, provide=["op.factorial"])
def broken_factorial(ctx: Context, config: Any) -> None:
    """An operation that installs itself and then fails to finish loading.

    The order matters and is the realistic one: it puts itself into service
    first, then builds the lookup table its arithmetic needs, and the table is
    what fails. Everything before the failure has already happened.
    """
    calculator: Calculator = ctx.calculator
    table: list[float] = []

    def factorial(left: float, right: float) -> float:
        return table[int(left)] * right

    install_as_effect(ctx, calculator, with_fn(FACTORIAL, factorial))
    raise RuntimeError("factorial lookup table could not be built")


# --------------------------------------------------------------------------
# the application
# --------------------------------------------------------------------------


class CordisCalculator:
    """Compose operations in and out, and evaluate expressions."""

    def __init__(self) -> None:
        self.root = Context()
        self.fibers: dict[str, Fiber] = {}
        self.calculator = Calculator()

    async def start(self, names: tuple[str, ...] = ()) -> None:
        """Provide the calculator, then compose the named operations."""
        self.root.use(calculator_plugin, self.calculator)
        await self.root.registry.settle()
        for name in names:
            await self.add(name)

    async def add(self, name: str, requires: tuple[str, ...] | None = None) -> Fiber:
        """Compose one operation in. Order does not matter; that is the point."""
        operation = operation_for(name, self.calculator)
        needs = requirements_for(name) if requires is None else requires
        fiber = self.root.use(operation_component(operation, needs))
        self.fibers[name] = fiber
        await self.root.registry.settle()
        return fiber

    async def add_broken(self) -> Fiber:
        """Compose the operation that raises halfway through loading."""
        fiber = self.root.use(broken_factorial)
        self.fibers["factorial"] = fiber
        await self.root.registry.settle()
        return fiber

    async def remove(self, name: str) -> None:
        """Retire one operation. Idempotent, like ``retire`` itself."""
        fiber = self.fibers.pop(name, None)
        if fiber is None:
            return
        await fiber.retire()
        await self.root.registry.settle()

    def state_of(self, name: str) -> str:
        """The runtime's own word for what an operation is doing."""
        fiber = self.fibers.get(name)
        return "ABSENT" if fiber is None else fiber.state.name

    def evaluate(self, text: str) -> str:
        """Evaluate, or report the failure the way a calculator would."""
        try:
            return format_result(self.calculator.evaluate(text))
        except Exception as error:
            return f"{type(error).__name__}: {error}"

    async def shutdown(self) -> None:
        for name in list(self.fibers):
            await self.remove(name)
        await self.root.registry.settle()
