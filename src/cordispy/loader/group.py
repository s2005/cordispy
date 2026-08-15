"""Groups: an entry whose configuration is a list of child entries.

``group`` is an ordinary component. It rests on the registration primitive of
Algorithm 4 like any other, so a nested configuration tree stays inside the
calculus and every result the paper proves of a flat composition holds of it.

What makes it the hinge of the loader is how it applies a configuration change:
its payload *is* the child list, so it applies the update as a keyed diff over
child ids -- creating, removing or updating each child. Updating a surviving
child re-enters the per-field dispatch of :meth:`Entry.reconcile`, so group
reconciliation and entry update recurse together down the tree.

Two properties of the diff matter and both come from keying on ``id``:

* a child that survives keeps its fiber, so revising one child does not restart
  its siblings and does not restart the group;
* a child moved to another group keeps its identity -- the same entry object,
  the same id, and the same private realms -- because a tree's entries are held
  in one store keyed by id, not by their position in any group.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from ..component import Component
from ..effect import Disposer
from .entry import Entry, EntryOptions, LoaderError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..context import Context
    from .loader import Loader, Tree

__all__ = ["EntryGroup", "group_component"]

logger = logging.getLogger("cordispy.loader")


class EntryGroup:
    """An ordered list of child entries realized on one context."""

    def __init__(self, ctx: Context, tree: Tree, owner: Entry | None = None) -> None:
        self.ctx = ctx
        self.tree = tree
        #: The entry whose component created this group; ``None`` for a tree root.
        self.owner = owner
        #: The local ids of this group's children, in configuration order.
        self.children: list[str] = []

    def __repr__(self) -> str:
        owner = "root" if self.owner is None else self.owner.id
        return f"<EntryGroup of {owner} with {len(self.children)} children>"

    @property
    def loader(self) -> Loader:
        return self.tree.loader

    def entries(self) -> list[Entry]:
        """This group's children, in configuration order."""
        found: list[Entry] = []
        for local_id in self.children:
            entry = self.tree.store.get(local_id)
            if entry is not None:
                found.append(entry)
        return found

    # ------------------------------------------------------------ the keyed diff

    async def reconcile(self, payload: Any) -> None:
        """Apply a child list as a keyed diff over child ids."""
        records = _as_records(payload)
        options = [EntryOptions.parse(record) for record in records]
        _reject_duplicates(options)

        previous = list(self.children)
        self.children = [item.id for item in options]

        for item in options:
            entry = self.tree.store.get(item.id)
            if entry is None:
                entry = Entry(self.loader, self.tree, item, self)
                self.tree.store[item.id] = entry
            await entry.reconcile(item, parent=self)

        for local_id in previous:
            if local_id in self.children:
                continue
            if self.loader.is_claimed(self.tree, local_id):
                # The entry appears elsewhere in the document being applied, so
                # it is moving between groups rather than leaving. The group it
                # moves into re-parents it and keeps its identity.
                continue
            await self.remove(local_id)

    async def remove(self, local_id: str) -> None:
        """Drop one child from the tree and retire its fiber."""
        entry = self.tree.store.pop(local_id, None)
        if entry is None:
            return
        await entry.retire()
        self.loader.realms.collect(self.loader.entries())

    async def stop(self) -> None:
        """Retire every child. The inverse of realizing the group."""
        for local_id in list(self.children):
            await self.remove(local_id)
        self.children = []


def _as_records(payload: Any) -> Sequence[Any]:
    if payload is None:
        return ()
    if isinstance(payload, Mapping | str | bytes):
        raise LoaderError(f"a group's config must be a list of entries, got {type(payload).__name__}")
    if not isinstance(payload, Sequence):
        raise LoaderError(f"a group's config must be a list of entries, got {type(payload).__name__}")
    return payload


def _reject_duplicates(options: Sequence[EntryOptions]) -> None:
    seen: set[str] = set()
    for item in options:
        if item.id in seen:
            raise LoaderError(f"duplicate entry id {item.id!r} in one group: ids are the reconciliation key")
        seen.add(item.id)


# ---------------------------------------------------------------------------
# the component
# ---------------------------------------------------------------------------


def group_component(entry: Entry) -> Component:
    """Build the ``group`` component for one entry.

    The component is built per entry rather than shared, because a group has to
    know which entry owns it in order to register itself as that entry's config
    updater. The effect function uses the iterator form so that the inverse is
    accumulated *before* any child is created: if realizing a child raises, the
    children created up to that point are still recovered.
    """

    async def apply(ctx: Context, config: Any) -> AsyncIterator[Disposer]:
        group = EntryGroup(ctx, entry.tree, owner=entry)
        entry.subgroup = group
        entry.updater = group.reconcile
        yield group.stop
        await group.reconcile(config)

    return Component(apply=apply, name=f"group<{entry.options.id}>", takes_config=True)
