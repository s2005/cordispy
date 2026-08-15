"""Isolation and interception -- derived realization, paper section 5.1.2."""

from __future__ import annotations

import asyncio
from typing import Any

from cordispy import Context, FiberState, Realm, plugin


async def test_two_siblings_isolating_the_same_key_resolve_independently() -> None:
    root = Context()
    left = root.isolate("store")
    right = root.isolate("store")

    left.set("store", "left value")
    right.set("store", "right value")

    assert left.get("store") == "left value"
    assert right.get("store") == "right value"
    assert root.get("store") is None
    assert left.realm_of("store") is not right.realm_of("store")


async def test_an_isolated_binding_only_satisfies_its_own_realm() -> None:
    root = Context()
    left = root.isolate("store")
    right = root.isolate("store")

    @plugin(name="consumer", inject=["store"])
    def consumer(ctx: Context, config: Any) -> None:
        pass

    inside = left.use(consumer)
    outside = right.use(consumer)
    await inside.wait()
    await outside.wait()
    assert inside.state is FiberState.PENDING
    assert outside.state is FiberState.PENDING

    left.set("store", "left value")
    await inside.wait()
    await outside.wait()

    assert inside.state is FiberState.ACTIVE
    assert outside.state is FiberState.PENDING, "the sibling realm was not notified"
    assert inside.ctx.store == "left value"


async def test_isolation_leaves_the_parent_untouched() -> None:
    root = Context()
    default_realm = root.realm_of("store")
    child = root.isolate("store")

    assert root.realm_of("store") is default_realm
    assert child.realm_of("store") is not default_realm
    assert child.root is root


async def test_an_explicit_realm_can_be_shared_between_contexts() -> None:
    root = Context()
    shared = Realm("shared store")
    left = root.isolate("store", shared)
    right = root.isolate("store", shared)

    left.set("store", "value")
    assert right.get("store") == "value"
    assert left.realm_of("store") is right.realm_of("store")


async def test_a_key_with_no_isolation_uses_its_default_realm() -> None:
    root = Context()
    other = Context()
    assert root.realm_of("store") is Realm.default("store")
    assert other.realm_of("store") is Realm.default("store")

    root.set("store", "value")
    assert other.get("store") is None, "separate roots hold separate stores"


async def test_intercept_merges_over_inherited_metadata() -> None:
    root = Context()
    outer = root.intercept("store", {"prefix": "a", "shared": 1})
    inner = outer.intercept("store", {"shared": 2, "extra": True})

    assert root.interception("store") == {}
    assert outer.interception("store") == {"prefix": "a", "shared": 1}
    assert inner.interception("store") == {"prefix": "a", "shared": 2, "extra": True}


async def test_intercept_does_not_change_what_a_key_resolves_to() -> None:
    root = Context()
    root.set("store", "value")
    derived = root.intercept("store", {"readonly": True})

    assert derived.get("store") == "value"
    assert derived.realm_of("store") is root.realm_of("store")


async def test_a_derived_context_is_recovered_by_discarding_it() -> None:
    """Derived realization needs no inverse: the parent was never modified."""
    root = Context()
    before = dict(root.bindings())

    derived = root.isolate("store").intercept("store", {"tag": 1})
    derived.set("store", "isolated")

    assert root.get("store") is None
    assert dict(root.bindings()) != before  # the shared store did receive it
    assert root.realm_of("store") not in {derived.realm_of("store")}


async def test_a_provider_publishing_into_a_derived_realm_reaches_that_realm() -> None:
    """The realm a notification carries is the binding's, not the notifier's.

    A provider that publishes through ``ctx.isolate(k, realm).set(k, value)``
    installs into ``realm`` while its own context still resolves ``k`` to the
    default one. A fiber-level notification resolved against that own context
    matches no dependent in ``realm`` at all, so the dependent never activates.
    Paper section 5.2.1: "A dependent sees the binding while its own realm at k
    is the realm the binding sits in."
    """
    root = Context()
    shared = Realm("shared store")

    @plugin(name="provider", provide=["store"])
    def provider(ctx: Context, config: Any) -> None:
        ctx.isolate("store", shared).set("store", "value")

    @plugin(name="consumer", inject=["store"])
    def consumer(ctx: Context, config: Any) -> None:
        pass

    supplier = root.use(provider)
    dependent = root.isolate("store", shared).use(consumer)
    await root.registry.settle()

    assert supplier.state is FiberState.ACTIVE
    assert supplier.installed() == [("store", shared)]
    assert dependent.state is FiberState.ACTIVE, "the dependent was never notified"
    assert dependent.target == (("store", supplier.uid),)
    assert dependent.ctx.store == "value"


async def test_a_realm_scoped_dependent_is_drained_before_the_providers_inverses() -> None:
    """The other half of the same defect: withdrawal ordering, Theorem 63.

    A dependent the unload notification misses is not merely told late -- it is
    never gathered into the drain of Algorithm 5 line 25, so the provider's own
    inverses run while the dependent is still holding what they release.
    """
    root = Context()
    shared = Realm("shared store")
    order: list[str] = []

    @plugin(name="provider", provide=["store"])
    def provider(ctx: Context, config: Any) -> Any:
        ctx.isolate("store", shared).set("store", "value")
        return lambda: order.append("provider-inverse")

    @plugin(name="consumer", inject=["store"])
    def consumer(ctx: Context, config: Any) -> Any:
        return lambda: order.append("consumer-inverse")

    supplier = root.use(provider)
    dependent = root.isolate("store", shared).use(consumer)
    await root.registry.settle()
    assert dependent.state is FiberState.ACTIVE

    await supplier.retire()
    await root.registry.settle()

    assert order == ["consumer-inverse", "provider-inverse"]
    assert dependent.state is FiberState.INACTIVE


async def test_a_non_default_realm_dependent_is_deactivated_and_awaited_by_the_unload() -> None:
    """Algorithm 5 lines 10 and 25, for a dependent outside the default realm.

    The previous test pins the *order* of two synchronous inverses, which a
    provider could produce by accident. This one pins the two things the paper
    actually demands, and neither survives a notification that misses the realm:

    * **L-Leave (line 10).** The provider stops providing, and its dependents
      recompute against that withdrawal, *before* any inverse is scheduled -- so
      when the dependent's own teardown begins the provider is already
      ``UNLOADING`` and has not run a single inverse of its own.
    * **The guard on L-Unload (line 25).** The provider then *waits*. The
      dependent's inverse here is asynchronous and spans several loop turns; the
      provider's inverse must not run until it has finished, not merely until it
      has started.

    Theorem 63 rides along: the binding whose withdrawal started the teardown is
    still readable from the dependent's committed view throughout.
    """
    root = Context()
    shared = Realm("shared store")
    order: list[str] = []
    seen: dict[str, Any] = {}

    @plugin(name="provider", provide=["store"])
    def provider(ctx: Context, config: Any) -> Any:
        ctx.isolate("store", shared).set("store", "value")
        return lambda: order.append("provider-inverse")

    @plugin(name="consumer", inject=["store"])
    def consumer(ctx: Context, config: Any) -> Any:
        async def release() -> None:
            order.append("consumer-inverse-start")
            seen["provider_state"] = supplier.state
            seen["provider_inverses_run"] = list(order)
            # Span several loop turns, so a provider that merely *starts* the
            # dependent's teardown rather than awaiting it would interleave.
            for _ in range(5):
                await asyncio.sleep(0)
            seen["binding_during_teardown"] = ctx.store
            order.append("consumer-inverse-end")

        return release

    supplier = root.use(provider)
    dependent = root.isolate("store", shared).use(consumer)
    await root.registry.settle()
    assert supplier.state is FiberState.ACTIVE
    assert dependent.state is FiberState.ACTIVE
    assert dependent.ctx.realm_of("store") is shared
    assert dependent.ctx.realm_of("store") is not Realm.default("store")

    await supplier.retire()
    await root.registry.settle()

    # L-Leave: the provider had already stopped providing, and had run none of
    # its own inverses, when the dependent's teardown began.
    assert seen["provider_state"] is FiberState.UNLOADING
    assert seen["provider_inverses_run"] == ["consumer-inverse-start"]
    # The guard on L-Unload: the dependent finished before the provider started.
    assert order == ["consumer-inverse-start", "consumer-inverse-end", "provider-inverse"]
    # Theorem 63: the withdrawn binding stayed readable through the teardown.
    assert seen["binding_during_teardown"] == "value"
    assert dependent.state is FiberState.INACTIVE
    assert dependent.committed is None


async def test_notify_accepts_a_predicate_that_overrides_the_realm_test() -> None:
    """The hook isolation reassignment uses: pick dependents by any rule."""
    root = Context()
    left = root.isolate("store")
    right = root.isolate("store")

    @plugin(name="consumer", inject=["store"])
    def consumer(ctx: Context, config: Any) -> None:
        pass

    inside = left.use(consumer)
    outside = right.use(consumer)
    await root.registry.settle()

    # The default realm test would report neither fiber for a root-level key.
    assert root.registry.notify(root, ["store"]) == []

    seen = root.registry.notify(root, ["store"], lambda ctx, key: True)
    assert set(seen) == {inside, outside}
