"""The demonstration application, as cordis components.

A small agent harness in the spirit of DeepSeek Harness: a request dispatcher, a
key/value store behind two interchangeable providers, a metrics counter, two
tools and an audit trail. Every feature is a component, and every mutation a
component performs goes through ``ctx.effect``.

The topology, written as declarations::

    events      -- provides bus
    timer       -- provides timer
    metrics     -- provides metrics
    server      -- injects bus                          -- provides server
    store_*     -- provides store
    tool_echo   -- injects server, timer  (metrics optional)
    tool_kv     -- injects server, store, timer, bus  (metrics optional)
    audit       -- injects bus

Two details in ``tool_kv`` carry most of the demonstration, and both concern
resources created **while serving a request** rather than while loading:

* a per-shard sqlite connection, opened the first time a key in that shard is
  written;
* a deferred compaction timer, armed on every write.

A hand-written teardown cannot see either of them -- it runs in a method whose
author knew only what the setup method did. Here they are ordinary effects, so
they are on the fiber's accumulator the moment they exist, and unloading the
component reverses them without the component's author having to remember
anything.

Note also what ``tool_kv`` does with ``ctx.store``: it reads it once, at load
time, and holds it for the life of the component. In a conventional system that
cached reference is a bug waiting for the provider to be replaced. Here it is
correct, because replacing the provider changes the fiber's target, which
unloads the component and loads it again -- with a fresh read of ``ctx.store``.
The runtime is what makes the obvious code correct.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any

from cordispy import Context, plugin
from cordispy.effect import Disposer

from .services import (
    MemoryStore,
    Metrics,
    Server,
    SqliteStore,
    mount_as_effect,
    open_connection,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from cordispy.plugins import Bus, Timer

__all__ = [
    "FLUSH_DELAY",
    "HEARTBEAT_PERIOD",
    "audit",
    "metrics_plugin",
    "server_plugin",
    "store_memory",
    "store_sqlite",
    "tool_broken",
    "tool_echo",
    "tool_kv",
]

#: How long a deferred compaction waits. Deliberately far longer than any demo
#: run: a task still pending when the measurement is taken is then unambiguously
#: residue rather than a race with the scheduler.
FLUSH_DELAY = 30.0

#: How often a tool reports that it is alive. Also longer than any demo run.
HEARTBEAT_PERIOD = 5.0


# --------------------------------------------------------------------------
# providers
# --------------------------------------------------------------------------


@plugin(name="metrics", provide=["metrics"])
def metrics_plugin(ctx: Context, config: Any) -> None:
    """Provide ``metrics``. Nothing depends on it -- it is optional everywhere."""
    ctx.set("metrics", Metrics())


@plugin(name="server", inject=["bus"], provide=["server"])
def server_plugin(ctx: Context, config: Any) -> None:
    """Provide ``server``, a request dispatcher that announces what it served."""
    ctx.set("server", Server(bus=ctx.bus))


@plugin(name="store_memory", provide=["store"])
def store_memory(ctx: Context, config: Any) -> Disposer:
    """Provide ``store``, backed by a dictionary."""
    store = MemoryStore()
    ctx.set("store", store)
    return store.close


@plugin(name="store_sqlite", provide=["store"])
def store_sqlite(ctx: Context, config: Any) -> Disposer:
    """Provide ``store``, backed by a real sqlite connection.

    The connection is the reason this provider is interesting: swapping it in or
    out has to close something, and the closing has to happen after every
    dependent has stopped using it.
    """
    store = SqliteStore()
    ctx.set("store", store)
    return store.close


# --------------------------------------------------------------------------
# tools
# --------------------------------------------------------------------------


@plugin(
    name="tool_echo",
    inject={"required": ["server", "timer"], "optional": ["metrics"]},
)
def tool_echo(ctx: Context, config: Any) -> None:
    """Mount ``/echo/say`` and a heartbeat."""
    server: Server = ctx.server
    timer: Timer = ctx.timer

    def say(payload: Any) -> Any:
        counter = ctx.optional("metrics")
        if counter is not None:
            counter.incr("echo.say")
        return {"echo": payload}

    mount_as_effect(ctx, server, "/echo", {"/say": say})
    timer.interval(ctx, HEARTBEAT_PERIOD, lambda: _beat(ctx, "echo"))


@plugin(
    name="tool_kv",
    inject={"required": ["server", "store", "timer", "bus"], "optional": ["metrics"]},
)
def tool_kv(ctx: Context, config: Any) -> None:
    """Mount ``/kv/put`` and ``/kv/get`` over whichever store is bound.

    ``config`` may be a list; the tool appends the paths whose detail events it
    observed to it, so a demo can show that the tracing subscriptions were real.
    """
    server: Server = ctx.server
    store = ctx.store
    timer: Timer = ctx.timer
    bus: Bus = ctx.bus
    detail: list[str] = config if isinstance(config, list) else []

    connections: dict[str, sqlite3.Connection] = {}
    traced: set[str] = set()
    flushed: list[str] = []

    def acquire(name: str) -> sqlite3.Connection:
        """Open a journal connection as an effect of this fiber.

        Called from a request handler as readily as from load, and it makes no
        difference: an effect created at any point in the component's life is
        owned by the same accumulator and reversed by the same unload.
        """
        existing = connections.get(name)
        if existing is not None:
            return existing

        def callback() -> Disposer:
            connection = open_connection()
            connections[name] = connection

            def release() -> None:
                connections.pop(name, None)
                connection.close()

            return release

        ctx.effect(callback)
        return connections[name]

    def trace(path: str) -> None:
        """Subscribe to a path's detail event the first time the path is used."""
        if path in traced:
            return
        traced.add(path)
        bus.on(ctx, f"request:{path}", lambda payload: detail.append(path))

    def counted(name: str) -> None:
        counter = ctx.optional("metrics")
        if counter is not None:
            counter.incr(f"kv.{name}")

    def put(payload: Any) -> Any:
        key = str(payload["key"])
        value = str(payload["value"])
        store.put(key, value)
        journal = acquire(f"shard:{key[:1] or '_'}")
        journal.execute("insert or replace into kv values (?, ?)", (key, value))
        journal.commit()
        trace("/kv/put")
        # Deferred compaction, armed per request. The inverse is the runtime's
        # problem, not this function's.
        timer.timeout(ctx, FLUSH_DELAY, lambda: flushed.append(key))
        counted("put")
        return {"stored": key, "backend": store.kind}

    def get(payload: Any) -> Any:
        key = str(payload["key"])
        trace("/kv/get")
        counted("get")
        return {"key": key, "value": store.get(key), "backend": store.kind}

    acquire("journal")
    mount_as_effect(ctx, server, "/kv", {"/put": put, "/get": get})
    timer.interval(ctx, HEARTBEAT_PERIOD, lambda: counted("heartbeat"))


@plugin(name="audit", inject=["bus"])
def audit(ctx: Context, config: Any) -> None:
    """Record every request the server announces.

    ``config`` is the list to append to, which is also how the demo reads the
    trail back out.
    """
    log: list[str] = config if isinstance(config, list) else []
    ctx.bus.on(ctx, "request", lambda path: log.append(path))


@plugin(name="tool_broken", inject=["server"])
def tool_broken(ctx: Context, config: Any) -> None:
    """Mount routes and then fail, to show what a half-applied component leaves.

    The mount happened; the failure happened after it. Algorithm 5 runs the
    inverses accumulated before the failure, so the routes come back out and the
    fiber settles in FAILED with the rest of the system untouched.
    """
    mount_as_effect(ctx, ctx.server, "/broken", {"/go": lambda payload: "unreachable"})
    raise RuntimeError("tool_broken: invalid configuration, halfway through apply")


def _beat(ctx: Context, tool: str) -> None:
    counter = ctx.optional("metrics")
    if counter is not None:
        counter.incr(f"{tool}.heartbeat")
