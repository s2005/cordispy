"""The demonstration application on a conventional plugin registry.

THIS IS A FAITHFUL CONVENTIONAL IMPLEMENTATION, NOT A STRAW MAN. It exists so
that ``run_benefit.py`` compares two real designs rather than one real design
and a caricature. The rules it was written under:

* the leaf services are imported from ``examples.harness.services`` -- the same
  dispatcher, the same stores, the same sqlite journals, the same event bus. The
  two applications do identical work;
* every plugin has a ``teardown``, and every ``teardown`` undoes what its
  ``setup`` did;
* no cleanup call was removed, and no bug was planted, to make the numbers look
  worse.

The residue the comparison measures comes from the shape of the design, not from
mistakes in these files: a service table that cannot express "retire the
previous provider", a dependency lookup that resolves once and is never
invalidated, and a ``teardown`` method that can only mention resources its
author knew about -- which excludes everything created while serving a request.

Modules:

``registry``
    the registry itself: ``provide`` / ``require`` / ``register`` / ``unregister``.

``plugins``
    the application's features as setup/teardown pairs.

``eager``
    a plugin module that resolves its dependency at import time.

``app``
    ``NaiveApp``: the hand-written composition order.
"""

from __future__ import annotations

from .app import EAGER_MODULE, STORE_PLUGINS, NaiveApp, import_eager_tool
from .plugins import (
    AuditPlugin,
    BrokenTool,
    EchoTool,
    EventsPlugin,
    KvTool,
    MemoryStorePlugin,
    MetricsPlugin,
    ServerPlugin,
    SqliteStorePlugin,
)
from .registry import SERVICES, MissingDependencyError, Plugin, PluginRegistry, reset

__all__ = [
    "EAGER_MODULE",
    "SERVICES",
    "STORE_PLUGINS",
    "AuditPlugin",
    "BrokenTool",
    "EchoTool",
    "EventsPlugin",
    "KvTool",
    "MemoryStorePlugin",
    "MetricsPlugin",
    "MissingDependencyError",
    "NaiveApp",
    "Plugin",
    "PluginRegistry",
    "ServerPlugin",
    "SqliteStorePlugin",
    "import_eager_tool",
    "reset",
]
