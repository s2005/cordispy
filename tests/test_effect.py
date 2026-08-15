"""Revertible effects -- paper Algorithm 1."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest

from cordispy import Context, DisposerChain, InvalidEffectError, compose, execute, plugin

# --------------------------------------------------------------------------
# the five accepted callback forms
# --------------------------------------------------------------------------


async def test_callback_returning_nothing() -> None:
    root = Context()
    log: list[str] = []
    dispose = root.effect(lambda: log.append("applied"))
    await dispose()
    assert log == ["applied"]


async def test_callback_returning_a_single_inverse() -> None:
    root = Context()
    log: list[str] = []

    def callback() -> Any:
        log.append("applied")
        return lambda: log.append("recovered")

    dispose = root.effect(callback)
    assert log == ["applied"]
    await dispose()
    assert log == ["applied", "recovered"]


async def test_async_callback_returning_an_inverse() -> None:
    root = Context()
    log: list[str] = []

    async def callback() -> Any:
        await asyncio.sleep(0)
        log.append("applied")
        return lambda: log.append("recovered")

    dispose = root.effect(callback)
    assert log == [], "asyncio is lazy: the body has not started when effect() returns"
    await asyncio.sleep(0)
    await dispose()
    assert log == ["applied", "recovered"]


async def test_generator_callback_yields_one_inverse_per_step() -> None:
    root = Context()
    log: list[str] = []

    def callback() -> Iterator[Any]:
        log.append("first")
        yield lambda: log.append("undo-first")
        log.append("second")
        yield lambda: log.append("undo-second")

    dispose = root.effect(callback)
    assert log == ["first", "second"]
    await dispose()
    assert log == ["first", "second", "undo-second", "undo-first"]


async def test_async_generator_callback_yields_one_inverse_per_step() -> None:
    root = Context()
    log: list[str] = []

    async def callback() -> AsyncIterator[Any]:
        log.append("first")
        yield lambda: log.append("undo-first")
        await asyncio.sleep(0)
        log.append("second")
        yield lambda: log.append("undo-second")

    dispose = root.effect(callback)
    await asyncio.sleep(0)
    await dispose()
    assert log == ["first", "second", "undo-second", "undo-first"]


async def test_an_effect_disposed_before_its_first_step_never_applies() -> None:
    """Algorithm 1 checks the guard *before* every step, the first included.

    An asyncio task does not run a single statement until the loop yields, so an
    async effect disposed in the same tick it was created is skipped outright --
    and that is the right outcome: nothing was applied, so nothing needs
    recovering.
    """
    root = Context()
    log: list[str] = []

    async def callback() -> Any:
        log.append("applied")
        return lambda: log.append("recovered")

    dispose = root.effect(callback)
    await dispose()
    assert log == []


async def test_a_non_callable_inverse_is_rejected() -> None:
    root = Context()
    with pytest.raises(InvalidEffectError):
        root.effect(lambda: "not an inverse")


# --------------------------------------------------------------------------
# LIFO, idempotence, and the synchronous fast path
# --------------------------------------------------------------------------


async def test_recovery_runs_last_applied_first() -> None:
    root = Context()
    log: list[int] = []
    for index in range(4):
        root.effect(lambda index=index: lambda: log.append(index))
    await root.fiber.dispose()
    assert log == [3, 2, 1, 0]


async def test_dispose_is_idempotent() -> None:
    root = Context()
    log: list[str] = []
    dispose = root.effect(lambda: lambda: log.append("recovered"))
    await dispose()
    await dispose()
    await dispose()
    assert log == ["recovered"]


def test_a_synchronous_effect_needs_no_event_loop() -> None:
    """``ctx.effect(lambda: cleanup)`` must work with no loop running at all."""
    root = Context()
    log: list[str] = []
    dispose = root.effect(lambda: lambda: log.append("recovered"))
    assert log == []
    asyncio.run(dispose())
    assert log == ["recovered"]


def test_an_async_effect_requires_a_running_loop() -> None:
    root = Context()

    async def callback() -> None:
        await asyncio.sleep(0)

    with pytest.raises(RuntimeError):
        root.effect(callback)


# --------------------------------------------------------------------------
# the guard, at a step boundary
# --------------------------------------------------------------------------


async def test_the_guard_halts_iteration_at_a_step_boundary() -> None:
    """Only the inverses accumulated before the guard tripped remain.

    The generator's ``finally`` must also run. The reference implementation
    abandons an interrupted async generator without closing it
    (``fiber.ts:263``), so cleanup written as ``try/finally`` never happens.
    """
    log: list[str] = []
    armed = {"value": True}

    async def callback() -> AsyncIterator[Any]:
        try:
            yield lambda: log.append("undo-a")
            armed["value"] = False
            yield lambda: log.append("undo-b")
            log.append("never reached")
            yield lambda: log.append("undo-c")
        finally:
            log.append("closed")

    chain = await execute(callback, lambda: armed["value"])
    assert log == ["closed"]

    await chain()
    assert log == ["closed", "undo-b", "undo-a"]
    assert "never reached" not in log


async def test_disposing_mid_flight_halts_the_effect_and_recovers() -> None:
    root = Context()
    log: list[str] = []
    gate = asyncio.Event()

    async def callback() -> AsyncIterator[Any]:
        yield lambda: log.append("undo-a")
        await gate.wait()
        yield lambda: log.append("undo-b")
        log.append("kept going")
        yield lambda: log.append("undo-c")

    dispose = root.effect(callback)
    await asyncio.sleep(0)
    task = asyncio.ensure_future(dispose())
    await asyncio.sleep(0)
    gate.set()
    await task

    assert "kept going" not in log
    assert log == ["undo-b", "undo-a"]


# --------------------------------------------------------------------------
# error isolation in the disposer chain
# --------------------------------------------------------------------------


async def test_one_throwing_inverse_does_not_leak_the_others() -> None:
    """Deliberate divergence from ``fiber.ts:281``."""
    chain = DisposerChain()
    log: list[str] = []

    def boom() -> None:
        raise RuntimeError("inverse failed")

    chain.prepend(lambda: log.append("first"))
    chain.prepend(boom)
    chain.prepend(lambda: log.append("third"))

    with pytest.raises(ExceptionGroup) as info:
        await chain()

    assert log == ["third", "first"], "every remaining inverse still ran"
    assert len(info.value.exceptions) == 1
    assert isinstance(info.value.exceptions[0], RuntimeError)


async def test_the_chain_runs_inverses_strictly_one_after_another() -> None:
    """Deliberate divergence from ``fiber.ts:438``: LIFO, not merely LIFO-started."""
    chain = DisposerChain()
    log: list[str] = []

    def make(label: str, delay: float) -> Any:
        async def inverse() -> None:
            await asyncio.sleep(delay)
            log.append(label)

        return inverse

    chain.prepend(make("slow", 0.02))
    chain.prepend(make("fast", 0.0))
    await chain()
    assert log == ["fast", "slow"]


async def test_compose_puts_the_newer_inverse_first() -> None:
    log: list[str] = []
    composed = compose(lambda: log.append("later"), lambda: log.append("earlier"))
    await composed()
    assert log == ["later", "earlier"]


# --------------------------------------------------------------------------
# effects belong to the fiber that created them
# --------------------------------------------------------------------------


async def test_a_child_effect_is_recovered_when_the_fiber_unloads() -> None:
    root = Context()
    log: list[str] = []

    @plugin(name="component")
    def component(ctx: Context, config: Any) -> None:
        ctx.effect(lambda: lambda: log.append("inner-1"))
        ctx.effect(lambda: lambda: log.append("inner-2"))

    fiber = root.use(component)
    await fiber.wait()
    assert log == []

    await fiber.retire()
    assert log == ["inner-2", "inner-1"]


async def test_an_effect_that_fails_synchronously_keeps_its_partial_inverses() -> None:
    root = Context()
    log: list[str] = []

    def callback() -> Iterator[Any]:
        yield lambda: log.append("undo-a")
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        root.effect(callback)

    await root.fiber.dispose()
    assert log == ["undo-a"]
