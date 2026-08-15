"""The declarative loader -- paper section 5.2.1.

The core library equips a component developer with imperative primitives:
``ctx.effect``, ``ctx.use``, ``ctx.set``. An orchestrator assembling pre-existing
components into a running system has a different problem, and the loader answers
it with a declarative layer: the orchestrator states the desired composition as
a persistent record, and the loader translates *changes* to that record into the
corresponding fiber operations.

Reconciliation is incremental, never a teardown and rebuild. Three results make
that sound:

* Theorem 73 makes the quiescent state a function of the final configuration
  alone, whatever instantiations and retirements the loader performs on the way;
* Theorem 66 proves the system does quiesce, so a reconciliation is complete
  once its operations have been issued;
* Corollary 62 puts a departing fiber's contribution to the state at nothing, so
  rebuilding one entry leaves the fibers around it as they were.

Theorem 63 supplies the last piece: entries can be instantiated together in any
order, because a fiber whose declared keys are not yet provided simply waits.
The orchestrator never arranges a load order.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeAlias

from ..component import Component
from ..effect import Disposer
from .entry import Entry, LoaderError, RealmManager
from .group import EntryGroup, group_component

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..context import Context

__all__ = [
    "BUILTIN_GROUP",
    "BUILTIN_INCLUDE",
    "Loader",
    "Resolver",
    "Tree",
    "import_target",
    "include_component",
    "normalize_document",
    "read_config",
]

logger = logging.getLogger("cordispy.loader")

#: The name selecting the built-in group component.
BUILTIN_GROUP = "cordispy:group"

#: The name selecting the built-in include component.
BUILTIN_INCLUDE = "cordispy:include"

#: An optional hook that maps an entry name to a component, consulted before the
#: name is treated as an import target. Returning ``None`` declines the name.
Resolver: TypeAlias = Callable[[str], Any]


# ---------------------------------------------------------------------------
# reading a configuration document
# ---------------------------------------------------------------------------


def read_config(path: str | os.PathLike[str]) -> Any:
    """Read a configuration document from a YAML or JSON file.

    JSON goes through the standard library, so a configuration in that form
    still loads when PyYAML is not installed.
    """
    location = Path(path)
    text = location.read_text(encoding="utf-8")
    suffix = location.suffix.lower()
    if suffix == ".json":
        return json.loads(text)
    if suffix in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError as error:  # pragma: no cover - depends on the environment
            raise LoaderError(
                f"cannot read {location}: PyYAML is not installed. "
                "Install it, or write the configuration as JSON."
            ) from error
        return yaml.safe_load(text)
    raise LoaderError(f"cannot read {location}: expected a .yaml, .yml or .json file")


def normalize_document(document: Any) -> list[Any]:
    """Reduce a configuration document to its list of top-level entries.

    A document may be the list itself, or a mapping carrying it under
    ``plugins`` or ``entries`` so that a file has room for a comment header.
    """
    if document is None:
        return []
    if isinstance(document, Mapping):
        for key in ("plugins", "entries"):
            if key in document:
                return list(_expect_sequence(document[key], key))
        raise LoaderError("a configuration mapping must carry its entries under 'plugins' or 'entries'")
    return list(_expect_sequence(document, "document"))


def _expect_sequence(value: Any, label: str) -> Sequence[Any]:
    if isinstance(value, Mapping | str | bytes):
        raise LoaderError(f"{label} must be a list of entries, got {type(value).__name__}")
    if not isinstance(value, Sequence):
        raise LoaderError(f"{label} must be a list of entries, got {type(value).__name__}")
    return value


# ---------------------------------------------------------------------------
# resolving a name to a component
# ---------------------------------------------------------------------------


def import_target(name: str) -> Any:
    """Import the component an entry names.

    The paper's entry field is a URL; the Python equivalent is an import target
    ``package.module:attribute``. Without the attribute the module's ``default``
    is used, falling back to the module itself so a module exposing ``apply`` can
    serve as a component directly.

    Import failures propagate untouched: the transactional reload of Algorithm
    10 has to see the original error, and a ``SyntaxError`` says far more than
    any wrapper would.
    """
    module_name, separator, attribute = name.partition(":")
    if not module_name:
        raise LoaderError(f"{name!r} does not name a module")
    module = importlib.import_module(module_name)
    if not separator or not attribute:
        return getattr(module, "default", module)
    try:
        return getattr(module, attribute)
    except AttributeError as error:
        raise LoaderError(f"module {module_name!r} has no attribute {attribute!r}") from error


# ---------------------------------------------------------------------------
# trees
# ---------------------------------------------------------------------------


class Tree:
    """One configuration file's worth of entries, and the group at its root.

    A tree owns a flat store of entries keyed by their *local* id, which is what
    lets a child move between the groups of one tree while keeping its identity.
    Ids are namespaced across trees instead: an entry grafted in by ``include``
    reports its id as ``<owner id>:<local id>``.
    """

    def __init__(self, loader: Loader, ctx: Context, *, owner: Entry | None = None, base: Path) -> None:
        self.loader = loader
        self.owner = owner
        self.base = base
        self.store: dict[str, Entry] = {}
        self.root = EntryGroup(ctx, self, owner)

    def __repr__(self) -> str:
        owner = "root" if self.owner is None else self.owner.id
        return f"<Tree of {owner} with {len(self.store)} entries>"

    def qualify(self, local_id: str) -> str:
        """Namespace a local id by the entry that owns this tree."""
        if self.owner is None:
            return local_id
        return f"{self.owner.id}:{local_id}"

    def entries(self) -> Iterator[Entry]:
        """Every entry of this tree and of every tree grafted into it."""
        for entry in list(self.store.values()):
            yield entry
            if entry.subtree is not None:
                yield from entry.subtree.entries()

    async def reconcile(self, document: Any) -> None:
        await self.root.reconcile(normalize_document(document))

    async def stop(self) -> None:
        await self.root.stop()


def include_component(entry: Entry) -> Component:
    """Build the ``include`` component: graft an external file in as a subtree."""

    async def apply(ctx: Context, config: Any) -> AsyncIterator[Disposer]:
        path = _include_path(entry, config)
        subtree = Tree(entry.loader, ctx, owner=entry, base=path.parent)
        entry.subtree = subtree
        yield subtree.stop
        await subtree.reconcile(read_config(path))

    return Component(apply=apply, name=f"include<{entry.options.id}>", takes_config=True)


def _include_path(entry: Entry, config: Any) -> Path:
    raw = config.get("path") if isinstance(config, Mapping) else config
    if not isinstance(raw, str) or not raw:
        raise LoaderError(f"entry {entry.id!r}: include needs a 'path' naming a configuration file")
    candidate = Path(raw)
    return candidate if candidate.is_absolute() else entry.tree.base / candidate


# ---------------------------------------------------------------------------
# the loader
# ---------------------------------------------------------------------------


class Loader:
    """Realizes a declarative configuration as fibers and keeps it in step."""

    def __init__(
        self,
        ctx: Context,
        *,
        base: str | os.PathLike[str] | None = None,
        resolve: Resolver | None = None,
    ) -> None:
        self.ctx = ctx
        self.realms = RealmManager()
        self.base = Path.cwd() if base is None else Path(base)
        self.tree = Tree(self, ctx, base=self.base)
        self._resolver = resolve
        self._claimed: set[str] = set()

    def __repr__(self) -> str:
        return f"<Loader with {len(list(self.entries()))} entries>"

    # ------------------------------------------------------------- inspection

    def entries(self) -> Iterator[Entry]:
        """Every entry the loader manages, in document order."""
        return self.tree.entries()

    def entry(self, entry_id: str) -> Entry:
        """The entry with a given (possibly namespaced) id."""
        for candidate in self.entries():
            if candidate.id == entry_id:
                return candidate
        raise LoaderError(f"no entry with id {entry_id!r}")

    def describe(self) -> list[str]:
        """The realized configuration as an indented ASCII tree."""
        lines: list[str] = []

        def walk(group: EntryGroup, depth: int) -> None:
            for item in group.entries():
                uid = "-" if item.uid is None else f"#{item.uid}"
                lines.append(f"{'  ' * depth}- {item.id} [{item.options.name}] {item.status} {uid}")
                if item.subgroup is not None:
                    walk(item.subgroup, depth + 1)
                elif item.subtree is not None:
                    walk(item.subtree.root, depth + 1)

        walk(self.tree.root, 0)
        return lines

    # -------------------------------------------------------------- lifecycle

    async def reconcile(self, document: Any) -> None:
        """Bring the running system into step with a configuration document.

        The operations are issued top-down and then the runtime is allowed to
        reach its fixed point, which by Theorem 66 it always does.
        """
        records = normalize_document(document)
        self._claimed = _claimed_ids(records)
        try:
            await self.tree.reconcile(records)
        finally:
            self._claimed = set()
        await self.settle()

    async def start(self, document: Any) -> None:
        """Alias of :meth:`reconcile` reading as the first application of one."""
        await self.reconcile(document)

    async def load(self, path: str | os.PathLike[str]) -> None:
        """Read a YAML or JSON file and reconcile against it."""
        location = Path(path)
        self.base = location.parent
        self.tree.base = self.base
        await self.reconcile(read_config(location))

    async def settle(self) -> None:
        """Wait for every fiber in the runtime to reach a fixed point."""
        await self.ctx.registry.settle()

    async def stop(self) -> None:
        """Retire every entry the loader realized."""
        await self.tree.stop()
        self.realms.collect(self.entries())
        await self.settle()

    # ------------------------------------------------------- component lookup

    def resolve_component(self, name: str, entry: Entry) -> Any:
        """Map an entry's ``name`` onto the component it selects."""
        if name == BUILTIN_GROUP:
            return group_component(entry)
        if name == BUILTIN_INCLUDE:
            return include_component(entry)
        if self._resolver is not None:
            found = self._resolver(name)
            if found is not None:
                return found
        if name.startswith("cordispy:"):
            raise LoaderError(f"unknown built-in component {name!r}")
        return import_target(name)

    def is_claimed(self, tree: Tree, local_id: str) -> bool:
        """Whether the document being applied still names this id elsewhere.

        A group drops a child that left it, unless the same id turns up
        somewhere else in the same document -- then the child is moving between
        groups, and moving must keep its identity.
        """
        return tree is self.tree and local_id in self._claimed


def _claimed_ids(records: Sequence[Any]) -> set[str]:
    """Every id named anywhere in one document, groups included."""
    claimed: set[str] = set()
    for record in records:
        if not isinstance(record, Mapping):
            continue
        entry_id = record.get("id")
        if isinstance(entry_id, str):
            claimed.add(entry_id)
        if record.get("name") == BUILTIN_GROUP:
            payload = record.get("config")
            if isinstance(payload, Sequence) and not isinstance(payload, str | bytes):
                claimed |= _claimed_ids(payload)
    return claimed
