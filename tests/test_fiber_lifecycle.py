"""The fiber lifecycle -- paper Algorithms 4 and 5.

The first test in this file is the one that matters most: the coeffect ordering
guarantee (Theorem 63). Everything else in the runtime is arranged around it.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from cordispy import Context, FiberState, Realm, plugin
from cordispy.component import Inject
from cordispy.fiber import UNLOAD_DRAIN_PASSES, Fiber

# --------------------------------------------------------------------------
# Theorem 63: a dependency stays readable through the dependent's own teardown
# --------------------------------------------------------------------------


async def test_dependency_is_readable_during_the_dependents_teardown() -> None:
    """The ordering guarantee, stated as a test.

    The provider is withdrawn. That is what triggers the consumer's teardown.
    While the consumer runs its own inverses it must still be able to read the
    very binding whose withdrawal started the teardown -- otherwise no component
    could ever release a resource it had acquired from a dependency.
    """
    root = Context()
    observed: list[Any] = []
    withdrawn: list[Any] = []

    @plugin(name="provider", provide=["store"])
    def provider(ctx: Context, config: Any) -> None:
        ctx.set("store", {"kind": "memory", "rows": []})

    @plugin(name="consumer", inject=["store"])
    def consumer(ctx: Context, config: Any) -> Any:
        ctx.store["rows"].append("registered")

        def release() -> None:
            # Read the dependency during our own teardown. The provider has
            # already stopped providing -- that is what recomputed this fiber's
            # target to None and started the teardown -- so `active_binding`
            # already reports nothing, and only the committed view still answers.
            observed.append(ctx.store["kind"])
            withdrawn.append(root.active_binding("store"))
            ctx.store["rows"].remove("registered")

        return release

    supplier = root.use(provider)
    dependent = root.use(consumer)
    await root.registry.settle()
    assert supplier.state is FiberState.ACTIVE
    assert dependent.state is FiberState.ACTIVE
    assert root.get("store")["rows"] == ["registered"]

    await supplier.retire()

    assert observed == ["memory"]
    assert withdrawn == [None], "the provider had already stopped providing"
    assert dependent.state is FiberState.INACTIVE
    assert dependent.committed is None


async def test_the_committed_view_outlives_the_binding_it_resolved() -> None:
    """The ordering guarantee where a store lookup would already have failed.

    The previous test withdraws the whole provider, and the drain of Algorithm 5
    line 25 keeps the *store* entry in place until every dependent has finished:
    a component reading through ``ctx.get`` would still have found something.
    This test removes that cushion. The provider stays ACTIVE and withdraws only
    the binding, so by the time the consumer runs its own inverse the entry is
    gone from the store entirely -- ``ctx.get('store')`` returns ``None`` -- and
    the only thing that still answers is the fiber's committed view. That
    distinction is the whole content of the theorem: teardown reads
    ``resolve(inject)`` as it stood when ``apply`` ran, not the store as it
    stands now.
    """
    root = Context()
    observed: list[tuple[Any, Any]] = []
    withdraw: list[Any] = []

    @plugin(name="provider", provide=["store"])
    def provider(ctx: Context, config: Any) -> None:
        withdraw.append(ctx.set("store", {"kind": "memory"}))

    @plugin(name="consumer", inject=["store"])
    def consumer(ctx: Context, config: Any) -> Any:
        def release() -> None:
            observed.append((ctx.store["kind"], ctx.get("store")))

        return release

    supplier = root.use(provider)
    dependent = root.use(consumer)
    await root.registry.settle()
    assert dependent.state is FiberState.ACTIVE
    assert root.get("store") == {"kind": "memory"}

    await withdraw[0]()
    await root.registry.settle()

    assert supplier.state is FiberState.ACTIVE, "the provider itself never unloaded"
    assert root.get("store") is None, "the binding is gone from the store"
    assert observed == [("memory", None)], (
        "the committed view answered during teardown where the store did not"
    )
    assert dependent.state is FiberState.INACTIVE
    assert dependent.committed is None


async def test_provider_drains_dependents_before_running_its_own_inverses() -> None:
    """Algorithm 5, line 25: the drain sits ahead of the whole recovery."""
    root = Context()
    order: list[str] = []

    @plugin(name="provider", provide=["store"])
    def provider(ctx: Context, config: Any) -> Any:
        ctx.set("store", object())
        return lambda: order.append("provider-inverse")

    @plugin(name="consumer", inject=["store"])
    def consumer(ctx: Context, config: Any) -> Any:
        return lambda: order.append("consumer-inverse")

    supplier = root.use(provider)
    dependent = root.use(consumer)
    await root.registry.settle()
    assert dependent.state is FiberState.ACTIVE

    await supplier.retire()
    assert order == ["consumer-inverse", "provider-inverse"]


# --------------------------------------------------------------------------
# activation and deactivation
# --------------------------------------------------------------------------


async def test_unsatisfied_inject_never_activates() -> None:
    root = Context()

    @plugin(name="consumer", inject=["missing"])
    def consumer(ctx: Context, config: Any) -> None:
        raise AssertionError("apply must not run while a required key is unsatisfied")

    fiber = root.use(consumer)
    await fiber.wait()
    assert fiber.state is FiberState.PENDING
    assert fiber.target is None
    assert fiber.committed is None


async def test_activates_by_itself_when_the_provider_arrives_late() -> None:
    root = Context()
    loads: list[int] = []

    @plugin(name="consumer", inject=["store"])
    def consumer(ctx: Context, config: Any) -> None:
        loads.append(1)

    @plugin(name="provider", provide=["store"])
    def provider(ctx: Context, config: Any) -> None:
        ctx.set("store", "late")

    dependent = root.use(consumer)
    await root.registry.settle()
    assert dependent.state is FiberState.PENDING

    root.use(provider)
    await root.registry.settle()

    assert dependent.state is FiberState.ACTIVE
    assert loads == [1]


async def test_deactivates_when_the_provider_leaves() -> None:
    root = Context()

    @plugin(name="provider", provide=["store"])
    def provider(ctx: Context, config: Any) -> None:
        ctx.set("store", "value")

    @plugin(name="consumer", inject=["store"])
    def consumer(ctx: Context, config: Any) -> None:
        pass

    supplier = root.use(provider)
    dependent = root.use(consumer)
    await root.registry.settle()
    assert dependent.state is FiberState.ACTIVE

    await supplier.retire()
    await dependent.wait()
    assert dependent.state is FiberState.INACTIVE
    assert dependent.target is None


async def test_a_component_with_no_inject_loads_immediately() -> None:
    root = Context()

    @plugin(name="standalone")
    def standalone(ctx: Context, config: Any) -> None:
        pass

    fiber = root.use(standalone)
    await fiber.wait()
    assert fiber.state is FiberState.ACTIVE
    assert fiber.target == ()


# --------------------------------------------------------------------------
# identity by provider uid
# --------------------------------------------------------------------------


async def test_a_replacement_provider_with_an_equal_value_still_reloads() -> None:
    """A binding is identified by its provider's uid, never by its value."""
    root = Context()
    loads: list[str] = []

    def make_provider(label: str) -> Any:
        @plugin(name=label, provide=["store"])
        def provider(ctx: Context, config: Any) -> None:
            ctx.set("store", {"equal": True})

        return provider

    @plugin(name="consumer", inject=["store"])
    def consumer(ctx: Context, config: Any) -> None:
        loads.append("load")

    first = root.use(make_provider("first"))
    dependent = root.use(consumer)
    await root.registry.settle()
    assert loads == ["load"]
    before = dependent.target

    await first.retire()
    second = root.use(make_provider("second"))
    await second.wait()
    await dependent.wait()

    assert dependent.state is FiberState.ACTIVE
    assert loads == ["load", "load"]
    assert dependent.target != before, "a fresh uid must produce a different target"
    assert root.get("store") == {"equal": True}


async def test_rebinding_a_key_in_place_withdraws_and_reinstalls() -> None:
    """Deliberate divergence from ``reflect.ts:162``.

    The reference implementation mutates the binding in place and never
    notifies, so a consumer observes a new value with no reload. Here the old
    binding is withdrawn first, which deactivates dependents, and the new one is
    installed afresh, which brings them back against the new value.
    """
    root = Context()
    seen: list[str] = []
    captured: dict[str, Context] = {}

    @plugin(name="provider", provide=["store"])
    def provider(ctx: Context, config: Any) -> None:
        captured["ctx"] = ctx
        ctx.set("store", "first")

    @plugin(name="consumer", inject=["store"])
    def consumer(ctx: Context, config: Any) -> None:
        seen.append(ctx.store)

    supplier = root.use(provider)
    dependent = root.use(consumer)
    await root.registry.settle()
    assert seen == ["first"]

    captured["ctx"].set("store", "second")
    await root.registry.settle()

    assert seen == ["first", "second"]
    assert dependent.state is FiberState.ACTIVE
    assert supplier.state is FiberState.ACTIVE


# --------------------------------------------------------------------------
# inertia
# --------------------------------------------------------------------------


async def test_target_change_mid_load_chains_into_unload_and_back() -> None:
    """Once a transition begins it completes; the tail chains into the other."""
    root = Context()
    gate = asyncio.Event()
    log: list[str] = []

    def make_provider(label: str) -> Any:
        @plugin(name=label, provide=["dep"])
        def provider(ctx: Context, config: Any) -> None:
            ctx.set("dep", label)

        return provider

    @plugin(name="slow", inject=["dep"])
    async def slow(ctx: Context, config: Any) -> Any:
        log.append("apply")
        await gate.wait()
        return lambda: log.append("inverse")

    first = root.use(make_provider("first"))
    await root.registry.settle()
    dependent = root.use(slow)
    await asyncio.sleep(0)
    assert dependent.state is FiberState.LOADING
    assert log == ["apply"]

    # Change the target while the load is in flight. The fiber must not react
    # yet -- it only records the new target.
    retire = asyncio.ensure_future(first.retire())
    await asyncio.sleep(0)
    assert dependent.state is FiberState.LOADING
    assert dependent.target is None

    gate.set()
    await retire
    await dependent.wait()

    # The completed load found a stale target and chained straight into unload.
    assert log == ["apply", "inverse"]
    assert dependent.state is FiberState.INACTIVE

    gate.clear()
    gate.set()
    second = root.use(make_provider("second"))
    await second.wait()
    await dependent.wait()

    assert dependent.state is FiberState.ACTIVE
    assert log == ["apply", "inverse", "apply"]


async def test_a_transition_in_flight_only_records_the_new_target() -> None:
    root = Context()
    gate = asyncio.Event()

    @plugin(name="provider", provide=["dep"])
    def provider(ctx: Context, config: Any) -> None:
        ctx.set("dep", "value")

    @plugin(name="slow", inject=["dep"])
    async def slow(ctx: Context, config: Any) -> None:
        await gate.wait()

    root.use(provider)
    await root.registry.settle()
    dependent = root.use(slow)
    await asyncio.sleep(0)

    inertia = dependent.inertia
    assert inertia is not None
    dependent.refresh()
    assert dependent.inertia is inertia, "refresh must not start a second transition"

    gate.set()
    await dependent.wait()
    assert dependent.state is FiberState.ACTIVE


# --------------------------------------------------------------------------
# failure containment
# --------------------------------------------------------------------------


async def test_a_failing_apply_still_runs_the_inverses_it_accumulated() -> None:
    root = Context()
    log: list[str] = []

    @plugin(name="broken")
    def broken(ctx: Context, config: Any) -> Any:
        def body() -> Any:
            yield lambda: log.append("undo-1")
            yield lambda: log.append("undo-2")
            raise RuntimeError("half way through")

        return body()

    @plugin(name="healthy")
    def healthy(ctx: Context, config: Any) -> None:
        log.append("healthy")

    fiber = root.use(broken)
    other = root.use(healthy)
    await root.registry.settle()

    assert fiber.state is FiberState.FAILED
    assert isinstance(fiber.error, RuntimeError)
    assert log == ["healthy", "undo-2", "undo-1"]
    assert other.state is FiberState.ACTIVE, "one failure must not disturb the rest"


async def test_the_error_is_cleared_by_a_later_successful_reload() -> None:
    """Deliberate divergence from ``fiber.ts:482``.

    The reference implementation clears the recorded error only in ``update()``,
    so a fiber that fails once reports FAILED forever.
    """
    root = Context()
    attempts: list[int] = []

    @plugin(name="flaky", inject=["dep"])
    def flaky(ctx: Context, config: Any) -> None:
        attempts.append(len(attempts))
        if len(attempts) == 1:
            raise RuntimeError("first attempt fails")

    def make_provider(label: str) -> Any:
        @plugin(name=label, provide=["dep"])
        def provider(ctx: Context, config: Any) -> None:
            ctx.set("dep", label)

        return provider

    first = root.use(make_provider("first"))
    fiber = root.use(flaky)
    await root.registry.settle()
    assert fiber.state is FiberState.FAILED
    assert fiber.error is not None

    await first.retire()
    second = root.use(make_provider("second"))
    await root.registry.settle()

    assert second.state is FiberState.ACTIVE
    assert fiber.state is FiberState.ACTIVE
    assert fiber.error is None


# --------------------------------------------------------------------------
# recovery that performs effects of its own
# --------------------------------------------------------------------------


async def test_an_effect_created_inside_an_inverse_is_recovered_by_the_same_unload() -> None:
    """The unload drains until the accumulator is genuinely empty.

    ``DisposerChain.__call__`` swaps its list out before running anything, which
    is what makes it idempotent -- so an inverse that itself calls ``ctx.effect``
    prepends onto a chain the current pass has already emptied. A single call
    would end the unload with that inverse still queued on the fiber, to fire one
    whole cycle late, during the *next* unload.
    """
    root = Context()
    log: list[str] = []

    @plugin(name="provider", provide=["store"])
    def provider(ctx: Context, config: Any) -> None:
        ctx.set("store", "value")

    @plugin(name="consumer", inject=["store"])
    def consumer(ctx: Context, config: Any) -> Any:
        def release() -> None:
            log.append("release")
            ctx.effect(lambda: lambda: log.append("late"))

        return release

    supplier = root.use(provider)
    dependent = root.use(consumer)
    await root.registry.settle()
    assert dependent.state is FiberState.ACTIVE

    await supplier.retire()
    await root.registry.settle()

    assert log == ["release", "late"]
    assert len(dependent.dispose) == 0, "the unload left an inverse queued on the fiber"
    assert dependent.state is FiberState.INACTIVE
    assert dependent.committed is None


async def test_a_recovery_that_never_converges_is_bounded_and_warned(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The drain is bounded: a component that keeps generating effects is named."""
    root = Context()
    passes: list[int] = []

    @plugin(name="provider", provide=["store"])
    def provider(ctx: Context, config: Any) -> None:
        ctx.set("store", "value")

    @plugin(name="looping", inject=["store"])
    def looping(ctx: Context, config: Any) -> Any:
        def again() -> None:
            passes.append(1)
            ctx.effect(lambda: again)

        return again

    supplier = root.use(provider)
    dependent = root.use(looping)
    await root.registry.settle()
    assert dependent.state is FiberState.ACTIVE

    with caplog.at_level("WARNING", logger="cordispy.fiber"):
        await supplier.retire()
        await root.registry.settle()

    assert len(passes) == UNLOAD_DRAIN_PASSES, "the unload stopped instead of hanging"
    assert "not converging" in caplog.text
    assert "looping" in caplog.text
    assert dependent.state is FiberState.INACTIVE


# --------------------------------------------------------------------------
# a transition that ends abnormally
# --------------------------------------------------------------------------


class _Interrupt(BaseException):
    """Deliberately a ``BaseException`` and not an ``Exception``."""


async def _settle_turns(times: int = 5) -> None:
    """Let pending transitions finish without going through ``wait``.

    The defect these tests pin turns ``wait`` and ``Registry.settle`` into hot
    spins, and a spin that never yields blocks the event loop outright -- so an
    ``asyncio.wait_for`` around either of them could never fire its timeout.
    Turning the loop by hand is what lets the assertions below fail fast instead
    of hanging.
    """
    for _ in range(times):
        await asyncio.sleep(0)


async def test_a_base_exception_from_apply_settles_the_fiber_instead_of_wedging_it() -> None:
    """A transition that ends abnormally must still clear its own inertia.

    ``wait`` follows ``fiber.inertia`` until it is ``None``, and the transition
    *tail* is the only thing that normally clears it. The ``except Exception``
    that absorbs an ordinary load failure does not cover a ``BaseException``, so
    one of those skips the tail and leaves the field pointing at an *already
    finished* task -- and awaiting a finished task does not block. ``wait`` and
    ``Registry.settle`` stop being waits and become hot spins that never return.

    The realistic trigger is not an exotic exception class but
    ``asyncio.CancelledError``: a task group, an ``asyncio.timeout`` or a Ctrl-C
    while a component is loading all raise it here.
    """
    root = Context()

    @plugin(name="boom")
    def boom(ctx: Context, config: Any) -> None:
        raise _Interrupt("not an Exception")

    fiber = root.use(boom)
    inertia = fiber.inertia
    assert inertia is not None

    await _settle_turns()

    assert inertia.done(), "the transition ended"
    assert fiber.inertia is None, "but it left its inertia pointing at the finished task"
    assert fiber.state is FiberState.FAILED
    assert isinstance(fiber.error, _Interrupt)
    # Only now are these safe to call, which is the point of the assertions above.
    await root.registry.settle()
    await fiber.wait()


async def test_cancelling_a_transition_in_flight_leaves_a_waitable_fiber() -> None:
    """Cancellation is re-raised, but the fiber is settled before it goes."""
    root = Context()
    started = asyncio.Event()

    @plugin(name="slow")
    async def slow(ctx: Context, config: Any) -> None:
        started.set()
        await asyncio.sleep(3600)

    fiber = root.use(slow)
    await asyncio.wait_for(started.wait(), timeout=5.0)
    inertia = fiber.inertia
    assert inertia is not None

    inertia.cancel()
    await _settle_turns()

    assert inertia.cancelled()
    assert fiber.inertia is None, "the cancelled transition left its inertia set"
    assert fiber.state is FiberState.FAILED
    await fiber.wait()


def test_a_transition_cancelled_before_it_starts_still_lets_wait_return() -> None:
    """The one case no transition tail can clean up, because none ever ran.

    ``asyncio.create_task`` runs nothing until the loop yields, so a task
    cancelled before its first statement never reaches the fiber's own
    abnormal-exit handling. ``wait`` clearing the field itself is the only thing
    between this and a loop that never ends.

    This runs in a subprocess on purpose. The failure mode is non-termination,
    and the spin does not yield, so nothing *inside* the event loop -- a
    timeout, a bounded manual drive of the coroutine -- can ever regain control
    to call it off. A subprocess timeout is the only bound that still works, and
    it keeps a regression here reporting as a failure rather than as a suite
    that hangs forever.
    """
    script = textwrap.dedent(
        """
        import asyncio
        from cordispy import Context, plugin

        async def main() -> None:
            root = Context()

            @plugin(name="never")
            def never(ctx, config):
                return None

            fiber = root.use(never)
            inertia = fiber.inertia
            assert inertia is not None, "the reload task exists before the loop yields"
            inertia.cancel()
            await fiber.wait()
            assert fiber.inertia is None
            await root.registry.settle()
            print("WAIT_RETURNED")

        asyncio.run(main())
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    assert completed.returncode == 0, completed.stderr
    assert "WAIT_RETURNED" in completed.stdout, "fiber.wait() never returned"


# --------------------------------------------------------------------------
# cascading and cycles
# --------------------------------------------------------------------------


async def test_unloading_a_parent_cascades_to_its_children() -> None:
    root = Context()
    log: list[str] = []

    @plugin(name="child")
    def child(ctx: Context, config: Any) -> Any:
        return lambda: log.append("child")

    @plugin(name="parent")
    def parent(ctx: Context, config: Any) -> Any:
        ctx.use(child)
        return lambda: log.append("parent")

    fiber = root.use(parent)
    await fiber.wait()
    assert len(root.registry) == 3  # the root, the parent and its child

    await fiber.retire()
    assert log == ["parent", "child"]
    assert fiber.state is FiberState.DISPOSED


async def test_a_declaration_cycle_is_detected_and_reported(
    caplog: pytest.LogCaptureFixture,
) -> None:
    root = Context()

    @plugin(name="alpha", inject=["beta"], provide=["alpha"])
    def alpha(ctx: Context, config: Any) -> None:
        ctx.set("alpha", 1)

    @plugin(name="beta", inject=["alpha"], provide=["beta"])
    def beta(ctx: Context, config: Any) -> None:
        ctx.set("beta", 2)

    first = root.use(alpha)
    with caplog.at_level("WARNING", logger="cordispy.registry"):
        second = root.use(beta)
    await root.registry.settle()

    cycles = root.registry.find_cycles()
    assert len(cycles) == 1
    assert set(cycles[0]) == {"alpha", "beta"}
    assert "dependency cycle detected" in caplog.text

    assert first.state is FiberState.PENDING
    assert second.state is FiberState.PENDING


def test_cycle_detection_handles_a_reverse_attached_deep_acyclic_graph() -> None:
    root = Context()
    registry = root.registry
    fibers = [
        Fiber(
            uid=registry.next_uid(),
            parent=root,
            inject=Inject(required=() if index == 0 else (f"key-{index - 1}",)),
            provide=(f"key-{index}",),
            label=f"link-{index}",
            registry=registry,
        )
        for index in range(5_000)
    ]
    for fiber in reversed(fibers):
        registry.attach(fiber)

    assert registry.find_cycles() == []


async def test_a_self_cycle_is_detected_and_reported(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A component injecting a key it also provides is a cycle of length one.

    It waits for a binding only its own activation could install, so it stays
    PENDING for good. That is as predictable from the declarations as any longer
    cycle (paper section 6.5), so skipping self-edges left the one shape a reader
    is most likely to write by accident with no diagnostic at all.
    """
    root = Context()

    @plugin(name="selfish", inject=["store"], provide=["store"])
    def selfish(ctx: Context, config: Any) -> None:
        ctx.set("store", "value")

    with caplog.at_level("WARNING", logger="cordispy.registry"):
        fiber = root.use(selfish)
    await root.registry.settle()

    assert root.registry.find_cycles() == [("selfish",)]
    assert "selfish -> selfish" in caplog.text
    assert fiber.state is FiberState.PENDING


async def test_a_self_edge_that_crosses_realms_is_reported_as_the_conditional_it_is(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The limit of declaration-only detection, stated rather than papered over.

    Reporting self-edges is what gives the ordinary 1-cycle above a diagnostic.
    The cost is this case: a wrapper that *reads* ``store`` from its own realm
    and *publishes* ``store`` into a derived one is a legitimate component that
    activates perfectly well, and its declarations are indistinguishable from
    the deadlocked one -- the realm it publishes into is chosen inside ``apply``,
    on a context that does not exist when the check runs.

    So the detection stays, and the warning does not claim an inactivity this
    fiber plainly contradicts.
    """
    root = Context()
    inner = Realm("wrapped store")

    @plugin(name="base", provide=["store"])
    def base(ctx: Context, config: Any) -> None:
        ctx.set("store", "raw")

    @plugin(name="wrapper", inject=["store"], provide=["store"])
    def wrapper(ctx: Context, config: Any) -> None:
        ctx.isolate("store", inner).set("store", f"wrapped({ctx.store})")

    root.use(base)
    with caplog.at_level("WARNING", logger="cordispy.registry"):
        fiber = root.use(wrapper)
    await root.registry.settle()

    assert fiber.state is FiberState.ACTIVE, "the wrapper is not stuck at all"
    assert root.isolate("store", inner).get("store") == "wrapped(raw)"

    # Still reported -- the declarations really are shaped like a 1-cycle.
    assert ("wrapper",) in root.registry.find_cycles()
    assert "wrapper -> wrapper" in caplog.text
    # But not asserted as fact against a fiber that went ACTIVE.
    assert "unless it publishes into a realm other than the one it reads" in caplog.text
    assert "every component in it stays inactive" not in caplog.text


async def test_a_dropped_fiber_refuses_new_effects() -> None:
    from cordispy import InactiveEffectError

    root = Context()
    captured: dict[str, Context] = {}

    @plugin(name="component")
    def component(ctx: Context, config: Any) -> None:
        captured["ctx"] = ctx

    fiber = root.use(component)
    await fiber.wait()
    await fiber.retire()

    with pytest.raises(InactiveEffectError):
        captured["ctx"].effect(lambda: None)


async def test_wait_reports_failure_without_raising() -> None:
    """The drain must not be derailed by a dependent that failed."""
    root = Context()

    @plugin(name="broken")
    def broken(ctx: Context, config: Any) -> None:
        raise RuntimeError("boom")

    fiber = root.use(broken)
    settled = await fiber.wait()

    assert settled is fiber
    assert fiber.state is FiberState.FAILED
    assert isinstance(fiber.error, RuntimeError)


async def test_retire_is_idempotent() -> None:
    root = Context()
    log: list[str] = []

    @plugin(name="component")
    def component(ctx: Context, config: Any) -> Any:
        return lambda: log.append("inverse")

    fiber = root.use(component)
    await root.registry.settle()

    await fiber.retire()
    await fiber.retire()

    assert log == ["inverse"]
    assert fiber.state is FiberState.DISPOSED
    assert fiber.uid is None
    assert len(root.registry) == 1  # only the root remains


async def test_the_package_quickstart_behaves_as_documented() -> None:
    """Keeps the tour in ``cordispy/__init__.py`` honest."""
    root = Context()

    @plugin(name="store", provide=["store"])
    def store(ctx: Context, config: Any) -> None:
        ctx.set("store", {})

    @plugin(name="counter", inject=["store"])
    def counter(ctx: Context, config: Any) -> Any:
        ctx.store["hits"] = 0
        return lambda: ctx.store.pop("hits", None)

    provider = root.use(store)
    consumer = root.use(counter)

    await root.registry.settle()
    assert consumer.state is FiberState.ACTIVE
    assert root.get("store") == {"hits": 0}

    await provider.retire()
    assert consumer.state is FiberState.INACTIVE
    assert root.get("store") is None
