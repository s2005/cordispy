"""Hot module replacement -- paper section 5.2.2, Algorithms 8, 9 and 10.

HMR applies the revertible-effect pattern one level up, at the module. Because a
fiber already bounds every effect and coeffect of its component, a module that
*is* a component can be replaced through fiber operations alone: retiring the
old fiber recovers everything the component installed, and a fiber instantiated
from the reloaded module reinstalls it. Nothing has to be annotated as an
acceptance boundary, which is what separates this from bundler HMR.

The engine runs in three phases.

**Phase 1, classification** (Algorithm 8) takes the *stashed* set -- modules
whose source changed -- and the *externals* set -- modules that cannot be
replaced -- and walks the changes' dependency subgraph, marking each module
accepted or declined. A module is accepted once one of its imports is accepted,
and declined once all of its imports are declined. Anything the fixed point
leaves undecided is caught in an import cycle and defaults to declined.

**Phase 2, stale-entry detection** (Algorithm 9) filters the entries down to the
ones whose dependency tree reaches a changed module, treating declined modules
as a boundary that the walk does not cross.

**Phase 3, transactional reload** (Algorithm 10) invalidates the accepted
modules while backing each one up, then re-imports each stale entry's component
and swaps in a fresh fiber. If anything raises, the backups are restored, every
stale entry is rebuilt from them, and the error is re-raised. The system is
never left half-reloaded.

The import graph is *bounded*: it is built with :mod:`ast` over the ``.py`` files
of the configured plugin packages only. Every import leaving that boundary is an
external, and therefore declined. Change detection is :func:`os.stat` polling, so
the runtime takes no watcher dependency.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import logging
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

from .entry import Entry, LoaderError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .loader import Loader

__all__ = [
    "Hmr",
    "HmrResult",
    "ImportGraph",
    "ModuleNode",
    "classify",
    "dependencies",
    "detect",
    "module_of",
]

logger = logging.getLogger("cordispy.hmr")

#: A file's identity for change detection: modification time and size together,
#: so an edit is still seen on a file system with a coarse clock.
Stamp = tuple[int, int]


# ---------------------------------------------------------------------------
# the bounded import graph
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModuleNode:
    """One module inside the boundary, and what it imports."""

    name: str
    path: Path
    imports: frozenset[str]
    stamp: Stamp
    broken: bool = False


class ImportGraph:
    """The import graph of the configured plugin packages, and nothing else.

    The graph is read from source with :mod:`ast` rather than from a live module
    object, because a module whose source no longer parses must still be a node:
    it is precisely the one that changed.
    """

    def __init__(self, packages: Sequence[str]) -> None:
        if not packages:
            raise LoaderError("an import graph needs at least one package to bound it")
        self.packages = tuple(packages)
        self._modules: dict[str, ModuleNode] = {}

    def __len__(self) -> int:
        return len(self._modules)

    @property
    def modules(self) -> Mapping[str, ModuleNode]:
        """Every module inside the boundary, by dotted name."""
        return dict(self._modules)

    def scan(self) -> None:
        """Re-read every source file inside the boundary."""
        importlib.invalidate_caches()
        found: dict[str, ModuleNode] = {}
        for package in self.packages:
            for name, path in _walk_package(package):
                found[name] = _read_module(name, path)
        self._modules = found

    def is_internal(self, name: str) -> bool:
        """Whether a dotted name sits inside the boundary."""
        return name in self._modules

    def imports(self, name: str) -> frozenset[str]:
        """``get_imports(url)``: what a module directly imports.

        A name outside the boundary has no readable imports, which is exactly
        the boundary condition Algorithms 8 and 9 rely on.
        """
        node = self._modules.get(name)
        return frozenset() if node is None else node.imports

    def externals(self) -> set[str]:
        """Every import target the modules inside the boundary reach outside it."""
        outside: set[str] = set()
        for node in self._modules.values():
            outside.update(target for target in node.imports if target not in self._modules)
        return outside

    def stamps(self) -> dict[str, Stamp]:
        """The change-detection stamp of every module inside the boundary."""
        return {name: node.stamp for name, node in self._modules.items()}

    def classify(self, stashed: Iterable[str]) -> tuple[set[str], set[str]]:
        """Algorithm 8 over this graph, with its own externals."""
        return classify(stashed, self.externals(), self.imports)


def _walk_package(package: str) -> list[tuple[str, Path]]:
    spec = importlib.util.find_spec(package)
    if spec is None or not spec.submodule_search_locations:
        raise LoaderError(f"{package!r} is not an importable package, so it cannot bound an import graph")
    found: list[tuple[str, Path]] = []
    for location in spec.submodule_search_locations:
        base = Path(location)
        for path in sorted(base.rglob("*.py")):
            parts = list(path.relative_to(base).parts)
            if "__pycache__" in parts:
                continue
            if parts[-1] == "__init__.py":
                parts = parts[:-1]
            else:
                parts[-1] = parts[-1][: -len(".py")]
            found.append((".".join([package, *parts]) if parts else package, path))
    return found


def _read_module(name: str, path: Path) -> ModuleNode:
    stat = path.stat()
    stamp: Stamp = (stat.st_mtime_ns, stat.st_size)
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        # A file that no longer parses still belongs to the graph: it is the one
        # that changed. It simply contributes no edges.
        return ModuleNode(name=name, path=path, imports=frozenset(), stamp=stamp, broken=True)
    is_package = path.name == "__init__.py"
    return ModuleNode(
        name=name,
        path=path,
        imports=frozenset(_targets(name, is_package, tree)),
        stamp=stamp,
    )


def _targets(name: str, is_package: bool, tree: ast.Module) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_relative(name, is_package, node.level, node.module)
            if base is None:
                continue
            if base:
                found.add(base)
            for alias in node.names:
                if alias.name != "*":
                    found.add(f"{base}.{alias.name}" if base else alias.name)
    return found


def _resolve_relative(name: str, is_package: bool, level: int, module: str | None) -> str | None:
    if level == 0:
        return module or ""
    parts = name.split(".")
    if not is_package:
        parts = parts[:-1]
    climb = level - 1
    if climb > len(parts):
        return None
    if climb:
        parts = parts[:-climb]
    base = ".".join(parts)
    if module:
        return f"{base}.{module}" if base else module
    return base


# ---------------------------------------------------------------------------
# phase 1: classification (Algorithm 8)
# ---------------------------------------------------------------------------


def classify(
    stashed: Iterable[str],
    externals: Iterable[str],
    imports_of: Callable[[str], frozenset[str]],
) -> tuple[set[str], set[str]]:
    """Split the changes' dependency subgraph into accepted and declined.

    Seeded with the imports of the stashed modules, the fixed point accepts a
    module once one of its imports is accepted and declines one once all of its
    imports are declined. A module left undecided is caught in an import cycle,
    and line 21 of Algorithm 8 defaults it to declined.
    """
    accepted = set(stashed)
    declined = set(externals)
    pending: set[str] = set()
    for url in accepted:
        pending |= imports_of(url) - accepted - declined

    while True:
        progress = False
        for url in sorted(pending):
            if url not in pending:
                continue
            imports = imports_of(url)
            if imports & accepted:
                accepted.add(url)
                pending.discard(url)
                progress = True
            elif imports <= declined:
                declined.add(url)
                pending.discard(url)
                progress = True
            else:
                fresh = imports - accepted - declined - pending
                if fresh:
                    pending |= fresh
                    progress = True
        if not progress:
            break

    declined |= pending
    return accepted, declined


# ---------------------------------------------------------------------------
# phase 2: stale-entry detection (Algorithm 9)
# ---------------------------------------------------------------------------


def dependencies(
    root: str,
    declined: Iterable[str],
    imports_of: Callable[[str], frozenset[str]],
) -> set[str]:
    """The transitive imports of a module, stopping at declined ones."""
    boundary = set(declined)
    found: set[str] = set()
    stack = [root]
    while stack:
        url = stack.pop()
        if url in found or url in boundary:
            continue
        found.add(url)
        stack.extend(imports_of(url))
    return found


def module_of(entry: Entry) -> str:
    """The module an entry's component lives in."""
    return entry.options.name.partition(":")[0]


def detect(
    entries: Sequence[Entry],
    accepted: set[str],
    declined: Iterable[str],
    imports_of: Callable[[str], frozenset[str]],
) -> list[Entry]:
    """The entries whose dependency tree reaches a changed module.

    ``accepted`` is grown in place, exactly as Algorithm 9 line 14 has it: once
    an entry is stale its whole tree is folded in, so every stale module along
    it is invalidated by phase 3.
    """
    boundary = set(declined)
    stale: list[Entry] = []
    for entry in entries:
        tree = dependencies(module_of(entry), boundary, imports_of)
        if tree & accepted:
            accepted |= tree
            stale.append(entry)
    return stale


# ---------------------------------------------------------------------------
# phase 3: transactional reload (Algorithm 10)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HmrResult:
    """What one reload cycle did."""

    stashed: tuple[str, ...]
    accepted: tuple[str, ...]
    declined: tuple[str, ...]
    stale: tuple[str, ...]

    @property
    def reloaded(self) -> bool:
        return bool(self.stale)


class Hmr:
    """The HMR engine over one loader and a bounded set of plugin packages."""

    def __init__(self, loader: Loader, *, packages: Sequence[str]) -> None:
        self.loader = loader
        self.graph = ImportGraph(packages)
        self.graph.scan()
        self._stamps = self.graph.stamps()
        #: Modules whose source changed since the last successful reload.
        self.stashed: set[str] = set()
        #: How many reload cycles had to roll back.
        self.rollbacks = 0

    # ------------------------------------------------------ change detection

    def poll(self) -> set[str]:
        """Re-stat every file in the graph and stash what changed.

        Polling rather than watching keeps the runtime free of a third-party
        file-watcher dependency, and the stamp pairs modification time with size
        so an edit is seen even where the clock is coarse.
        """
        self.graph.scan()
        stamps = self.graph.stamps()
        changed = {name for name, stamp in stamps.items() if self._stamps.get(name) != stamp}
        self._stamps = stamps
        self.stashed |= changed
        return changed

    # ---------------------------------------------------------- the reload

    async def apply(self) -> HmrResult:
        """Run one full classify / detect / reload cycle.

        On failure the module caches are restored, every stale entry is rebuilt
        from the backup, and the original error is re-raised -- Algorithm 10
        lines 7-12.
        """
        stashed = set(self.stashed)
        accepted, declined = self.graph.classify(stashed)
        candidates = [entry for entry in self.loader.entries() if self.graph.is_internal(module_of(entry))]
        stale = detect(candidates, accepted, declined, self.graph.imports)

        result = HmrResult(
            stashed=tuple(sorted(stashed)),
            accepted=tuple(sorted(accepted)),
            declined=tuple(sorted(declined)),
            stale=tuple(entry.id for entry in stale),
        )
        if not stale:
            self.stashed -= stashed
            return result

        backup = _invalidate(accepted)
        try:
            for entry in stale:
                await entry.rebuild()
        except Exception as error:
            self.rollbacks += 1
            logger.error("hot reload failed; rolling back to the previous modules", exc_info=error)
            _restore(backup, accepted)
            for entry in stale:
                await _rebuild_quietly(entry)
            await self.loader.settle()
            raise

        await self.loader.settle()
        self.stashed -= stashed
        return result


def _invalidate(accepted: Iterable[str]) -> dict[str, ModuleType]:
    """Evict the accepted modules from :data:`sys.modules`, backing each up."""
    backup: dict[str, ModuleType] = {}
    for name in sorted(accepted):
        module = sys.modules.pop(name, None)
        if module is not None:
            backup[name] = module
    importlib.invalidate_caches()
    return backup


def _restore(backup: Mapping[str, ModuleType], evicted: Iterable[str]) -> None:
    """Undo :func:`_invalidate`, discarding the re-imports the failure produced.

    A name that was evicted and then re-imported by the failed attempt is
    replaced by its backup, and a name that was evicted, re-imported, but had not
    been loaded before the reload started is dropped again -- so the evicted set
    goes back to exactly the state :func:`_invalidate` found it in. Nothing
    outside that set is touched: :data:`sys.modules` is process-wide, and
    evicting a module this reload never displaced would be a larger claim than a
    rollback is entitled to make.
    """
    for name in evicted:
        if name not in backup:
            sys.modules.pop(name, None)
    sys.modules.update(backup)


async def _rebuild_quietly(entry: Entry) -> None:
    try:
        await entry.rebuild()
    except Exception as error:  # the original failure is the one worth raising
        entry.error = error
        logger.error("entry %s could not be rebuilt during rollback", entry.id, exc_info=error)
