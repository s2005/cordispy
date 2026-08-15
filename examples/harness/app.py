"""Composing the demonstration application on the cordis runtime.

The whole file is thin on purpose. Composition here is ``ctx.use`` and
``fiber.retire``, and there is no lifecycle code beyond that: no ordering table,
no dependency resolution, no teardown list. Those exist in the conventional
implementation (``examples.naive.app``) because they have to.

Two habits are worth copying from this file:

* wait for ``registry.settle()``, not for one fiber. ``await fiber.wait()``
  waits for *that* fiber, and a consumer whose provider is still loading is not
  yet transitioning at all -- it is PENDING with nothing in flight, so its
  ``wait()`` returns instantly and tells you nothing. ``settle()`` is the fixed
  point of the whole runtime.
* the order components are added in does not matter. ``start`` adds them in a
  readable order rather than a required one, and ``run_benefit.py`` deliberately
  adds a consumer before its provider.
"""

from __future__ import annotations

import importlib
import sys
from typing import TYPE_CHECKING, Any

from cordispy import Context, Fiber
from cordispy.plugins import events_plugin, timer_plugin

from .plugins import audit, metrics_plugin, server_plugin, store_memory, store_sqlite, tool_echo, tool_kv

if TYPE_CHECKING:  # pragma: no cover - typing only
    from cordispy.plugins import Bus

    from .services import Server

__all__ = ["LAZY_MODULE", "STORE_PROVIDERS", "Harness", "import_lazy_tool"]

#: The two interchangeable providers of the ``store`` key.
STORE_PROVIDERS = {"memory": store_memory, "sqlite": store_sqlite}

#: The module whose component declares ``store`` without resolving it. The
#: counterpart of ``examples.naive.app.EAGER_MODULE``.
LAZY_MODULE = "examples.harness.lazy"


def import_lazy_tool() -> Any:
    """Import the declaring plugin module afresh, with nothing bound to ``store``.

    Dropping it from ``sys.modules`` first makes the import repeatable across
    scenarios, exactly as ``examples.naive.app.import_eager_tool`` does, so the
    two sides of the comparison run the same act rather than being described.
    """
    sys.modules.pop(LAZY_MODULE, None)
    return importlib.import_module(LAZY_MODULE)


class Harness:
    """The demonstration application, composed of components."""

    def __init__(self) -> None:
        self.root = Context()
        self.fibers: dict[str, Fiber] = {}
        #: Filled by the ``audit`` component with the path of every request.
        self.audit_log: list[str] = []
        #: Filled by ``tool_kv`` with the paths whose detail events it traced.
        self.traced: list[str] = []

    def __repr__(self) -> str:
        return f"<Harness {len(self.fibers)} components>"

    # ------------------------------------------------------------ composition

    async def add(self, name: str, component: Any, config: Any = None) -> Fiber:
        """Compose one component in and wait for the runtime to settle."""
        fiber = self.root.use(component, config)
        self.fibers[name] = fiber
        await self.root.registry.settle()
        return fiber

    async def remove(self, name: str) -> None:
        """Take one component out. Idempotent, like ``retire`` itself."""
        fiber = self.fibers.pop(name, None)
        if fiber is None:
            return
        await fiber.retire()
        await self.root.registry.settle()

    async def start(
        self,
        *,
        store: str | None = "memory",
        tools: tuple[str, ...] = ("tool_echo", "tool_kv"),
    ) -> None:
        """Compose the application.

        ``store=None`` leaves the ``store`` key unprovided, which is how the
        late-arrival scenario starts: ``tool_kv`` is composed in anyway and
        simply waits, PENDING, until a provider turns up.
        """
        await self.add("timer", timer_plugin)
        await self.add("events", events_plugin)
        await self.add("metrics", metrics_plugin)
        await self.add("server", server_plugin)
        await self.add("audit", audit, self.audit_log)
        if store is not None:
            await self.add("store", STORE_PROVIDERS[store])
        if "tool_echo" in tools:
            await self.add("tool_echo", tool_echo)
        if "tool_kv" in tools:
            await self.add("tool_kv", tool_kv, self.traced)

    async def swap_store(self, kind: str) -> Fiber:
        """Replace the ``store`` provider while the application is running.

        Retire the old provider, compose the new one in. Everything else --
        deactivating the dependents, closing the old store after they have
        stopped using it, reactivating them against the new binding -- follows
        from the declarations and needs no code here.
        """
        await self.remove("store")
        return await self.add("store", STORE_PROVIDERS[kind])

    async def shutdown(self) -> None:
        """Take the whole application apart, newest component first."""
        for name in reversed(list(self.fibers)):
            await self.remove(name)

    # -------------------------------------------------------------- accessors

    @property
    def server(self) -> Server | None:
        server: Server | None = self.root.get("server")
        return server

    @property
    def bus(self) -> Bus | None:
        bus: Bus | None = self.root.get("bus")
        return bus

    @property
    def store_kind(self) -> str | None:
        store = self.root.get("store")
        return None if store is None else str(store.kind)

    def state_of(self, name: str) -> str:
        """The state of one component, as a bare string for the demo tables."""
        fiber = self.fibers.get(name)
        return "ABSENT" if fiber is None else fiber.state.value

    def dispatch(self, path: str, payload: Any = None) -> Any:
        """Serve one request through the application's dispatcher."""
        server = self.server
        if server is None:
            raise RuntimeError("the server component is not loaded")
        return server.dispatch(path, payload)
