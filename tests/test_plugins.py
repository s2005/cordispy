"""The built-in plugins -- the temporal claim, asserted rather than described.

``cordispy.plugins.timer`` and ``cordispy.plugins.events`` exist to make one promise
checkable: unloading a component leaves *zero* of the two resources that
conventional plugin systems leak most reliably. Every count below is read from
the live process -- ``asyncio.all_tasks()`` and the bus's own handler lists --
rather than from bookkeeping the plugins keep about themselves.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

from cordispy import Context, CordisError, FiberState, plugin
from cordispy.plugins import Bus, Timer, events_plugin, timer_plugin

# A delay far longer than any test. A timer still armed when a count is taken is
# then unambiguous residue rather than a race with the scheduler.
FOREVER = 3600.0

# How long a test will wait for a timer that is supposed to fire. Generous on
# purpose: it bounds a hang, it does not measure latency.
SETTLE_TIMEOUT = 5.0


class Rang:
    """A timer callback that records its firings and announces them.

    These tests assert that an armed timer *fires*, not that it fires inside a
    particular number of milliseconds. Sleeping a fixed span and asserting
    afterwards makes them statements about scheduler latency instead -- and that
    is not a property this runtime controls: the default Windows timer
    resolution is around 15ms, coarser than the periods used here, so a loaded
    machine can deliver one tick where the arithmetic predicted several.

    So the callback says when it has fired often enough, and the test waits for
    exactly that. The timeout is a bound on a hang, not a measurement.

    The negative assertions ("nothing more fired once the inverse ran") stay
    ordinary sleeps: waiting longer can only make those stronger.
    """

    def __init__(self, times: int = 1) -> None:
        self.marks: list[Any] = []
        self._times = times
        self._enough = asyncio.Event()

    def __call__(self, mark: Any = None) -> None:
        self.marks.append(mark)
        if len(self.marks) >= self._times:
            self._enough.set()

    def of(self, mark: Any) -> Callable[[], None]:
        """A zero-argument callback that records ``mark``."""
        return lambda: self(mark)

    async def enough(self, *, within: float = SETTLE_TIMEOUT) -> None:
        """Wait until it has fired the agreed number of times."""
        async with asyncio.timeout(within):
            await self._enough.wait()


async def loop_turns(times: int = 3) -> None:
    """Let the loop deliver callbacks that are already scheduled.

    A task's *done* callback -- which is how the timer deregisters a fired
    timer -- is delivered on a later turn than the one its coroutine finished
    in. Turning the loop is deterministic; it does not wait on the clock.
    """
    for _ in range(times):
        await asyncio.sleep(0)


async def timer_root() -> Context:
    root = Context()
    root.use(timer_plugin)
    await root.registry.settle()
    return root


async def bus_root() -> Context:
    root = Context()
    root.use(events_plugin)
    await root.registry.settle()
    return root


# --------------------------------------------------------------------------
# timer: it fires
# --------------------------------------------------------------------------


async def test_timeout_fires_once() -> None:
    root = await timer_root()
    timer: Timer = root.get("timer")
    rang = Rang()

    timer.timeout(root, 0.01, rang.of("fired"))
    assert timer.pending == 1
    await rang.enough()

    assert rang.marks == ["fired"]
    # Deregistration is the task's own done callback, delivered on a later turn
    # than the one the timer's callback ran in.
    await loop_turns()
    assert timer.pending == 0, "a timer that has fired is no longer armed"


async def test_interval_repeats_until_it_is_cancelled() -> None:
    root = await timer_root()
    timer: Timer = root.get("timer")
    rang = Rang(times=2)

    dispose = timer.interval(root, 0.01, rang.of(1))
    await rang.enough()
    await dispose()
    seen = len(rang.marks)

    assert seen >= 2, f"expected several ticks, saw {seen}"
    await asyncio.sleep(0.05)
    assert len(rang.marks) == seen, "the interval stopped when its inverse ran"
    assert timer.pending == 0


async def test_the_inverse_cancels_before_the_callback_runs() -> None:
    root = await timer_root()
    timer: Timer = root.get("timer")
    fired: list[str] = []

    dispose = timer.timeout(root, 0.05, lambda: fired.append("fired"))
    await dispose()
    await asyncio.sleep(0.1)

    assert fired == []
    assert timer.pending == 0


# --------------------------------------------------------------------------
# timer: it leaves nothing behind
# --------------------------------------------------------------------------


async def test_unloading_a_consumer_leaves_zero_pending_timer_tasks() -> None:
    """The claim in the SPEC, measured with ``asyncio.all_tasks()``."""
    root = await timer_root()
    baseline = len(asyncio.all_tasks())

    @plugin(name="worker", inject=["timer"])
    def worker(ctx: Context, config: Any) -> None:
        timer: Timer = ctx.timer
        timer.interval(ctx, FOREVER, lambda: None)
        timer.timeout(ctx, FOREVER, lambda: None)

    fiber = root.use(worker)
    await root.registry.settle()
    assert root.get("timer").pending == 2
    assert len(asyncio.all_tasks()) == baseline + 2

    await fiber.retire()
    await asyncio.sleep(0)

    assert fiber.state is FiberState.DISPOSED
    assert root.get("timer").pending == 0
    assert len(asyncio.all_tasks()) == baseline, "cancel() is not enough; the inverse awaits the task"


async def test_a_timer_armed_after_loading_is_still_cancelled_on_unload() -> None:
    """The case a hand-written teardown cannot see: armed while serving."""
    root = await timer_root()
    baseline = len(asyncio.all_tasks())
    handle: dict[str, Context] = {}

    @plugin(name="worker", inject=["timer"])
    def worker(ctx: Context, config: Any) -> None:
        handle["ctx"] = ctx

    fiber = root.use(worker)
    await root.registry.settle()
    assert len(asyncio.all_tasks()) == baseline

    # Long after loading, as a request handler would.
    ctx = handle["ctx"]
    for _ in range(3):
        ctx.timer.timeout(ctx, FOREVER, lambda: None)
    assert len(asyncio.all_tasks()) == baseline + 3

    await fiber.retire()
    await asyncio.sleep(0)
    assert len(asyncio.all_tasks()) == baseline
    assert root.get("timer").pending == 0


async def test_retiring_a_provider_cancels_its_dependents_timers() -> None:
    """The cascade: the consumer never hears about the provider going away."""
    root = await timer_root()

    @plugin(name="provider", provide=["gate"])
    def provider(ctx: Context, config: Any) -> None:
        ctx.set("gate", object())

    @plugin(name="worker", inject=["timer", "gate"])
    def worker(ctx: Context, config: Any) -> None:
        ctx.timer.interval(ctx, FOREVER, lambda: None)

    baseline = len(asyncio.all_tasks())
    gate = root.use(provider)
    consumer = root.use(worker)
    await root.registry.settle()
    assert consumer.state is FiberState.ACTIVE
    assert len(asyncio.all_tasks()) == baseline + 1

    await gate.retire()
    await asyncio.sleep(0)

    assert consumer.state is FiberState.INACTIVE
    assert root.get("timer").pending == 0
    assert len(asyncio.all_tasks()) == baseline


async def test_disposing_one_timer_leaves_the_others_alone() -> None:
    """Regression for the closure-capture bug at ``utils/src/index.ts:19``.

    There, the disposer closes over the container's *current* sequence number
    rather than the one captured when the entry was pushed, so disposing an
    earlier entry removes the most recent one instead.
    """
    root = await timer_root()
    timer: Timer = root.get("timer")
    rang = Rang(times=2)

    first = timer.timeout(root, 0.01, rang.of(1))
    timer.timeout(root, 0.01, rang.of(2))
    timer.timeout(root, 0.01, rang.of(3))
    assert timer.pending == 3

    await first()
    await rang.enough()
    await asyncio.sleep(0.05)

    assert rang.marks == [2, 3], "the disposer must cancel the timer it was returned for"


async def test_the_timer_component_cancels_stragglers_when_it_is_retired() -> None:
    root = await timer_root()
    timer: Timer = root.get("timer")
    baseline = len(asyncio.all_tasks())

    timer.timeout(root, FOREVER, lambda: None)
    assert len(asyncio.all_tasks()) == baseline + 1

    provider = next(fiber for fiber in root.registry if fiber.label == "timer")
    await provider.retire()
    await asyncio.sleep(0)

    assert timer.pending == 0
    assert len(asyncio.all_tasks()) == baseline
    assert root.get("timer") is None


async def test_arming_a_timer_needs_the_service_to_be_declared() -> None:
    """A component that did not declare ``timer`` cannot reach it by accident."""
    from cordispy import UndeclaredAccessError

    root = await timer_root()
    seen: list[str] = []

    @plugin(name="sneaky")
    def sneaky(ctx: Context, config: Any) -> None:
        try:
            _ = ctx.timer
        except UndeclaredAccessError as error:
            seen.append(type(error).__name__)

    root.use(sneaky)
    await root.registry.settle()
    assert seen == ["UndeclaredAccessError"]


# --------------------------------------------------------------------------
# events: delivery
# --------------------------------------------------------------------------


async def test_a_subscription_receives_events_and_its_inverse_removes_it() -> None:
    root = await bus_root()
    bus: Bus = root.get("bus")
    seen: list[str] = []

    dispose = bus.on(root, "request", lambda path: seen.append(path))
    bus.emit("request", "/a")
    assert seen == ["/a"]
    assert bus.subscribers("request") == 1

    await dispose()
    bus.emit("request", "/b")

    assert seen == ["/a"]
    assert bus.subscribers() == 0
    assert bus.events() == ()


async def test_handlers_run_in_subscription_order() -> None:
    root = await bus_root()
    bus: Bus = root.get("bus")
    order: list[str] = []

    bus.on(root, "e", lambda payload: order.append("first"))
    bus.on(root, "e", lambda payload: order.append("second"))
    bus.emit("e", None)

    assert order == ["first", "second"]


async def test_the_same_handler_may_subscribe_twice_and_be_removed_once() -> None:
    """Subscriptions are compared by identity, not by the handler's equality."""
    root = await bus_root()
    bus: Bus = root.get("bus")
    seen: list[int] = []

    def handler(payload: Any) -> None:
        seen.append(1)

    first = bus.on(root, "e", handler)
    bus.on(root, "e", handler)
    bus.emit("e", None)
    assert len(seen) == 2

    await first()
    seen.clear()
    bus.emit("e", None)

    assert len(seen) == 1
    assert bus.subscribers("e") == 1


async def test_removing_an_earlier_subscription_keeps_the_later_ones() -> None:
    """The other half of the ``utils/src/index.ts:19`` closure-capture bug."""
    root = await bus_root()
    bus: Bus = root.get("bus")
    seen: list[str] = []

    first = bus.on(root, "e", lambda payload: seen.append("a"))
    bus.on(root, "e", lambda payload: seen.append("b"))
    bus.on(root, "e", lambda payload: seen.append("c"))

    await first()
    bus.emit("e", None)

    assert seen == ["b", "c"]


async def test_a_handler_that_subscribes_during_dispatch_is_not_called_by_it() -> None:
    root = await bus_root()
    bus: Bus = root.get("bus")
    seen: list[str] = []

    def outer(payload: Any) -> None:
        seen.append("outer")
        bus.on(root, "e", lambda _payload: seen.append("inner"))

    bus.on(root, "e", outer)
    bus.emit("e", None)
    assert seen == ["outer"]

    seen.clear()
    bus.emit("e", None)
    assert seen == ["outer", "inner"]


async def test_a_coroutine_handler_is_rejected_rather_than_left_unawaited() -> None:
    root = await bus_root()
    bus: Bus = root.get("bus")

    async def handler(payload: Any) -> None:  # pragma: no cover - never awaited
        await asyncio.sleep(0)

    bus.on(root, "e", handler)
    with pytest.raises(CordisError, match="synchronous"):
        bus.emit("e", None)


# --------------------------------------------------------------------------
# events: it leaves nothing behind
# --------------------------------------------------------------------------


async def test_unloading_a_consumer_leaves_zero_subscribers() -> None:
    root = await bus_root()
    bus: Bus = root.get("bus")

    @plugin(name="listener", inject=["bus"])
    def listener(ctx: Context, config: Any) -> None:
        ctx.bus.on(ctx, "request", lambda path: None)
        ctx.bus.on(ctx, "response", lambda path: None)

    fiber = root.use(listener)
    await root.registry.settle()
    assert bus.subscribers() == 2

    await fiber.retire()
    assert bus.subscribers() == 0
    assert bus.events() == ()


async def test_a_subscription_made_after_loading_is_still_removed_on_unload() -> None:
    """The leak a hand-written teardown cannot see: subscribed while serving."""
    root = await bus_root()
    bus: Bus = root.get("bus")
    handle: dict[str, Context] = {}

    @plugin(name="listener", inject=["bus"])
    def listener(ctx: Context, config: Any) -> None:
        handle["ctx"] = ctx

    fiber = root.use(listener)
    await root.registry.settle()
    assert bus.subscribers() == 0

    ctx = handle["ctx"]
    for path in ("/a", "/b", "/c"):
        ctx.bus.on(ctx, f"request:{path}", lambda payload: None)
    assert bus.subscribers() == 3

    await fiber.retire()
    assert bus.subscribers() == 0


async def test_the_raw_subscribe_hands_the_obligation_back_to_the_caller() -> None:
    """``subscribe`` is the unmanaged form the conventional implementation uses."""
    bus = Bus()
    seen: list[str] = []

    remove = bus.subscribe("e", lambda payload: seen.append("x"))
    bus.emit("e", None)
    assert bus.subscribers() == 1

    remove()
    bus.emit("e", None)

    assert seen == ["x"]
    assert bus.subscribers() == 0


async def test_retiring_the_events_component_drops_every_subscription() -> None:
    root = await bus_root()
    bus: Bus = root.get("bus")
    bus.subscribe("e", lambda payload: None)
    assert bus.subscribers() == 1

    provider = next(fiber for fiber in root.registry if fiber.label == "events")
    await provider.retire()

    assert bus.subscribers() == 0
    assert root.get("bus") is None


# --------------------------------------------------------------------------
# the two together
# --------------------------------------------------------------------------


async def test_both_services_recover_when_a_shared_provider_is_withdrawn() -> None:
    """One withdrawal, two kinds of resource, no cleanup code in the consumer."""
    root = Context()
    root.use(timer_plugin)
    root.use(events_plugin)
    await root.registry.settle()
    baseline = len(asyncio.all_tasks())

    @plugin(name="provider", provide=["session"])
    def provider(ctx: Context, config: Any) -> None:
        ctx.set("session", {"id": 1})

    @plugin(name="worker", inject=["timer", "bus", "session"])
    def worker(ctx: Context, config: Any) -> None:
        ctx.timer.interval(ctx, FOREVER, lambda: None)
        ctx.bus.on(ctx, "request", lambda path: None)

    session = root.use(provider)
    consumer = root.use(worker)
    await root.registry.settle()

    bus: Bus = root.get("bus")
    timer: Timer = root.get("timer")
    assert consumer.state is FiberState.ACTIVE
    assert (timer.pending, bus.subscribers()) == (1, 1)
    assert len(asyncio.all_tasks()) == baseline + 1

    await session.retire()
    await asyncio.sleep(0)

    assert consumer.state is FiberState.INACTIVE
    assert (timer.pending, bus.subscribers()) == (0, 0)
    assert len(asyncio.all_tasks()) == baseline
