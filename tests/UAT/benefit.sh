#!/usr/bin/env bash
#
# benefit.sh - guided, self-checking walkthrough of benefit.md.
#
# Runs every check the guide describes for examples/run_benefit.py: each step
# explains what it exercises, echoes and runs the guide's exact command, shows
# the real output and the observed vs expected exit code, asserts the
# expectation, and waits for a keypress before the next step.
#
# Prerequisites - the shared setup in README.md:
#   uv sync
# or, equivalently, run this script once with --seed.
#
# Read-only: every step issues one run_benefit.py invocation, which builds both
# applications inside its own process and prints a table. It writes no file and
# opens no network connection, so there is nothing for this script to clean up.
#
# Exit codes: 0 all assertions passed, 1 an assertion failed, 2 bad invocation,
# 3 a prerequisite is missing, 130 interrupted.
#
# No `set -e`: step 6 expects a non-zero exit code (see common.sh).
set -uo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

# The demo this guide drives, and the scenario names it accepts. These are paths
# and argument values inside this checkout - the demo reaches nothing outside the
# process it runs in, so there is no environment value to keep synthetic.
readonly DEMO="examples/run_benefit.py"
readonly UNKNOWN_SCENARIO="bogus"

# Invariant counts the demo reports per scenario. They are part of the guide's
# expectation: a scenario that silently stops checking something would still
# print a clean table.
readonly RESIDUE_INVARIANTS=5
readonly HOTSWAP_INVARIANTS=6
readonly LATE_INVARIANTS=5
readonly FAILURE_INVARIANTS=6
readonly ALL_INVARIANTS=22

STEP_TITLES=(
    "Preflight - the runner, the demo, and a runtime it can import"
    "Residue after unload is zero on one side and nine on the other"
    "A swapped provider leaves the consumer serving, not holding a corpse"
    "A missing dependency is a wait, not an error"
    "A component that fails mid-load rolls back what it already did"
    "All four scenarios together hold every invariant"
    "An unknown scenario name is refused before anything is built"
)
readonly LAST_STEP=$((${#STEP_TITLES[@]} - 1))

usage() {
    cat <<'USAGE'
benefit.sh - guided walkthrough of benefit.md

Usage:
  ./benefit.sh [options]

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

Nothing to clean: every step runs examples/run_benefit.py, which prints a table
and writes no files, so this guide leaves nothing behind.
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
    ./benefit.sh --seed"
    fi
    uat_pass "the demo starts and the runtime imports"
}

step_1() {
    uat_step 1 "${STEP_TITLES[1]}"
    uat_explain "The headline claim. Both applications acquire the same 13 resources doing the same work, then the component is retired. What separates them is not what they did but what they released, so the residue columns are the whole argument: on the conventional side the sqlite connections and the deferred timer were armed while serving a request, after setup() returned, which is the one place a teardown() author could have written them down."
    uat_expect "exit 0, every cordis residue category at 0 against 1 route / 2 subscribers / 3 connections / 3 tasks, a total residue of 0 versus 9, and OK: $RESIDUE_INVARIANTS cordis-side invariants hold."

    uat_run 0 "${UAT_BASE_CMD[@]}" "$DEMO" --scenario residue
    uat_assert_matches "resources the component acquired +\| 13 +\| 13 " \
        "both sides acquired the same 13 resources - they did identical work"
    uat_assert_matches "leftover route handlers +\| 0 +\| 1 " \
        "0 leftover route handlers against 1"
    uat_assert_matches "leftover event subscribers +\| 0 +\| 2 " \
        "0 leftover event subscribers against 2"
    uat_assert_matches "still-open sqlite connections +\| 0 +\| 3 " \
        "0 still-open sqlite connections against 3"
    uat_assert_matches "still-pending asyncio tasks +\| 0 +\| 3 " \
        "0 still-pending asyncio tasks against 3"
    uat_assert_matches "total residue +\| 0 +\| 9 " \
        "total residue is 0 against 9 - exactly zero, not merely small"
    uat_assert_contains "OK: $RESIDUE_INVARIANTS cordis-side invariants hold" \
        "all $RESIDUE_INVARIANTS residue invariants hold"
}

step_2() {
    uat_step 2 "${STEP_TITLES[2]}"
    uat_explain "Replacing a dependency under a running consumer. Both registries report the new provider, so a status check would call both healthy. The difference is whether the consumer was reloaded against the new binding or is still holding the one that was just closed - which only shows up on the next request."
    uat_expect "exit 0, both sides reporting the sqlite provider, the consumer ACTIVE against REGISTERED, a StoreClosedError on the conventional side, and OK: $HOTSWAP_INVARIANTS cordis-side invariants hold."

    uat_run 0 "${UAT_BASE_CMD[@]}" "$DEMO" --scenario hotswap
    uat_assert_matches "provider the runtime reports after the swap +\| sqlite +\| sqlite " \
        "both registries report the new provider - a status check cannot tell them apart"
    uat_assert_matches "consumer state after the swap +\| ACTIVE +\| REGISTERED " \
        "the consumer is ACTIVE against a merely REGISTERED one"
    uat_assert_contains "StoreClosedError" \
        "the conventional side raises StoreClosedError on the next write"
    uat_assert_matches "backend that served it +\| sqlite +\| unreachable " \
        "sqlite served the write; the other side was unreachable"
    uat_assert_contains "OK: $HOTSWAP_INVARIANTS cordis-side invariants hold" \
        "all $HOTSWAP_INVARIANTS hotswap invariants hold"
}

step_3() {
    uat_step 3 "${STEP_TITLES[3]}"
    uat_explain "Composition order. A consumer loaded before its provider is a waiting component, not a failure: it holds PENDING, the rest of the application keeps serving, and it activates itself when the provider appears. The conventional registry raises at registration time and never retries, so the consumer stays ABSENT even after the provider is there."
    uat_expect "exit 0, PENDING against MissingDependencyError, ACTIVE against ABSENT once the provider arrives, and OK: $LATE_INVARIANTS cordis-side invariants hold."

    uat_run 0 "${UAT_BASE_CMD[@]}" "$DEMO" --scenario late
    uat_assert_matches "consumer state with no provider +\| PENDING +\| MissingDependencyError" \
        "the consumer waits where the conventional registry raises"
    uat_assert_matches "consumer state once the provider arrives +\| ACTIVE +\| ABSENT " \
        "it activates by itself; the other side is still absent"
    uat_assert_matches "importing a plugin module with no provider +\| ok +\| KeyError" \
        "the plugin module imports without a provider present"
    uat_assert_matches "request after the provider arrives +\| ok +\| RouteError" \
        "requests are served afterwards; the other side has no route"
    uat_assert_contains "OK: $LATE_INVARIANTS cordis-side invariants hold" \
        "all $LATE_INVARIANTS late-provider invariants hold"
}

step_4() {
    uat_step 4 "${STEP_TITLES[4]}"
    uat_explain "A component that raises halfway through loading has already mounted routes and armed handlers. Rolling back the inverses accumulated up to the failure is what keeps a half-loaded component from answering requests - the conventional registry logs the error and leaves both routes mounted, one of which still responds."
    uat_expect "exit 0, FAILED against ABSENT, 0 routes left behind against 2, the half-mounted route gone, and OK: $FAILURE_INVARIANTS cordis-side invariants hold."

    uat_run 0 "${UAT_BASE_CMD[@]}" "$DEMO" --scenario failure
    uat_assert_matches "state recorded for the failed component +\| FAILED +\| ABSENT " \
        "the failure is recorded on the component instead of losing it"
    uat_assert_matches "routes the failed component left behind +\| 0 +\| 2 " \
        "0 routes left mounted against 2"
    uat_assert_matches "total residue from the failure +\| 0 +\| 2 " \
        "the failed load leaves no residue at all"
    uat_assert_matches "its half-mounted route still answers +\| RouteError" \
        "the half-mounted route is gone rather than still answering"
    uat_assert_matches "the rest of the application still serves +\| ok +\| ok " \
        "the rest of the application is untouched on both sides"
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
    uat_assert_not_contains "[FAIL]" \
        "no invariant reported a failure"
    uat_assert_not_contains "FAILED:" \
        "the demo did not report a failing invariant count"
    uat_assert_contains "verdicts" \
        "the per-scenario verdicts were printed"
}

step_6() {
    uat_step 6 "${STEP_TITLES[6]}"
    uat_explain "The argument validation. An unknown scenario name is an invocation error, distinct from a scenario that ran and disagreed, and it has to be refused before either application is built rather than producing an empty table."
    uat_expect "exit 2, argparse naming the valid choices, and no results table."

    uat_run 2 "${UAT_BASE_CMD[@]}" "$DEMO" --scenario "$UNKNOWN_SCENARIO"
    uat_assert_contains "invalid choice" "the unknown scenario is reported as an invalid choice"
    uat_assert_contains "$UNKNOWN_SCENARIO" "the rejected value is named back"
    uat_assert_not_contains "total residue" "no results table was produced"
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
        uat_list_steps "benefit.sh"
        exit "$UAT_EXIT_OK"
    fi

    uat_resolve_range "$LAST_STEP"

    uat_banner "UAT walkthrough: run_benefit.py - cordis against a conventional registry"
    printf '  guide  : benefit.md\n'
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
