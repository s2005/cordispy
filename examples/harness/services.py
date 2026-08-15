"""The leaf services of the demonstration application.

Nothing in this module knows about components, fibers or plugin registries. It
is the part of the application that would exist whichever plugin system it was
wired up with: a request dispatcher, two key/value stores, a metrics counter and
a tracked sqlite connection factory.

``examples.naive`` imports this module too, and that is the point. The two
implementations of the application must differ *only* in how features are
composed and taken apart. If they differed in what the features do, the measured
comparison in ``run_benefit.py`` would prove nothing.

The one measurement apparatus that lives here is the connection tracker. It does
not keep connections alive on its own account -- ``open_connections`` asks each
tracked connection whether it still works, so what is counted is the state of
the live process rather than the state of a bookkeeping list.
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, TypeAlias

if TYPE_CHECKING:  # pragma: no cover - typing only
    from cordispy import AsyncDisposer, Context, Disposer
    from cordispy.plugins import Bus

__all__ = [
    "Handler",
    "MemoryStore",
    "Metrics",
    "RouteError",
    "Server",
    "SqliteStore",
    "Store",
    "StoreClosedError",
    "mount",
    "mount_as_effect",
    "open_connection",
    "open_connections",
    "reset_connections",
    "unmount",
]

#: A request handler: it receives the request payload and returns a response.
Handler: TypeAlias = Callable[[Any], Any]


# --------------------------------------------------------------------------
# tracked sqlite connections
# --------------------------------------------------------------------------

_TRACKED: list[sqlite3.Connection] = []


def open_connection() -> sqlite3.Connection:
    """Open an in-memory sqlite connection and remember it for measurement."""
    connection = sqlite3.connect(":memory:")
    connection.execute("create table if not exists kv (k text primary key, v text)")
    connection.commit()
    _TRACKED.append(connection)
    return connection


def open_connections() -> int:
    """How many tracked connections are still usable.

    Every tracked connection is asked to run a trivial statement. A closed one
    raises ``sqlite3.ProgrammingError``, so this counts connections that are
    genuinely still open rather than connections somebody forgot to deregister.
    """
    live = 0
    for connection in _TRACKED:
        try:
            connection.execute("select 1")
        except sqlite3.ProgrammingError:
            continue
        live += 1
    return live


def reset_connections() -> None:
    """Close every tracked connection and forget them. Between scenarios."""
    for connection in _TRACKED:
        with contextlib.suppress(sqlite3.Error):
            connection.close()
    _TRACKED.clear()


# --------------------------------------------------------------------------
# services
# --------------------------------------------------------------------------


class Metrics:
    """A counter bag. The optional dependency of the two tools."""

    __slots__ = ("counters",)

    def __init__(self) -> None:
        self.counters: dict[str, int] = {}

    def __repr__(self) -> str:
        return f"<Metrics {self.total()} counted>"

    def incr(self, name: str, amount: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + amount

    def total(self) -> int:
        return sum(self.counters.values())


class RouteError(LookupError):
    """No handler is mounted at the requested path."""


class Server:
    """A request dispatcher standing in for an HTTP server.

    ``routes`` is a plain dictionary, deliberately. Both implementations of the
    application mount handlers into the same structure; only the way they are
    taken out again differs.
    """

    __slots__ = ("bus", "routes", "served")

    def __init__(self, bus: Bus | None = None) -> None:
        self.routes: dict[str, Handler] = {}
        self.bus = bus
        self.served = 0

    def __repr__(self) -> str:
        return f"<Server {len(self.routes)} routes, {self.served} served>"

    @property
    def route_count(self) -> int:
        return len(self.routes)

    def dispatch(self, path: str, payload: Any = None) -> Any:
        """Serve one request.

        The two events are announced *after* the handler returns, so a handler
        that subscribes to its own detail event while serving starts receiving
        it from the next request rather than re-entrantly during this one.
        """
        handler = self.routes.get(path)
        if handler is None:
            raise RouteError(f"no route mounted at {path!r}")
        self.served += 1
        response = handler(payload)
        if self.bus is not None:
            self.bus.emit("request", path)
            self.bus.emit(f"request:{path}", payload)
        return response


def mount(server: Server, prefix: str, handlers: Mapping[str, Handler]) -> None:
    """Mount a tool's endpoints under ``prefix``.

    One route per entry, at ``prefix + suffix``, **plus an index route at**
    ``prefix`` itself that lists them. The index is worth noticing: it is
    created inside this helper and never named at the call site, so a teardown
    written by listing the paths the caller passed in will not remove it.
    """
    for suffix, handler in handlers.items():
        server.routes[prefix + suffix] = handler
    listing = sorted(prefix + suffix for suffix in handlers)
    server.routes[prefix] = lambda payload: listing


def unmount(server: Server, prefix: str, handlers: Mapping[str, Handler]) -> None:
    """Remove exactly what :func:`mount` added, index route included."""
    for suffix in handlers:
        server.routes.pop(prefix + suffix, None)
    server.routes.pop(prefix, None)


def mount_as_effect(
    ctx: Context,
    server: Server,
    prefix: str,
    handlers: Mapping[str, Handler],
) -> AsyncDisposer:
    """Mount routes as a revertible effect of ``ctx``'s fiber.

    The inverse is written once, next to the thing it inverts, and the runtime
    is what remembers to run it -- which is the whole difference between this
    and a hand-written ``teardown``.
    """

    def callback() -> Disposer:
        mount(server, prefix, handlers)
        return lambda: unmount(server, prefix, handlers)

    return ctx.effect(callback)


# --------------------------------------------------------------------------
# stores
# --------------------------------------------------------------------------


class StoreClosedError(RuntimeError):
    """The store was used after its provider released it."""


class MemoryStore:
    """A dictionary-backed store. Holds no operating-system resource."""

    kind = "memory"

    __slots__ = ("_data", "closed")

    def __init__(self) -> None:
        self._data: dict[str, str] = {}
        self.closed = False

    def __repr__(self) -> str:
        return f"<MemoryStore {len(self._data)} keys{' closed' if self.closed else ''}>"

    def put(self, key: str, value: str) -> None:
        self._guard()
        self._data[key] = value

    def get(self, key: str) -> str | None:
        self._guard()
        return self._data.get(key)

    def keys(self) -> list[str]:
        self._guard()
        return sorted(self._data)

    def close(self) -> None:
        """Release the store. Using it afterwards raises, as a real one would."""
        self.closed = True

    def _guard(self) -> None:
        if self.closed:
            raise StoreClosedError("the memory store has been closed by its provider")


class SqliteStore:
    """A store backed by a real ``sqlite3`` connection that must be closed."""

    kind = "sqlite"

    __slots__ = ("connection",)

    def __init__(self) -> None:
        self.connection = open_connection()

    def __repr__(self) -> str:
        return "<SqliteStore>"

    def put(self, key: str, value: str) -> None:
        self.connection.execute("insert or replace into kv values (?, ?)", (key, value))
        self.connection.commit()

    def get(self, key: str) -> str | None:
        row = self.connection.execute("select v from kv where k = ?", (key,)).fetchone()
        return None if row is None else str(row[0])

    def keys(self) -> list[str]:
        return sorted(str(row[0]) for row in self.connection.execute("select k from kv"))

    def close(self) -> None:
        self.connection.close()


#: Either store. They are interchangeable, which is what makes the hot-swap
#: scenario a swap of *providers* rather than a rewrite of the consumers.
Store: TypeAlias = "MemoryStore | SqliteStore"
