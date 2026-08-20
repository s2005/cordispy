"""cordispy: spatiotemporal composability for Python.

A faithful, teaching-scale realization of the runtime described in
*A Programming Paradigm for Spatiotemporal Composability* (Shi, Zhang, Cui --
DeepSeek-AI / Peking University). The paper is at
https://github.com/cordiverse/paper and the reference TypeScript implementation
at https://github.com/cordiverse/cordis.

Two dimensions, one context:

*Temporal* -- every mutation a component performs goes through ``ctx.effect``,
which takes an inverse alongside the effect. Unloading the component runs the
inverses last-first, returning the system to the state it had before the
component was composed in.

*Spatial* -- a component declares the coeffects it needs with ``inject`` and
publishes its own with ``ctx.set``. The runtime activates it when its
dependencies are satisfied, deactivates it when they are withdrawn, and
guarantees that a dependency stays readable through the dependent's own
teardown.

A one-minute tour::

    import asyncio
    from cordispy import Context, plugin

    @plugin(name="store", provide=["store"])
    def store(ctx, config):
        ctx.set("store", {})

    @plugin(name="counter", inject=["store"])
    def counter(ctx, config):
        ctx.store["hits"] = 0
        return lambda: ctx.store.pop("hits", None)

    async def main():
        root = Context()
        provider = root.use(store)
        consumer = root.use(counter)

        # `fiber.wait()` waits for that one fiber's transition. One activation
        # starts another -- the provider reaching ACTIVE is what lets the
        # consumer load -- so wait for the whole runtime to reach a fixed point.
        await root.registry.settle()
        assert consumer.state.name == "ACTIVE"
        assert root.get("store") == {"hits": 0}

        await provider.retire()      # the consumer deactivates by itself
        assert consumer.state.name == "INACTIVE"
        assert root.get("store") is None

    asyncio.run(main())
"""

from __future__ import annotations

from .component import ApplyFn, Component, Inject, InjectSpec, plugin, to_component
from .context import Context
from .effect import (
    AsyncDisposer,
    Disposer,
    DisposerChain,
    EffectCallback,
    Guard,
    Pending,
    compose,
    execute,
    invoke,
    noop,
    spawn,
    start,
)
from .errors import (
    AccessError,
    CordisError,
    InactiveAccessError,
    InactiveEffectError,
    InvalidEffectError,
    UndeclaredAccessError,
)
from .fiber import Fiber, FiberState, Target
from .realm import Binding, Realm, Store
from .registry import NotifyPredicate, Registry, notify

__version__ = "0.1.0"

__all__ = [
    "AccessError",
    "ApplyFn",
    "AsyncDisposer",
    "Binding",
    "Component",
    "Context",
    "CordisError",
    "Disposer",
    "DisposerChain",
    "EffectCallback",
    "Fiber",
    "FiberState",
    "Guard",
    "InactiveAccessError",
    "InactiveEffectError",
    "Inject",
    "InjectSpec",
    "InvalidEffectError",
    "NotifyPredicate",
    "Pending",
    "Realm",
    "Registry",
    "Store",
    "Target",
    "UndeclaredAccessError",
    "__version__",
    "compose",
    "execute",
    "invoke",
    "noop",
    "notify",
    "plugin",
    "spawn",
    "start",
    "to_component",
]
