"""The ``timer`` service: delayed and repeated work, as revertible effects.

Scheduling is the cleanest demonstration of the temporal dimension (paper
section 5.1.1, Algorithm 1). A timer is a mutation of the future: something will
happen that would not otherwise have happened. Its inverse is cancellation.

Every method here is a thin wrapper over ``ctx.effect``, so a timer belongs to
the fiber that armed it. Unloading that component cancels the timer, and -- this
is the part hand-rolled cleanup usually misses -- so does unloading a *provider*
the component depends on, or a parent it was instantiated under, because all
three run the same accumulator.

The inverse both cancels the task **and awaits it**. ``Task.cancel`` only
requests cancellation; the task is still in ``asyncio.all_tasks()`` until the
loop has delivered the ``CancelledError``. Awaiting it is what lets a component
claim, and a test assert, that unloading leaves exactly zero pending tasks.

Because Python has no receiver rebinding, a component passes its own context
explicitly::

    @plugin(name="poller", inject=["timer"])
    def poller(ctx, config):
        ctx.timer.interval(ctx, 1.0, lambda: ctx.get("metrics").incr("tick"))

The reference implementation hides that argument behind a tracker Proxy
(``utils.ts:120-155``); this port declines to reproduce it, so the owning
context is named at the call site.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any, TypeAlias

from ..component import plugin
from ..effect import AsyncDisposer, Disposer, spawn

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..context import Context

__all__ = ["Timer", "TimerCallback", "timer_plugin"]

#: What a timer fires. It is called with no arguments and its result is ignored;
#: a coroutine function would produce a coroutine nobody awaits, so schedule
#: asynchronous follow-up work with :meth:`Timer.timeout` instead.
TimerCallback: TypeAlias = Callable[[], Any]


async def _once(delay: float, fn: TimerCallback) -> None:
    """Fire ``fn`` once, ``delay`` seconds from now."""
    await asyncio.sleep(delay)
    fn()


async def _repeat(period: float, fn: TimerCallback) -> None:
    """Fire ``fn`` every ``period`` seconds until cancelled."""
    while True:
        await asyncio.sleep(period)
        fn()


class Timer:
    """The value bound to the ``timer`` coeffect key."""

    __slots__ = ("_tasks",)

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[None]] = set()

    def __repr__(self) -> str:
        return f"<Timer {len(self._tasks)} armed>"

    @property
    def pending(self) -> int:
        """How many timers are armed right now. For inspection and tests."""
        return len(self._tasks)

    def timeout(self, ctx: Context, delay: float, fn: TimerCallback) -> AsyncDisposer:
        """Fire ``fn`` once after ``delay`` seconds. Returns the inverse.

        The returned disposer is also prepended to ``ctx``'s fiber, so the timer
        is cancelled by that component unloading even if the caller discards it.
        """
        return ctx.effect(lambda: self._arm(_once(delay, fn), f"cordis.timeout<{delay}s>"))

    def interval(self, ctx: Context, period: float, fn: TimerCallback) -> AsyncDisposer:
        """Fire ``fn`` every ``period`` seconds. Returns the inverse.

        A callback that raises ends the interval; the failure reaches the
        ``cordispy.effect`` logger through the task's done callback rather than
        being swallowed.
        """
        return ctx.effect(lambda: self._arm(_repeat(period, fn), f"cordis.interval<{period}s>"))

    async def shutdown(self) -> None:
        """Cancel every timer still armed.

        The inverse of the component itself, and a safety net only: a timer
        armed through :meth:`timeout` or :meth:`interval` is already owned by the
        fiber that armed it, and dependents are drained before this provider
        recovers (paper Algorithm 5, line 25). Anything left here was armed by
        something outside the runtime.
        """
        for task in tuple(self._tasks):
            await self._retract(task)

    def _arm(self, body: Coroutine[Any, Any, None], name: str) -> Disposer:
        """Start one timer task and return the inverse that retracts it."""
        task = spawn(body, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

        # Bind the task to a local before the inverse closes over it. The
        # reference implementation's disposer closes over the *current* sequence
        # number rather than the one captured when the entry was pushed
        # (packages/utils/src/index.ts:19), so disposing an earlier entry
        # removes the most recent one. Python closures capture the same way.
        async def retract() -> None:
            await self._retract(task)

        return retract

    async def _retract(self, task: asyncio.Task[None]) -> None:
        """Cancel a timer and wait for it to actually be gone."""
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        self._tasks.discard(task)


# The component is called ``timer_plugin`` rather than ``timer`` so that
# re-exporting it from ``cordispy.plugins`` does not shadow the module of the same
# name. Its *label* is ``timer``, which is what appears in logs and diagnostics.
@plugin(name="timer", provide=["timer"])
def timer_plugin(ctx: Context, config: Any) -> AsyncDisposer:
    """Provide the ``timer`` service."""
    service = Timer()
    ctx.set("timer", service)
    return service.shutdown
