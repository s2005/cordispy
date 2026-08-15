"""Property access -- paper section 5.1.4, Algorithm 6.

``ctx.get(key)`` is a lookup against the store that never fails.
``ctx.key`` resolves against the accessing fiber's committed view and enforces
the coeffect specification at the point of use. These tests pin the difference.
"""

from __future__ import annotations

from typing import Any

import pytest

from cordispy import (
    Context,
    FiberState,
    InactiveAccessError,
    UndeclaredAccessError,
    plugin,
)


async def test_reading_an_undeclared_key_is_rejected() -> None:
    root = Context()
    root.set("store", "value")

    @plugin(name="consumer")
    def consumer(ctx: Context, config: Any) -> None:
        with pytest.raises(UndeclaredAccessError):
            _ = ctx.store

    fiber = root.use(consumer)
    await root.registry.settle()
    assert fiber.state is FiberState.ACTIVE


async def test_reading_a_declared_key_while_inactive_is_rejected() -> None:
    root = Context()
    captured: dict[str, Context] = {}

    @plugin(name="provider", provide=["store"])
    def provider(ctx: Context, config: Any) -> None:
        ctx.set("store", "value")

    @plugin(name="consumer", inject=["store"])
    def consumer(ctx: Context, config: Any) -> None:
        captured["ctx"] = ctx

    supplier = root.use(provider)
    dependent = root.use(consumer)
    await root.registry.settle()
    assert captured["ctx"].store == "value"

    await supplier.retire()
    await dependent.wait()
    assert dependent.state is FiberState.INACTIVE

    with pytest.raises(InactiveAccessError):
        _ = captured["ctx"].store


async def test_get_never_fails_where_property_access_would() -> None:
    root = Context()
    assert root.get("missing") is None

    with pytest.raises(UndeclaredAccessError):
        _ = root.missing


async def test_the_walk_climbs_to_the_parent_fiber() -> None:
    """Algorithm 6 line 7: an undeclared key is looked for further up the chain."""
    root = Context()
    seen: list[Any] = []

    @plugin(name="grandchild")
    def grandchild(ctx: Context, config: Any) -> None:
        # `grandchild` declares nothing, so the walk climbs to `child`, then to
        # `parent`, which committed the binding.
        seen.append(ctx.store)

    @plugin(name="child")
    def child(ctx: Context, config: Any) -> None:
        ctx.use(grandchild)

    @plugin(name="parent", inject=["store"])
    def parent(ctx: Context, config: Any) -> None:
        ctx.use(child)

    root.set("store", "shared")
    root.use(parent)
    await root.registry.settle()
    assert seen == ["shared"]


async def test_the_walk_stops_at_the_first_fiber_that_declares_the_key() -> None:
    root = Context()
    errors: list[BaseException] = []

    @plugin(name="inner", inject=["store"])
    def inner(ctx: Context, config: Any) -> None:
        raise AssertionError("inner never activates")

    @plugin(name="outer", inject=["store"])
    def outer(ctx: Context, config: Any) -> None:
        raise AssertionError("outer never activates")

    root.use(outer)
    fiber = root.use(inner)
    await root.registry.settle()

    try:
        _ = fiber.ctx.store
    except InactiveAccessError as error:
        errors.append(error)

    assert len(errors) == 1
    assert "inner" in str(errors[0])


# --------------------------------------------------------------------------
# ctx.optional: Algorithm 6 for a key that may have no provider
# --------------------------------------------------------------------------


async def test_an_optional_coeffect_reads_as_none_when_nothing_provides_it() -> None:
    """An ACTIVE fiber must be able to read its own unsatisfied optional key.

    Optional injects are an extension over the paper's flat specification, and
    they land in ``fiber.inject`` like any other declaration -- so Algorithm 6's
    ``if key in fiber.inject: raise`` rejects the one read the declaration was
    made for. ``ctx.optional`` answers ``None`` instead.
    """
    root = Context()
    seen: list[Any] = []
    rejected: list[str] = []

    @plugin(name="tool", inject={"required": [], "optional": ["metrics"]})
    def tool(ctx: Context, config: Any) -> None:
        seen.append(ctx.optional("metrics"))
        try:
            _ = ctx.metrics
        except InactiveAccessError as error:
            rejected.append(str(error))

    fiber = root.use(tool)
    await root.registry.settle()

    assert fiber.state is FiberState.ACTIVE
    assert fiber.error is None
    assert seen == [None]
    assert len(rejected) == 1, "property access still refuses to answer"
    assert "ctx.optional('metrics')" in rejected[0], "and it names the call that can"


async def test_optional_reads_the_committed_view_once_a_provider_arrives() -> None:
    """It is the committed view, not ``ctx.get``: the store can answer wrongly."""
    root = Context()
    seen: list[Any] = []

    @plugin(name="metrics", provide=["metrics"])
    def metrics(ctx: Context, config: Any) -> None:
        ctx.set("metrics", {"hits": 0})

    @plugin(name="tool", inject={"optional": ["metrics"]})
    def tool(ctx: Context, config: Any) -> None:
        seen.append(ctx.optional("metrics"))

    root.use(metrics)
    await root.registry.settle()
    fiber = root.use(tool)
    await root.registry.settle()

    assert fiber.state is FiberState.ACTIVE
    assert seen == [{"hits": 0}]
    assert fiber.ctx.optional("metrics") is fiber.ctx.metrics, "the same binding as ctx.key"


async def test_optional_still_rejects_a_key_that_was_never_declared() -> None:
    """A checked accessor, not a silent ``getattr`` escape hatch."""
    root = Context()
    root.set("store", "value")

    @plugin(name="consumer", inject=["store"])
    def consumer(ctx: Context, config: Any) -> None:
        with pytest.raises(UndeclaredAccessError) as inner:
            ctx.optional("metrics")
        assert "'metrics'" in str(inner.value)

    fiber = root.use(consumer)
    await root.registry.settle()
    assert fiber.state is FiberState.ACTIVE

    with pytest.raises(UndeclaredAccessError) as info:
        root.optional("anything")
    assert "'anything'" in str(info.value), "the rejection names the key, not the accessor"


async def test_optional_returns_none_for_a_required_key_on_an_unloaded_fiber() -> None:
    """Declared but not committed is ``None`` here, whatever gated the key."""
    root = Context()
    captured: dict[str, Context] = {}

    @plugin(name="provider", provide=["store"])
    def provider(ctx: Context, config: Any) -> None:
        ctx.set("store", "value")

    @plugin(name="consumer", inject=["store"])
    def consumer(ctx: Context, config: Any) -> None:
        captured["ctx"] = ctx

    supplier = root.use(provider)
    dependent = root.use(consumer)
    await root.registry.settle()
    assert captured["ctx"].optional("store") == "value"

    await supplier.retire()
    await dependent.wait()

    assert captured["ctx"].optional("store") is None
    with pytest.raises(InactiveAccessError) as info:
        _ = captured["ctx"].store
    message = str(info.value)
    assert "INACTIVE" in message, "a required key reports the fiber state instead"
    assert "ctx.optional" not in message


async def test_adding_optional_did_not_weaken_algorithm_6() -> None:
    """The three rejections ``ctx.optional`` must not have relaxed.

    Widening a read path is the easy way to lose an enforcement, so the three
    refusals are pinned together rather than inferred from the tests above:

    1. a **required** key on a fiber that is not loaded still raises
       :class:`InactiveAccessError` through property access -- ``ctx.optional``
       is an addition, not a redirection of ``ctx.key``;
    2. a key the fiber **never declared** still raises
       :class:`UndeclaredAccessError` through property access;
    3. the same undeclared key raises :class:`UndeclaredAccessError` through
       ``ctx.optional`` too, rather than answering ``None``. This is the one
       that would quietly turn the checked accessor into a ``getattr`` escape
       hatch, because ``None`` is exactly what an unsatisfied *declared* key
       gives -- the two cases must stay distinguishable.
    """
    root = Context()
    captured: dict[str, Context] = {}

    @plugin(name="provider", provide=["store"])
    def provider(ctx: Context, config: Any) -> None:
        ctx.set("store", "value")

    @plugin(name="consumer", inject=["store"])
    def consumer(ctx: Context, config: Any) -> None:
        captured["ctx"] = ctx

    supplier = root.use(provider)
    dependent = root.use(consumer)
    await root.registry.settle()
    ctx = captured["ctx"]
    assert dependent.state is FiberState.ACTIVE

    # Take the provider away, so `store` is declared but no longer committed.
    await supplier.retire()
    await dependent.wait()
    assert dependent.state is FiberState.INACTIVE

    # 1. a required key on a fiber that is not loaded.
    with pytest.raises(InactiveAccessError) as inactive:
        _ = ctx.store
    assert "store" in str(inactive.value)

    # 2. a key that was never declared.
    with pytest.raises(UndeclaredAccessError) as undeclared:
        _ = ctx.never_declared
    assert "never_declared" in str(undeclared.value)

    # 3. the same undeclared key through the new accessor: still an error, and
    #    emphatically not None, which is what a *declared* key would give.
    with pytest.raises(UndeclaredAccessError) as through_optional:
        ctx.optional("never_declared")
    assert "never_declared" in str(through_optional.value)
    assert ctx.optional("store") is None, "a declared-but-uncommitted key is the None case"


async def test_internal_names_never_reach_the_coeffect_walk() -> None:
    root = Context()
    for name in ("__deepcopy__", "__copy__", "_private"):
        with pytest.raises(AttributeError):
            getattr(root, name)


async def test_assignment_on_a_context_is_refused() -> None:
    root = Context()
    with pytest.raises(UndeclaredAccessError) as info:
        root.store = "value"
    assert "ctx.set" in str(info.value)


async def test_real_attributes_are_not_shadowed_by_the_walk() -> None:
    root = Context()
    assert root.fiber.label == "root"
    assert root.root is root
    assert callable(root.get)
    assert callable(root.use)
    assert callable(root.plugin)
