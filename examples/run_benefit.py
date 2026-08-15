"""Example 4: what the paradigm buys, measured rather than asserted.

The same application is built twice. Once on this runtime
(``examples.harness``), where every feature is a component and every mutation is
a revertible effect. Once on a conventional plugin registry
(``examples.naive``), where every feature is a ``setup``/``teardown`` pair. Both
use the same leaf services, do the same work and serve the same requests; only
the composition mechanism differs.

Four scenarios then ask the same question in four ways: *when a component goes
away, does the process return to the state it was in before the component
arrived?*

Every number below is read out of the live process at the moment it is printed:

* pending tasks are the difference of two ``asyncio.all_tasks()`` sets;
* open sqlite connections are counted by asking each tracked connection to run a
  statement, which a closed connection refuses;
* routes and subscribers are the lengths of the real dispatcher and bus
  registries.

Nothing is hard-coded, and the script exits non-zero if any cordis-side
invariant fails -- the demonstration doubles as an assertion.

Usage::

    uv run python examples/run_benefit.py --scenario all
    uv run python examples/run_benefit.py --scenario residue --verbose
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from examples.harness import (
    Harness,
    Residue,
    compare,
    import_lazy_tool,
    reclaim,
    reset_connections,
    snapshot,
)
from examples.harness.plugins import store_memory, tool_broken, tool_kv
from examples.naive import BrokenTool, KvTool, MemoryStorePlugin, NaiveApp, import_eager_tool

#: The workload both implementations serve before anything is unloaded. Three
#: distinct first letters means three sqlite shards, and three writes means three
#: deferred compaction timers -- resources created while *serving*, which is
#: exactly the category a hand-written teardown cannot see.
WORKLOAD = (("alpha", "1"), ("beta", "2"), ("gamma", "3"))

MAX_CELL = 46


# --------------------------------------------------------------------------
# results
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Row:
    """One measurement, taken on both sides."""

    measurement: str
    cordis: str
    naive: str


@dataclass
class ScenarioResult:
    """Everything one scenario found."""

    key: str
    title: str
    rows: list[Row] = field(default_factory=list)
    invariants: list[tuple[str, bool]] = field(default_factory=list)
    verdict: str = ""

    def measure(self, measurement: str, cordis: Any, naive: Any) -> None:
        self.rows.append(Row(measurement, _cell(cordis), _cell(naive)))

    def require(self, description: str, holds: bool) -> None:
        """Record a cordis-side invariant. A false one fails the whole run."""
        self.invariants.append((description, bool(holds)))

    def row(self, measurement: str) -> Row:
        """One measurement by name, for tests that assert on the numbers."""
        for row in self.rows:
            if row.measurement == measurement:
                return row
        raise KeyError(measurement)

    @property
    def ok(self) -> bool:
        return all(holds for _, holds in self.invariants)

    @property
    def broken(self) -> list[str]:
        return [description for description, holds in self.invariants if not holds]


def _cell(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    text = str(value).replace("\n", " ")
    if len(text) > MAX_CELL:
        return text[: MAX_CELL - 3] + "..."
    return text


def _residue_rows(result: ScenarioResult, cordis: Residue, naive: Residue) -> None:
    result.measure("leftover route handlers", cordis.routes, naive.routes)
    result.measure("leftover event subscribers", cordis.subscribers, naive.subscribers)
    result.measure("still-open sqlite connections", cordis.connections, naive.connections)
    result.measure("still-pending asyncio tasks", cordis.tasks, naive.tasks)


def _error_of(call: Any, *args: Any) -> str:
    """Call something and report the exception it raised, or ``ok``."""
    try:
        call(*args)
    except Exception as error:
        return f"{type(error).__name__}: {error}"
    return "ok"


# --------------------------------------------------------------------------
# scenario 1: residue after unload
# --------------------------------------------------------------------------


async def scenario_residue(verbose: bool = False) -> ScenarioResult:
    """Remove one component from a running application and count what is left."""
    result = ScenarioResult("residue", "residue after unloading one component")

    # ------------------------------------------------------------ cordis
    reset_connections()
    harness = Harness()
    await harness.start(store="sqlite", tools=("tool_echo",))
    before = await snapshot(harness.server, harness.bus)
    _say(verbose, f"cordis: application up, baseline {before}")

    await harness.add("tool_kv", tool_kv, harness.traced)
    for key, value in WORKLOAD:
        harness.dispatch("/kv/put", {"key": key, "value": value})
    harness.dispatch("/kv/get", {"key": "alpha"})
    during = await snapshot(harness.server, harness.bus)
    _say(verbose, f"cordis: tool_kv loaded and exercised, now {during}")

    await harness.remove("tool_kv")
    after = await snapshot(harness.server, harness.bus)
    cordis_added = compare(before, during)
    cordis_residue = compare(before, after)
    _say(verbose, f"cordis: tool_kv retired, residue {cordis_residue.itemize()}")
    await harness.shutdown()

    # ------------------------------------------------------------ naive
    reset_connections()
    app = NaiveApp()
    app.start(store="sqlite", tools=("tool_echo",))
    naive_before = await snapshot(app.server, app.bus)
    _say(verbose, f"naive: application up, baseline {naive_before}")

    app.add(KvTool(app.traced))
    for key, value in WORKLOAD:
        app.dispatch("/kv/put", {"key": key, "value": value})
    app.dispatch("/kv/get", {"key": "alpha"})
    naive_during = await snapshot(app.server, app.bus)
    _say(verbose, f"naive: tool_kv registered and exercised, now {naive_during}")

    app.remove("tool_kv")
    naive_after = await snapshot(app.server, app.bus)
    naive_added = compare(naive_before, naive_during)
    naive_residue = compare(naive_before, naive_after)
    _say(verbose, f"naive: tool_kv unregistered, residue {naive_residue.itemize()}")
    await reclaim(naive_before, naive_after)
    app.shutdown()
    reset_connections()

    # ------------------------------------------------------------ report
    result.measure("resources the component acquired", cordis_added.total, naive_added.total)
    _residue_rows(result, cordis_residue, naive_residue)
    result.measure("total residue", cordis_residue.total, naive_residue.total)

    result.require("the component actually acquired resources", cordis_added.total > 0)
    result.require("no route handler survives the unload", cordis_residue.routes == 0)
    result.require("no event subscriber survives the unload", cordis_residue.subscribers == 0)
    result.require("no sqlite connection survives the unload", cordis_residue.connections == 0)
    result.require("no asyncio task survives the unload", cordis_residue.tasks == 0)
    result.verdict = (
        f"cordis leaves {cordis_residue.itemize()}; "
        f"the conventional registry leaves {naive_residue.itemize()}"
    )
    return result


# --------------------------------------------------------------------------
# scenario 2: provider hot-swap
# --------------------------------------------------------------------------


async def scenario_hotswap(verbose: bool = False) -> ScenarioResult:
    """Replace the ``store`` provider under a running consumer."""
    result = ScenarioResult("hotswap", "replacing a provider while consumers are running")

    # ------------------------------------------------------------ cordis
    reset_connections()
    harness = Harness()
    await harness.start(store="memory")
    harness.dispatch("/kv/put", {"key": "alpha", "value": "1"})
    old_store = harness.root.get("store")
    _say(verbose, f"cordis: serving on the {harness.store_kind} store")

    await harness.swap_store("sqlite")
    _say(verbose, f"cordis: swapped to {harness.store_kind}, tool_kv is {harness.state_of('tool_kv')}")

    cordis_state = harness.state_of("tool_kv")
    cordis_kind = harness.store_kind
    cordis_write = _error_of(harness.dispatch, "/kv/put", {"key": "beta", "value": "2"})
    cordis_read = harness.dispatch("/kv/get", {"key": "beta"}) if cordis_write == "ok" else None
    cordis_backend = None if cordis_read is None else cordis_read["backend"]
    cordis_value = None if cordis_read is None else cordis_read["value"]
    cordis_released = bool(getattr(old_store, "closed", False))
    await harness.shutdown()

    # ------------------------------------------------------------ naive
    reset_connections()
    naive_baseline = await snapshot()
    app = NaiveApp()
    app.start(store="memory")
    app.dispatch("/kv/put", {"key": "alpha", "value": "1"})
    naive_old = app.registry.services["store"]
    _say(verbose, f"naive: serving on the {app.store_kind} store")

    app.swap_store("sqlite")
    _say(verbose, f"naive: registry now reports the {app.store_kind} store")

    naive_state = app.state_of("tool_kv")
    naive_kind = app.store_kind
    naive_write = _error_of(app.dispatch, "/kv/put", {"key": "beta", "value": "2"})
    naive_read = app.dispatch("/kv/get", {"key": "beta"}) if naive_write == "ok" else None
    naive_backend = "unreachable" if naive_read is None else naive_read["backend"]
    naive_value = None if naive_read is None else naive_read["value"]
    naive_released = bool(getattr(naive_old, "closed", False))

    app.shutdown()
    await reclaim(naive_baseline, await snapshot())
    reset_connections()

    # ------------------------------------------------------------ report
    result.measure("provider the runtime reports after the swap", cordis_kind, naive_kind)
    result.measure("consumer state after the swap", cordis_state, naive_state)
    result.measure("previous provider released", cordis_released, naive_released)
    result.measure("write served after the swap", cordis_write, naive_write)
    result.measure("backend that served it", cordis_backend, naive_backend)
    result.measure("value read back after the swap", cordis_value, naive_value)

    result.require("the new provider is bound", cordis_kind == "sqlite")
    result.require("the consumer is active again", cordis_state == "ACTIVE")
    result.require("the previous provider was released", cordis_released)
    result.require("requests are served after the swap", cordis_write == "ok")
    result.require("the new provider serves them", cordis_backend == "sqlite")
    result.require("data written after the swap reads back", cordis_value == "2")
    result.verdict = (
        "cordis reloads the consumer against the new binding and keeps serving; "
        f"the conventional registry leaves it holding the old provider ({naive_write})"
    )
    return result


# --------------------------------------------------------------------------
# scenario 3: late dependency arrival
# --------------------------------------------------------------------------


async def scenario_late(verbose: bool = False) -> ScenarioResult:
    """Compose a consumer before anything provides what it needs."""
    result = ScenarioResult("late", "a consumer composed before its provider exists")

    # ------------------------------------------------------------ cordis
    reset_connections()
    harness = Harness()
    await harness.start(store=None)
    cordis_before = harness.state_of("tool_kv")
    cordis_healthy = _error_of(harness.dispatch, "/echo/say", "ping")
    cordis_early = _error_of(harness.dispatch, "/kv/put", {"key": "alpha", "value": "1"})
    # The same act the naive side performs below: import a plugin module while
    # nothing provides the key it needs. Measured, not asserted in prose.
    cordis_import = _error_of(import_lazy_tool)
    _say(verbose, f"cordis: tool_kv is {cordis_before} with no store provider")

    await harness.add("store", store_memory)
    cordis_after = harness.state_of("tool_kv")
    cordis_served = _error_of(harness.dispatch, "/kv/put", {"key": "alpha", "value": "1"})
    _say(verbose, f"cordis: store provider arrived, tool_kv is {cordis_after}")
    await harness.shutdown()

    # ------------------------------------------------------------ naive
    reset_connections()
    naive_baseline = await snapshot()
    app = NaiveApp()
    app.start(store=None, tools=("tool_echo",))
    naive_before = _error_of(app.add, KvTool())
    naive_eager = _error_of(import_eager_tool)
    naive_healthy = _error_of(app.dispatch, "/echo/say", "ping")
    naive_early = _error_of(app.dispatch, "/kv/put", {"key": "alpha", "value": "1"})
    _say(verbose, f"naive: registering tool_kv gave {naive_before}")

    app.add(MemoryStorePlugin())
    naive_after = app.state_of("tool_kv")
    naive_served = _error_of(app.dispatch, "/kv/put", {"key": "alpha", "value": "1"})
    _say(verbose, f"naive: store registered, tool_kv is {naive_after}")

    app.shutdown()
    await reclaim(naive_baseline, await snapshot())
    reset_connections()

    # ------------------------------------------------------------ report
    result.measure("consumer state with no provider", cordis_before, naive_before)
    result.measure("rest of the application still serves", cordis_healthy, naive_healthy)
    result.measure("request before the provider arrives", cordis_early, naive_early)
    result.measure("importing a plugin module with no provider", cordis_import, naive_eager)
    result.measure("consumer state once the provider arrives", cordis_after, naive_after)
    result.measure("request after the provider arrives", cordis_served, naive_served)

    result.require("the consumer waits instead of failing", cordis_before == "PENDING")
    result.require("the rest of the application is unaffected", cordis_healthy == "ok")
    result.require("importing the consumer module needs no provider", cordis_import == "ok")
    result.require("the consumer activates by itself", cordis_after == "ACTIVE")
    result.require("it then serves requests", cordis_served == "ok")
    result.verdict = (
        "cordis holds the consumer PENDING and activates it when the provider appears; "
        "the conventional registry fails at registration and never retries"
    )
    return result


# --------------------------------------------------------------------------
# scenario 4: failure containment
# --------------------------------------------------------------------------


async def scenario_failure(verbose: bool = False) -> ScenarioResult:
    """Load a component whose setup mounts routes and then raises."""
    result = ScenarioResult("failure", "a component that fails halfway through loading")

    # ------------------------------------------------------------ cordis
    reset_connections()
    harness = Harness()
    await harness.start(store="memory")
    before = await snapshot(harness.server, harness.bus)
    fiber = await harness.add("tool_broken", tool_broken)
    after = await snapshot(harness.server, harness.bus)
    cordis_residue = compare(before, after)
    cordis_state = fiber.state.value
    cordis_error = type(fiber.error).__name__ if fiber.error is not None else "none"
    cordis_orphan = _error_of(harness.dispatch, "/broken/go", None)
    cordis_healthy = _error_of(harness.dispatch, "/kv/put", {"key": "alpha", "value": "1"})
    _say(verbose, f"cordis: tool_broken is {cordis_state}, residue {cordis_residue.itemize()}")
    await harness.shutdown()

    # ------------------------------------------------------------ naive
    reset_connections()
    naive_baseline = await snapshot()
    app = NaiveApp()
    app.start(store="memory")
    naive_before = await snapshot(app.server, app.bus)
    app.registry.load_all([BrokenTool()])
    naive_after = await snapshot(app.server, app.bus)
    naive_residue = compare(naive_before, naive_after)
    naive_state = app.state_of("tool_broken")
    naive_error = app.registry.failures[0][1].__class__.__name__ if app.registry.failures else "none"
    naive_orphan = _error_of(app.dispatch, "/broken/go", None)
    naive_healthy = _error_of(app.dispatch, "/kv/put", {"key": "alpha", "value": "1"})
    _say(verbose, f"naive: tool_broken is {naive_state}, residue {naive_residue.itemize()}")

    app.shutdown()
    await reclaim(naive_baseline, await snapshot())
    reset_connections()

    # ------------------------------------------------------------ report
    result.measure("state recorded for the failed component", cordis_state, naive_state)
    result.measure("error recorded", cordis_error, naive_error)
    result.measure("routes the failed component left behind", cordis_residue.routes, naive_residue.routes)
    result.measure("total residue from the failure", cordis_residue.total, naive_residue.total)
    result.measure("its half-mounted route still answers", cordis_orphan, naive_orphan)
    result.measure("the rest of the application still serves", cordis_healthy, naive_healthy)

    result.require("the failure is recorded on the component", cordis_state == "FAILED")
    result.require("the error is retained", cordis_error == "RuntimeError")
    result.require("the routes it mounted were rolled back", cordis_residue.routes == 0)
    result.require("it leaves no residue at all", cordis_residue.clean)
    result.require("its half-mounted route is gone", cordis_orphan.startswith("RouteError"))
    result.require("the rest of the application is untouched", cordis_healthy == "ok")
    result.verdict = (
        "cordis rolls back the inverses accumulated before the failure and records FAILED; "
        f"the conventional registry logs the error and leaves {naive_residue.routes} route(s) mounted"
    )
    return result


# --------------------------------------------------------------------------
# presentation
# --------------------------------------------------------------------------

SCENARIOS = {
    "residue": scenario_residue,
    "hotswap": scenario_hotswap,
    "late": scenario_late,
    "failure": scenario_failure,
}


def _say(verbose: bool, message: str) -> None:
    if verbose:
        print(f"  . {message}")


def render(results: list[ScenarioResult]) -> str:
    """The ASCII results table."""
    header = ("scenario", "measurement", "cordis", "conventional registry")
    body: list[tuple[str, str, str, str]] = []
    for result in results:
        for index, row in enumerate(result.rows):
            body.append((result.key if index == 0 else "", row.measurement, row.cordis, row.naive))

    widths = [len(text) for text in header]
    for line in body:
        widths = [max(width, len(cell)) for width, cell in zip(widths, line, strict=True)]

    rule = "+" + "+".join("-" * (width + 2) for width in widths) + "+"

    def line_of(cells: tuple[str, ...]) -> str:
        padded = (f" {cell.ljust(width)} " for cell, width in zip(cells, widths, strict=True))
        return "|" + "|".join(padded) + "|"

    out = [rule, line_of(header), rule]
    previous = ""
    for line in body:
        if line[0] and previous:
            out.append(rule)
        if line[0]:
            previous = line[0]
        out.append(line_of(line))
    out.append(rule)
    return "\n".join(out)


def report(results: list[ScenarioResult]) -> int:
    """Print the table, the verdicts and the invariant check. Returns the exit code."""
    print(render(results))
    print()
    print("verdicts")
    for result in results:
        print(f"  {result.key}: {result.verdict}")
    print()
    print("cordis-side invariants (this demo doubles as an assertion)")
    failed = 0
    for result in results:
        for description, holds in result.invariants:
            status = "PASS" if holds else "FAIL"
            failed += 0 if holds else 1
            print(f"  [{status}] {result.key}: {description}")
    print()
    if failed:
        print(f"FAILED: {failed} cordis-side invariant(s) did not hold")
        return 1
    print(f"OK: {sum(len(result.invariants) for result in results)} cordis-side invariants hold")
    return 0


async def run(names: list[str], verbose: bool) -> int:
    results = [await SCENARIOS[name](verbose) for name in names]
    return report(results)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run_benefit.py",
        description=(
            "Build the same application on the cordis runtime and on a conventional "
            "plugin registry, then measure what each leaves behind."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--scenario",
        choices=[*SCENARIOS, "all"],
        default="all",
        help="which comparison to run (default: all)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="narrate each step as it is measured",
    )
    parser.add_argument(
        "--log-level",
        choices=["debug", "info", "warning", "error", "critical"],
        default="critical",
        help=(
            "logging threshold (default: critical). The failure scenario makes a "
            "component fail on purpose, and both runtimes report that failure "
            "through the logging module; the default keeps those expected reports "
            "out of the results table."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level.upper()), format="log %(name)s: %(message)s")
    names = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
    return asyncio.run(run(names, args.verbose))


if __name__ == "__main__":
    raise SystemExit(main())
