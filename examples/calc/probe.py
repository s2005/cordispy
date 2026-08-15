"""Tidying up after the comparison has been made.

Deliberately *outside* both implementations, exactly as
``examples/harness/probe.py`` is. An eviction timer that outlived the operation
which armed it is the thing this demo exists to demonstrate; cancelling it from
inside either implementation would be cheating, and cancelling it from inside the
conventional one would be impossible anyway -- that is the point.

So the demo cleans up by hand, from the outside, after proving the leak exists.
That is also the position a real operator is left in: holding a process with
timers in it that nothing else remembers arming.
"""

from __future__ import annotations

import asyncio

from .engine import Calculator

__all__ = ["leaked_tasks", "reclaim"]


def leaked_tasks(calculator: Calculator) -> list[asyncio.Task[None]]:
    """Eviction timers still pending, whatever armed them."""
    return [task for task in calculator.evictions.values() if not task.done()]


async def reclaim(calculator: Calculator) -> None:
    """Cancel every pending eviction timer and forget the caches they guarded.

    Called by the demo and by the tests once a measurement has been taken, so
    that proving a leak does not itself leave one behind.
    """
    pending = leaked_tasks(calculator)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    calculator.evictions.clear()
    calculator.caches.clear()
