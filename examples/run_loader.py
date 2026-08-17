"""Example 5: declarative configuration, incremental reconciliation, and HMR.

Run it with::

    uv run python examples/run_loader.py --config examples/config/app.yaml

The example is in two parts, and every step prints the evidence for its own
claim rather than asserting it out of sight.

Part A -- the declarative loader (paper section 5.2.1). A configuration document
is realized as a tree of fibers, then revised four times. Each revision touches
one field of one entry, and the printed fiber uids show that the loader applied
the least disruptive operation available: a uid is drawn fresh and never reused,
so a uid that did not change is a fiber that was never touched.

Part B -- hot module replacement (paper section 5.2.2, Algorithms 8 to 10). A
throw-away plugin package is written to disk, loaded, and then edited. The first
edit is valid and the entry reloads while the state around it survives; the
second edit does not parse, and the transactional reload restores the previous
modules and rebuilds the entry from them, leaving the system fully serving.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import logging
import shutil
import sys
import tempfile
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from cordispy import Context
from cordispy.loader import (
    BUILTIN_GROUP,
    Hmr,
    Loader,
    normalize_document,
    read_config,
)

#: Entry names in the shipped configuration are plain import targets such as
#: ``loader_plugins:store``, so this directory has to be importable. It is added
#: to the path when the example runs rather than when it is imported, which
#: keeps every import in this file at the top of it where it belongs.
EXAMPLES = Path(__file__).resolve().parent
DEFAULT_CONFIG = EXAMPLES / "config" / "app.yaml"
HOT_PACKAGE = "hot_plugins"

WIDTH = 78


# ---------------------------------------------------------------------------
# printing
# ---------------------------------------------------------------------------


class _TerseFormatter(logging.Formatter):
    """Keep the runtime's log lines, drop their tracebacks.

    The example prints its own evidence for every failure it provokes, so a
    twenty-line traceback in the middle of the transcript would obscure the
    point rather than make it.
    """

    def format(self, record: logging.LogRecord) -> str:
        record.exc_info = None
        record.exc_text = None
        return super().format(record)


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_TerseFormatter("      runtime log [%(name)s] %(message)s"))
    logger = logging.getLogger("cordispy")
    logger.handlers = [handler]
    logger.setLevel(logging.WARNING)
    logger.propagate = False


def banner(text: str) -> None:
    print()
    print("=" * WIDTH)
    print(text)
    print("=" * WIDTH)


def step(number: str, text: str) -> None:
    print()
    print(f"-- {number}. {text}")
    print("-" * WIDTH)


def field(label: str, value: str) -> None:
    print(f"    {label:<22}{value}")


def listing(label: str, values: Iterable[str]) -> None:
    items = list(values)
    field(label, ", ".join(items) if items else "(none)")


# ---------------------------------------------------------------------------
# invariants
# ---------------------------------------------------------------------------


class Checks:
    """The example doubles as an assertion: a violated claim fails the run."""

    def __init__(self) -> None:
        self.failures: list[str] = []

    def claim(self, holds: bool, description: str) -> None:
        if holds:
            field("claim holds", description)
        else:
            self.failures.append(description)
            field("CLAIM FAILED", description)


# ---------------------------------------------------------------------------
# document surgery
# ---------------------------------------------------------------------------


def find_record(records: Sequence[Any], entry_id: str) -> dict[str, Any]:
    """The record with a given id, looked up through nested group configs."""
    found = _search(records, entry_id)
    if found is None:
        raise KeyError(entry_id)
    return found


def _search(records: Sequence[Any], entry_id: str) -> dict[str, Any] | None:
    for record in records:
        if not isinstance(record, dict):
            continue
        if record.get("id") == entry_id:
            return record
        if record.get("name") == BUILTIN_GROUP:
            children = record.get("config")
            if isinstance(children, list):
                nested = _search(children, entry_id)
                if nested is not None:
                    return nested
    return None


def revise(document: Any, entry_id: str, **changes: Any) -> Any:
    fresh = copy.deepcopy(document)
    find_record(normalize_document(fresh), entry_id).update(changes)
    return fresh


def add_child(document: Any, group_id: str, child: dict[str, Any]) -> Any:
    fresh = copy.deepcopy(document)
    group = find_record(normalize_document(fresh), group_id)
    children = group.setdefault("config", [])
    children.append(child)
    return fresh


# ---------------------------------------------------------------------------
# observation
# ---------------------------------------------------------------------------


def snapshot(loader: Loader) -> dict[str, int | None]:
    return {entry.id: entry.uid for entry in loader.entries()}


def routes_of(root: Context) -> dict[str, Any]:
    data = root.get("store")
    return {} if data is None else dict(data["routes"])


def report(loader: Loader, root: Context, before: dict[str, int | None] | None = None) -> None:
    print("    fiber tree")
    for line in loader.describe():
        print(f"      {line}")
    listing("routes serving", sorted(routes_of(root)))
    if before is None:
        return
    after = snapshot(loader)
    replaced = [name for name, uid in after.items() if before.get(name) != uid]
    untouched = [name for name, uid in after.items() if name in before and before[name] == uid]
    gone = [name for name in before if name not in after]
    listing("fibers replaced", replaced)
    listing("fibers untouched", untouched)
    if gone:
        listing("entries removed", gone)


# ---------------------------------------------------------------------------
# part A: the declarative loader
# ---------------------------------------------------------------------------


async def part_a(config: Path, checks: Checks) -> Loader:
    banner(f"PART A -- declarative reconciliation from {config.name}")

    document = read_config(config)
    twin = config.with_suffix(".json" if config.suffix != ".json" else ".yaml")
    if twin.exists():
        field("companion document", str(twin.name))
        checks.claim(read_config(twin) == document, "the YAML and JSON documents parse identically")

    root = Context()
    loader = Loader.trusted(root, base=config.parent)

    step("1", "load the configuration and show the fiber tree")
    await loader.reconcile(document)
    report(loader, root)
    first = snapshot(loader)
    checks.claim(
        all(entry.status == "ACTIVE" for entry in loader.entries()),
        "every entry of the document is active",
    )
    checks.claim(
        sorted(routes_of(root)) == ["/count", "/greet"],
        "each active entry contributed exactly its own route",
    )

    step("2", "set disabled on one entry -- only that fiber unloads")
    before = snapshot(loader)
    document_2 = revise(document, "counter", disabled=True)
    await loader.reconcile(document_2)
    report(loader, root, before)
    after = snapshot(loader)
    checks.claim(after["counter"] is None, "the disabled entry has no fiber at all")
    checks.claim("/count" not in routes_of(root), "and its route was withdrawn")
    checks.claim(
        after["greeter"] == before["greeter"] and after["tools"] == before["tools"],
        "its sibling and its group were not touched",
    )

    step("3", "change one entry's config -- it reconciles, its group does not restart")
    before = snapshot(loader)
    document_3 = revise(document_2, "greeter", config={"greeting": "welcome"})
    await loader.reconcile(document_3)
    report(loader, root, before)
    after = snapshot(loader)
    field("reply from /greet", routes_of(root)["/greet"]("world"))
    checks.claim(after["greeter"] != before["greeter"], "the reconfigured entry got a fresh fiber")
    checks.claim(after["tools"] == before["tools"], "its group kept the fiber it already had")
    checks.claim(after["store"] == before["store"], "and so did the provider it depends on")

    step("4", "add a child entry to a group -- the keyed diff creates one fiber")
    before = snapshot(loader)
    fibers = len(root.registry)
    document_4 = add_child(
        document_3,
        "tools",
        {"id": "echo", "name": "loader_plugins:echo", "config": {"prefix": "echo"}},
    )
    await loader.reconcile(document_4)
    report(loader, root, before)
    field("fibers in runtime", f"{fibers} -> {len(root.registry)}")
    after = snapshot(loader)
    checks.claim(len(root.registry) == fibers + 1, "exactly one fiber was created")
    checks.claim(
        all(after[name] == uid for name, uid in before.items() if name in after),
        "every entry that survived the diff kept its own fiber",
    )
    checks.claim("/echo" in routes_of(root), "and the new child is serving")

    print()
    field("first snapshot", str(first))
    field("final snapshot", str(snapshot(loader)))
    return loader


# ---------------------------------------------------------------------------
# part B: hot module replacement
# ---------------------------------------------------------------------------

STATE_SOURCE = '''"""Provides the store. Never edited, so HMR must never replace it."""

from typing import Any

from cordispy import Context, plugin


@plugin(name="state", provide=["store"])
def state(ctx: Context, config: Any) -> Any:
    data: dict[str, Any] = {"routes": {}, "served": 0}
    ctx.set("store", data)
    return lambda: data["routes"].clear()
'''

GREETER_SOURCE = '''"""Serves /greet. This is the file the example edits on disk."""

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
'''

BROKEN_SOURCE = '''"""A deliberate syntax error, to provoke the transactional rollback."""

from cordispy import plugin


def greeter(ctx, config)
    return None
'''


def write_workspace(site: Path, greeting: str) -> Path:
    package = site / HOT_PACKAGE
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "state.py").write_text(STATE_SOURCE, encoding="utf-8")
    (package / "greeter.py").write_text(GREETER_SOURCE.format(greeting=greeting), encoding="utf-8")
    return package


async def part_b(site: Path, checks: Checks) -> None:
    banner("PART B -- hot module replacement over a bounded import graph")

    package = write_workspace(site, "hello")
    if str(site) not in sys.path:
        sys.path.insert(0, str(site))
    field("plugin package", str(package))

    root = Context()
    loader = Loader.trusted(root, base=site)
    await loader.reconcile(
        [
            {"id": "state", "name": f"{HOT_PACKAGE}.state:state"},
            {"id": "greeter", "name": f"{HOT_PACKAGE}.greeter:greeter"},
        ]
    )
    hmr = Hmr(loader, packages=[HOT_PACKAGE])

    step("5", "edit a plugin source file on disk and trigger HMR")
    report(loader, root)
    listing("modules in graph", sorted(hmr.graph.modules))
    listing("externals (declined)", sorted(hmr.graph.externals()))

    store = root.get("store")
    for _ in range(3):
        store["routes"]["/greet"]("world")
    field("reply from /greet", store["routes"]["/greet"]("world"))
    field("requests served", str(store["served"]))
    before = snapshot(loader)
    served_before = int(store["served"])

    (package / "greeter.py").write_text(GREETER_SOURCE.format(greeting="welcome"), encoding="utf-8")
    field("edited on disk", "greeter.py  GREETING: hello -> welcome")
    listing("stashed by polling", sorted(hmr.poll()))

    result = await hmr.apply()
    listing("accepted modules", result.accepted)
    listing("declined modules", result.declined)
    listing("stale entries", result.stale)
    report(loader, root, before)

    store = root.get("store")
    field("reply from /greet", store["routes"]["/greet"]("world"))
    field("requests served", str(store["served"]))
    after = snapshot(loader)
    checks.claim(result.stale == ("greeter",), "only the edited module's entry was stale")
    checks.claim(after["greeter"] != before["greeter"], "the edited entry was rebuilt")
    checks.claim(after["state"] == before["state"], "the module nobody edited kept its fiber")
    checks.claim(
        store["served"] == served_before + 1,
        "the state held by the untouched entry survived the reload",
    )
    checks.claim(
        store["routes"]["/greet"]("world").startswith("welcome"),
        "and the new code is the one serving",
    )

    step("6", "introduce a syntax error and trigger HMR -- the reload rolls back")
    before = snapshot(loader)
    served_before = int(root.get("store")["served"])
    (package / "greeter.py").write_text(BROKEN_SOURCE, encoding="utf-8")
    field("edited on disk", "greeter.py  now a syntax error")
    listing("stashed by polling", sorted(hmr.poll()))

    failure: BaseException | None = None
    try:
        await hmr.apply()
    except SyntaxError as error:
        failure = error
        field("re-import raised", f"{type(error).__name__}: {error.msg} (line {error.lineno})")

    report(loader, root, before)
    store = root.get("store")
    field("reply from /greet", store["routes"]["/greet"]("world"))
    field("requests served", str(store["served"]))
    field("rollbacks so far", str(hmr.rollbacks))

    entry = loader.entry("greeter")
    checks.claim(failure is not None, "the failure reached the caller instead of being swallowed")
    checks.claim(entry.status == "ACTIVE", "the entry is serving again after the rollback")
    checks.claim(
        store["routes"]["/greet"]("world").startswith("welcome"),
        "on the previous version of the module, restored from the backup",
    )
    checks.claim(
        store["served"] == served_before + 2,
        "and the surrounding state was never disturbed",
    )

    await loader.stop()
    checks.claim(len(root.registry) == 1, "stopping the loader leaves only the root fiber")
    checks.claim(root.get("store") is None, "and the store it provided is gone")


def forget_workspace(site: Path) -> None:
    if str(site) in sys.path:
        sys.path.remove(str(site))
    stale = [n for n in sys.modules if n == HOT_PACKAGE or n.startswith(f"{HOT_PACKAGE}.")]
    for name in stale:
        del sys.modules[name]


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_loader.py",
        description=(
            "Realize a declarative configuration as fibers, reconcile four revisions of it "
            "incrementally, and then hot-replace a plugin module twice: once successfully and "
            "once into a rollback."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="the YAML or JSON configuration document to realize",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="directory to write the throw-away plugin package into (default: a temporary one)",
    )
    parser.add_argument(
        "--keep-workspace",
        action="store_true",
        help="leave the generated plugin package on disk after the run",
    )
    parser.add_argument(
        "--skip-hmr",
        action="store_true",
        help="run only part A, the declarative reconciliation",
    )
    return parser


async def run(args: argparse.Namespace, config: Path) -> int:
    checks = Checks()
    loader = await part_a(config, checks)
    await loader.stop()

    if not args.skip_hmr:
        temporary = args.workspace is None
        site = Path(tempfile.mkdtemp(prefix="cordispy-hmr-")) if temporary else Path(args.workspace)
        try:
            await part_b(site, checks)
        finally:
            forget_workspace(site)
            if temporary and not args.keep_workspace:
                shutil.rmtree(site, ignore_errors=True)
            elif args.keep_workspace:
                print()
                field("workspace kept at", str(site))

    banner("VERDICT")
    if checks.failures:
        for description in checks.failures:
            print(f"    FAILED: {description}")
        return 1
    print("    every claim above held: reconciliation was incremental and the reload transactional.")
    return 0


def main() -> int:
    configure_logging()
    args = build_parser().parse_args()
    config = Path(args.config).resolve()
    if not config.exists():
        print(f"no such configuration file: {config}")
        return 2
    if str(EXAMPLES) not in sys.path:
        sys.path.insert(0, str(EXAMPLES))
    return asyncio.run(run(args, config))


if __name__ == "__main__":
    raise SystemExit(main())
