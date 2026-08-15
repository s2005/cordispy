"""Coeffect provision and reactive notification -- paper Algorithms 2 and 3."""

from __future__ import annotations

from typing import Any

import pytest

from cordispy import Context, FiberState, Inject, Realm, UndeclaredAccessError, plugin


async def test_get_returns_none_for_an_unbound_key() -> None:
    root = Context()
    assert root.get("nothing") is None


async def test_set_binds_and_its_inverse_withdraws() -> None:
    root = Context()
    dispose = root.set("store", {"kind": "memory"})
    assert root.get("store") == {"kind": "memory"}
    await dispose()
    assert root.get("store") is None


async def test_a_binding_records_the_fiber_that_installed_it() -> None:
    root = Context()

    @plugin(name="provider", provide=["store"])
    def provider(ctx: Context, config: Any) -> None:
        ctx.set("store", "value")

    fiber = root.use(provider)
    await root.registry.settle()

    binding = root.binding("store")
    assert binding is not None
    assert binding.provider is fiber
    assert binding.key == "store"
    assert fiber.provided() == ["store"]


async def test_two_fibers_cannot_provide_the_same_key() -> None:
    root = Context()
    root.set("store", "first")

    @plugin(name="second", provide=["store"])
    def second(ctx: Context, config: Any) -> None:
        ctx.set("store", "second")

    fiber = root.use(second)
    await root.registry.settle()
    assert fiber.state is FiberState.FAILED
    assert isinstance(fiber.error, UndeclaredAccessError)
    assert root.get("store") == "first"


async def test_a_binding_counts_only_while_its_provider_is_active() -> None:
    """``active_binding`` is the ``provided by`` relation of Definition 46."""
    root = Context()

    @plugin(name="provider", provide=["store"])
    def provider(ctx: Context, config: Any) -> None:
        ctx.set("store", "value")

    fiber = root.use(provider)
    await root.registry.settle()
    assert root.active_binding("store") is not None

    fiber.state = FiberState.UNLOADING
    assert root.binding("store") is not None, "the binding is still installed"
    assert root.active_binding("store") is None, "but it no longer counts as provided"


# --------------------------------------------------------------------------
# inject normalization
# --------------------------------------------------------------------------


def test_inject_accepts_every_declared_spelling() -> None:
    assert Inject.parse(None) == Inject()
    assert Inject.parse("store") == Inject(required=("store",))
    assert Inject.parse(["a", "b"]) == Inject(required=("a", "b"))
    assert Inject.parse({"required": ["a"], "optional": ["b"]}) == Inject(required=("a",), optional=("b",))
    assert Inject.parse(Inject(required=("a",))) == Inject(required=("a",))


def test_inject_rejects_a_key_declared_twice() -> None:
    from cordispy import CordisError

    with pytest.raises(CordisError):
        Inject(required=("a",), optional=("a",))


async def test_an_optional_key_does_not_gate_activation() -> None:
    root = Context()
    seen: list[Any] = []

    @plugin(name="consumer", inject={"optional": ["metrics"]})
    def consumer(ctx: Context, config: Any) -> None:
        seen.append(ctx.get("metrics"))

    fiber = root.use(consumer)
    await root.registry.settle()
    assert fiber.state is FiberState.ACTIVE
    assert seen == [None]


async def test_an_optional_key_still_participates_in_the_target() -> None:
    root = Context()
    loads: list[Any] = []

    @plugin(name="consumer", inject={"optional": ["metrics"]})
    def consumer(ctx: Context, config: Any) -> None:
        loads.append(ctx.get("metrics"))

    @plugin(name="metrics", provide=["metrics"])
    def metrics(ctx: Context, config: Any) -> None:
        ctx.set("metrics", "counter")

    dependent = root.use(consumer)
    await root.registry.settle()
    assert dependent.target == ()

    root.use(metrics)
    await root.registry.settle()

    assert loads == [None, "counter"]
    assert dependent.target is not None
    assert dependent.target[0][0] == "metrics"


# --------------------------------------------------------------------------
# notification
# --------------------------------------------------------------------------


async def test_a_neutral_notification_is_harmless() -> None:
    """Algorithm 3 re-evaluates every declaring fiber; ``refresh`` is idempotent.

    ``notify`` reports what it re-evaluated, not what changed. A fiber whose
    recomputed target equals the one it already holds returns immediately from
    ``refresh``, so a redundant notification never restarts anything.
    """
    root = Context()
    loads: list[int] = []

    @plugin(name="consumer", inject=["store"])
    def consumer(ctx: Context, config: Any) -> None:
        loads.append(1)

    fiber = root.use(consumer)
    await root.registry.settle()

    root.set("store", "value")
    await root.registry.settle()
    assert fiber.state is FiberState.ACTIVE
    target = fiber.target

    affected = root.registry.notify(root, ["store"])
    assert affected == [fiber], "the fiber was re-evaluated"
    assert fiber.inertia is None, "but no transition started"
    assert fiber.target == target
    assert loads == [1]


async def test_notify_accepts_a_batch_of_keys() -> None:
    """Deliberate divergence from ``reflect.ts:205``: one call, not one sweep per key."""
    root = Context()

    @plugin(name="consumer", inject=["a", "b"])
    def consumer(ctx: Context, config: Any) -> None:
        pass

    fiber = root.use(consumer)
    await root.registry.settle()

    root.set("a", 1)
    root.set("b", 2)
    await root.registry.settle()
    assert fiber.state is FiberState.ACTIVE

    fiber.target = None  # force a change so refresh has something to do
    affected = root.registry.notify(root, ["a", "b"])
    assert affected == [fiber], "the fiber is reported once, not once per key"


async def test_notify_takes_the_realm_from_the_binding_not_from_the_notifier() -> None:
    """Algorithm 3's realm test, keyed on where the binding sits.

    An item of the batch may be a ``(key, realm)`` pair. That is what a
    fiber-level notification passes, because the fiber's own context is not in
    general the context that installed the binding.
    """
    root = Context()
    shared = Realm("shared store")

    @plugin(name="consumer", inject=["store"])
    def consumer(ctx: Context, config: Any) -> None:
        pass

    fiber = root.isolate("store", shared).use(consumer)
    await root.registry.settle()

    assert root.registry.notify(root, ["store"]) == [], "the default realm reaches nobody"
    assert root.registry.notify(root, [("store", shared)]) == [fiber]


async def test_a_fiber_that_declares_nothing_is_never_notified() -> None:
    root = Context()

    @plugin(name="standalone")
    def standalone(ctx: Context, config: Any) -> None:
        pass

    fiber = root.use(standalone)
    await root.registry.settle()
    assert fiber.state is FiberState.ACTIVE
    assert root.registry.notify(root, ["anything"]) == []


async def test_a_provider_reads_its_own_binding_through_get_not_through_access() -> None:
    """A committed view is ``resolve(inject)`` and nothing else.

    Providing a key does not add it to the provider's own view, so property
    access keeps enforcing the coeffect specification even for the fiber that
    published the binding. ``ctx.get`` is the reflective read that always works.
    """
    root = Context()
    seen: list[Any] = []
    rejected: list[str] = []

    @plugin(name="provider", provide=["store"])
    def provider(ctx: Context, config: Any) -> None:
        ctx.set("store", {"rows": 0})
        seen.append(ctx.get("store"))
        try:
            _ = ctx.store
        except UndeclaredAccessError:
            rejected.append("store")

    fiber = root.use(provider)
    await root.registry.settle()

    assert fiber.state is FiberState.ACTIVE
    assert seen == [{"rows": 0}]
    assert rejected == ["store"]
