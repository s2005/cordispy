#!/usr/bin/env bash
#
# hotswap.sh - guided, self-checking walkthrough of hotswap.md.
#
# Runs every check the guide describes for examples/run_hotswap.py: each step
# explains what it exercises, echoes and runs the guide's exact command, shows
# the real output and the observed vs expected exit code, asserts the
# expectation, and waits for a keypress before the next step.
#
# Prerequisites - the shared setup in README.md:
#   uv sync
# or, equivalently, run this script once with --seed.
#
# Read-only: every step issues one run_hotswap.py invocation, which builds its
# application inside its own process and uses an in-process sqlite store. It
# writes no files, so there is nothing for this script to clean up.
#
# Exit codes: 0 all assertions passed, 1 an assertion failed, 2 bad invocation,
# 3 a prerequisite is missing, 130 interrupted.
#
# No `set -e`: steps 4 and 5 expect a non-zero exit code (see common.sh).
set -uo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

# The demo this guide drives and the provider names it accepts. These are a path
# and argument values inside this checkout - the sqlite store is in-process, so
# there is no host or database name to keep synthetic.
readonly DEMO="examples/run_hotswap.py"
readonly MEMORY_STORE="memory"
readonly SQLITE_STORE="sqlite"
readonly UNKNOWN_STORE="bogus"

STEP_TITLES=(
    "Preflight - the runner, the demo, and a runtime it can import"
    "The consumer survives the gap and reloads against the new provider"
    "The same holds swapping the other way, so it is not a backend property"
    "--verbose explains the window rather than only measuring it"
    "An unknown provider name is refused"
    "A flag given without its value is refused"
)
readonly LAST_STEP=$((${#STEP_TITLES[@]} - 1))

usage() {
    cat <<'USAGE'
hotswap.sh - guided walkthrough of hotswap.md

Usage:
  ./hotswap.sh [options]

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

Nothing to clean: every step runs examples/run_hotswap.py, which uses an
in-process sqlite store and writes no files, so this guide leaves nothing
behind.
USAGE
}

# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

step_0() {
    uat_step 0 "${STEP_TITLES[0]}"
    uat_explain "Everything below needs a Python launcher, the demo file, and an environment where the cordis package imports. Checking all three once here turns a missing prerequisite into one clear message instead of five confusing step failures."
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
    ./hotswap.sh --seed"
    fi
    uat_pass "the demo starts and the runtime imports"
}

step_1() {
    uat_step 1 "${STEP_TITLES[1]}"
    uat_explain "The whole swap in one run. What matters is the middle: for a moment the consumer's required key has no provider at all. It does not fail and it is not asked to do anything - it goes INACTIVE, its routes come down, its connections close, and when the replacement is composed in it loads again against the new binding. None of that is written in the consumer."
    uat_expect "exit 0; memory serves the first write; retiring it leaves no routes, no open connections and the old store released; sqlite serves the write after the swap; the pre-swap value is gone; and the application shuts down with 0 connections and 0 routes."

    uat_run 0 "${UAT_BASE_CMD[@]}" "$DEMO" --source "$MEMORY_STORE" --target "$SQLITE_STORE"
    uat_assert_matches "store provider +$MEMORY_STORE" "the application started on the memory store"
    uat_assert_contains "{'stored': 'alpha', 'backend': 'memory'}" \
        "the memory store served the first write"
    uat_assert_matches "routes still mounted by tool_kv +\[\]" \
        "retiring the provider took the consumer's routes down"
    uat_assert_matches "open sqlite connections +0" \
        "no connection survived the provider being retired"
    uat_assert_matches "the old store was released +True" \
        "the previous provider was released"
    uat_assert_contains "{'stored': 'beta', 'backend': 'sqlite'}" \
        "the sqlite store served the write after the swap"
    uat_assert_matches "the value written before the swap +None" \
        "the swap moved to a genuinely different backend, not a copy of the old one"
    uat_assert_contains "the consumer reloaded against the new provider yes" \
        "the consumer reloaded itself against the new binding"
    uat_assert_contains "after shutting the application down: connections 0" \
        "shutdown closed every connection"
    uat_assert_matches "routes left mounted +0" "shutdown left no route mounted"
}

step_2() {
    uat_step 2 "${STEP_TITLES[2]}"
    uat_explain "Running the swap in reverse rules out the obvious alternative explanation - that this works because sqlite happens to tolerate it, or because memory does. The mechanism is in the runtime, so the direction should not matter."
    uat_expect "exit 0; sqlite serves the first write, memory serves the one after the swap, and the consumer reloads exactly as it did in step 1."

    uat_run 0 "${UAT_BASE_CMD[@]}" "$DEMO" --source "$SQLITE_STORE" --target "$MEMORY_STORE"
    uat_assert_matches "store provider +$SQLITE_STORE" "the application started on the sqlite store"
    uat_assert_contains "{'stored': 'alpha', 'backend': 'sqlite'}" \
        "the sqlite store served the first write"
    uat_assert_contains "{'stored': 'beta', 'backend': 'memory'}" \
        "the memory store served the write after the swap"
    uat_assert_contains "the consumer reloaded against the new provider yes" \
        "the consumer reloaded in this direction too"
    uat_assert_matches "routes left mounted +0" "shutdown left no route mounted"
}

step_3() {
    uat_step 3 "${STEP_TITLES[3]}"
    uat_explain "The measured lines show what happened; --verbose says why it is allowed to happen. It is worth running once because the claim an operator most needs to believe - that the consumer was never failed and never asked to cooperate - is prose, not a number."
    uat_expect "exit 0, the same result lines as step 1, plus narration stating the consumer did not fail and was not asked to do anything."

    uat_run 0 "${UAT_BASE_CMD[@]}" "$DEMO" --source "$MEMORY_STORE" --target "$SQLITE_STORE" --verbose
    uat_assert_contains "The consumer did not fail" \
        "the narration states the consumer was never failed"
    uat_assert_contains "the consumer reloaded against the new provider yes" \
        "the verbose run reaches the same result as the quiet one"
}

step_4() {
    uat_step 4 "${STEP_TITLES[4]}"
    uat_explain "The argument validation. An unknown provider name is an invocation error, distinct from a swap that ran and disagreed, and it has to be refused before the application is built rather than starting on a store that does not exist."
    uat_expect "exit 2, argparse naming the valid choices, and no swap performed."

    uat_run 2 "${UAT_BASE_CMD[@]}" "$DEMO" --source "$UNKNOWN_STORE"
    uat_assert_contains "invalid choice" "the unknown provider is reported as an invalid choice"
    uat_assert_contains "$UNKNOWN_STORE" "the rejected value is named back"
    uat_assert_not_contains "the consumer reloaded" "no swap was performed"
}

step_5() {
    uat_step 5 "${STEP_TITLES[5]}"
    uat_explain "The other invocation error: a flag whose value was forgotten. It must be reported as a missing argument rather than silently falling back to the default target, which would run a different swap than the operator asked for."
    uat_expect "exit 2, argparse reporting the expected argument, and no swap performed."

    uat_run 2 "${UAT_BASE_CMD[@]}" "$DEMO" --target
    uat_assert_contains "expected one argument" "the missing value is reported"
    uat_assert_not_contains "the consumer reloaded" "no swap was performed"
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
        uat_list_steps "hotswap.sh"
        exit "$UAT_EXIT_OK"
    fi

    uat_resolve_range "$LAST_STEP"

    uat_banner "UAT walkthrough: run_hotswap.py - replacing a provider under a live consumer"
    printf '  guide  : hotswap.md\n'
    printf '  runner : %s\n' "${UAT_BASE_CMD[*]}"
    printf '  steps  : %s\n' "$(uat_step_range)"

    [[ "$UAT_DO_SEED" -eq 1 ]] && uat_seed

    uat_run_steps

    # Nothing to clean: every step above runs the demo, which uses an in-process
    # sqlite store and writes no files. --cleanup is honored anyway so the flag
    # means the same thing in every guide of this suite.
    [[ "$UAT_DO_CLEANUP" -eq 1 ]] && uat_cleanup

    uat_summary
}

main "$@"
