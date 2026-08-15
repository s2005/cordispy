"""Revertible effects -- paper section 5.1.1, Algorithm 1.

Every context mutation in this runtime flows through one primitive. A callback
applies something and hands back the *inverse* that undoes it; the engine folds
the inverses of every step into one composite that runs them last-applied-first.

The engine is deliberately polymorphic in the callback, exactly as the paper
allows both a plain effect function and an effect *iterator*:

===========================================  ==========================================
Form                                         Meaning
===========================================  ==========================================
``def cb() -> None``                         effect with no inverse
``def cb() -> Disposer``                     a single inverse
``async def cb() -> Disposer | None``        the same, asynchronously
``def cb() -> Generator[Disposer, ...]``     iterator form, one inverse per ``yield``
``async def cb() -> AsyncGenerator[...]``    iterator form, asynchronous
===========================================  ==========================================

The runtime does **not** verify that an inverse actually reverses its effect.
That is an obligation on the component author (paper section 5.1.1); the runtime
only guarantees *when* the inverse runs.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, TypeAlias

from .errors import InvalidEffectError

__all__ = [
    "AsyncDisposer",
    "DisposerChain",
    "EffectCallback",
    "Guard",
    "Pending",
    "compose",
    "execute",
    "invoke",
    "noop",
    "spawn",
    "start",
]

logger = logging.getLogger("cordispy.effect")

#: An inverse. It may run synchronously or return an awaitable.
Disposer: TypeAlias = Callable[[], Any]

#: A composed inverse, which is always awaited.
AsyncDisposer: TypeAlias = Callable[[], Awaitable[None]]

#: The predicate consulted at every iteration boundary (Algorithm 1, line 4).
Guard: TypeAlias = Callable[[], bool]

#: An effect callback in any of the five accepted forms.
EffectCallback: TypeAlias = Callable[[], Any]


def noop() -> None:
    """The identity inverse -- the paper's ``id``."""


async def invoke(disposer: Disposer) -> None:
    """Run one inverse, awaiting it when it is asynchronous."""
    result = disposer()
    if inspect.isawaitable(result):
        await result


class DisposerChain:
    """The accumulated inverse of a sequence of effects.

    ``prepend`` realizes the paper's composition ``f . g``: the newest inverse
    runs *first*, which is LIFO recovery. The chain is modelled as a list rather
    than as nested closures so that recovery is iterative -- a fiber that has
    accumulated ten thousand effects must not need ten thousand stack frames to
    undo them.

    Two deliberate divergences from the reference implementation live here:

    * ``fiber.ts:281`` empties the disposer array *before* running it, so one
      throwing inverse permanently leaks every inverse after it. This chain runs
      every inverse, collects the failures, and raises them together as an
      :class:`ExceptionGroup`.
    * ``fiber.ts:438`` maps the reversed list through ``Promise.all``, so the
      inverses only *start* in LIFO order. This chain awaits them strictly one
      after another, which is what ``dispose_2 . dispose_1`` means.
    """

    __slots__ = ("_items",)

    def __init__(self) -> None:
        self._items: list[Disposer] = []

    def __len__(self) -> int:
        return len(self._items)

    def __bool__(self) -> bool:
        return bool(self._items)

    def __repr__(self) -> str:
        return f"DisposerChain({len(self._items)} pending)"

    def prepend(self, disposer: Disposer) -> None:
        """Compose ``disposer`` in front of everything accumulated so far."""
        self._items.insert(0, disposer)

    async def __call__(self) -> None:
        """Run every accumulated inverse in LIFO order, then reset to ``id``.

        Draining the list before running it makes the chain idempotent: calling
        it twice runs each inverse at most once, because firing an inverse twice
        would apply it at a state no application of the effect ever produced.
        """
        items, self._items = self._items, []
        errors: list[Exception] = []
        for item in items:
            try:
                await invoke(item)
            except ExceptionGroup as group:
                errors.extend(group.exceptions)
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise ExceptionGroup("effect recovery failed", errors)


def compose(later: Disposer, earlier: Disposer) -> DisposerChain:
    """Return the composite inverse that runs ``later`` and then ``earlier``."""
    chain = DisposerChain()
    chain.prepend(earlier)
    chain.prepend(later)
    return chain


def _collect(value: Any, chain: DisposerChain) -> None:
    """Fold one step's yielded value into the accumulated inverse."""
    if value is None:
        return
    if callable(value):
        chain.prepend(value)
        return
    raise InvalidEffectError(f"an effect must produce a callable inverse or None, got {type(value).__name__}")


def _drive_sync_generator(
    generator: Any,
    guard: Guard,
    chain: DisposerChain,
) -> None:
    """Algorithm 1, lines 4-7 over a synchronous effect iterator."""
    try:
        while guard():
            try:
                value = next(generator)
            except StopIteration:
                return
            _collect(value, chain)
    finally:
        # The reference implementation abandons an interrupted iterator without
        # closing it (fiber.ts:263), so a `try/finally` inside a generator effect
        # never runs its cleanup. Closing it here is both a correctness fix and
        # what keeps Python from reporting "generator ignored GeneratorExit".
        generator.close()


async def _drive_async_generator(
    generator: Any,
    guard: Guard,
    chain: DisposerChain,
) -> None:
    """Algorithm 1, lines 4-7 over an asynchronous effect iterator."""
    try:
        while guard():
            try:
                value = await generator.__anext__()
            except StopAsyncIteration:
                return
            _collect(value, chain)
    finally:
        await generator.aclose()


async def _drive_awaitable(
    awaitable: Awaitable[Any],
    guard: Guard,
    chain: DisposerChain,
) -> None:
    """The degenerate one-step iterator: an ``async def`` effect callback."""
    if not guard():
        if inspect.iscoroutine(awaitable):
            awaitable.close()
        return
    _collect(await awaitable, chain)


class Pending:
    """An asynchronous effect that still has to be driven to completion."""

    __slots__ = ("driver", "source")

    def __init__(self, driver: Coroutine[Any, Any, None], source: Any) -> None:
        self.driver = driver
        self.source = source

    def abandon(self) -> None:
        """Discard the effect unstarted, closing what it was going to drive.

        Used when there is no event loop to drive it on. Closing both halves is
        what keeps Python from reporting "coroutine was never awaited" for an
        effect the caller was told, by an exception, was never begun.
        """
        self.driver.close()
        if inspect.iscoroutine(self.source):
            self.source.close()


def start(
    callback: EffectCallback,
    guard: Guard,
    chain: DisposerChain,
) -> Pending | None:
    """Begin an effect, accumulating its inverses into ``chain``.

    Returns ``None`` when the effect was wholly synchronous and has already run
    to completion, otherwise the work that still has to be driven. This is the
    synchronous fast path: ``ctx.effect(lambda: cleanup)`` works with no event
    loop running at all, and only a genuinely asynchronous effect needs one.

    ``chain`` is supplied by the caller rather than returned, because a callback
    that raises half way through must still leave its caller holding the
    inverses accumulated before the failure.
    """
    result = callback()
    if inspect.isasyncgen(result):
        return Pending(_drive_async_generator(result, guard, chain), result)
    if inspect.isawaitable(result):
        return Pending(_drive_awaitable(result, guard, chain), result)
    if inspect.isgenerator(result):
        _drive_sync_generator(result, guard, chain)
        return None
    _collect(result, chain)
    return None


async def execute(
    callback: EffectCallback,
    guard: Guard,
    chain: DisposerChain | None = None,
) -> DisposerChain:
    """The engine of Algorithm 1: drive ``callback`` and fold its inverses.

    Iteration stops as soon as ``guard`` reports ``False`` at a step boundary;
    only the inverses accumulated up to that point remain, which is the
    step-boundary interruption the paper's partial rollback rests on.
    """
    chain = DisposerChain() if chain is None else chain
    pending = start(callback, guard, chain)
    if pending is not None:
        await pending.driver
    return chain


# asyncio keeps only weak references to tasks, so a fire-and-forget task can be
# collected mid-flight. Every task this runtime creates is held here until it
# settles, and its exception is consumed by the done callback so a failure
# reaches the logger instead of surfacing as "Task exception was never
# retrieved" at collection time.
_TASKS: set[asyncio.Task[None]] = set()


def _consume(task: asyncio.Task[None]) -> None:
    _TASKS.discard(task)
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.error("unhandled error in %s", task.get_name(), exc_info=error)


def spawn(coroutine: Coroutine[Any, Any, None], *, name: str) -> asyncio.Task[None]:
    """Create an owned task. Requires a running event loop."""
    task = asyncio.get_running_loop().create_task(coroutine, name=name)
    _TASKS.add(task)
    task.add_done_callback(_consume)
    return task
