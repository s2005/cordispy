"""The context -- paper sections 5.1.2 and 5.1.4.

The context is the first-class handle a component is given. It carries three
tables:

* ``_store``    -- realm -> binding. Shared, root-owned, inherited by every
  derived context. The reference implementation keys this with framework-internal
  symbols (``@@store``); Python has no symbols, so the slots are private
  attributes with the same names.
* ``_isolate``  -- key -> realm. Copied on derivation. A key with no entry
  resolves to its own default realm.
* ``_intercept``-- key -> metadata. Copied on derivation. Consulted at read time
  only: it adjusts how a binding is used, not what the key resolves to.

There are three ways to read a coeffect, and the differences matter:

``ctx.get(key)``
    A lookup against the *store*. Returns the bound value or ``None``, and never
    fails. It answers with whatever is bound right now, including a binding
    whose provider has already begun unloading.

``ctx.key``
    Algorithm 6. Resolves against the accessing fiber's own *committed view*,
    walking up the fiber chain, and enforces the coeffect specification at the
    point of use. Reading the view rather than the store is what keeps a
    dependency readable to a component whose teardown that dependency triggered.

``ctx.optional(key)``
    The same resolution as ``ctx.key``, and the same rejection of a key the
    fiber never declared, but ``None`` instead of an error where the key is
    declared and has no committed binding. This is what an *optional* inject
    needs: it never gates activation, so an ACTIVE fiber may hold one that
    nothing provides.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from .component import Inject, to_component
from .effect import AsyncDisposer, Disposer, DisposerChain, EffectCallback, spawn, start
from .errors import InactiveAccessError, UndeclaredAccessError
from .fiber import Fiber, FiberState
from .realm import Binding, Realm, Store
from .registry import Registry

__all__ = ["Context"]


class Context:
    """A context. Constructing one directly creates a fresh root."""

    __slots__ = ("_fiber", "_intercept", "_isolate", "_registry", "_root", "_store")

    def __init__(self) -> None:
        object.__setattr__(self, "_store", {})
        object.__setattr__(self, "_isolate", {})
        object.__setattr__(self, "_intercept", {})
        object.__setattr__(self, "_root", self)
        registry = Registry()
        object.__setattr__(self, "_registry", registry)
        fiber = Fiber(
            uid=registry.next_uid(),
            parent=None,
            inject=Inject(),
            provide=(),
            label="root",
            registry=registry,
        )
        fiber.bind(self, _nothing)
        fiber.state = FiberState.ACTIVE
        fiber.target = ()
        fiber.committed = {}
        object.__setattr__(self, "_fiber", fiber)
        registry.attach(fiber)

    # ------------------------------------------------------------- attributes

    def __repr__(self) -> str:
        return f"<Context of {self._fiber.label}>"

    def __getattr__(self, name: str) -> Any:
        """Algorithm 6: resolve ``ctx.key`` against the fiber chain.

        Python calls this only for names that ordinary lookup did not find, so
        it never shadows a real attribute or method. Names starting with an
        underscore are rejected outright, which keeps the dunder probes the
        interpreter performs (``__deepcopy__``, ``__getstate__``, ...) from
        being mistaken for coeffect reads.
        """
        if name.startswith("_"):
            raise AttributeError(name)
        fiber: Fiber | None = self._fiber
        while fiber is not None:
            committed = fiber.committed
            if committed is not None and name in committed:
                return committed[name].value
            if name in fiber.inject:
                raise InactiveAccessError(_inactive_message(fiber, name))
            parent = fiber.parent
            if parent is None:
                break
            fiber = parent.fiber
        raise UndeclaredAccessError(f"cannot read coeffect {name!r} without declaring it in inject")

    def __setattr__(self, name: str, value: Any) -> None:
        """Route internal slots, and refuse every other assignment.

        Unlike the JavaScript ``set`` trap, ``__setattr__`` fires on *every*
        assignment, so the internal slots need an explicit route before any
        provide discipline is applied. Everything else is refused: a context is
        mutated through ``ctx.set``, which is a tracked effect and therefore
        recoverable, and a bare assignment would not be.
        """
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        raise UndeclaredAccessError(
            f"cannot assign {name!r} on a context directly; use ctx.set({name!r}, value) "
            "so the binding is tracked and recoverable"
        )

    # ---------------------------------------------------------------- accessors

    def optional(self, key: str) -> Any:
        """Algorithm 6 for a key that is allowed to have no provider.

        This resolves exactly as ``ctx.key`` does -- against the accessing
        fiber's own committed view, walking up the fiber chain -- and differs in
        one point: where ``ctx.key`` raises :class:`InactiveAccessError` because
        a declaring fiber has no committed binding for the key, this returns
        ``None``.

        That case is not an error for an optional inject, which is an extension
        this port adds over the paper's flat coeffect specification. An optional
        key never gates activation, so a perfectly ACTIVE fiber can declare one
        that nothing provides; property access has no way to report "there is
        no such binding" other than by raising.

        It is still a *checked* accessor, not a `getattr` escape hatch: a key
        the fiber never declared raises :class:`UndeclaredAccessError` exactly
        as property access does. And it is still the committed view rather than
        ``ctx.get(key)``: the store can answer with a binding from a provider
        that is already UNLOADING, or from a different provider than the one
        this fiber activated against, neither of which this fiber ever agreed
        to read.
        """
        fiber: Fiber | None = self._fiber
        while fiber is not None:
            committed = fiber.committed
            if committed is not None and key in committed:
                return committed[key].value
            if key in fiber.inject:
                return None
            parent = fiber.parent
            if parent is None:
                break
            fiber = parent.fiber
        raise UndeclaredAccessError(f"cannot read coeffect {key!r} without declaring it in inject")

    @property
    def fiber(self) -> Fiber:
        """The fiber this context belongs to."""
        return self._fiber

    @property
    def registry(self) -> Registry:
        """The runtime-wide registry of fibers."""
        return self._registry

    @property
    def root(self) -> Context:
        """The root context this one was derived from."""
        return self._root

    def bindings(self) -> Mapping[Realm, Binding]:
        """A read-only view of the shared store. For inspection and tests."""
        return MappingProxyType(self._store)

    # --------------------------------------------------------------- derivation

    def _derive(
        self,
        *,
        fiber: Fiber | None = None,
        isolate: dict[str, Realm] | None = None,
        intercept: dict[str, dict[str, Any]] | None = None,
    ) -> Context:
        """Create a child context: shared store, copied realm/metadata tables."""
        child = Context.__new__(Context)
        object.__setattr__(child, "_store", self._store)
        object.__setattr__(child, "_registry", self._registry)
        object.__setattr__(child, "_root", self._root)
        object.__setattr__(child, "_fiber", self._fiber if fiber is None else fiber)
        object.__setattr__(child, "_isolate", dict(self._isolate) if isolate is None else isolate)
        object.__setattr__(
            child,
            "_intercept",
            {key: dict(value) for key, value in self._intercept.items()} if intercept is None else intercept,
        )
        return child

    # ------------------------------------------------- Algorithm 2: resolution

    def realm_of(self, key: str) -> Realm:
        """``sigma(key)``: the realm this context resolves a key to."""
        realm = self._isolate.get(key)
        return Realm.default(key) if realm is None else realm

    def get(self, key: str) -> Any:
        """Read the bound value, or ``None``. Never raises.

        This is the reflective read of Algorithm 2: a lookup against the store,
        with no reference to who is asking. Contrast ``ctx.key``, which resolves
        against the accessing fiber's committed view and enforces ``inject``.
        """
        binding = self._store.get(self.realm_of(key))
        return None if binding is None else binding.value

    def binding(self, key: str) -> Binding | None:
        """The raw binding for a key, whatever state its provider is in."""
        return self._store.get(self.realm_of(key))

    def active_binding(self, key: str) -> Binding | None:
        """The binding for a key, but only while its provider is ACTIVE.

        This is the ``provided by`` relation of Definition 46. A provider that
        has entered UNLOADING has stopped providing, so its dependents recompute
        an unsatisfied target while its bindings are all still in place.
        """
        binding = self._store.get(self.realm_of(key))
        if binding is None:
            return None
        if binding.provider.state is not FiberState.ACTIVE or binding.provider.uid is None:
            return None
        return binding

    def provided_by(self, fiber: Fiber) -> list[str]:
        """The keys whose binding ``fiber`` installed."""
        return [binding.key for binding in self._store.values() if binding.provider is fiber]

    def installed_by(self, fiber: Fiber) -> list[tuple[str, Realm]]:
        """The ``(key, realm)`` pairs whose binding ``fiber`` installed.

        This is what a fiber-level notification carries. The realm is read off
        the store, so it is the realm the binding genuinely sits in -- which is
        *not* in general the realm any particular context resolves the key to.
        A provider that publishes through a derived context,
        ``ctx.isolate(k, realm).set(k, value)``, installs into ``realm`` while
        its own context still resolves ``k`` to the default one; notifying
        against the latter would filter out every dependent that can actually
        see the binding. Paper section 5.2.1: "A dependent sees the binding
        while its own realm at k is the realm the binding sits in."

        A fiber may hold two bindings for the same key in different realms, so
        this is a list of pairs rather than a mapping.
        """
        return [(binding.key, realm) for realm, binding in self._store.items() if binding.provider is fiber]

    def set(self, key: str, value: Any) -> AsyncDisposer:
        """Bind a value into the store -- Algorithm 2, lines 4-12.

        Provision is an ordinary ``ctx.effect`` call and inherits its tracking
        and recovery: installing notifies dependents, and the inverse removes
        the binding and notifies again.

        Re-binding a key the same fiber already provides withdraws the old
        binding first and installs the new one afresh. The reference
        implementation mutates the impl in place and never notifies
        (``reflect.ts:162``), so consumers observe a new value mid-transition
        with no reload at all. Note the paper's own consequence of identifying a
        binding by its provider: a component that wants its replacement to
        propagate must withdraw and re-install, which is exactly what this does.

        Providing a key does *not* make it readable as ``ctx.key`` on the
        provider itself: a committed view is ``resolve(inject)`` and nothing
        else, so property access keeps enforcing the coeffect specification.
        A provider reads its own binding through ``ctx.get(key)``, or simply
        keeps the value it just published.
        """
        provider = self._fiber
        realm = self.realm_of(key)
        store: Store = self._store

        def callback() -> Disposer:
            existing = store.get(realm)
            if existing is not None:
                if existing.provider is not provider:
                    raise UndeclaredAccessError(
                        f"coeffect {key!r} is already provided by <{existing.provider.label}>"
                    )
                del store[realm]
                self._registry.notify(self, [key])

            # Bind the binding to a local before the inverse closes over it.
            # The reference implementation's disposer closes over the *current*
            # sequence number instead of the one captured at push time
            # (packages/utils/src/index.ts:19), so disposing an earlier entry
            # deletes the most recent one. Python closures capture the same way.
            binding = Binding(key=key, value=value, provider=provider)
            store[realm] = binding
            self._registry.notify(self, [key])

            def inverse() -> None:
                if store.get(realm) is binding:
                    del store[realm]
                self._registry.notify(self, [key])

            return inverse

        return self.effect(callback)

    # ------------------------------------- Algorithm 1: effects on this context

    def effect(self, callback: EffectCallback) -> AsyncDisposer:
        """Perform a revertible effect and return its inverse.

        This call is synchronous and returns an awaitable disposer. A wholly
        synchronous effect runs to completion before this returns and needs no
        event loop at all; an asynchronous one is driven by a task, which does
        require a running loop.

        The returned disposer halts any in-flight iteration at the next step
        boundary and fires the accumulated inverses at most once. It is also
        prepended to the owning fiber's accumulator, so a child effect's inverse
        is itself an effect on the parent and unloading the parent recovers it.
        """
        fiber = self._fiber
        fiber.assert_active()

        chain = DisposerChain()
        armed = True
        task: asyncio.Task[None] | None = None

        async def dispose() -> None:
            nonlocal armed
            if not armed:
                return
            armed = False
            if task is not None:
                # The failure, if any, has already reached the logger through
                # the task's done callback; recovery must still run.
                with contextlib.suppress(Exception):
                    await task
            await chain()

        # Algorithm 1, line 17. Registered before the callback runs, so that a
        # callback which raises half way through still leaves the inverses it
        # did accumulate owned by the fiber.
        fiber.dispose.prepend(dispose)

        pending = start(callback, lambda: armed, chain)
        if pending is not None:
            try:
                task = spawn(pending.driver, name=f"cordispy.effect<{fiber.label}>")
            except RuntimeError:
                # No running loop. The effect never began, so discard it rather
                # than leave an unawaited coroutine behind.
                pending.abandon()
                raise
        return dispose

    # ---------------------------------- Algorithm 4: component instantiation

    def use(self, component: Any, config: Any = None) -> Fiber:
        """Instantiate a component as a fiber on this context.

        The instantiation is registered as an ordinary effect of the parent
        fiber, so unloading a parent cascades to its children. The returned
        fiber is the handle: ``await fiber.wait()`` waits for it to settle, and
        ``await fiber.retire()`` is the inverse of this call.
        """
        spec = to_component(component)
        registry = self._registry
        fiber = Fiber(
            uid=registry.next_uid(),
            parent=self,
            inject=spec.inject,
            provide=spec.provide,
            label=spec.name,
            registry=registry,
        )
        fiber.bind(self._derive(fiber=fiber), lambda: spec.invoke(fiber.ctx, config))
        registry.attach(fiber)
        registry.warn_on_cycles(fiber)

        def callback() -> Disposer:
            fiber.refresh()
            return fiber.drop

        fiber.retire = self.effect(callback)
        return fiber

    def plugin(self, component: Any, config: Any = None) -> Fiber:
        """Alias of :meth:`use`, matching the reference implementation's name."""
        return self.use(component, config)

    # ------------------------------ derived realization: isolate and intercept

    def isolate(self, key: str, realm: Realm | None = None) -> Context:
        """Redirect a key to an independent realm in a derived context.

        Isolation is *derived realization*: the parent is untouched and the
        inverse is the identity, so recovery is simply discarding the child.
        """
        isolate = dict(self._isolate)
        isolate[key] = Realm.fresh(key) if realm is None else realm
        return self._derive(isolate=isolate)

    def intercept(self, key: str, metadata: Mapping[str, Any]) -> Context:
        """Attach read-time metadata for a key in a derived context.

        Following Definition 31 the new metadata is merged over whatever the
        context already carries for the key, with the new taking priority.
        """
        table = {name: dict(value) for name, value in self._intercept.items()}
        table[key] = {**table.get(key, {}), **metadata}
        return self._derive(intercept=table)

    def interception(self, key: str) -> dict[str, Any]:
        """The metadata this context carries for a key. Read-time only."""
        return dict(self._intercept.get(key, {}))


def _inactive_message(fiber: Fiber, name: str) -> str:
    """Why property access could not answer, and what to call instead.

    The two cases are genuinely different failures. A required key with no
    committed binding means the fiber is not loaded -- naming its state is the
    diagnostic. An optional key with no committed binding is the ordinary state
    of an optional dependency nothing provides, and the caller wants
    ``ctx.optional`` rather than a different fiber state.
    """
    if name in fiber.inject.optional:
        return (
            f"cannot read optional coeffect {name!r}: <{fiber.label}> declares it as optional and "
            f"has no committed binding for it (state {fiber.state.value}); "
            f"use ctx.optional({name!r}), which returns None instead of raising"
        )
    return f"cannot read coeffect {name!r}: <{fiber.label}> declares it but is {fiber.state.value}"


def _nothing(*_args: Any, **_kwargs: Any) -> None:
    """The root fiber's effect function: the root applies nothing."""
