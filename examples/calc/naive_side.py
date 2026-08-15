"""The same calculator on a conventional plugin registry.

THIS IS A FAITHFUL CONVENTIONAL IMPLEMENTATION, NOT A STRAW MAN.

Read :meth:`OperationPlugin.teardown` before anything else. It is *complete*
with respect to what its setup did: it calls ``uninstall``, which removes the
operation from all four tables, and it drops the references it resolved. There
is no forgotten line here and no planted bug. Every difference the demo measures
follows from two properties of the design itself, both already stated in
``examples/naive/registry.py``:

* ``require`` resolves once, at registration, and hands back a direct reference.
  Nothing invalidates it later, because nothing is tracking it. So ``mod`` keeps
  its reference to division after division has been unregistered -- and, worse,
  keeps its catalog entry, so the calculator goes on *offering* a remainder it
  can no longer compute. There is nowhere in this model to say "and withdraw
  everything that was counting on it".
* ``teardown`` is a method, so it can only undo what its author could name when
  they wrote it. A memo cache and its eviction timer are created during
  evaluation, which happens after ``setup`` has returned; they are not reachable
  from inside a method written beforehand.

The arithmetic, the four tables, the caches and the eviction delay are the
shared ``engine`` module, identical to the cordis side. Only composition
differs.
"""

from __future__ import annotations

import asyncio
from typing import Any

from examples.naive.registry import MissingDependencyError, PluginRegistry

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
    "BrokenFactorialPlugin",
    "CalculatorPlugin",
    "NaiveCalculator",
    "OperationPlugin",
]


class CalculatorPlugin:
    """Registers the calculator every operation installs itself into."""

    name = "calculator"

    def __init__(self, calculator: Calculator) -> None:
        self.calculator = calculator

    def setup(self, registry: PluginRegistry) -> None:
        registry.provide("calculator", self.calculator)

    def teardown(self, registry: PluginRegistry) -> None:
        registry.services.pop("calculator", None)


class OperationPlugin:
    """One operation, as a conventional plugin."""

    def __init__(self, operation: Operation, requires: tuple[str, ...] = ()) -> None:
        self.name = f"op.{operation.name}"
        self.operation = operation
        self.requires = requires
        self.calculator: Calculator | None = None
        #: What ``require`` handed back at registration. Never invalidated,
        #: because nothing in this model is tracking it.
        self.resolved: dict[str, Any] = {}

    def setup(self, registry: PluginRegistry) -> None:
        calculator: Calculator = registry.require("calculator")
        self.calculator = calculator
        for key in self.requires:
            self.resolved[key] = registry.require(key)

        name = self.operation.name

        def allocate() -> dict[tuple[float, float], float]:
            """Open this operation's memo cache, mid-evaluation.

            Identical to what the cordis side does, and at the identical moment.
            The difference is only where the cache is recorded: here, in the
            calculator's own dictionaries, reachable from nothing that runs at
            teardown time.
            """
            cache: dict[tuple[float, float], float] = {}
            calculator.caches[name] = cache
            calculator.evictions[name] = asyncio.ensure_future(evict_later(calculator, name))
            return cache

        install(calculator, memoized(self.operation, calculator, allocate))
        registry.provide(self.name, self.operation)

    def teardown(self, registry: PluginRegistry) -> None:
        """Undo what setup did -- completely, and it is still not enough.

        ``uninstall`` removes the operation from all four tables, so this leaves
        nothing of its own behind. What it cannot do is either of the two things
        that actually matter: reach the memo cache and eviction timer evaluation
        created after this method was written, or tell ``mod`` that the division
        it resolved at registration has gone away.
        """
        if self.calculator is not None:
            uninstall(self.calculator, self.operation)
        registry.services.pop(self.name, None)
        self.resolved.clear()


class BrokenFactorialPlugin:
    """An operation that installs itself and then fails to finish loading.

    The order is the realistic one: put yourself into service, then build the
    table your arithmetic needs. The table is what fails, and by then the
    installation has already happened.
    """

    name = "op.factorial"

    def __init__(self) -> None:
        self.calculator: Calculator | None = None

    def setup(self, registry: PluginRegistry) -> None:
        calculator: Calculator = registry.require("calculator")
        self.calculator = calculator
        table: list[float] = []

        def factorial(left: float, right: float) -> float:
            return table[int(left)] * right

        install(calculator, with_fn(FACTORIAL, factorial))
        raise RuntimeError("factorial lookup table could not be built")

    def teardown(self, registry: PluginRegistry) -> None:
        if self.calculator is not None:
            uninstall(self.calculator, FACTORIAL)


# --------------------------------------------------------------------------
# the application
# --------------------------------------------------------------------------


class NaiveCalculator:
    """The same calculator, composed through a conventional registry."""

    def __init__(self) -> None:
        self.registry = PluginRegistry()
        self.calculator = Calculator()
        self.plugins: dict[str, OperationPlugin] = {}
        #: Registrations that raised. A conventional application logs these and
        #: carries on; there is nowhere to record "waiting for its dependency".
        self.rejected: dict[str, str] = {}

    async def start(self, names: tuple[str, ...] = ()) -> None:
        self.registry.register(CalculatorPlugin(self.calculator))
        for name in names:
            await self.add(name)

    async def add(self, name: str, requires: tuple[str, ...] | None = None) -> None:
        """Register one operation, or fail now and stay failed."""
        needs = requirements_for(name) if requires is None else requires
        plugin = OperationPlugin(operation_for(name, self.calculator), needs)
        try:
            self.registry.register(plugin)
        except MissingDependencyError as error:
            # No retry. Registration is a function call that either finds its
            # dependencies or does not.
            self.rejected[name] = f"MissingDependencyError: {error}"
            return
        self.rejected.pop(name, None)
        self.plugins[name] = plugin

    async def add_broken(self) -> None:
        try:
            self.registry.register(BrokenFactorialPlugin())
        except RuntimeError as error:
            self.rejected["factorial"] = f"RuntimeError: {error}"

    async def remove(self, name: str) -> None:
        plugin = self.plugins.pop(name, None)
        if plugin is None:
            return
        self.registry.unregister(plugin.name)

    def state_of(self, name: str) -> str:
        """What the registry can say about an operation.

        Three answers, and none of them is "waiting": it is registered, it was
        rejected, or nobody has heard of it.
        """
        if name in self.plugins:
            return "REGISTERED"
        if name in self.rejected:
            return "REJECTED"
        return "ABSENT"

    def evaluate(self, text: str) -> str:
        """Evaluate, or report the failure the way a calculator would."""
        try:
            return format_result(self.calculator.evaluate(text))
        except Exception as error:
            return f"{type(error).__name__}: {error}"

    async def shutdown(self) -> None:
        for name in list(self.plugins):
            await self.remove(name)
