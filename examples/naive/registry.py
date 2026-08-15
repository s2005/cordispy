"""A conventional plugin registry: a service dictionary and setup/teardown pairs.

THIS IS A FAITHFUL CONVENTIONAL IMPLEMENTATION, NOT A STRAW MAN. Nothing here is
sabotaged, and no bug is planted. It is the design almost every plugin system in
wide use converges on: a process-wide service table, a ``register`` that runs a
plugin's ``setup``, an ``unregister`` that runs its ``teardown``, and an
expectation that each plugin cleans up after itself. Everything the comparison
in ``run_benefit.py`` measures follows from that design rather than from any
mistake in this file:

* ``provide`` is a dictionary assignment, so registering a second provider of a
  key silently displaces the first without retiring it. There is nowhere in this
  model to express "and take the old one out";
* ``require`` resolves once, at registration, and hands back a direct reference.
  Nothing invalidates that reference later, because nothing is tracking it;
* ``teardown`` is a method, so it can only undo what its author remembered.
  Resources created after setup -- while serving a request -- are not visible
  from inside it;
* ``load_all`` keeps booting when a plugin's ``setup`` raises, which is what an
  application that must start needs, and which leaves whatever that ``setup``
  had already done in place.

The application built on top of this registry (``examples.naive.plugins``) uses
exactly the same leaf services as the cordis version and offers exactly the same
features. Only the composition mechanism differs.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "SERVICES",
    "MissingDependencyError",
    "Plugin",
    "PluginRegistry",
    "reset",
]

logger = logging.getLogger("examples.naive")

#: The process-wide service table. A module-level singleton, because that is how
#: a conventional plugin system makes services reachable from a plugin module
#: that was imported before anything was registered.
SERVICES: dict[str, Any] = {}


def reset() -> None:
    """Empty the service table. Between demo scenarios."""
    SERVICES.clear()


class MissingDependencyError(LookupError):
    """A plugin asked for a service that has not been registered yet.

    There is no way to wait: registration is a function call that either finds
    its dependencies or does not.
    """


@runtime_checkable
class Plugin(Protocol):
    """What the registry requires of a plugin."""

    name: str

    def setup(self, registry: PluginRegistry) -> None:
        """Acquire dependencies and install everything this plugin adds."""

    def teardown(self, registry: PluginRegistry) -> None:
        """Undo it. By hand, from memory."""


class PluginRegistry:
    """The conventional registry."""

    def __init__(self) -> None:
        self.services = SERVICES
        self.plugins: dict[str, Plugin] = {}
        #: ``(plugin name, error)`` for every ``setup`` that raised.
        self.failures: list[tuple[str, BaseException]] = []

    def __repr__(self) -> str:
        return f"<PluginRegistry {len(self.plugins)} plugins, {len(self.services)} services>"

    # ----------------------------------------------------------- the services

    def provide(self, key: str, value: Any) -> None:
        """Publish a service.

        A plain assignment. If something already provides this key, it is
        displaced here and now, with no chance to release what it held and no
        notification to anything holding a reference to it.
        """
        self.services[key] = value

    def require(self, key: str) -> Any:
        """Resolve a dependency, or fail."""
        try:
            return self.services[key]
        except KeyError:
            raise MissingDependencyError(f"no service registered for {key!r}") from None

    def optional(self, key: str) -> Any:
        """Resolve a dependency that the caller can do without."""
        return self.services.get(key)

    # ------------------------------------------------------------ the plugins

    def register(self, plugin: Plugin) -> None:
        """Run a plugin's ``setup`` and remember it."""
        plugin.setup(self)
        self.plugins[plugin.name] = plugin

    def load_all(self, plugins: list[Plugin]) -> None:
        """Register several plugins, surviving one that fails.

        An application that must come up cannot let a single broken plugin stop
        the boot, so the failure is recorded and the loop continues -- with
        whatever the failed ``setup`` had already installed still installed.
        """
        for plugin in plugins:
            try:
                self.register(plugin)
            except Exception as error:
                logger.warning("plugin %s failed to load: %s", plugin.name, error)
                self.failures.append((plugin.name, error))

    def unregister(self, name: str) -> None:
        """Run a plugin's ``teardown`` and forget it."""
        plugin = self.plugins.pop(name, None)
        if plugin is None:
            return
        plugin.teardown(self)

    def shutdown(self) -> None:
        """Tear everything down, newest first."""
        for name in reversed(list(self.plugins)):
            self.unregister(name)
