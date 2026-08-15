"""Entries: the declarative record of one fiber -- paper section 5.2.1.

Definition 74 says an entry records six things and nothing else, because that is
exactly what supports a fiber:

``id``
    A stable identifier. It is the reconciliation key when the entry's group
    revises its child list, which is why the loader never derives it from the
    position in a list.
``name``
    The component to instantiate. This is the paper's ``url``; in Python the
    import target ``package.module:attribute`` plays that role.
``isolate``
    An isolation annotation applied to the entry's context.
``intercept``
    An interception annotation applied to the entry's context.
``config``
    The configuration bound into the component to form its effect function.
``disabled``
    Whether the entry is administratively turned off.

The reconciliation ladder lives on :class:`Entry`. When an entry's record
changes the loader dispatches on *which* fields changed and applies the least
disruptive operation for each, rather than tearing the fiber down and rebuilding
it wholesale. Corollary 62 is what makes that sound: a departing fiber
contributes nothing to the quiescent state, so rebuilding one entry withdraws
what its fiber installed and leaves the fibers around it exactly as they were.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..errors import CordisError
from ..realm import Realm

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..context import Context
    from ..fiber import Fiber
    from .group import EntryGroup
    from .loader import Loader, Tree

__all__ = [
    "ENTRY_FIELDS",
    "Entry",
    "EntryOptions",
    "LoaderError",
    "RealmManager",
]

logger = logging.getLogger("cordispy.loader")

#: The six fields of Definition 74, in the order the loader reports them.
ENTRY_FIELDS = ("id", "name", "config", "isolate", "intercept", "disabled")

#: The separator that namespaces an entry id by the tree that owns it.
TREE_SEPARATOR = ":"


class LoaderError(CordisError):
    """A declarative configuration could not be read or realized."""


# ---------------------------------------------------------------------------
# the record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EntryOptions:
    """One entry of a configuration, normalized and immutable.

    The record is frozen because reconciliation compares the incoming record
    against the one currently realized; a record that could be mutated in place
    would make that comparison meaningless.
    """

    id: str
    name: str
    config: Any = None
    isolate: Mapping[str, bool | str] = field(default_factory=dict)
    intercept: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    disabled: bool = False

    @classmethod
    def parse(cls, record: Any) -> EntryOptions:
        """Normalize one entry of a configuration document."""
        if isinstance(record, EntryOptions):
            return record
        if not isinstance(record, Mapping):
            raise LoaderError(f"an entry must be a mapping, got {type(record).__name__}")
        unknown = set(record) - set(ENTRY_FIELDS)
        if unknown:
            raise LoaderError(f"unknown entry fields: {', '.join(sorted(unknown))}")

        entry_id = record.get("id")
        if not isinstance(entry_id, str) or not entry_id:
            raise LoaderError("every entry needs a non-empty string 'id': it is the reconciliation key")
        if TREE_SEPARATOR in entry_id:
            raise LoaderError(
                f"entry id {entry_id!r} must not contain {TREE_SEPARATOR!r}: "
                "that separator namespaces an id by the tree that owns it"
            )

        name = record.get("name")
        if not isinstance(name, str) or not name:
            raise LoaderError(f"entry {entry_id!r} needs a non-empty string 'name' selecting its component")

        return cls(
            id=entry_id,
            name=name,
            config=record.get("config"),
            isolate=_parse_isolate(entry_id, record.get("isolate")),
            intercept=_parse_intercept(entry_id, record.get("intercept")),
            disabled=bool(record.get("disabled", False)),
        )

    def diff(self, other: EntryOptions) -> frozenset[str]:
        """The fields in which two records disagree."""
        return frozenset(name for name in ENTRY_FIELDS if getattr(self, name) != getattr(other, name))

    def to_record(self) -> dict[str, Any]:
        """The plain-data form, suitable for writing back to a document."""
        record: dict[str, Any] = {"id": self.id, "name": self.name}
        if self.config is not None:
            record["config"] = self.config
        if self.isolate:
            record["isolate"] = dict(self.isolate)
        if self.intercept:
            record["intercept"] = {key: dict(value) for key, value in self.intercept.items()}
        if self.disabled:
            record["disabled"] = True
        return record


def _parse_isolate(entry_id: str, value: Any) -> Mapping[str, bool | str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise LoaderError(f"entry {entry_id!r}: 'isolate' must map a coeffect key to true or a realm label")
    table: dict[str, bool | str] = {}
    for key, label in value.items():
        if not isinstance(key, str):
            raise LoaderError(f"entry {entry_id!r}: isolate keys must be strings, got {key!r}")
        if isinstance(label, bool):
            if label:
                table[key] = True
        elif isinstance(label, str) and label:
            table[key] = label
        else:
            raise LoaderError(
                f"entry {entry_id!r}: isolate[{key!r}] must be true (a realm private to this entry) "
                "or a non-empty string (a realm shared by every entry naming it)"
            )
    return table


def _parse_intercept(entry_id: str, value: Any) -> Mapping[str, Mapping[str, Any]]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise LoaderError(f"entry {entry_id!r}: 'intercept' must map a coeffect key to a metadata mapping")
    table: dict[str, Mapping[str, Any]] = {}
    for key, metadata in value.items():
        if not isinstance(key, str):
            raise LoaderError(f"entry {entry_id!r}: intercept keys must be strings, got {key!r}")
        if not isinstance(metadata, Mapping):
            raise LoaderError(f"entry {entry_id!r}: intercept[{key!r}] must be a mapping")
        table[key] = dict(metadata)
    return table


# ---------------------------------------------------------------------------
# managed realms
# ---------------------------------------------------------------------------


class RealmManager:
    """The realms the loader manages on behalf of entries.

    Core isolation derives a child context that overrides the realm table at one
    key, which is enough while the context tree stands still. An entry may be
    moved between groups at run time, so the loader owns its realms instead and
    the ``isolate`` field selects between two scoping rules per key:

    ``true``
        a realm private to the entry, tagged by its id. The entry carries it
        wherever it moves.
    a string label
        a realm shared by every entry naming that label, so moving such an entry
        changes which entries it shares a binding with, not which realm it is in.

    A realm is discarded once no entry names it.
    """

    __slots__ = ("_private", "_shared")

    def __init__(self) -> None:
        self._private: dict[tuple[str, str], Realm] = {}
        self._shared: dict[tuple[str, str], Realm] = {}

    def assign(self, entry_id: str, isolate: Mapping[str, bool | str]) -> dict[str, Realm]:
        """The realm each isolated key of one entry resolves to."""
        realms: dict[str, Realm] = {}
        for key, label in isolate.items():
            if label is True:
                realms[key] = self._reuse(self._private, (entry_id, key), f"{key}#{entry_id}")
            else:
                realms[key] = self._reuse(self._shared, (str(label), key), f"{key}@{label}")
        return realms

    @staticmethod
    def _reuse(table: dict[tuple[str, str], Realm], slot: tuple[str, str], label: str) -> Realm:
        realm = table.get(slot)
        if realm is None:
            realm = Realm(label)
            table[slot] = realm
        return realm

    def collect(self, entries: Iterable[Entry]) -> None:
        """Discard every managed realm that no live entry names any more."""
        private: set[tuple[str, str]] = set()
        shared: set[tuple[str, str]] = set()
        for entry in entries:
            for key, label in entry.options.isolate.items():
                if label is True:
                    private.add((entry.id, key))
                else:
                    shared.add((str(label), key))
        self._private = {slot: realm for slot, realm in self._private.items() if slot in private}
        self._shared = {slot: realm for slot, realm in self._shared.items() if slot in shared}

    def __len__(self) -> int:
        return len(self._private) + len(self._shared)


# ---------------------------------------------------------------------------
# the live entry
# ---------------------------------------------------------------------------


def _intercept_table(ctx: Context) -> dict[str, dict[str, Any]]:
    """The read-time interception table a context carries.

    Interception metadata is consulted at read time and adjusts how a binding is
    used, not what a key resolves to, so revising it needs no reload at all
    (paper section 5.2.1). Every other table in a context is copy-on-derive and
    must never be written through; this one is the single exception the
    reconciliation ladder depends on, so it is reached through one named helper
    rather than being poked at from three places.
    """
    table: dict[str, dict[str, Any]] = ctx._intercept
    return table


class Entry:
    """One entry of a configuration together with the fiber it manages.

    The binding runs in both directions: the loader responds to a change in an
    entry's fields by adjusting the fiber, and the entry keeps a handle on the
    fiber so callers can inspect what the declaration actually produced.
    """

    def __init__(self, loader: Loader, tree: Tree, options: EntryOptions, parent: EntryGroup) -> None:
        self.loader = loader
        self.tree = tree
        self.options = options
        self.parent = parent
        #: The fiber realizing this entry, or ``None`` while it is not loaded.
        self.fiber: Fiber | None = None
        #: Set by the ``group`` component: the child group this entry owns.
        self.subgroup: EntryGroup | None = None
        #: Set by the ``include`` component: the nested tree this entry grafts in.
        self.subtree: Tree | None = None
        #: Registered by a component that applies a config change itself.
        self.updater: Callable[[Any], Awaitable[None]] | None = None
        #: The last failure while realizing this entry, or ``None``.
        self.error: BaseException | None = None
        self.realms: dict[str, Realm] = {}
        self.ctx: Context = parent.ctx
        self._build_context()

    def __repr__(self) -> str:
        return f"<Entry {self.id} [{self.options.name}] {self.status}>"

    # ------------------------------------------------------------- identity

    @property
    def id(self) -> str:
        """The identifier, namespaced by the tree that owns this entry."""
        return self.tree.qualify(self.options.id)

    @property
    def status(self) -> str:
        """A one-word description of what the declaration currently produced."""
        if self.fiber is not None:
            return self.fiber.state.value
        if self.options.disabled:
            return "DISABLED"
        if self.error is not None:
            return "FAILED"
        return "UNLOADED"

    @property
    def uid(self) -> int | None:
        """The uid of the fiber this entry currently manages."""
        return None if self.fiber is None else self.fiber.uid

    # ------------------------------------------------ the reconciliation ladder

    async def reconcile(self, options: EntryOptions, *, parent: EntryGroup) -> None:
        """Apply a revised record with the least disruptive operation.

        The ladder is paper section 5.2.1, in order:

        ``id`` / ``name``
            rebuild, since the entry's identity or its component has changed;
        ``isolate``
            reassign the entry's realms;
        ``intercept``
            update in place -- read-time metadata needs no reload;
        ``config``
            hand to the component, which decides how to apply the new payload;
        ``disabled``
            unload when set, reload when cleared.
        """
        legacy = self.options
        moved = self.parent is not parent
        self.parent = parent
        self.options = options
        changed = legacy.diff(options)

        try:
            await self._dispatch(legacy, changed, moved=moved)
        except Exception as error:  # a bad entry must not derail the rest of the tree
            self.error = error
            logger.error("entry %s could not be realized", self.id, exc_info=error)
        else:
            self.error = None

    async def _dispatch(self, legacy: EntryOptions, changed: frozenset[str], *, moved: bool) -> None:
        if self.options.disabled:
            await self.unload()
            return

        # A moved entry keeps its identity but not its context lineage, and an
        # entry that is not loaded at all has nothing to reconcile against.
        if self.fiber is None or moved or "id" in changed or "name" in changed:
            await self.rebuild()
            return

        if not changed:
            return

        if "intercept" in changed:
            self._write_intercept(legacy)

        if "isolate" in changed:
            # Reassignment rebuilds the fiber against the new realms, which
            # carries any config change along with it.
            await self._reassign_realms()
            return

        if "config" in changed:
            await self._update_config()

    # --------------------------------------------------------------- actions

    async def rebuild(self) -> None:
        """Retire the fiber, rebuild the context, and instantiate afresh.

        This is the operation for a change of identity or of component, and it
        is what HMR performs on a stale entry. It propagates a failure to import
        the component, because the transactional reload has to see it.
        """
        await self._retire()
        self._build_context()
        self._instantiate()

    async def unload(self) -> None:
        """Retire the fiber but keep the entry, which is what ``disabled`` means."""
        await self._retire()

    async def retire(self) -> None:
        """Remove the entry from the running system for good."""
        await self._retire()

    async def _retire(self) -> None:
        """Drop the fiber and everything its component registered on the entry.

        The handles go first so that nothing observes a group or a subtree whose
        fiber is already on its way out; the component reinstalls them itself if
        the entry is instantiated again.
        """
        fiber, self.fiber = self.fiber, None
        self.subgroup = None
        self.subtree = None
        self.updater = None
        if fiber is not None:
            await fiber.retire()

    def _instantiate(self) -> None:
        component = self.loader.resolve_component(self.options.name, self)
        self.fiber = self.ctx.use(component, self.options.config)

    def _build_context(self) -> None:
        """Derive the entry's context from its group's, applying its annotations."""
        self.realms = self.loader.realms.assign(self.id, self.options.isolate)
        ctx = self.parent.ctx
        for key, realm in self.realms.items():
            ctx = ctx.isolate(key, realm)
        for key, metadata in self.options.intercept.items():
            ctx = ctx.intercept(key, metadata)
        self.ctx = ctx

    async def _update_config(self) -> None:
        """Hand the new payload to the component, or reload if it cannot take it.

        A component that knows how to absorb a new configuration registers an
        updater -- ``group`` does exactly that, and applies the payload as a
        keyed diff over its child ids. A component that does not is reloaded
        with the new configuration bound in, which is the least disruptive
        operation available: it withdraws only what this fiber installed and
        leaves every fiber around it untouched.
        """
        updater = self.updater
        if updater is not None:
            await updater(self.options.config)
            return
        await self._retire()
        self._instantiate()

    def _write_intercept(self, legacy: EntryOptions) -> None:
        """Revise interception metadata in place, with no reload.

        The tables written are those of the fibers at and below this entry's
        own, and no others. Two reasons, both of which would otherwise turn a
        free annotation change into a leak:

        * an entry that annotates nothing derives no context of its own, so its
          ``ctx`` *is* its group's -- writing there would hand the metadata to
          every sibling. A fiber's context is always freshly derived by
          ``ctx.use``, so it belongs to that fiber alone;
        * ``entry.ctx`` is rebuilt from the group's context on the next rebuild
          anyway, with the current annotation applied, so leaving it alone
          loses nothing.

        Only the keys this entry annotates -- now or a moment ago -- are
        touched, so metadata a component attached for itself survives, and a
        key the entry stops annotating falls back to what it inherits.
        """
        inherited = _intercept_table(self.parent.ctx)
        touched = set(legacy.intercept) | set(self.options.intercept)
        desired: dict[str, dict[str, Any]] = {}
        for key in touched:
            merged = dict(inherited.get(key, {}))
            merged.update(self.options.intercept.get(key, {}))
            if merged:
                desired[key] = merged

        for fiber in self.subtree_fibers():
            table = _intercept_table(fiber.ctx)
            for key in touched:
                if key in desired:
                    table[key] = dict(desired[key])
                else:
                    table.pop(key, None)

    async def _reassign_realms(self) -> None:
        """Move the entry between realms -- paper Algorithm 7.

        Contexts are copy-on-derive in their realm table, so a reassignment
        rebuilds the entry's context and its fiber rather than mutating either.
        What that rebuild does not by itself reach are the dependents *outside*
        the entry: whether such a dependent gains or loses the binding turns on
        whether it moved when the provider did. Algorithm 7 lines 15-18 state
        that as a predicate, which replaces the realm equality test of
        Algorithm 3 for this one notification.
        """
        changed = self._changed_keys()
        if not changed:
            self._build_context()
            self.loader.realms.collect(self.loader.entries())
            return

        before = {key: self.ctx.realm_of(key) for key in changed}
        inside = set(self.subtree_fibers())
        provided_by_us = {}
        for key in changed:
            binding = self.ctx.binding(key)
            provided_by_us[key] = binding is not None and binding.provider in inside

        await self.rebuild()

        after = {key: self.ctx.realm_of(key) for key in changed}
        subtree = set(self.subtree_fibers())

        def affected(fiber_ctx: Context, key: str) -> bool:
            realm = fiber_ctx.realm_of(key)
            if realm is not before[key] and realm is not after[key]:
                return False
            return (fiber_ctx.fiber in subtree) != provided_by_us[key]

        self.loader.ctx.registry.notify(self.ctx, changed, affected)
        self.loader.realms.collect(self.loader.entries())

    def _changed_keys(self) -> list[str]:
        """The isolated keys whose realm the pending record moves."""
        pending = self.loader.realms.assign(self.id, self.options.isolate)
        keys = sorted(set(self.realms) | set(pending))
        return [key for key in keys if self.realms.get(key) is not pending.get(key)]

    # ---------------------------------------------------------------- inspection

    def subtree_fibers(self) -> list[Fiber]:
        """Every live fiber at or below this entry's own fiber."""
        root = self.fiber
        if root is None:
            return []
        found: list[Fiber] = []
        for fiber in self.loader.ctx.registry:
            node: Fiber | None = fiber
            while node is not None:
                if node is root:
                    found.append(fiber)
                    break
                parent = node.parent
                node = None if parent is None else parent.fiber
        return found
