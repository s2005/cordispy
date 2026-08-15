"""Suite-wide resource guards.

The whole point of this runtime is that a component leaves nothing behind, so
the test suite holds itself to the same standard: a test that finishes with an
asyncio task still pending, or with a tracked sqlite connection still open,
fails on that ground alone.

Both guards measure a *difference* around the test rather than an absolute
count, so anything that legitimately exists on both sides cancels out.
"""

from __future__ import annotations

import asyncio
import gc
import sqlite3
from collections.abc import AsyncIterator

import pytest


def _pending_tasks() -> set[asyncio.Task[object]]:
    """Every task on the running loop that has not finished."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover - no loop in a synchronous test
        return set()
    return {task for task in asyncio.all_tasks() if not task.done()}


def _open_connections() -> set[int]:
    """The identity of every live sqlite connection that still answers a query.

    A closed connection raises ``sqlite3.ProgrammingError``, so this counts open
    handles rather than bookkeeping entries -- the same rule
    ``examples/harness/services.py:open_connections`` uses, but reaching every
    connection in the process rather than only the tracked ones.
    """
    found: set[int] = set()
    for obj in gc.get_objects():
        if isinstance(obj, sqlite3.Connection):
            try:
                obj.execute("select 1")
            except sqlite3.Error:
                continue
            found.add(id(obj))
    return found


@pytest.fixture(autouse=True)
async def _no_leaked_tasks() -> AsyncIterator[None]:
    """Fail a test that leaves an asyncio task pending.

    A cancelled task is not gone when ``cancel()`` returns -- the loop still has
    to deliver the ``CancelledError`` -- so the loop is turned a few times before
    measuring. Nothing here sleeps for a positive time, so a timer armed for
    thirty seconds cannot slip through.
    """
    before = _pending_tasks()
    yield
    for _ in range(5):
        await asyncio.sleep(0)
    current = asyncio.current_task()
    leaked = {task for task in _pending_tasks() - before if task is not current}
    if leaked:
        names = ", ".join(sorted(task.get_name() for task in leaked))
        pytest.fail(f"the test left {len(leaked)} asyncio task(s) pending: {names}")


@pytest.fixture(autouse=True)
def _no_unclosed_connections() -> object:
    """Fail a test that leaves a sqlite connection open.

    This runs around the async guard above rather than inside it: a connection
    is closed synchronously, so there is nothing to wait for.
    """
    before = _open_connections()
    yield
    gc.collect()
    leaked = _open_connections() - before
    if leaked:
        pytest.fail(f"the test left {len(leaked)} sqlite connection(s) open")
