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
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, TypeAlias, cast

from ..component import Component
from ..effect import Disposer
from .entry import Entry, EntryOptions, LoaderError, RealmManager
from .group import EntryGroup, group_component

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..context import Context

__all__ = [
    "BUILTIN_GROUP",
    "BUILTIN_INCLUDE",
    "Loader",
    "LoaderPolicy",
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


@dataclass(frozen=True, kw_only=True)
class LoaderPolicy:
    """Capabilities and resource limits applied before reconciliation.

    Imports and includes are intentionally unavailable by default. Resolver
    results remain available as the application's explicit component allowlist.
    """

    allow_imports: bool = False
    include_roots: tuple[Path, ...] = ()
    max_file_bytes: int = 1_048_576
    max_nesting_depth: int = 64
    max_entries: int = 10_000
    max_include_depth: int = 16
    max_included_files: int = 128

    def __post_init__(self) -> None:
        limits = {
            "max_file_bytes": self.max_file_bytes,
            "max_nesting_depth": self.max_nesting_depth,
            "max_entries": self.max_entries,
            "max_include_depth": self.max_include_depth,
            "max_included_files": self.max_included_files,
        }
        for name, value in limits.items():
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")

        roots: list[Path] = []
        for root in self.include_roots:
            try:
                resolved = Path(root).resolve(strict=True)
            except OSError as error:
                raise ValueError(f"include root {str(root)!r} cannot be resolved") from error
            if not resolved.is_dir():
                raise ValueError(f"include root {str(root)!r} is not a directory")
            if resolved not in roots:
                roots.append(resolved)
        object.__setattr__(self, "include_roots", tuple(roots))


@dataclass(frozen=True)
class _PreparedInclude:
    path: Path
    records: tuple[Mapping[str, Any], ...]


@dataclass
class _PreflightFrame:
    records: Sequence[Any]
    base: Path | None
    include_depth: int
    entry_depth: int
    active_paths: frozenset[Path]
    parent_record: dict[str, Any] | None = None
    parent_kind: str | None = None
    include_path: Path | None = None
    index: int = 0
    seen: set[str] = field(default_factory=set)
    output: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# reading a configuration document
# ---------------------------------------------------------------------------


def read_config(path: str | os.PathLike[str], *, max_bytes: int | None = None) -> Any:
    """Read a configuration document from a YAML or JSON file.

    JSON goes through the standard library, so a configuration in that form
    still loads when PyYAML is not installed.
    """
    location = Path(path)
    suffix = location.suffix.lower()
    if suffix not in (".json", ".yaml", ".yml"):
        raise LoaderError(f"cannot read {location}: expected a .yaml, .yml or .json file")
    if not location.is_file():
        raise LoaderError(f"cannot read {location}: expected a regular configuration file")
    if max_bytes is not None:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        with location.open("rb") as stream:
            raw = stream.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise LoaderError(f"cannot read {location}: file exceeds max_file_bytes={max_bytes}")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise LoaderError(f"cannot read {location}: file is not valid UTF-8") from None
    else:
        text = location.read_text(encoding="utf-8")

    if suffix == ".json":
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            raise LoaderError(f"cannot parse {location}: invalid JSON document") from None
    if suffix in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError as error:  # pragma: no cover - depends on the environment
            raise LoaderError(
                f"cannot read {location}: PyYAML is not installed. "
                "Install it, or write the configuration as JSON."
            ) from error
        try:
            return yaml.safe_load(text)
        except yaml.YAMLError:
            raise LoaderError(f"cannot parse {location}: invalid YAML document") from None
    raise AssertionError("configuration suffix was validated")


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

    def __init__(
        self,
        loader: Loader,
        ctx: Context,
        *,
        owner: Entry | None = None,
        base: Path | None,
    ) -> None:
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
        if not isinstance(config, _PreparedInclude):
            raise LoaderError(f"entry {entry.id!r}: include was not validated before reconciliation")
        subtree = Tree(entry.loader, ctx, owner=entry, base=config.path.parent)
        entry.subtree = subtree
        yield subtree.stop
        await subtree.reconcile(config.records)

    return Component(apply=apply, name=f"include<{entry.options.id}>", takes_config=True)


def _include_path(*, base: Path | None, config: Any, policy: LoaderPolicy, label: str) -> Path:
    raw = config.get("path") if isinstance(config, Mapping) else config
    if not isinstance(raw, str) or not raw:
        raise LoaderError(f"{label}: include needs a 'path' naming a configuration file")
    if not policy.include_roots:
        raise LoaderError(f"{label}: includes are disabled; configure an explicit include_roots directory")
    candidate = Path(raw)
    if not candidate.is_absolute():
        if base is None:
            raise LoaderError(f"{label}: a relative include needs an explicit loader base directory")
        candidate = base / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        raise LoaderError(f"{label}: include path {raw!r} cannot be resolved") from None
    if resolved.suffix.lower() not in (".json", ".yaml", ".yml"):
        raise LoaderError(f"{label}: include path {raw!r} must name a .json, .yaml or .yml file")
    if not resolved.is_file():
        raise LoaderError(f"{label}: include path {raw!r} is not a regular file")
    if not any(resolved.is_relative_to(root) for root in policy.include_roots):
        raise LoaderError(f"{label}: include path {raw!r} escapes the configured include roots")
    return resolved


def _root_config_path(path: str | os.PathLike[str]) -> Path:
    try:
        return Path(path).resolve(strict=True)
    except OSError:
        raise LoaderError(f"cannot read {path}: configuration path cannot be resolved") from None


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
        policy: LoaderPolicy | None = None,
    ) -> None:
        self.ctx = ctx
        self.realms = RealmManager()
        self.base = None if base is None else Path(base).resolve()
        if self.base is not None and not self.base.is_dir():
            raise ValueError(f"loader base {str(base)!r} is not a directory")
        self.policy = LoaderPolicy() if policy is None else policy
        self.tree = Tree(self, ctx, base=self.base)
        self._resolver = resolve
        self._claimed: set[str] = set()

    @classmethod
    def trusted(
        cls,
        ctx: Context,
        *,
        base: str | os.PathLike[str] | None = None,
        resolve: Resolver | None = None,
    ) -> Loader:
        """Create an explicitly trusted loader with imports and local includes.

        Imported modules and included configuration are executable-code inputs.
        Use the ordinary constructor for less-trusted configuration.
        """
        trusted_base = Path.cwd() if base is None else Path(base)
        resolved_base = trusted_base.resolve(strict=True)
        if not resolved_base.is_dir():
            raise ValueError(f"loader base {str(trusted_base)!r} is not a directory")
        return cls(
            ctx,
            base=resolved_base,
            resolve=resolve,
            policy=LoaderPolicy(allow_imports=True, include_roots=(resolved_base,)),
        )

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
        records = self._preflight(document, base=self.base, active_paths=frozenset())
        await self._reconcile_prepared(records)

    async def _reconcile_prepared(self, records: Sequence[Any]) -> None:
        """Reconcile one document that has already passed preflight."""
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
        location = _root_config_path(path)
        document = read_config(location, max_bytes=self.policy.max_file_bytes)
        records = self._preflight(
            document,
            base=location.parent,
            active_paths=frozenset({location}),
        )
        self.base = location.parent
        self.tree.base = self.base
        await self._reconcile_prepared(records)

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
        if not self.policy.allow_imports:
            raise LoaderError(
                f"entry {entry.id!r}: imports are disabled; provide the component through resolve "
                "or use Loader.trusted for trusted configuration"
            )
        return import_target(name)

    def is_claimed(self, tree: Tree, local_id: str) -> bool:
        """Whether the document being applied still names this id elsewhere.

        A group drops a child that left it, unless the same id turns up
        somewhere else in the same document -- then the child is moving between
        groups, and moving must keep its identity.
        """
        return tree is self.tree and local_id in self._claimed

    def _preflight(
        self,
        document: Any,
        *,
        base: Path | None,
        active_paths: frozenset[Path],
    ) -> tuple[Mapping[str, Any], ...]:
        """Validate and snapshot a complete document before live mutation."""
        self._validate_graph(document, label="configuration document")
        frames = [
            _PreflightFrame(
                records=normalize_document(document),
                base=base,
                include_depth=0,
                entry_depth=1,
                active_paths=active_paths,
            )
        ]
        prepared_root: tuple[Mapping[str, Any], ...] | None = None
        entry_count = 0
        included_files = 0

        while frames:
            frame = frames[-1]
            if frame.index >= len(frame.records):
                completed = tuple(cast(Mapping[str, Any], _freeze_value(record)) for record in frame.output)
                frames.pop()
                if frame.parent_record is None:
                    prepared_root = completed
                elif frame.parent_kind == "group":
                    frame.parent_record["config"] = completed
                else:
                    include_path = frame.include_path
                    if include_path is None:
                        raise AssertionError("include frame has no path")
                    frame.parent_record["config"] = _PreparedInclude(include_path, completed)
                continue

            record = frame.records[frame.index]
            frame.index += 1
            options = EntryOptions.parse(record)
            if options.id in frame.seen:
                raise LoaderError(
                    f"duplicate entry id {options.id!r} in one group: ids are the reconciliation key"
                )
            frame.seen.add(options.id)
            if frame.entry_depth > self.policy.max_nesting_depth:
                raise LoaderError(
                    f"entry {options.id!r}: configuration exceeds "
                    f"max_nesting_depth={self.policy.max_nesting_depth}"
                )
            entry_count += 1
            if entry_count > self.policy.max_entries:
                raise LoaderError(
                    f"entry {options.id!r}: configuration exceeds max_entries={self.policy.max_entries}"
                )

            prepared = options.to_record()
            frame.output.append(prepared)
            if options.name == BUILTIN_GROUP:
                payload = () if options.config is None else _expect_sequence(options.config, "group config")
                frames.append(
                    _PreflightFrame(
                        records=payload,
                        base=frame.base,
                        include_depth=frame.include_depth,
                        entry_depth=frame.entry_depth + 1,
                        active_paths=frame.active_paths,
                        parent_record=prepared,
                        parent_kind="group",
                    )
                )
                continue
            if options.name != BUILTIN_INCLUDE:
                continue

            next_include_depth = frame.include_depth + 1
            if next_include_depth > self.policy.max_include_depth:
                raise LoaderError(
                    f"entry {options.id!r}: configuration exceeds "
                    f"max_include_depth={self.policy.max_include_depth}"
                )
            included_files += 1
            if included_files > self.policy.max_included_files:
                raise LoaderError(
                    f"entry {options.id!r}: configuration exceeds "
                    f"max_included_files={self.policy.max_included_files}"
                )
            include_path = _include_path(
                base=frame.base,
                config=options.config,
                policy=self.policy,
                label=f"entry {options.id!r}",
            )
            if include_path in frame.active_paths:
                raise LoaderError(f"entry {options.id!r}: include cycle reaches {include_path}")
            included_document = read_config(include_path, max_bytes=self.policy.max_file_bytes)
            self._validate_graph(included_document, label=f"included file {include_path}")
            frames.append(
                _PreflightFrame(
                    records=normalize_document(included_document),
                    base=include_path.parent,
                    include_depth=next_include_depth,
                    entry_depth=frame.entry_depth + 1,
                    active_paths=frame.active_paths | {include_path},
                    parent_record=prepared,
                    parent_kind="include",
                    include_path=include_path,
                )
            )

        if prepared_root is None:
            raise AssertionError("preflight did not produce a root document")
        return prepared_root

    def _validate_graph(self, value: Any, *, label: str) -> None:
        """Reject recursive Python containers and excessive document depth."""
        active: set[int] = set()
        stack: list[tuple[Any, int, bool]] = [(value, 0, False)]
        while stack:
            current, depth, leaving = stack.pop()
            is_mapping = isinstance(current, Mapping)
            is_sequence = isinstance(current, Sequence) and not isinstance(current, str | bytes)
            if not is_mapping and not is_sequence:
                continue
            identity = id(current)
            if leaving:
                active.remove(identity)
                continue
            if depth > self.policy.max_nesting_depth:
                raise LoaderError(f"{label} exceeds max_nesting_depth={self.policy.max_nesting_depth}")
            if identity in active:
                raise LoaderError(f"{label} contains a self-referential mapping or sequence")
            active.add(identity)
            stack.append((current, depth, True))
            children = list(current.values()) if is_mapping else list(current)
            for child in reversed(children):
                stack.append((child, depth + 1, False))


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


def _freeze_value(value: Any) -> Any:
    """Freeze a validated value; recursion is bounded by ``LoaderPolicy``."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_value(item) for key, item in value.items()})
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return tuple(_freeze_value(item) for item in value)
    return value
