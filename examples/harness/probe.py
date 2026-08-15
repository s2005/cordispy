"""Measuring what a plugin leaves behind.

Every number the benefit demo prints comes from here, and every one of them is
read out of the live process:

``routes``
    ``len(server.routes)`` -- the actual dictionary the dispatcher looks in.

``subscribers``
    ``bus.subscribers()`` -- the actual handler lists.

``connections``
    the number of tracked ``sqlite3`` connections that still answer a query.
    A closed connection raises, so this counts open file handles rather than
    bookkeeping entries.

``tasks``
    the difference of two ``asyncio.all_tasks()`` sets. Comparing sets rather
    than counts means the current task, and any task that merely happened to
    exist on both sides, cancel out.

Residue is always a *difference* between two snapshots taken around the thing
being measured, never an absolute count: the question is what the component left
behind, not how big the application is.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .services import open_connections

if TYPE_CHECKING:  # pragma: no cover - typing only
    from cordispy.plugins import Bus

    from .services import Server

__all__ = ["Residue", "Snapshot", "compare", "reclaim", "snapshot", "turn"]


async def turn(times: int = 3) -> None:
    """Let the event loop run without advancing the clock.

    A cancelled task is not gone when ``cancel()`` returns; the loop still has
    to deliver the ``CancelledError``. Yielding a few times before measuring is
    what makes "zero pending tasks" a statement about the process rather than a
    statement about scheduling luck. Nothing here sleeps for a positive time, so
    a timer armed for thirty seconds cannot slip through.
    """
    for _ in range(times):
        await asyncio.sleep(0)


@dataclass(frozen=True)
class Snapshot:
    """What the process looked like at one instant."""

    routes: int
    subscribers: int
    connections: int
    tasks: frozenset[asyncio.Task[Any]]

    def __repr__(self) -> str:
        return (
            f"Snapshot(routes={self.routes}, subscribers={self.subscribers}, "
            f"connections={self.connections}, tasks={len(self.tasks)})"
        )


@dataclass(frozen=True)
class Residue:
    """What a component left behind: an itemized difference of two snapshots."""

    routes: int
    subscribers: int
    connections: int
    tasks: int

    @property
    def total(self) -> int:
        return self.routes + self.subscribers + self.connections + self.tasks

    @property
    def clean(self) -> bool:
        """True when the process returned exactly to its pre-composition state."""
        return self.total == 0

    def itemize(self) -> str:
        """A one-line breakdown, for the demo's verdict lines."""
        if self.clean:
            return "nothing"
        parts = [
            f"{value} {label}" if value == 1 else f"{value} {label}s"
            for label, value in (
                ("route handler", self.routes),
                ("event subscriber", self.subscribers),
                ("sqlite connection", self.connections),
                ("pending task", self.tasks),
            )
            if value
        ]
        return ", ".join(parts)


async def snapshot(server: Server | None = None, bus: Bus | None = None) -> Snapshot:
    """Take a measurement, after letting the loop settle."""
    await turn()
    return Snapshot(
        routes=0 if server is None else server.route_count,
        subscribers=0 if bus is None else bus.subscribers(),
        connections=open_connections(),
        tasks=frozenset(asyncio.all_tasks()),
    )


def compare(before: Snapshot, after: Snapshot) -> Residue:
    """The residue: what ``after`` has that ``before`` did not."""
    return Residue(
        routes=after.routes - before.routes,
        subscribers=after.subscribers - before.subscribers,
        connections=after.connections - before.connections,
        tasks=len(after.tasks - before.tasks),
    )


async def reclaim(before: Snapshot, after: Snapshot) -> None:
    """Cancel the tasks that leaked, so the demo itself ends clean.

    This is deliberately *outside* both implementations. It is the demo tidying
    up after proving that a leak exists, by hand and from the outside -- which is
    the only place the information needed to do it was ever available.
    """
    leaked = [task for task in after.tasks - before.tasks if not task.done()]
    if not leaked:
        return
    for task in leaked:
        task.cancel()
    await asyncio.gather(*leaked, return_exceptions=True)
