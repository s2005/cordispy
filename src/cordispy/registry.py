"""The registry: every live fiber, and reactive notification.

Reactive notification is paper Algorithm 3. The reference implementation sweeps
every fiber of every runtime once per changed key (``reflect.ts:205``, called
once per name at ``fiber.ts:364``). This port keeps a reverse index from
coeffect key to the fibers that declare it and accepts a whole batch of keys in
one call, which turns a per-key full sweep into a lookup.

Each item of that batch may name the realm its binding occupies. The realm is a
property of the *binding*, not of whoever happens to be doing the notifying, and
the two part company as soon as a fiber publishes through a derived context.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable, Iterator
from typing import TYPE_CHECKING, TypeAlias

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .context import Context
    from .fiber import Fiber
    from .realm import Realm

__all__ = ["NotifyKeys", "NotifyPredicate", "Registry", "notify"]

logger = logging.getLogger("cordispy.registry")

#: Overrides the realm test in :meth:`Registry.notify`. Used by isolation
#: reassignment, where the interesting fibers are exactly the ones whose realm
#: for the key is *about to* change.
NotifyPredicate: TypeAlias = Callable[["Context", str], bool]

#: What a notification carries. A bare key is resolved against the notifying
#: context, which is right for :meth:`Context.set` -- the context that installed
#: the binding is the context being notified from. A ``(key, realm)`` pair names
#: the realm the changed binding actually *sits in*, which is the only correct
#: answer when the notifier is not the context that installed it: a fiber
#: publishing through a derived context (``ctx.isolate(k, r).set(k, v)``) is
#: notified from its own context, which resolves ``k`` somewhere else entirely.
NotifyKeys: TypeAlias = "Iterable[str | tuple[str, Realm]]"


class Registry:
    """Owns fiber identity, the fiber set, and the key -> dependents index."""

    def __init__(self) -> None:
        self._counter = 0
        self._fibers: dict[int, Fiber] = {}
        self._by_key: dict[str, set[Fiber]] = {}

    def __len__(self) -> int:
        return len(self._fibers)

    def __iter__(self) -> Iterator[Fiber]:
        return iter(tuple(self._fibers.values()))

    def next_uid(self) -> int:
        """Draw a fresh identifier. Monotonic, and never reused."""
        uid = self._counter
        self._counter += 1
        return uid

    @property
    def fibers(self) -> tuple[Fiber, ...]:
        """Every fiber currently in the runtime, in creation order."""
        return tuple(self._fibers.values())

    def attach(self, fiber: Fiber) -> None:
        """Add a fiber to the runtime and index the keys it declares."""
        self._fibers[id(fiber)] = fiber
        for key in fiber.inject:
            self._by_key.setdefault(key, set()).add(fiber)

    def detach(self, fiber: Fiber) -> None:
        """Remove a fiber from the runtime and from the index."""
        for key in fiber.inject:
            dependents = self._by_key.get(key)
            if dependents is not None:
                dependents.discard(fiber)
                if not dependents:
                    del self._by_key[key]
        self._fibers.pop(id(fiber), None)

    # --------------------------------------------------------- Algorithm 3

    def notify(
        self,
        ctx: Context,
        keys: NotifyKeys,
        predicate: NotifyPredicate | None = None,
    ) -> list[Fiber]:
        """Propagate a binding change to the fibers that declare the keys.

        A fiber is re-evaluated when a changed key is in its ``inject`` *and*
        its own context resolves that key to the realm the changed binding sits
        in. That realm test is the runtime form of the guard's demand that the
        dependent see the key from *this* provider rather than merely declare
        it; the paper states it as "a dependent sees the binding while its own
        realm at k is the realm the binding sits in" (section 5.2.1).

        The realm therefore comes from the binding, not from a context: an item
        of ``keys`` may be a ``(key, realm)`` pair naming it outright. A bare
        key falls back to ``ctx.realm_of(key)``, which is exact whenever the
        notifying context is the one that installed the binding.

        Returns the fibers it re-evaluated so a caller can await them.
        """
        affected: list[Fiber] = []
        seen: set[Fiber] = set()
        for item in keys:
            key, realm = item if isinstance(item, tuple) else (item, ctx.realm_of(item))
            for fiber in tuple(self._by_key.get(key, ())):
                if fiber in seen:
                    continue
                if predicate is not None:
                    if not predicate(fiber.ctx, key):
                        continue
                elif fiber.ctx.realm_of(key) is not realm:
                    continue
                seen.add(fiber)
                fiber.refresh()
                affected.append(fiber)
        return affected

    async def settle(self) -> None:
        """Wait until no fiber anywhere has a transition in flight.

        One activation can start another -- a provider reaching ACTIVE notifies
        its dependents, whose reloads start after the provider's own task is
        already done -- so quiescence is a fixed point rather than a single
        await. This never raises; a fiber that failed reports it on itself.
        """
        while True:
            pending = [fiber for fiber in self._fibers.values() if fiber.inertia is not None]
            if not pending:
                return
            await asyncio.gather(*(fiber.wait() for fiber in pending), return_exceptions=True)

    # ------------------------------------------------------- cycle detection

    def find_cycles(self) -> list[tuple[str, ...]]:
        """Report dependency cycles among the declarations of live fibers.

        A cycle leaves every component involved permanently inactive: each waits
        for a provider that is itself waiting. Unlike a deadlock this is
        predictable from the declarations alone (paper section 6.5), so it is
        worth reporting up front rather than diagnosing from a hung system.

        A self-edge counts. A component that injects a key it also provides is a
        cycle of length one: it waits for a binding only its own activation
        could install, so it stays PENDING for good. That is exactly as
        predictable from the declarations as a two-component cycle, and it is
        the one shape a reader is most likely to write by accident, so it is
        reported rather than silently skipped.

        Realms are deliberately *not* consulted, here or for longer cycles. A
        declaration says which keys a component reads and publishes, never which
        realm either lands in -- and the realm a component publishes into may be
        chosen inside ``apply``, by deriving a context that does not exist yet
        when this runs. So a component that reads ``k`` from one realm and
        publishes ``k`` into another, which is a legitimate wrapper and activates
        perfectly well, is reported here too. It is a prediction from the
        declarations, which is all Definition 74 makes available;
        :meth:`warn_on_cycles` words a self-edge accordingly.

        Returns one tuple of fiber labels per cycle found.
        """
        providers: dict[str, list[Fiber]] = {}
        for fiber in self._fibers.values():
            for key in fiber.provide:
                providers.setdefault(key, []).append(fiber)

        edges: dict[Fiber, list[Fiber]] = {}
        for fiber in self._fibers.values():
            targets: list[Fiber] = []
            for key in fiber.inject:
                for provider in providers.get(key, ()):
                    if provider not in targets:
                        targets.append(provider)
            edges[fiber] = targets

        cycles: list[tuple[str, ...]] = []
        seen: set[frozenset[int]] = set()
        colour: dict[Fiber, int] = dict.fromkeys(edges, _WHITE)
        path: list[Fiber] = []
        positions: dict[Fiber, int] = {}

        for fiber in edges:
            if colour[fiber] != _WHITE:
                continue
            colour[fiber] = _GREY
            positions[fiber] = len(path)
            path.append(fiber)
            stack: list[tuple[Fiber, int]] = [(fiber, 0)]

            while stack:
                node, successor_index = stack[-1]
                successors = edges[node]
                if successor_index >= len(successors):
                    stack.pop()
                    path.pop()
                    positions.pop(node)
                    colour[node] = _BLACK
                    continue

                successor = successors[successor_index]
                stack[-1] = (node, successor_index + 1)
                if colour[successor] == _GREY:
                    loop = tuple(path[positions[successor] :])
                    signature = frozenset(id(member) for member in loop)
                    if signature not in seen:
                        seen.add(signature)
                        cycles.append(tuple(member.label for member in loop))
                elif colour[successor] == _WHITE:
                    colour[successor] = _GREY
                    positions[successor] = len(path)
                    path.append(successor)
                    stack.append((successor, 0))
        return cycles

    def warn_on_cycles(self, fiber: Fiber) -> None:
        """Log a warning if ``fiber`` takes part in a declaration cycle.

        A cycle of two or more components cannot resolve however the realms fall:
        each waits for another that is waiting, and no realm assignment breaks
        that. A cycle of *one* has an escape the declarations cannot show --
        publishing into a realm other than the one it reads -- so it is worded as
        the conditional it is, rather than asserting an inactivity that a working
        wrapper would contradict.
        """
        for cycle in self.find_cycles():
            if fiber.label not in cycle:
                continue
            arrow = " -> ".join((*cycle, cycle[0]))
            if len(cycle) == 1:
                logger.warning(
                    "dependency cycle detected: %s -- <%s> injects a key it also provides, "
                    "so it stays inactive unless it publishes into a realm other than the "
                    "one it reads",
                    arrow,
                    cycle[0],
                )
            else:
                logger.warning(
                    "dependency cycle detected: %s -- every component in it stays inactive",
                    arrow,
                )


_WHITE = 0
_GREY = 1
_BLACK = 2


def notify(
    ctx: Context,
    keys: NotifyKeys,
    predicate: NotifyPredicate | None = None,
) -> list[Fiber]:
    """Module-level spelling of :meth:`Registry.notify` (paper Algorithm 3)."""
    return ctx.registry.notify(ctx, keys, predicate)
