"""Composing the demonstration application on the conventional registry.

THIS IS A FAITHFUL CONVENTIONAL IMPLEMENTATION, NOT A STRAW MAN. Compare this
file with ``examples.harness.app``: the difference in size is the point. The
cordis version has no ordering table and no teardown list, because the runtime
derives both from the declarations. Here the order is written out by hand, and
it has to be right -- ``ServerPlugin`` must come after ``EventsPlugin``, and both
tools after everything -- because ``registry.require`` resolves once and fails
if it is early.
"""

from __future__ import annotations

import importlib
import sys
from typing import Any

from cordispy.plugins import Bus

from ..harness.services import Server
from . import registry as registry_module
from .plugins import (
    AuditPlugin,
    EchoTool,
    EventsPlugin,
    KvTool,
    MemoryStorePlugin,
    MetricsPlugin,
    ServerPlugin,
    SqliteStorePlugin,
)
from .registry import Plugin, PluginRegistry

__all__ = ["EAGER_MODULE", "STORE_PLUGINS", "NaiveApp", "import_eager_tool"]

#: The two providers of ``store``, matching ``harness.app.STORE_PROVIDERS``.
STORE_PLUGINS = {"memory": MemoryStorePlugin, "sqlite": SqliteStorePlugin}

#: The module whose import resolves its dependency at import time.
EAGER_MODULE = "examples.naive.eager"


def import_eager_tool() -> Any:
    """Import the eager plugin module afresh, letting its lookup happen now.

    Dropping it from ``sys.modules`` first is what makes this repeatable across
    scenarios; in a real process the import happens once, at start-up, and
    whether it succeeds is decided by the order of the import statements.
    """
    sys.modules.pop(EAGER_MODULE, None)
    return importlib.import_module(EAGER_MODULE)


class NaiveApp:
    """The demonstration application, composed by hand."""

    def __init__(self) -> None:
        registry_module.reset()
        self.registry = PluginRegistry()
        self.audit_log: list[str] = []
        self.traced: list[str] = []
        self.instances: dict[str, Plugin] = {}

    def __repr__(self) -> str:
        return f"<NaiveApp {len(self.registry.plugins)} plugins>"

    # ------------------------------------------------------------ composition

    def add(self, plugin: Plugin) -> Plugin:
        """Register one plugin. Raises if its dependencies are not there yet."""
        self.registry.register(plugin)
        self.instances[plugin.name] = plugin
        return plugin

    def remove(self, name: str) -> None:
        """Unregister one plugin, running its ``teardown``."""
        self.registry.unregister(name)
        self.instances.pop(name, None)

    def start(
        self,
        *,
        store: str | None = "memory",
        tools: tuple[str, ...] = ("tool_echo", "tool_kv"),
    ) -> None:
        """Compose the application, in the one order that works.

        ``store=None`` omits the store provider, which is how the late-arrival
        scenario starts. ``KvTool`` cannot be registered at all in that case --
        its ``setup`` raises ``MissingDependencyError`` -- so the caller has to be
        prepared for the registration itself to fail.
        """
        self.add(EventsPlugin())
        self.add(MetricsPlugin())
        self.add(ServerPlugin())
        self.add(AuditPlugin(self.audit_log))
        if store is not None:
            self.add(STORE_PLUGINS[store]())
        if "tool_echo" in tools:
            self.add(EchoTool())
        if "tool_kv" in tools:
            self.add(KvTool(self.traced))

    def swap_store(self, kind: str) -> Plugin:
        """Replace the ``store`` provider while the application is running.

        The registry offers one way to do this: register the new provider, which
        overwrites the service entry, then unregister the old one so that it
        releases what it held. Both steps are correct in themselves. Neither
        tells ``KvTool`` that the object it resolved at registration time is now
        a closed store, because nothing in this model can.
        """
        replacement = self.add(STORE_PLUGINS[kind]())
        for name in ("store_memory", "store_sqlite"):
            if name != replacement.name and name in self.registry.plugins:
                self.remove(name)
        return replacement

    def shutdown(self) -> None:
        """Tear everything down, newest first."""
        self.registry.shutdown()
        self.instances.clear()

    # -------------------------------------------------------------- accessors

    @property
    def server(self) -> Server | None:
        server: Server | None = self.registry.services.get("server")
        return server

    @property
    def bus(self) -> Bus | None:
        bus: Bus | None = self.registry.services.get("bus")
        return bus

    @property
    def store_kind(self) -> str | None:
        store = self.registry.services.get("store")
        return None if store is None else str(store.kind)

    def state_of(self, name: str) -> str:
        """A plugin is either registered or it is not. There is no third state."""
        return "REGISTERED" if name in self.registry.plugins else "ABSENT"

    def dispatch(self, path: str, payload: Any = None) -> Any:
        """Serve one request through the application's dispatcher."""
        server = self.server
        if server is None:
            raise RuntimeError("the server plugin is not registered")
        return server.dispatch(path, payload)
