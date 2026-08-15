"""Hot module replacement -- paper section 5.2.2, Algorithms 8, 9 and 10.

Two halves. The classification and detection tests drive the algorithms against
a synthetic import graph, because a fixed point is easiest to pin down when the
edges are written out by hand. The reload tests build a real package on disk,
edit it, and check the two claims that matter: the fiber is swapped while the
state around it is left alone, and a re-import that raises leaves the system
serving the previous version.
"""

from __future__ import annotations

import importlib
import shutil
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from cordispy import Context
from cordispy.loader import (
    Hmr,
    ImportGraph,
    Loader,
    LoaderError,
    classify,
    dependencies,
    detect,
)

# ---------------------------------------------------------------------------
# a synthetic graph
# ---------------------------------------------------------------------------


def edges(**graph: str) -> Callable[[str], frozenset[str]]:
    """Build ``get_imports`` from ``module="a b c"`` keyword pairs."""
    table = {name: frozenset(targets.split()) for name, targets in graph.items()}
    return lambda name: table.get(name, frozenset())


# ---------------------------------------------------------------------------
# phase 1: classification (Algorithm 8)
# ---------------------------------------------------------------------------


def test_classification_declines_the_boundary_below_a_change() -> None:
    """Nothing under a changed module needs replacing just because it is under it."""
    imports_of = edges(a="b", b="c", c="")
    accepted, declined = classify({"a"}, set(), imports_of)

    assert accepted == {"a"}
    assert declined == {"b", "c"}


def test_classification_accepts_a_module_that_imports_an_accepted_one() -> None:
    """A module reached from a change and importing it back is part of the change."""
    imports_of = edges(a="b", b="c ext", c="a")
    accepted, declined = classify({"a"}, {"ext"}, imports_of)

    assert accepted == {"a", "b", "c"}
    assert declined == {"ext"}


def test_classification_defaults_an_import_cycle_to_declined() -> None:
    """Algorithm 8 line 21: whatever the fixed point leaves undecided is declined."""
    imports_of = edges(a="b", b="c", c="b")
    accepted, declined = classify({"a"}, set(), imports_of)

    assert accepted == {"a"}
    assert declined == {"b", "c"}, "b and c decide nothing about each other"


def test_externals_are_declined_from_the_start() -> None:
    imports_of = edges(a="ext", ext="")
    accepted, declined = classify({"a"}, {"ext"}, imports_of)

    assert accepted == {"a"}
    assert declined == {"ext"}


# ---------------------------------------------------------------------------
# phase 2: stale-entry detection (Algorithm 9)
# ---------------------------------------------------------------------------


def test_a_dependency_walk_stops_at_a_declined_module() -> None:
    imports_of = edges(entry="a", a="b", b="deep")
    assert dependencies("entry", {"b"}, imports_of) == {"entry", "a"}
    assert dependencies("entry", set(), imports_of) == {"entry", "a", "b", "deep"}


def test_a_declined_root_has_no_dependencies_at_all() -> None:
    assert dependencies("entry", {"entry"}, edges(entry="a")) == set()


async def test_detection_marks_only_the_entries_whose_tree_reaches_a_change() -> None:
    root = Context()
    components = {name: _inert(name) for name in ("pkg.left:go", "pkg.right:go")}
    loader = Loader(root, resolve=components.get)
    await loader.reconcile(
        [
            {"id": "left", "name": "pkg.left:go"},
            {"id": "right", "name": "pkg.right:go"},
        ]
    )

    imports_of = edges(**{"pkg.left": "pkg.shared", "pkg.right": "", "pkg.shared": ""})
    accepted = {"pkg.shared"}
    stale = detect(list(loader.entries()), accepted, set(), imports_of)

    assert [entry.id for entry in stale] == ["left"]
    assert accepted == {"pkg.shared", "pkg.left"}, "a stale entry folds its whole tree in"

    await loader.stop()


def _inert(name: str) -> Any:
    from cordispy import plugin

    @plugin(name=name)
    def component(ctx: Context, config: Any) -> None:
        return None

    return component


# ---------------------------------------------------------------------------
# the bounded import graph
# ---------------------------------------------------------------------------

_COUNTER = 0


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[tuple[Path, str]]:
    """A throw-away importable package, removed from the interpreter afterwards."""
    global _COUNTER
    _COUNTER += 1
    package = f"hotpkg{_COUNTER}"
    site = tmp_path / "site"
    directory = site / package
    directory.mkdir(parents=True)
    (directory / "__init__.py").write_text("", encoding="utf-8")
    sys.path.insert(0, str(site))
    importlib.invalidate_caches()
    try:
        yield directory, package
    finally:
        sys.path.remove(str(site))
        for name in [n for n in list(sys.modules) if n == package or n.startswith(f"{package}.")]:
            del sys.modules[name]
        importlib.invalidate_caches()
        shutil.rmtree(site, ignore_errors=True)


STATE = """
from typing import Any

from cordispy import Context, plugin


@plugin(name="state", provide=["store"])
def state(ctx: Context, config: Any) -> Any:
    data: dict[str, Any] = {"routes": {}, "served": 0}
    ctx.set("store", data)
    return lambda: data["routes"].clear()
"""

GREETER = """
from typing import Any

from cordispy import Context, plugin

GREETING = "{greeting}"


@plugin(name="greeter", inject=["store"])
def greeter(ctx: Context, config: Any) -> Any:
    store = ctx.store

    def greet(who: str) -> str:
        store["served"] += 1
        return GREETING + ", " + who

    store["routes"]["/greet"] = greet
    return lambda: store["routes"].pop("/greet", None)
"""

BROKEN = """
from cordispy import plugin


def greeter(ctx, config)
    return None
"""


def test_the_graph_is_bounded_to_the_configured_package(workspace: tuple[Path, str]) -> None:
    directory, package = workspace
    (directory / "state.py").write_text(STATE, encoding="utf-8")
    (directory / "greeter.py").write_text(GREETER.format(greeting="hello"), encoding="utf-8")

    graph = ImportGraph([package])
    graph.scan()

    assert set(graph.modules) == {package, f"{package}.state", f"{package}.greeter"}
    assert graph.is_internal(f"{package}.state")
    assert not graph.is_internal("cordispy")
    assert "cordispy" in graph.externals(), "everything outside the boundary is external"
    assert graph.imports("cordispy") == frozenset(), "an external module contributes no edges"


def test_a_package_that_is_not_importable_cannot_bound_a_graph() -> None:
    graph = ImportGraph(["no_such_package_anywhere"])
    with pytest.raises(LoaderError, match="not an importable package"):
        graph.scan()


def test_a_file_that_no_longer_parses_stays_in_the_graph(workspace: tuple[Path, str]) -> None:
    directory, package = workspace
    (directory / "greeter.py").write_text(BROKEN, encoding="utf-8")

    graph = ImportGraph([package])
    graph.scan()

    node = graph.modules[f"{package}.greeter"]
    assert node.broken is True
    assert node.imports == frozenset(), "a broken file contributes no edges, but is still a node"


def test_polling_stashes_a_module_whose_source_changed(workspace: tuple[Path, str]) -> None:
    directory, package = workspace
    (directory / "state.py").write_text(STATE, encoding="utf-8")
    (directory / "greeter.py").write_text(GREETER.format(greeting="hello"), encoding="utf-8")

    root = Context()
    loader = Loader(root)
    hmr = Hmr(loader, packages=[package])

    assert hmr.poll() == set(), "nothing has changed since the baseline"

    (directory / "greeter.py").write_text(GREETER.format(greeting="welcome"), encoding="utf-8")

    assert hmr.poll() == {f"{package}.greeter"}
    assert hmr.stashed == {f"{package}.greeter"}
    assert hmr.poll() == set(), "a stamp is only reported once"


# ---------------------------------------------------------------------------
# phase 3: transactional reload (Algorithm 10)
# ---------------------------------------------------------------------------


async def _serving(workspace: tuple[Path, str], greeting: str) -> tuple[Context, Loader, Hmr, str]:
    directory, package = workspace
    (directory / "state.py").write_text(STATE, encoding="utf-8")
    (directory / "greeter.py").write_text(GREETER.format(greeting=greeting), encoding="utf-8")

    root = Context()
    loader = Loader(root, base=directory)
    await loader.reconcile(
        [
            {"id": "state", "name": f"{package}.state:state"},
            {"id": "greeter", "name": f"{package}.greeter:greeter"},
        ]
    )
    return root, loader, Hmr(loader, packages=[package]), package


async def test_a_reload_swaps_the_fiber_and_leaves_the_state_around_it_alone(
    workspace: tuple[Path, str],
) -> None:
    """The claim of section 5.2.2: a fiber already bounds the component's effects.

    Only the edited module's entry is replaced. The store belongs to another
    entry that nothing changed, so the requests it counted survive the reload --
    and the new fiber picks up exactly where the old one left off.
    """
    directory, _ = workspace
    root, loader, hmr, package = await _serving(workspace, "hello")

    store = root.get("store")
    assert store["routes"]["/greet"]("world") == "hello, world"
    assert store["served"] == 1

    state_uid = loader.entry("state").uid
    greeter_uid = loader.entry("greeter").uid

    (directory / "greeter.py").write_text(GREETER.format(greeting="welcome"), encoding="utf-8")
    assert hmr.poll() == {f"{package}.greeter"}
    result = await hmr.apply()

    assert result.stale == ("greeter",)
    assert result.accepted == (f"{package}.greeter",)
    assert "cordispy" in result.declined, "the framework is outside the boundary"
    assert f"{package}.state" not in result.accepted, "an untouched neighbour is never invalidated"

    assert loader.entry("state").uid == state_uid, "the untouched entry keeps its fiber"
    assert loader.entry("greeter").uid != greeter_uid, "the stale entry got a fresh one"

    store = root.get("store")
    assert store["routes"]["/greet"]("world") == "welcome, world", "the new code is serving"
    assert store["served"] == 2, "and the state it counts into survived the swap"
    assert hmr.stashed == set()

    await loader.stop()


async def test_a_reload_with_nothing_stale_changes_nothing(workspace: tuple[Path, str]) -> None:
    root, loader, hmr, _ = await _serving(workspace, "hello")
    before = {entry.id: entry.uid for entry in loader.entries()}

    result = await hmr.apply()

    assert result.stale == ()
    assert result.reloaded is False
    assert {entry.id: entry.uid for entry in loader.entries()} == before
    assert root.get("store")["routes"]["/greet"]("world") == "hello, world"

    await loader.stop()


async def test_a_failing_reimport_rolls_back_and_the_system_still_serves(
    workspace: tuple[Path, str],
) -> None:
    """Algorithm 10 lines 7-12. The system is never left half-reloaded."""
    directory, _ = workspace
    root, loader, hmr, package = await _serving(workspace, "hello")

    store = root.get("store")
    store["routes"]["/greet"]("world")
    assert store["served"] == 1

    (directory / "greeter.py").write_text(BROKEN, encoding="utf-8")
    assert hmr.poll() == {f"{package}.greeter"}

    with pytest.raises(SyntaxError):
        await hmr.apply()

    assert hmr.rollbacks == 1
    entry = loader.entry("greeter")
    assert entry.status == "ACTIVE", "the entry is serving again, on the previous version"
    assert entry.error is None

    store = root.get("store")
    assert store["routes"]["/greet"]("world") == "hello, world", "the old code, restored"
    assert store["served"] == 2, "and the state around it never went away"
    assert sys.modules[f"{package}.greeter"].GREETING == "hello", "the cache holds the backup"

    await loader.stop()


async def test_a_repaired_module_reloads_after_a_rollback(workspace: tuple[Path, str]) -> None:
    """A rollback keeps the change stashed, so the fix lands on the next cycle."""
    directory, _ = workspace
    root, loader, hmr, _package = await _serving(workspace, "hello")

    (directory / "greeter.py").write_text(BROKEN, encoding="utf-8")
    hmr.poll()
    with pytest.raises(SyntaxError):
        await hmr.apply()

    (directory / "greeter.py").write_text(GREETER.format(greeting="repaired"), encoding="utf-8")
    hmr.poll()
    result = await hmr.apply()

    assert result.stale == ("greeter",)
    assert root.get("store")["routes"]["/greet"]("world") == "repaired, world"

    await loader.stop()
