"""A calculator whose operations are plugins, built twice and compared.

Every arithmetic operation is a separate plugin. Composing one in teaches the
calculator a symbol; taking one out should make the calculator forget it. That
second half is the whole demonstration, because it is where the two designs stop
agreeing.

The measurement is not "does it leak". It is sharper than that: **does the
calculator still offer operations it can no longer perform?** A calculator that
lists ``%`` under ``help`` and then raises when you type ``22 % 8`` is not
leaking, it is lying, and a user has no way to find that out except by hitting
it.

Four scenarios::

    remove   take an operation out from under something that was built on it
    late     compose a derived operation before the ones it is derived from
    residue  evaluate, then retire, and count what survived the retirement
    failure  an operation that installs itself and then fails to finish loading

Run one with ``--scenario``, or all four with no arguments. The demo doubles as
an assertion: it checks the cordis-side invariants and exits non-zero if any of
them does not hold.
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

from examples.calc import CordisCalculator, NaiveCalculator
from examples.calc.engine import DERIVED, Calculator
from examples.calc.probe import reclaim

__all__ = ["SCENARIOS", "Row", "ScenarioResult", "main", "report"]

#: The five primitives every scenario starts from.
PRIMITIVES = ("add", "sub", "mul", "div", "pow")

#: Warmed before an operation is removed, so the memo cache holds this pair.
#: Its twin, WITNESS_FRESH, is deliberately never evaluated beforehand.
WITNESS_SEEN = "17 % 5"
WITNESS_FRESH = "22 % 8"

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


def _cell(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (set, frozenset)):
        return " ".join(sorted(value)) if value else "(none)"
    text = str(value).replace("\n", " ")
    if len(text) > MAX_CELL:
        return text[: MAX_CELL - 3] + "..."
    return text


def _say(verbose: bool, message: str) -> None:
    if verbose:
        print(f"  . {message}")


def _catalog(calculator: Calculator) -> str:
    """What ``help`` currently offers, as a line."""
    return " ".join(sorted(calculator.advertised())) or "(nothing)"


async def _build(app: CordisCalculator | NaiveCalculator, *, with_derived: bool = True) -> None:
    """The same starting calculator on either side."""
    await app.start(PRIMITIVES)
    if with_derived:
        await app.add("mod")


# --------------------------------------------------------------------------
# scenario 1: take an operation out from under a derived one
# --------------------------------------------------------------------------


async def scenario_remove(verbose: bool) -> ScenarioResult:
    result = ScenarioResult("remove", "remove an operation something else was built on")

    cordis = CordisCalculator()
    await _build(cordis)
    cordis.evaluate(WITNESS_SEEN)
    _say(verbose, f"cordis: calculator offers {_catalog(cordis.calculator)}")
    await cordis.remove("div")
    _say(verbose, f"cordis: div retired, calculator now offers {_catalog(cordis.calculator)}")

    naive = NaiveCalculator()
    await _build(naive)
    naive.evaluate(WITNESS_SEEN)
    _say(verbose, f"naive: calculator offers {_catalog(naive.calculator)}")
    await naive.remove("div")
    _say(verbose, f"naive: div unregistered, calculator still offers {_catalog(naive.calculator)}")

    result.measure("operations offered by help", _catalog(cordis.calculator), _catalog(naive.calculator))
    result.measure("state of the derived operation", cordis.state_of("mod"), naive.state_of("mod"))
    result.measure(
        f"{WITNESS_SEEN} -- a pair already in the memo cache",
        cordis.evaluate(WITNESS_SEEN),
        naive.evaluate(WITNESS_SEEN),
    )
    result.measure(
        f"{WITNESS_FRESH} -- a pair never evaluated before",
        cordis.evaluate(WITNESS_FRESH),
        naive.evaluate(WITNESS_FRESH),
    )
    result.measure(
        "advertised but not performable",
        cordis.calculator.broken(),
        naive.calculator.broken(),
    )

    result.require(
        "the calculator stops offering the derived operation",
        DERIVED.symbol not in cordis.calculator.advertised(),
    )
    result.require("the derived operation is deactivated, not deleted", cordis.state_of("mod") == "INACTIVE")
    result.require("nothing is advertised that cannot be performed", not cordis.calculator.broken())
    result.require("the primitives that remain still work", cordis.evaluate("2 + 3") == "5")

    result.verdict = (
        "cordis withdraws the derived operation with its dependency, so help stops offering it; "
        f"the conventional registry keeps advertising {' '.join(sorted(naive.calculator.broken()))} "
        "and answers from a stale memo cache until a pair it has not seen arrives"
    )
    await cordis.shutdown()
    await naive.shutdown()
    # The conventional side still holds eviction timers nothing remembers arming.
    # Cancelling them here, from outside both implementations, is the demo tidying
    # up after proving they exist -- see examples/calc/probe.py.
    await reclaim(cordis.calculator)
    await reclaim(naive.calculator)
    return result


# --------------------------------------------------------------------------
# scenario 2: compose a derived operation before its dependencies
# --------------------------------------------------------------------------


async def scenario_late(verbose: bool) -> ScenarioResult:
    result = ScenarioResult("late", "compose a derived operation before the ones it needs")

    cordis = CordisCalculator()
    await cordis.start()
    await cordis.add("mod")
    _say(verbose, f"cordis: mod composed with nothing to build on -- {cordis.state_of('mod')}")
    early_cordis = cordis.state_of("mod")
    early_cordis_offer = _catalog(cordis.calculator)

    naive = NaiveCalculator()
    await naive.start()
    await naive.add("mod")
    _say(verbose, f"naive: mod registered with nothing to build on -- {naive.state_of('mod')}")
    early_naive = naive.state_of("mod")
    early_naive_offer = _catalog(naive.calculator)

    for name in PRIMITIVES:
        await cordis.add(name)
        await naive.add(name)
    _say(verbose, "both: the primitives arrive")

    result.measure("state before its dependencies exist", early_cordis, early_naive)
    result.measure("offered before its dependencies exist", early_cordis_offer, early_naive_offer)
    result.measure("state once its dependencies arrive", cordis.state_of("mod"), naive.state_of("mod"))
    result.measure("offered afterwards", _catalog(cordis.calculator), _catalog(naive.calculator))
    result.measure(WITNESS_FRESH, cordis.evaluate(WITNESS_FRESH), naive.evaluate(WITNESS_FRESH))

    result.require("it waits instead of being rejected", early_cordis == "PENDING")
    result.require("it is not advertised while it cannot run", DERIVED.symbol not in early_cordis_offer)
    result.require("it activates by itself once its dependencies arrive", cordis.state_of("mod") == "ACTIVE")
    result.require("and is then advertised", DERIVED.symbol in cordis.calculator.advertised())
    result.require("and then evaluates", cordis.evaluate(WITNESS_FRESH) == "6")

    result.verdict = (
        "cordis holds the derived operation PENDING and activates it when its dependencies appear; "
        "the conventional registry rejects it at registration and never retries, so it is absent "
        "even after everything it needed has arrived"
    )
    await cordis.shutdown()
    await naive.shutdown()
    # The conventional side still holds eviction timers nothing remembers arming.
    # Cancelling them here, from outside both implementations, is the demo tidying
    # up after proving they exist -- see examples/calc/probe.py.
    await reclaim(cordis.calculator)
    await reclaim(naive.calculator)
    return result


# --------------------------------------------------------------------------
# scenario 3: what a retired operation leaves behind
# --------------------------------------------------------------------------


async def scenario_residue(verbose: bool) -> ScenarioResult:
    result = ScenarioResult("residue", "evaluate, retire an operation, and count what survived")

    async def run(app: CordisCalculator | NaiveCalculator, label: str) -> tuple[int, int, int, int]:
        await _build(app, with_derived=False)
        for expression in ("2 ^ 3", "4 ^ 2", "10 / 4", "1 + 1"):
            app.evaluate(expression)
        before = (app.calculator.live_caches(), app.calculator.pending_evictions())
        _say(verbose, f"{label}: after evaluating, {before[0]} memo caches and {before[1]} eviction timers")
        await app.remove("pow")
        after = (app.calculator.live_caches(), app.calculator.pending_evictions())
        _say(verbose, f"{label}: pow retired, {after[0]} memo caches and {after[1]} eviction timers remain")
        return (*before, *after)

    cordis = CordisCalculator()
    naive = NaiveCalculator()
    c_caches, c_timers, c_caches_after, c_timers_after = await run(cordis, "cordis")
    n_caches, n_timers, n_caches_after, n_timers_after = await run(naive, "naive")

    result.measure("memo caches while serving", c_caches, n_caches)
    result.measure("eviction timers while serving", c_timers, n_timers)
    result.measure("memo caches after retiring pow", c_caches_after, n_caches_after)
    result.measure("eviction timers after retiring pow", c_timers_after, n_timers_after)
    result.measure("2 ^ 3 afterwards", cordis.evaluate("2 ^ 3"), naive.evaluate("2 ^ 3"))
    result.measure("symbols help still offers", _catalog(cordis.calculator), _catalog(naive.calculator))

    result.require("the operation actually cached something", c_caches > 0)
    result.require("its memo cache did not survive the retirement", c_caches_after == c_caches - 1)
    result.require("nor did its eviction timer", c_timers_after == c_timers - 1)
    result.require("the symbol is gone from help", "^" not in cordis.calculator.advertised())
    result.require("and the tokenizer no longer accepts it", "^" not in cordis.calculator.alphabet)

    result.verdict = (
        f"cordis releases the retired operation's memo cache and eviction timer; the conventional "
        f"registry keeps {n_caches_after} cache(s) and {n_timers_after} timer(s), because both were "
        "created while evaluating an expression, after setup() had returned"
    )
    await cordis.shutdown()
    await naive.shutdown()
    # The conventional side still holds eviction timers nothing remembers arming.
    # Cancelling them here, from outside both implementations, is the demo tidying
    # up after proving they exist -- see examples/calc/probe.py.
    await reclaim(cordis.calculator)
    await reclaim(naive.calculator)
    return result


# --------------------------------------------------------------------------
# scenario 4: an operation that fails halfway through loading
# --------------------------------------------------------------------------


async def scenario_failure(verbose: bool) -> ScenarioResult:
    result = ScenarioResult("failure", "an operation that installs itself and then fails to load")

    cordis = CordisCalculator()
    await _build(cordis, with_derived=False)
    await cordis.add_broken()
    _say(verbose, f"cordis: the failing operation is {cordis.state_of('factorial')}")

    naive = NaiveCalculator()
    await _build(naive, with_derived=False)
    await naive.add_broken()
    _say(verbose, f"naive: the failing operation is {naive.state_of('factorial')}")

    result.measure("state recorded for it", cordis.state_of("factorial"), naive.state_of("factorial"))
    result.measure("symbols help offers", _catalog(cordis.calculator), _catalog(naive.calculator))
    result.measure(
        "does the tokenizer accept !", "!" in cordis.calculator.alphabet, "!" in naive.calculator.alphabet
    )
    result.measure("5 ! 1", cordis.evaluate("5 ! 1"), naive.evaluate("5 ! 1"))
    result.measure(
        "tables of 4 it is still installed in",
        cordis.calculator.installed_in("!"),
        naive.calculator.installed_in("!"),
    )
    result.measure("the rest of the calculator", cordis.evaluate("2 + 3"), naive.evaluate("2 + 3"))

    result.require("the failure is recorded on the component", cordis.state_of("factorial") == "FAILED")
    result.require("its symbol was rolled back out of help", "!" not in cordis.calculator.advertised())
    result.require("and out of the tokenizer", "!" not in cordis.calculator.alphabet)
    result.require("it is left in none of the four tables", cordis.calculator.installed_in("!") == 0)
    result.require("the rest of the calculator is untouched", cordis.evaluate("2 + 3") == "5")

    result.verdict = (
        "cordis rolls back the installation the failing operation had already done and records FAILED; "
        f"the conventional registry logs the error and leaves it in {naive.calculator.installed_in('!')} "
        "of the 4 tables, so the symbol tokenizes, dispatches, and dies inside a handler that never "
        "finished loading -- a shape no static check can catch, which is why it has to be rolled back"
    )
    await cordis.shutdown()
    await naive.shutdown()
    # The conventional side still holds eviction timers nothing remembers arming.
    # Cancelling them here, from outside both implementations, is the demo tidying
    # up after proving they exist -- see examples/calc/probe.py.
    await reclaim(cordis.calculator)
    await reclaim(naive.calculator)
    return result


SCENARIOS = {
    "remove": scenario_remove,
    "late": scenario_late,
    "residue": scenario_residue,
    "failure": scenario_failure,
}


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def render(results: list[ScenarioResult]) -> str:
    header = ("scenario", "measurement", "cordis", "conventional registry")
    body: list[tuple[str, ...]] = []
    for result in results:
        for index, row in enumerate(result.rows):
            body.append((result.key if index == 0 else "", row.measurement, row.cordis, row.naive))

    widths = [len(cell) for cell in header]
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
        prog="run_calculator.py",
        description=(
            "Build the same calculator twice -- once on the cordis runtime, once on a "
            "conventional plugin registry -- with every arithmetic operation as a separate "
            "plugin, then add and remove operations and see which calculator still tells the "
            "truth about what it can do."
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
            "logging threshold (default: critical). The failure scenario makes an operation "
            "fail on purpose and both runtimes report that failure through the logging "
            "module; the default keeps those expected reports out of the results table."
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
