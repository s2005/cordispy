"""The fiber: a component instantiated in a context -- paper section 5.1.3.

A fiber is the inertial state machine of Algorithm 5. Two ideas carry the whole
design:

*Inertia.* Once a transition begins it runs to completion before the system
responds to a change of target. A transition that finishes against a stale
target does not unwind the world; it simply chains into the opposite transition.

*The committed view.* ``reload`` commits the bindings it resolved *before*
running ``apply``, and ``unload`` discards them only *after* every inverse has
run. That is what keeps a dependency readable to a component whose own teardown
was triggered by that dependency going away (paper Theorem 63).
"""

from __future__ import annotations

import asyncio
import logging
from enum import Enum
from typing import TYPE_CHECKING, TypeAlias

from .component import Inject
from .effect import AsyncDisposer, DisposerChain, EffectCallback, execute, spawn
from .errors import CordisError, InactiveEffectError
from .realm import Binding, Realm

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .context import Context
    from .registry import Registry

__all__ = ["UNLOAD_DRAIN_PASSES", "Fiber", "FiberState", "Target"]

logger = logging.getLogger("cordispy.fiber")

#: How many times ``unload`` re-drains the accumulator before giving up. An
#: inverse may itself create an effect -- releasing a resource by acquiring a
#: temporary one, say -- and that effect's own inverse lands on a chain the
#: current pass has already emptied. Each pass therefore recovers what the
#: previous one added; a component whose recovery is still generating new
#: effects after this many passes is not converging, and looping further would
#: hang the unload instead of reporting the defect.
UNLOAD_DRAIN_PASSES = 8

#: A digest of the bindings a fiber depends on: a sorted tuple of
#: ``(key, provider uid)`` pairs, or ``None`` when a required key is unsatisfied.
#: ``None`` is this port's spelling of the paper's ``INACTIVE`` sentinel; the
#: reference implementation uses the magic string ``'__INACTIVE__'``.
Target: TypeAlias = "tuple[tuple[str, int], ...] | None"


class FiberState(Enum):
    """Where a fiber sits in its lifecycle."""

    PENDING = "PENDING"
    """Declared but never loaded: a required coeffect is unsatisfied."""

    LOADING = "LOADING"
    """A reload transition is in flight."""

    ACTIVE = "ACTIVE"
    """Loaded. Only an ACTIVE fiber's bindings count as provided."""

    UNLOADING = "UNLOADING"
    """An unload transition is in flight. The fiber has stopped providing."""

    INACTIVE = "INACTIVE"
    """Loaded once, now fully recovered."""

    FAILED = "FAILED"
    """``apply`` raised. The inverses accumulated before the failure have run."""

    DISPOSED = "DISPOSED"
    """Dropped from its runtime: ``uid`` has been cleared and cannot come back."""


async def _cannot_retire() -> None:
    raise CordisError("the root fiber has no registration to retire")


class Fiber:
    """The instantiation of a component in a context."""

    #: The child context the fiber runs in. Assigned by :meth:`bind`.
    ctx: Context

    #: The config-applied effect function. Assigned by :meth:`bind`.
    apply: EffectCallback

    def __init__(
        self,
        *,
        uid: int,
        parent: Context | None,
        inject: Inject,
        provide: tuple[str, ...],
        label: str,
        registry: Registry,
    ) -> None:
        #: Fresh, monotonic, never reused. ``None`` once the fiber is dropped.
        self.uid: int | None = uid
        #: The context the component was instantiated on. ``None`` for the root.
        self.parent = parent
        self.inject = inject
        self.provide = provide
        self.label = label
        self.registry = registry
        self.state = FiberState.PENDING
        self.target: Target = None
        #: The in-flight transition, or ``None`` when the fiber is quiescent.
        #: It holds an ``asyncio.Task``, never a bare coroutine: it is awaited
        #: from several places and awaiting a coroutine twice is an error.
        self.inertia: asyncio.Task[None] | None = None
        #: The committed view: the bindings this fiber reads while it is loaded.
        self.committed: dict[str, Binding] | None = None
        #: The accumulator. Every effect the fiber performs prepends its inverse.
        self.dispose = DisposerChain()
        self.error: BaseException | None = None
        #: The inverse of ``ctx.use``: awaiting it drops the fiber for good.
        self.retire: AsyncDisposer = _cannot_retire

    def __repr__(self) -> str:
        return f"<Fiber #{self.uid} {self.label} {self.state.value}>"

    # ---------------------------------------------------------------- helpers

    def bind(self, ctx: Context, apply: EffectCallback) -> None:
        """Attach the derived context and the config-applied effect function."""
        self.ctx = ctx
        self.apply = apply

    def assert_active(self) -> None:
        """Reject effects on a fiber that has been dropped."""
        if self.uid is None:
            raise InactiveEffectError(f"cannot create an effect on the disposed fiber <{self.label}>")

    def provided(self) -> list[str]:
        """The keys whose binding this fiber installed (the paper's ``provided``)."""
        return self.ctx.provided_by(self)

    def installed(self) -> list[tuple[str, Realm]]:
        """The ``(key, realm)`` pairs this fiber's bindings occupy.

        What :meth:`provided` reports, plus the realm each binding actually sits
        in. Notification needs the realm from the binding rather than from this
        fiber's context: a fiber that publishes through a derived context
        installs into that context's realm, and its own resolves the key
        elsewhere.
        """
        return self.ctx.installed_by(self)

    def compute_target(self) -> Target:
        """Recompute ``target(sigma, ctx)`` -- paper Definition 46.

        A binding counts only while the fiber that installed it is ACTIVE, which
        is what makes a withdrawal visible to dependents one step *before* it
        happens: a provider that has entered UNLOADING has stopped providing.
        """
        entries: list[tuple[str, int]] = []
        for key in self.inject.required:
            uid = self._provider_uid(key)
            if uid is None:
                return None
            entries.append((key, uid))
        for key in self.inject.optional:
            uid = self._provider_uid(key)
            if uid is not None:
                entries.append((key, uid))
        return tuple(sorted(entries))

    def _provider_uid(self, key: str) -> int | None:
        binding = self.ctx.active_binding(key)
        if binding is None:
            return None
        return binding.provider.uid

    def _resolve(self) -> dict[str, Binding]:
        """``resolve(inject)``: the bindings the declared keys resolve to now."""
        view: dict[str, Binding] = {}
        for key in self.inject:
            binding = self.ctx.active_binding(key)
            if binding is not None:
                view[key] = binding
        return view

    def _settled_state(self) -> FiberState:
        if self.uid is None:
            return FiberState.DISPOSED
        if self.error is not None:
            return FiberState.FAILED
        return FiberState.INACTIVE

    def _abandon(self, error: BaseException) -> None:
        """Settle a fiber whose transition ended without finishing.

        ``wait`` follows :attr:`inertia` until it is ``None``, and a transition
        tail is what normally clears it. An exception that escapes the tail --
        or the transition body without being handled there -- skips that, so the
        field keeps pointing at an *already finished* task. Awaiting a finished
        task does not block, which turns ``wait`` and :meth:`Registry.settle`
        from a wait into a hot spin that never terminates.

        ``CancelledError``, ``KeyboardInterrupt`` and ``SystemExit`` are not
        this runtime's to absorb, and the caller re-raises them. Leaving the
        fiber wedged mid-transition *is* this runtime's to prevent.
        """
        self.error = error
        self.target = None
        self.state = self._settled_state()
        self.inertia = None

    async def wait(self) -> Fiber:
        """Await quiescence: no transition in flight.

        The loop is not a busy-wait. Each transition tail *replaces*
        ``self.inertia`` with the task for the transition it chains into, so
        re-reading the field is how a caller follows the chain to its end.

        This never raises on the fiber's own account: a fiber that failed
        reports it through :attr:`state` and :attr:`error`, and the drain in
        ``unload`` must not be derailed by a dependent's failure. Cancellation
        of the *caller* still propagates, which is the one thing that must never
        be swallowed -- it is told apart from cancellation of the transition by
        asking the task itself.

        The field is cleared when a finished task left it set. A transition
        cancelled before its first statement ran never reaches its own tail, so
        nothing else would ever clear it, and the loop below would spin.
        """
        while (inertia := self.inertia) is not None:
            try:
                await inertia
            except asyncio.CancelledError:
                if not inertia.cancelled():
                    raise
            except Exception as error:
                # Already reported on the fiber, or through the task's own done
                # callback. A dependent's failure must not derail this wait.
                logger.debug(
                    "fiber <%s> wait observed its already reported transition failure",
                    self.label,
                    exc_info=error,
                )
            if self.inertia is inertia:
                self.inertia = None
        return self

    # ------------------------------------------------------- Algorithm 5: refresh

    def refresh(self) -> None:
        """Recompute the target and start whatever transition it demands.

        Returning early on an unchanged target is what makes a *neutral*
        notification harmless -- the idempotence the reactive classification of
        Definition 26 relies on.
        """
        target = self.compute_target()
        if target == self.target:
            return
        self.target = target
        if self.inertia is not None:
            # Inertia: a transition in flight runs to completion. Its tail will
            # observe the new target and chain into the opposite transition.
            return
        if target is not None:
            self._start_reload()
        else:
            self._start_unload()

    # -------------------------------------------------------- Algorithm 5: reload

    def _start_reload(self) -> None:
        """The synchronous prelude of ``reload``, then the task.

        Everything here happens *before* the task exists. JavaScript starts a
        promise body eagerly, but ``asyncio.create_task`` does not run a single
        statement until the loop yields, so a refresh interleaved between the
        two would otherwise commit the wrong snapshot.
        """
        target0 = self.target
        # The reference implementation clears the error only in `update()`
        # (fiber.ts:482), so a fiber that fails once reports FAILED forever even
        # after a successful reload. Clear it at the start of every reload.
        self.error = None
        self.committed = self._resolve()  # Algorithm 5, line 14: commit the view
        self.state = FiberState.LOADING
        self.inertia = spawn(self._reload(target0), name=f"cordis.reload<{self.label}>")

    async def _reload(self, target0: Target) -> None:
        try:
            chain = DisposerChain()
            try:
                # The same effect engine as `ctx.effect`, with a guard that tests
                # target stability instead of an armed flag. That is what gives
                # partial rollback inside a single transition.
                await execute(self.apply, lambda: self.target == target0, chain)
            except Exception as error:
                logger.error("component <%s> failed to load", self.label, exc_info=error)
                self.error = error
                self.target = None
            finally:
                # Algorithm 5, line 16. In `finally` so the inverses accumulated
                # before a failure are still owned by the fiber and still run.
                self.dispose.prepend(chain)

            if self.target == target0:
                self.state = FiberState.ACTIVE
                self.registry.notify(self.ctx, self.installed())  # line 19
                self.inertia = None
            else:
                self._start_unload()  # lines 22-23: inertial chaining
        except BaseException as error:
            # Only what the handler above did not absorb reaches here: a
            # `BaseException` out of `apply`, or a failure in the tail itself.
            # Settle the fiber before letting it go, or `wait` spins on a
            # finished task for good.
            self._abandon(error)
            raise

    # -------------------------------------------------------- Algorithm 5: unload

    def _start_unload(self) -> None:
        """The synchronous prelude of ``unload``, then the task.

        Marking the fiber UNLOADING before the task is created (Algorithm 5,
        line 10) is the L-Leave step: the fiber stops providing, and dependents
        recompute against that withdrawal before any inverse is scheduled. The
        notification of line 25 is hoisted here for the same reason -- it is
        synchronous in the paper, and delaying it to the task body would let a
        dependent observe a provider that is already tearing down.

        The bindings are read with their realms (:meth:`installed`) because a
        dependent this misses is not merely notified late: it is never drained
        ahead of this fiber's own inverses, which is the withdrawal ordering of
        Theorem 63.
        """
        self.state = FiberState.UNLOADING
        affected = self.registry.notify(self.ctx, self.installed())
        self.inertia = spawn(self._unload(affected), name=f"cordis.unload<{self.label}>")

    async def _unload(self, affected: list[Fiber]) -> None:
        try:
            if affected:
                # Algorithm 5, line 25: drain the dependents. The wait sits ahead
                # of the whole recovery rather than inside one of the inverses,
                # because a wait placed within one inverse would leave the rest
                # unordered.
                results = await asyncio.gather(*(fiber.wait() for fiber in affected), return_exceptions=True)
                for result in results:
                    if isinstance(result, BaseException):
                        logger.error("error draining a dependent of <%s>", self.label, exc_info=result)

            # Lines 26-27: LIFO recovery, then reset to id.
            # `DisposerChain.__call__` empties itself before running anything, so
            # an inverse that performs an effect of its own prepends onto a chain
            # this pass has already drained. One call would leave that inverse
            # queued on a fiber that has finished unloading, to fire during the
            # *next* unload instead. Drain until the chain is genuinely empty.
            for _ in range(UNLOAD_DRAIN_PASSES):
                if not self.dispose:
                    break
                try:
                    await self.dispose()
                except Exception as error:
                    logger.error("component <%s> failed to unload cleanly", self.label, exc_info=error)
            if self.dispose:
                logger.warning(
                    "component <%s> still holds %d inverse(s) after %d recovery passes: "
                    "its recovery keeps creating new effects and is not converging",
                    self.label,
                    len(self.dispose),
                    UNLOAD_DRAIN_PASSES,
                )

            self.committed = None  # line 28: only now is the view discarded

            if self.target is None:
                self.state = self._settled_state()
                self.inertia = None
            else:
                self._start_reload()  # lines 33-34: inertial chaining
        except BaseException as error:
            # As in `_reload`: an inverse that raises a `BaseException`, or a
            # cancellation, must not leave the fiber pointing at a finished task.
            # The committed view is deliberately left in place -- line 28 says it
            # is discarded only once every inverse has run, and here they have not.
            self._abandon(error)
            raise

    # -------------------------------------------- Algorithm 4: the O-Retire step

    async def drop(self) -> None:
        """Force the target undefined, unload, and drop the fiber for good.

        This is the inverse the registration callback of Algorithm 4 returns.
        Clearing ``uid`` first takes the fiber out of the runtime, so any
        dependent recomputing its target during the teardown already sees this
        fiber as no provider at all.
        """
        if self.uid is None:
            await self.wait()
            return
        self.uid = None
        self.registry.detach(self)
        self.target = None
        if self.inertia is None and self.state in (FiberState.ACTIVE, FiberState.FAILED):
            self._start_unload()
        while self.inertia is not None:
            await self.inertia
        self.state = FiberState.DISPOSED
