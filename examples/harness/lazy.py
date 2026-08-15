"""A plugin module that resolves nothing at import time.

The counterpart of ``examples/naive/eager.py``, and it exists so that the late
arrival scenario in ``run_benefit.py`` can *measure* both sides of the same act
instead of asserting one of them in prose. The act is: import a plugin module
while nothing provides the key that plugin needs.

``examples/naive/eager.py`` reaches into the process-wide service table at module
scope, so importing it before the provider is registered raises during the import
-- early, loud, and unrecoverable, because a half-imported module is not
something the interpreter finishes later.

Here the same dependency is a *declaration*. ``inject=["store"]`` records what
the component needs; nothing is looked up until the runtime activates the
component, which it does when, and only when, the declaration is satisfied
(paper Algorithms 3 and 5). Importing this module therefore cannot fail for want
of a provider, and the demo reads that off the live process by importing it.
"""

from __future__ import annotations

from typing import Any

from cordispy import Context, plugin

__all__ = ["lazy_tool", "summary"]


@plugin(name="lazy_tool", inject=["store"])
def lazy_tool(ctx: Context, config: Any) -> None:
    """Read the dependency at activation, which is the only time it is defined."""
    _ = ctx.store


def summary() -> str:
    """What this module bound itself to at import time: nothing."""
    return "lazy tool declares 'store' and resolves it at activation, not at import"
