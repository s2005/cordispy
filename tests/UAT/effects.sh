#!/usr/bin/env bash
#
# effects.sh - guided, self-checking walkthrough of effects.md.
#
# Runs every check the guide describes for examples/run_effects.py: each step
# explains what it exercises, echoes and runs the guide's exact command, shows
# the real output and the observed vs expected exit code, asserts the
# expectation, and waits for a keypress before the next step.
#
# Prerequisites - the shared setup in README.md:
#   uv sync
# or, equivalently, run this script once with --seed.
#
# Read-only: every step issues one run_effects.py invocation, which builds its
# components inside its own process and prints what they acquired and released.
# It writes no files, so there is nothing for this script to clean up.
#
# Exit codes: 0 all assertions passed, 1 an assertion failed, 2 bad invocation,
# 3 a prerequisite is missing, 130 interrupted.
#
# No `set -e`: step 6 expects a non-zero exit code (see common.sh).
set -uo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

# The demo this guide drives, and the section name it must refuse. These are a
# path and an argument value inside this checkout; the demo reaches nothing
# outside the process it runs in.
readonly DEMO="examples/run_effects.py"
readonly UNKNOWN_SECTION="bogus"

STEP_TITLES=(
    "Preflight - the runner, the demo, and a runtime it can import"
    "All five callback forms are accepted, and a generator unwinds in reverse"
    "Recovery is last-applied-first and happens at most once"
    "An interrupted effect keeps only what it had accumulated"
    "An effect belongs to the fiber that created it"
    "The built-in timer and bus services leave nothing behind"
    "An unknown section name is refused"
)
readonly LAST_STEP=$((${#STEP_TITLES[@]} - 1))

usage() {
    cat <<'USAGE'
effects.sh - guided walkthrough of effects.md

Usage:
  ./effects.sh [options]

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

Nothing to clean: every step runs examples/run_effects.py, which prints its
measurements and writes no files, so this guide leaves nothing behind.
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
    ./effects.sh --seed"
    fi
    uat_pass "the demo starts and the runtime imports"
}

step_1() {
    uat_step 1 "${STEP_TITLES[1]}"
    uat_explain "An effect is a pair - do this, and here is how to undo it - written in one place. The five accepted forms are what make that ergonomic enough to actually use: a plain callback, a callback returning a disposer, the async version, and the two generator forms where each yield is one more inverse pushed onto the stack."
    uat_expect "exit 0; a callback returning None recovers [], the disposer and async forms recover ['recovered'], and both generator forms unwind step 2 before step 1."

    uat_run 0 "${UAT_BASE_CMD[@]}" "$DEMO" --section forms
    uat_assert_contains "applied ['applied'] then []" \
        "a callback returning None recovers nothing"
    uat_assert_contains "applied ['applied'] then ['recovered']" \
        "the disposer form recovers what it acquired"
    uat_assert_contains "async def cb() -> Disposer | None" \
        "the async disposer form is accepted"
    uat_assert_contains "['recovered step 2', 'recovered step 1']" \
        "a generator effect unwinds its steps in reverse"
    uat_assert_contains "async def cb() -> AsyncGenerator[Disposer]" \
        "the async generator form is accepted"
}

step_2() {
    uat_step 2 "${STEP_TITLES[2]}"
    uat_explain "Order is not cosmetic: an inverse frequently depends on something an earlier effect acquired, so recovery has to run last-applied-first or it tears down the ground it is standing on. The at-most-once guarantee is the other half - a disposer reached twice through two paths must still run once."
    uat_expect "exit 0; applied effect-0..effect-3, recovered undo-3..undo-0 in exactly the reverse order, and three calls to one disposer producing 1 recovery run."

    uat_run 0 "${UAT_BASE_CMD[@]}" "$DEMO" --section order
    uat_assert_matches "applied +effect-0 effect-1 effect-2 effect-3" \
        "the four effects applied in order"
    uat_assert_matches "recovered +undo-3 undo-2 undo-1 undo-0" \
        "recovery ran last-applied-first - exactly the reverse"
    uat_assert_matches "three calls to the same disposer +1 recovery run" \
        "a disposer called three times recovered once"
}

step_3() {
    uat_step 3 "${STEP_TITLES[3]}"
    uat_explain "This is the case a hand-written teardown gets wrong. An effect interrupted partway has acquired some things and not others, and the only correct recovery is the prefix it actually reached. Recovering more would undo what was never done; recovering less is the residue benefit.md counts."
    uat_expect "exit 0; the generator is closed at the guard, the two inverses it had accumulated run as undo-b then undo-a, and the step after the guard never ran."

    uat_run 0 "${UAT_BASE_CMD[@]}" "$DEMO" --section guard
    uat_assert_matches "after the guard tripped +generator closed" \
        "the interrupted generator was closed rather than abandoned"
    uat_assert_matches "inverses that ran +undo-b undo-a" \
        "exactly the two inverses it had accumulated ran, newest first"
    uat_assert_matches "the step after the guard +never ran" \
        "nothing past the guard was applied, so nothing past it was recovered"
}

step_4() {
    uat_step 4 "${STEP_TITLES[4]}"
    uat_explain "The ownership rule that makes the rest work. An effect is recorded on the fiber, not on the call that created it, so one created long after apply() returned - inside a request handler, say - is still on the accumulator when the component is retired. That is precisely the resource a conventional teardown() cannot know about, because it was written before the request existed."
    uat_expect "exit 0; a second effect created after loading is registered on the fiber, both inverses run on unload newest-first, and the component ends DISPOSED."

    uat_run 0 "${UAT_BASE_CMD[@]}" "$DEMO" --section fiber
    uat_assert_matches "component state +ACTIVE" \
        "the component is ACTIVE before the unload"
    uat_assert_matches "a second effect created after loading +registered nowhere but on the fiber" \
        "an effect created after apply() returned still belongs to the fiber"
    uat_assert_matches "recovered on unload +undo-created-later undo-at-load-time" \
        "both inverses ran on unload, the later-created one first"
    uat_assert_matches "component state +DISPOSED" \
        "the component reached DISPOSED"
}

step_5() {
    uat_step 5 "${STEP_TITLES[5]}"
    uat_explain "The built-in services are held to the same rule as user code: arming a timer and subscribing to the bus are ordinary effects. Retiring the worker therefore disarms and unsubscribes without the worker's author writing any of it, and the pending-task delta going back to zero is what proves no orphaned asyncio task survives."
    uat_expect "exit 0; while ACTIVE the worker shows 2 timers, 1 subscriber and a task delta of 2, and after it is DISPOSED all three are 0."

    uat_run 0 "${UAT_BASE_CMD[@]}" "$DEMO" --section plugins
    uat_assert_matches "worker state +ACTIVE" "the worker loaded"
    uat_assert_matches "timers armed +2" "2 timers were armed while it was active"
    uat_assert_matches "bus subscribers +1" "1 bus subscriber while it was active"
    uat_assert_matches "pending asyncio tasks \(delta\) +2" "2 pending tasks while it was active"
    uat_assert_matches "after retiring the worker: state +DISPOSED" "the worker reached DISPOSED"
    uat_assert_matches "timers armed +0" "no timer survived the unload"
    uat_assert_matches "bus subscribers +0" "no bus subscriber survived the unload"
    uat_assert_matches "pending asyncio tasks \(delta\) +0" "no asyncio task survived the unload"
}

step_6() {
    uat_step 6 "${STEP_TITLES[6]}"
    uat_explain "The argument validation. An unknown section name is an invocation error, distinct from a section that ran and disagreed, and it has to be refused before any component is built rather than printing an empty section."
    uat_expect "exit 2, argparse naming the valid choices, and no section output."

    uat_run 2 "${UAT_BASE_CMD[@]}" "$DEMO" --section "$UNKNOWN_SECTION"
    uat_assert_contains "invalid choice" "the unknown section is reported as an invalid choice"
    uat_assert_contains "$UNKNOWN_SECTION" "the rejected value is named back"
    uat_assert_not_contains "recovered" "no section was printed"
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
        uat_list_steps "effects.sh"
        exit "$UAT_EXIT_OK"
    fi

    uat_resolve_range "$LAST_STEP"

    uat_banner "UAT walkthrough: run_effects.py - revertible effects"
    printf '  guide  : effects.md\n'
    printf '  runner : %s\n' "${UAT_BASE_CMD[*]}"
    printf '  steps  : %s\n' "$(uat_step_range)"

    [[ "$UAT_DO_SEED" -eq 1 ]] && uat_seed

    uat_run_steps

    # Nothing to clean: every step above runs the demo, which prints its
    # measurements and writes no files. --cleanup is honored anyway so the flag
    # means the same thing in every guide of this suite.
    [[ "$UAT_DO_CLEANUP" -eq 1 ]] && uat_cleanup

    uat_summary
}

main "$@"
