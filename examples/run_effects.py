"""Example 1: revertible effects -- the temporal dimension on its own.

An effect is applied together with the inverse that undoes it (paper section
5.1.1, Algorithm 1). The runtime does not check that an inverse really reverses
anything -- that stays an obligation on the component author -- but it does
guarantee exactly *when* the inverses run: last-applied first, once, and at a
step boundary rather than in the middle of one.

Five sections, each runnable on its own::

    uv run python examples/run_effects.py --section forms
    uv run python examples/run_effects.py --section all --verbose

``forms``
    the five accepted callback shapes.
``order``
    recovery runs last-applied-first, and runs at most once.
``guard``
    an interrupted effect keeps only the inverses it had accumulated, and an
    interrupted generator still runs its ``finally``.
``fiber``
    an effect belongs to the fiber that created it, so unloading the component
    recovers it -- including effects created long after the component loaded.
``plugins``
    the built-in ``timer`` and ``bus`` services, and the pending-task and
    subscriber counts before and after an unload.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cordispy import Context, FiberState, execute, plugin
from cordispy.plugins import Bus, Timer, events_plugin, timer_plugin


def heading(title: str) -> None:
    print()
    print(title)
    print("-" * len(title))


def show(label: str, value: Any) -> None:
    print(f"  {label:<44} {value}")


# --------------------------------------------------------------------------
# the five callback forms
# --------------------------------------------------------------------------


async def section_forms(verbose: bool) -> None:
    heading("the five accepted effect-callback forms")
    root = Context()

    log: list[str] = []

    def nothing() -> None:
        log.append("applied")

    def single() -> Any:
        log.append("applied")
        return lambda: log.append("recovered")

    async def asynchronous() -> Any:
        log.append("applied")
        return lambda: log.append("recovered")

    def generator() -> Iterator[Any]:
        log.append("applied step 1")
        yield lambda: log.append("recovered step 1")
        log.append("applied step 2")
        yield lambda: log.append("recovered step 2")

    async def async_generator() -> AsyncIterator[Any]:
        log.append("applied step 1")
        yield lambda: log.append("recovered step 1")
        log.append("applied step 2")
        yield lambda: log.append("recovered step 2")

    for name, callback in (
        ("def cb() -> None", nothing),
        ("def cb() -> Disposer", single),
        ("async def cb() -> Disposer | None", asynchronous),
        ("def cb() -> Generator[Disposer]", generator),
        ("async def cb() -> AsyncGenerator[Disposer]", async_generator),
    ):
        log.clear()
        dispose = root.effect(callback)
        # A synchronous callback has already finished; an asynchronous one is
        # driven by a task, which has not started until the loop yields.
        await asyncio.sleep(0)
        applied = list(log)
        await dispose()
        show(name, f"applied {applied} then {log[len(applied) :]}")

    if verbose:
        print()
        print("  Note the asynchronous forms: `ctx.effect` is a synchronous call that")
        print("  returns an awaitable disposer. A wholly synchronous effect has already")
        print("  run by the time it returns and needs no event loop at all.")


# --------------------------------------------------------------------------
# LIFO order and idempotence
# --------------------------------------------------------------------------


async def section_order(verbose: bool) -> None:
    heading("recovery is last-applied-first, and happens at most once")
    root = Context()
    log: list[str] = []

    for index in range(4):
        root.effect(lambda index=index: lambda: log.append(f"undo-{index}"))
    show("applied", "effect-0 effect-1 effect-2 effect-3")
    await root.fiber.dispose()
    show("recovered", " ".join(log))

    log.clear()
    dispose = root.effect(lambda: lambda: log.append("undo"))
    await dispose()
    await dispose()
    await dispose()
    show("three calls to the same disposer", f"{len(log)} recovery run")

    if verbose:
        print()
        print("  Firing an inverse twice would apply it at a state no application of the")
        print("  effect ever produced, so the accumulator drains itself before running.")


# --------------------------------------------------------------------------
# the guard
# --------------------------------------------------------------------------


async def section_guard(verbose: bool) -> None:
    heading("an interrupted effect keeps only what it had accumulated")
    log: list[str] = []
    armed = {"value": True}

    async def callback() -> AsyncIterator[Any]:
        try:
            yield lambda: log.append("undo-a")
            armed["value"] = False  # the guard trips before the next step
            yield lambda: log.append("undo-b")
            log.append("this step never runs")
            yield lambda: log.append("undo-c")
        finally:
            log.append("generator closed")

    chain = await execute(callback, lambda: armed["value"])
    show("after the guard tripped", " ".join(log))
    await chain()
    show("inverses that ran", " ".join(item for item in log if item.startswith("undo")))
    show("the step after the guard", "never ran" if "this step never runs" not in log else "ran")

    if verbose:
        print()
        print("  The reference implementation abandons an interrupted async generator")
        print("  without closing it (fiber.ts:263), so a try/finally inside a generator")
        print("  effect never runs its cleanup. This port always closes it.")


# --------------------------------------------------------------------------
# effects belong to a fiber
# --------------------------------------------------------------------------


async def section_fiber(verbose: bool) -> None:
    heading("an effect belongs to the fiber that created it")
    root = Context()
    log: list[str] = []
    handle: dict[str, Context] = {}

    @plugin(name="component")
    def component(ctx: Context, config: Any) -> None:
        handle["ctx"] = ctx
        ctx.effect(lambda: lambda: log.append("undo-at-load-time"))

    fiber = root.use(component)
    await root.registry.settle()
    show("component state", fiber.state.value)

    # Long after loading, from what would be a request handler.
    handle["ctx"].effect(lambda: lambda: log.append("undo-created-later"))
    show("a second effect created after loading", "registered nowhere but on the fiber")

    await fiber.retire()
    show("recovered on unload", " ".join(log))
    show("component state", fiber.state.value)

    if verbose:
        print()
        print("  Nothing registered the second effect for cleanup. It was created through")
        print("  the component's own context, and that is the whole registration.")


# --------------------------------------------------------------------------
# the built-in plugins
# --------------------------------------------------------------------------


async def section_plugins(verbose: bool) -> None:
    heading("the built-in timer and bus services leave nothing behind")
    root = Context()
    fired: list[str] = []

    @plugin(name="worker", inject=["timer", "bus"])
    def worker(ctx: Context, config: Any) -> None:
        timer: Timer = ctx.timer
        bus: Bus = ctx.bus
        # A repeating timer, a one-shot far in the future, and a subscription.
        timer.interval(ctx, 3600.0, lambda: fired.append("tick"))
        timer.timeout(ctx, 3600.0, lambda: fired.append("once"))
        bus.on(ctx, "request", lambda path: fired.append(str(path)))

    root.use(timer_plugin)
    root.use(events_plugin)
    await root.registry.settle()

    # The baseline is taken before the worker exists, so the numbers below are
    # what the worker itself added and what it left behind.
    baseline = len(asyncio.all_tasks())
    timer: Timer = root.get("timer")
    bus: Bus = root.get("bus")

    fiber = root.use(worker)
    await root.registry.settle()
    bus.emit("request", "/hello")
    show("worker state", fiber.state.value)
    show("timers armed", timer.pending)
    show("bus subscribers", bus.subscribers())
    show("events delivered", fired)
    show("pending asyncio tasks (delta)", len(asyncio.all_tasks()) - baseline)

    await fiber.retire()
    for _ in range(3):
        await asyncio.sleep(0)

    show("after retiring the worker: state", fiber.state.value)
    show("timers armed", timer.pending)
    show("bus subscribers", bus.subscribers())
    show("pending asyncio tasks (delta)", len(asyncio.all_tasks()) - baseline)
    assert fiber.state is FiberState.DISPOSED

    if verbose:
        print()
        print("  The timer's inverse cancels the task and then awaits it. `cancel()` only")
        print("  requests cancellation; without the await the task would still be in")
        print("  asyncio.all_tasks() when the count was taken.")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

SECTIONS = {
    "forms": section_forms,
    "order": section_order,
    "guard": section_guard,
    "fiber": section_fiber,
    "plugins": section_plugins,
}


async def run(names: list[str], verbose: bool) -> None:
    for name in names:
        await SECTIONS[name](verbose)
    print()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run_effects.py",
        description="Revertible effects: the five callback forms, LIFO recovery, and the guard.",
    )
    parser.add_argument(
        "--section",
        choices=[*SECTIONS, "all"],
        default="all",
        help="which part to demonstrate (default: all)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="add a note explaining what each section shows",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    names = list(SECTIONS) if args.section == "all" else [args.section]
    asyncio.run(run(names, args.verbose))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
