"""The demonstration application: an agent harness where every feature is a plugin.

Four modules:

``services``
    the leaf services -- request dispatcher, stores, metrics, tracked sqlite
    connections. Shared with ``examples.naive`` so that the two implementations
    differ only in composition.

``plugins``
    those services wrapped as cordis components.

``app``
    ``Harness``: compose, swap, remove, shut down.

``probe``
    the measurement apparatus used by ``run_benefit.py``.

``lazy``
    a one-component module that declares ``store`` and resolves nothing at
    import time. The counterpart of ``examples.naive.eager``, imported for real
    by the late-arrival scenario so that both sides of that row are measured.
"""

from __future__ import annotations

from .app import LAZY_MODULE, STORE_PROVIDERS, Harness, import_lazy_tool
from .plugins import (
    FLUSH_DELAY,
    HEARTBEAT_PERIOD,
    audit,
    metrics_plugin,
    server_plugin,
    store_memory,
    store_sqlite,
    tool_broken,
    tool_echo,
    tool_kv,
)
from .probe import Residue, Snapshot, compare, reclaim, snapshot, turn
from .services import (
    MemoryStore,
    Metrics,
    RouteError,
    Server,
    SqliteStore,
    StoreClosedError,
    mount,
    mount_as_effect,
    open_connection,
    open_connections,
    reset_connections,
    unmount,
)

__all__ = [
    "FLUSH_DELAY",
    "HEARTBEAT_PERIOD",
    "LAZY_MODULE",
    "STORE_PROVIDERS",
    "Harness",
    "MemoryStore",
    "Metrics",
    "Residue",
    "RouteError",
    "Server",
    "Snapshot",
    "SqliteStore",
    "StoreClosedError",
    "audit",
    "compare",
    "import_lazy_tool",
    "metrics_plugin",
    "mount",
    "mount_as_effect",
    "open_connection",
    "open_connections",
    "reclaim",
    "reset_connections",
    "server_plugin",
    "snapshot",
    "store_memory",
    "store_sqlite",
    "tool_broken",
    "tool_echo",
    "tool_kv",
    "turn",
    "unmount",
]
