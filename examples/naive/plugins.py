"""The same application, written for the conventional registry.

THIS IS A FAITHFUL CONVENTIONAL IMPLEMENTATION, NOT A STRAW MAN. Every plugin
here has a ``teardown``. Every ``teardown`` undoes what its ``setup`` did. No
resource is left behind deliberately and no cleanup call has been removed to
make a point. The services, the routes, the events and the sqlite journals are
imported from ``examples.harness.services`` -- the very same code the cordis
version runs -- so the two applications do the same work in the same way.

What differs is where the knowledge of "what must be undone" lives. Here it
lives in a method written by hand, and a method can only mention what its author
knew about:

* ``mount`` installs an index route as well as the handlers it is given
  (see its docstring). ``KvTool.teardown`` removes the handler paths it
  mounted, because those are the paths this class named. The index route was
  created inside the helper and is not visible from here.
* ``KvTool`` opens a journal connection per shard, and arms a deferred
  compaction, *while serving a request*. ``teardown`` runs in a different method
  written before any request existed.
* ``KvTool`` subscribes to a path's detail event the first time it sees that
  path. Same story: the subscription is created at request time, and
  ``Bus.subscribe`` hands back a removal function that this code has nowhere
  durable to put.
* ``KvTool.setup`` resolves ``store`` once and keeps the reference, which is the
  only thing it can do -- ``registry.require`` returns an object, not a
  subscription. When the store provider is replaced, this reference keeps
  pointing at the old one.

Each of those is a leak that experienced authors ship regularly, and none of
them is visible in a code review of this file: every line is locally correct.
That is the point the comparison makes.
"""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Any

from cordispy.plugins import Bus

from ..harness.plugins import FLUSH_DELAY, HEARTBEAT_PERIOD
from ..harness.services import (
    MemoryStore,
    Metrics,
    Server,
    SqliteStore,
    mount,
    open_connection,
)
from .registry import PluginRegistry

__all__ = [
    "AuditPlugin",
    "BrokenTool",
    "EchoTool",
    "EventsPlugin",
    "KvTool",
    "MemoryStorePlugin",
    "MetricsPlugin",
    "ServerPlugin",
    "SqliteStorePlugin",
]


# --------------------------------------------------------------------------
# providers
# --------------------------------------------------------------------------


class EventsPlugin:
    """Provides ``bus``."""

    name = "events"

    def __init__(self) -> None:
        self.bus = Bus()

    def setup(self, registry: PluginRegistry) -> None:
        registry.provide("bus", self.bus)

    def teardown(self, registry: PluginRegistry) -> None:
        if registry.services.get("bus") is self.bus:
            del registry.services["bus"]
        self.bus.clear()


class MetricsPlugin:
    """Provides ``metrics``."""

    name = "metrics"

    def __init__(self) -> None:
        self.metrics = Metrics()

    def setup(self, registry: PluginRegistry) -> None:
        registry.provide("metrics", self.metrics)

    def teardown(self, registry: PluginRegistry) -> None:
        if registry.services.get("metrics") is self.metrics:
            del registry.services["metrics"]


class ServerPlugin:
    """Provides ``server``."""

    name = "server"

    def __init__(self) -> None:
        self.server: Server | None = None

    def setup(self, registry: PluginRegistry) -> None:
        self.server = Server(bus=registry.require("bus"))
        registry.provide("server", self.server)

    def teardown(self, registry: PluginRegistry) -> None:
        if registry.services.get("server") is self.server:
            del registry.services["server"]


class MemoryStorePlugin:
    """Provides ``store``, backed by a dictionary."""

    name = "store_memory"

    def __init__(self) -> None:
        self.store = MemoryStore()

    def setup(self, registry: PluginRegistry) -> None:
        registry.provide("store", self.store)

    def teardown(self, registry: PluginRegistry) -> None:
        if registry.services.get("store") is self.store:
            del registry.services["store"]
        self.store.close()


class SqliteStorePlugin:
    """Provides ``store``, backed by a real sqlite connection."""

    name = "store_sqlite"

    def __init__(self) -> None:
        self.store = SqliteStore()

    def setup(self, registry: PluginRegistry) -> None:
        registry.provide("store", self.store)

    def teardown(self, registry: PluginRegistry) -> None:
        if registry.services.get("store") is self.store:
            del registry.services["store"]
        self.store.close()


# --------------------------------------------------------------------------
# tools
# --------------------------------------------------------------------------


class EchoTool:
    """Mounts ``/echo/say`` and a heartbeat."""

    name = "tool_echo"

    def __init__(self) -> None:
        self.server: Server | None = None
        self.metrics: Metrics | None = None
        self.heartbeat: asyncio.Task[None] | None = None

    def setup(self, registry: PluginRegistry) -> None:
        self.server = registry.require("server")
        self.metrics = registry.optional("metrics")
        mount(self.server, "/echo", {"/say": self.say})
        self.heartbeat = asyncio.ensure_future(self._heartbeat())

    def teardown(self, registry: PluginRegistry) -> None:
        if self.server is not None:
            self.server.routes.pop("/echo/say", None)
        if self.heartbeat is not None:
            self.heartbeat.cancel()

    def say(self, payload: Any) -> Any:
        if self.metrics is not None:
            self.metrics.incr("echo.say")
        return {"echo": payload}

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_PERIOD)
            if self.metrics is not None:
                self.metrics.incr("echo.heartbeat")


class KvTool:
    """Mounts ``/kv/put`` and ``/kv/get`` over whichever store is registered."""

    name = "tool_kv"

    def __init__(self, detail: list[str] | None = None) -> None:
        self.server: Server | None = None
        self.store: Any = None
        self.bus: Bus | None = None
        self.metrics: Metrics | None = None
        self.journal: sqlite3.Connection | None = None
        self.shards: dict[str, sqlite3.Connection] = {}
        self.traced: set[str] = set()
        self.detail: list[str] = [] if detail is None else detail
        self.flushed: list[str] = []
        self.inflight: set[asyncio.Task[None]] = set()
        self.heartbeat: asyncio.Task[None] | None = None
        self.handlers = {"/put": self.put, "/get": self.get}

    # ------------------------------------------------------------- lifecycle

    def setup(self, registry: PluginRegistry) -> None:
        self.server = registry.require("server")
        # Resolved once. There is nothing else `require` could do: it returns an
        # object, and no part of this model can tell us later that it is stale.
        self.store = registry.require("store")
        self.bus = registry.require("bus")
        self.metrics = registry.optional("metrics")
        self.journal = open_connection()
        mount(self.server, "/kv", self.handlers)
        self.heartbeat = asyncio.ensure_future(self._heartbeat())

    def teardown(self, registry: PluginRegistry) -> None:
        # Undo, from memory, what setup() did.
        if self.server is not None:
            self.server.routes.pop("/kv/put", None)
            self.server.routes.pop("/kv/get", None)
        if self.heartbeat is not None:
            self.heartbeat.cancel()
        if self.journal is not None:
            self.journal.close()

    # --------------------------------------------------------------- handlers

    def put(self, payload: Any) -> Any:
        key = str(payload["key"])
        value = str(payload["value"])
        self.store.put(key, value)
        journal = self._shard(f"shard:{key[:1] or '_'}")
        journal.execute("insert or replace into kv values (?, ?)", (key, value))
        journal.commit()
        self._trace("/kv/put")
        task = asyncio.ensure_future(self._flush_later(key))
        self.inflight.add(task)
        task.add_done_callback(self.inflight.discard)
        if self.metrics is not None:
            self.metrics.incr("kv.put")
        return {"stored": key, "backend": self.store.kind}

    def get(self, payload: Any) -> Any:
        key = str(payload["key"])
        self._trace("/kv/get")
        if self.metrics is not None:
            self.metrics.incr("kv.get")
        return {"key": key, "value": self.store.get(key), "backend": self.store.kind}

    # ---------------------------------------------------------------- helpers

    def _shard(self, name: str) -> sqlite3.Connection:
        connection = self.shards.get(name)
        if connection is None:
            connection = open_connection()
            self.shards[name] = connection
        return connection

    def _trace(self, path: str) -> None:
        if path in self.traced:
            return
        self.traced.add(path)
        if self.bus is not None:
            self.bus.subscribe(f"request:{path}", self._detail)

    def _detail(self, payload: Any) -> None:
        self.detail.append(str(payload))

    async def _flush_later(self, key: str) -> None:
        await asyncio.sleep(FLUSH_DELAY)
        self.flushed.append(key)

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_PERIOD)
            if self.metrics is not None:
                self.metrics.incr("kv.heartbeat")


class AuditPlugin:
    """Records every request the server announces."""

    name = "audit"

    def __init__(self, log: list[str] | None = None) -> None:
        self.log: list[str] = [] if log is None else log
        self.remove: Any = None

    def setup(self, registry: PluginRegistry) -> None:
        bus: Bus = registry.require("bus")
        self.remove = bus.subscribe("request", self.record)

    def teardown(self, registry: PluginRegistry) -> None:
        if self.remove is not None:
            self.remove()

    def record(self, path: str) -> None:
        self.log.append(path)


class BrokenTool:
    """Mounts routes and then fails, halfway through ``setup``."""

    name = "tool_broken"

    def __init__(self) -> None:
        self.server: Server | None = None

    def setup(self, registry: PluginRegistry) -> None:
        self.server = registry.require("server")
        mount(self.server, "/broken", {"/go": lambda payload: "unreachable"})
        raise RuntimeError("tool_broken: invalid configuration, halfway through setup")

    def teardown(self, registry: PluginRegistry) -> None:
        # Never reached: a plugin whose setup raised was never registered, so
        # there is nothing for the registry to call teardown on.
        if self.server is not None:
            self.server.routes.pop("/broken/go", None)
