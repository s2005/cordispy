#!/usr/bin/env bash
#
# calculator.sh - guided, self-checking walkthrough of calculator.md.
#
# Runs every check the guide describes for examples/run_calculator.py: each step
# explains what it exercises, echoes and runs the guide's exact command, shows
# the real output and the observed vs expected exit code, asserts the
# expectation, and waits for a keypress before the next step.
#
# Prerequisites - the shared setup in README.md:
#   uv sync
# or, equivalently, run this script once with --seed.
#
# Read-only: every step issues one run_calculator.py invocation, which builds
# both calculators inside its own process and prints a table. It writes no file
# and opens no network connection, so there is nothing to clean up.
#
# Exit codes: 0 all assertions passed, 1 an assertion failed, 2 bad invocation,
# 3 a prerequisite is missing, 130 interrupted.
#
# No `set -e`: step 6 expects a non-zero exit code (see common.sh).
set -uo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

# The demo this guide drives, and the scenario name it must refuse. These are a
# path and an argument value inside this checkout; the demo reaches nothing
# outside the process it runs in.
readonly DEMO="examples/run_calculator.py"
readonly UNKNOWN_SCENARIO="bogus"

# Invariant counts the demo reports per scenario. They are part of the guide's
# expectation: a scenario that quietly stopped checking something would still
# print a clean table.
readonly REMOVE_INVARIANTS=4
readonly LATE_INVARIANTS=5
readonly RESIDUE_INVARIANTS=5
readonly FAILURE_INVARIANTS=5
readonly ALL_INVARIANTS=19

STEP_TITLES=(
    "Preflight - the runner, the demo, and a runtime it can import"
    "Removing an operation something else was built on"
    "Composing a derived operation before the ones it needs"
    "What a retired operation leaves behind"
    "An operation that installs itself and then fails to load"
    "All four scenarios together hold every invariant"
    "An unknown scenario name is refused before anything is built"
)
readonly LAST_STEP=$((${#STEP_TITLES[@]} - 1))

usage() {
    cat <<'USAGE'
calculator.sh - guided walkthrough of calculator.md

Usage:
  ./calculator.sh [options]

Options:
  --auto, --no-pause     Run every step back-to-back with no keypress prompt
  --stop-on-fail         Abort at the first failed step (default: run them all)
  --from-step N          Start at step N, skipping the earlier ones
  --only-step N          Run exactly one step
  --list-steps           Print the step index and exit
  --runner CMD           How to launch the demo (default: "uv run python")
  --seed                 Resolve the project environment (uv sync) first
  --cleanup              Remove the suite's generated workspace afterwards
  --help                 Show this help and exit

Prerequisites: uv on PATH and the project environment resolved, or pass --seed.
See README.md.

Nothing to clean: every step runs examples/run_calculator.py, which prints a
table and writes no files, so this guide leaves nothing behind.
USAGE
}

# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

step_0() {
    uat_step 0 "${STEP_TITLES[0]}"
    uat_explain "Everything below needs a Python launcher, the demo file, and an environment where the cordis package imports. Checking all three once here turns a missing prerequisite into one clear message instead of six confusing step failures."
    uat_expect "the runner is on PATH, $DEMO exists, and the demo answers --help. If the environment is not resolved, this exits 3 naming the seed command."

    uat_require_cmd "${UAT_BASE_CMD[0]}" "Install uv (https://docs.astral.sh/uv/), or pass --runner with an interpreter that can import cordis, e.g. --runner python."
    uat_pass "runner is on PATH: ${UAT_BASE_CMD[*]}"

    uat_require_file "$DEMO" "Run this from a cordispy checkout; the guide resolves paths from the repo root."
    uat_pass "demo exists: $DEMO"

    uat_run_quiet "${UAT_BASE_CMD[@]}" "$DEMO" --help
    if [[ "$UAT_RC" -ne 0 ]]; then
        printf '%s\n' "$UAT_OUT"
        uat_fatal "$UAT_EXIT_PREREQ" \
            "the demo could not start (exit $UAT_RC). Resolve the environment first:
    ./calculator.sh --seed"
    fi
    uat_pass "the demo starts and the runtime imports"
}

step_1() {
    uat_step 1 "${STEP_TITLES[1]}"
    uat_explain "The headline. Division is removed on both sides, and 'mod' is defined in terms of it - a remainder is a - floor(a / b) * b. A calculator that has lost an operation should stop offering it; the interesting failure is not a leak but a lie, and the two middle rows show it is also an intermittent one - the conventional side answers a pair it has cached correctly, and raises on the first pair it has not."
    uat_expect "exit 0; cordis offers '* + - ^' and the registry still offers '% * + - ^'; the derived operation is INACTIVE against REGISTERED; a cached pair answers 2 on the registry while cordis reports unknown operator; a fresh pair raises KeyError there; and OK: $REMOVE_INVARIANTS cordis-side invariants hold."

    uat_run 0 "${UAT_BASE_CMD[@]}" "$DEMO" --scenario remove
    uat_assert_matches "operations offered by help +\| \* \+ - \^ +\| % \* \+ - \^" \
        "help stops offering % on one side and goes on offering it on the other"
    uat_assert_matches "state of the derived operation +\| INACTIVE +\| REGISTERED " \
        "the derived operation is deactivated against merely registered"
    uat_assert_matches "already in the memo cache +\| UnknownSymbolError" \
        "cordis reports the operator as unknown even for a cached pair"
    uat_assert_contains "KeyError: '/'" \
        "the registry raises KeyError on the first pair it has not cached"
    uat_assert_matches "advertised but not performable +\| \(none\) +\| % " \
        "cordis advertises nothing it cannot perform; the registry advertises %"
    uat_assert_contains "OK: $REMOVE_INVARIANTS cordis-side invariants hold" \
        "all $REMOVE_INVARIANTS remove invariants hold"
}

step_2() {
    uat_step 2 "${STEP_TITLES[2]}"
    uat_explain "Composition order. A derived operation composed before the ones it is built from is a waiting component, not an error. Both sides are honest about not advertising it yet; what separates them is whether it ever becomes available once its dependencies arrive."
    uat_expect "exit 0; PENDING against REJECTED before, ACTIVE against REJECTED after, cordis then evaluating 22 % 8 to 6 while the registry still calls % unknown, and OK: $LATE_INVARIANTS cordis-side invariants hold."

    uat_run 0 "${UAT_BASE_CMD[@]}" "$DEMO" --scenario late
    uat_assert_matches "state before its dependencies exist +\| PENDING +\| REJECTED " \
        "cordis waits where the registry rejects outright"
    uat_assert_matches "state once its dependencies arrive +\| ACTIVE +\| REJECTED " \
        "it activates by itself; the registry never retries"
    uat_assert_matches "22 % 8 +\| 6 +\| UnknownSymbolError" \
        "cordis then evaluates the remainder; the registry never learned the symbol"
    uat_assert_contains "OK: $LATE_INVARIANTS cordis-side invariants hold" \
        "all $LATE_INVARIANTS late invariants hold"
}

step_3() {
    uat_step 3 "${STEP_TITLES[3]}"
    uat_explain "The residue case, and the one where the conventional teardown is complete: it removes the symbol from all four tables correctly, so help agrees on both sides. What it cannot reach is the memo cache and the eviction timer, because evaluation created them after setup() had returned."
    uat_expect "exit 0; both sides hold 3 memo caches and 3 eviction timers while serving, then cordis drops to 2 of each and the registry stays at 3; and OK: $RESIDUE_INVARIANTS cordis-side invariants hold."

    uat_run 0 "${UAT_BASE_CMD[@]}" "$DEMO" --scenario residue
    uat_assert_matches "memo caches while serving +\| 3 +\| 3 " \
        "both sides cached the same amount doing the same work"
    uat_assert_matches "memo caches after retiring pow +\| 2 +\| 3 " \
        "the retired operation's memo cache survived on one side only"
    uat_assert_matches "eviction timers after retiring pow +\| 2 +\| 3 " \
        "and so did its eviction timer"
    uat_assert_contains "OK: $RESIDUE_INVARIANTS cordis-side invariants hold" \
        "all $RESIDUE_INVARIANTS residue invariants hold"
}

step_4() {
    uat_step 4 "${STEP_TITLES[4]}"
    uat_explain "An operation that puts itself into service and then fails to finish loading. Its arithmetic raises from inside its own closure, which no static check can detect - so the only thing that helps is rolling the installation back, and the count of tables it is still in is the measurement that says whether that happened."
    uat_expect "exit 0; the tokenizer rejects ! on cordis and accepts it on the registry, 5 ! 1 gives unknown operator against IndexError, the failed operation is in 0 of 4 tables against 4 of 4, and OK: $FAILURE_INVARIANTS cordis-side invariants hold."

    uat_run 0 "${UAT_BASE_CMD[@]}" "$DEMO" --scenario failure
    uat_assert_matches "does the tokenizer accept ! +\| no +\| yes " \
        "the failed operation's symbol is gone from the tokenizer on one side only"
    uat_assert_contains "IndexError: list index out of range" \
        "the registry dispatches into a handler that never finished loading"
    uat_assert_matches "tables of 4 it is still installed in +\| 0 +\| 4 " \
        "cordis rolled it out of every table; the registry left it in all four"
    uat_assert_matches "the rest of the calculator +\| 5 +\| 5 " \
        "the rest of the calculator still works on both sides"
    uat_assert_contains "OK: $FAILURE_INVARIANTS cordis-side invariants hold" \
        "all $FAILURE_INVARIANTS failure invariants hold"
}

step_5() {
    uat_step 5 "${STEP_TITLES[5]}"
    uat_explain "The demo doubles as an assertion suite: it checks every cordis-side invariant across all four scenarios in one process and exits non-zero if any of them does not hold. Running them together also proves the scenarios do not depend on being run alone."
    uat_expect "exit 0, OK: $ALL_INVARIANTS cordis-side invariants hold, and no [FAIL] line anywhere in the output."

    uat_run 0 "${UAT_BASE_CMD[@]}" "$DEMO" --scenario all
    uat_assert_contains "OK: $ALL_INVARIANTS cordis-side invariants hold" \
        "all $ALL_INVARIANTS invariants hold across the four scenarios"
    uat_assert_not_contains "[FAIL]" "no invariant reported a failure"
    uat_assert_not_contains "FAILED:" "the demo did not report a failing invariant count"
    uat_assert_contains "verdicts" "the per-scenario verdicts were printed"
}

step_6() {
    uat_step 6 "${STEP_TITLES[6]}"
    uat_explain "The argument validation. An unknown scenario name is an invocation error, distinct from a scenario that ran and disagreed, and it has to be refused before either calculator is built rather than producing an empty table."
    uat_expect "exit 2, argparse naming the valid choices, and no results table."

    uat_run 2 "${UAT_BASE_CMD[@]}" "$DEMO" --scenario "$UNKNOWN_SCENARIO"
    uat_assert_contains "invalid choice" "the unknown scenario is reported as an invalid choice"
    uat_assert_contains "$UNKNOWN_SCENARIO" "the rejected value is named back"
    uat_assert_not_contains "advertised but not performable" "no results table was produced"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

main() {
    uat_init

    local arg
    for arg in "$@"; do
        if [[ "$arg" == "--help" || "$arg" == "-h" ]]; then
            usage
            exit "$UAT_EXIT_OK"
        fi
    done

    uat_parse_common_args "$@"
    if [[ ${#UAT_REST[@]} -gt 0 ]]; then
        uat_fatal "$UAT_EXIT_USAGE" "unknown argument: ${UAT_REST[0]} (see --help)"
    fi

    if [[ "$UAT_LIST_ONLY" -eq 1 ]]; then
        uat_list_steps "calculator.sh"
        exit "$UAT_EXIT_OK"
    fi

    uat_resolve_range "$LAST_STEP"

    uat_banner "UAT walkthrough: run_calculator.py - operations as plugins"
    printf '  guide  : calculator.md\n'
    printf '  runner : %s\n' "${UAT_BASE_CMD[*]}"
    printf '  steps  : %s\n' "$(uat_step_range)"

    [[ "$UAT_DO_SEED" -eq 1 ]] && uat_seed

    uat_run_steps

    # Nothing to clean: every step above runs the demo, which prints a table and
    # writes no files. --cleanup is honored anyway so the flag means the same
    # thing in every guide of this suite.
    [[ "$UAT_DO_CLEANUP" -eq 1 ]] && uat_cleanup

    uat_summary
}

main "$@"
