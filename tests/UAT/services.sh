#!/usr/bin/env bash
#
# services.sh - guided, self-checking walkthrough of services.md.
#
# Runs every check the guide describes for examples/run_services.py: each step
# explains what it exercises, echoes and runs the guide's exact command, shows
# the real output and the observed vs expected exit code, asserts the
# expectation, and waits for a keypress before the next step.
#
# Prerequisites - the shared setup in README.md:
#   uv sync
# or, equivalently, run this script once with --seed.
#
# Read-only: every step issues one run_services.py invocation, which composes
# its contexts inside its own process and prints what each resolved to. It
# writes no files, so there is nothing for this script to clean up.
#
# Exit codes: 0 all assertions passed, 1 an assertion failed, 2 bad invocation,
# 3 a prerequisite is missing, 130 interrupted.
#
# No `set -e`: step 5 expects a non-zero exit code (see common.sh).
set -uo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

# The demo this guide drives, and the section name it must refuse. These are a
# path and an argument value inside this checkout; the demo reaches nothing
# outside the process it runs in.
readonly DEMO="examples/run_services.py"
readonly UNKNOWN_SECTION="bogus"

STEP_TITLES=(
    "Preflight - the runner, the demo, and a runtime it can import"
    "Activation follows the declarations, not the composition order"
    "A dependency stays readable through the dependent's own teardown"
    "Two ways to read a coeffect, and two ways to be refused"
    "Isolating a key gives two contexts independent bindings"
    "An unknown section name is refused"
)
readonly LAST_STEP=$((${#STEP_TITLES[@]} - 1))

usage() {
    cat <<'USAGE'
services.sh - guided walkthrough of services.md

Usage:
  ./services.sh [options]

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

Nothing to clean: every step runs examples/run_services.py, which prints its
measurements and writes no files, so this guide leaves nothing behind.
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
    ./services.sh --seed"
    fi
    uat_pass "the demo starts and the runtime imports"
}

step_1() {
    uat_step 1 "${STEP_TITLES[1]}"
    uat_explain "Declaring a need instead of fetching a dependency is what lets the runtime decide when a component may run. Composed before its provider, the consumer holds PENDING rather than raising; when the provider appears it activates itself; when the provider is retired it deactivates. Nobody wrote that sequence - it falls out of the declaration."
    uat_expect "exit 0; PENDING before the provider, ACTIVE after it, apply having run exactly once, INACTIVE when the provider is retired, ACTIVE again for a replacement, and both provider values in the load history."

    uat_run 0 "${UAT_BASE_CMD[@]}" "$DEMO" --section activation
    uat_assert_matches "consumer composed before the provider +PENDING" \
        "a consumer composed before its provider waits instead of failing"
    uat_assert_matches "after the provider is composed in +ACTIVE" \
        "it activates itself once the provider is there"
    uat_assert_matches "times its apply has run +1" \
        "apply ran exactly once - no speculative re-run"
    uat_assert_matches "after the provider is retired +INACTIVE" \
        "retiring the provider deactivates the consumer"
    uat_assert_matches "after a replacement provider arrives +ACTIVE" \
        "a replacement provider reactivates it"
    uat_assert_contains "['first', 'second']" \
        "it loaded against both providers in turn"
}

step_2() {
    uat_step 2 "${STEP_TITLES[2]}"
    uat_explain "The ordering guarantee that makes a correct teardown writable at all. A component being retired can still read the dependency it was using, so its inverse can hand back exactly what it took. If the binding were cleared first, every release would be a guess."
    uat_expect "exit 0; the trace reads 'acquired against first | released against first', the consumer ends INACTIVE, and the binding is cleared only afterwards."

    uat_run 0 "${UAT_BASE_CMD[@]}" "$DEMO" --section ordering
    uat_assert_contains "['row from the consumer']" \
        "the consumer did real work against the dependency"
    uat_assert_contains "acquired against first | released against first" \
        "it released against the same provider it acquired against"
    uat_assert_matches "consumer state +INACTIVE" \
        "the consumer is INACTIVE once the provider is gone"
    uat_assert_matches "the store binding afterwards +None" \
        "the binding was cleared only after the teardown read it"
}

step_3() {
    uat_step 3 "${STEP_TITLES[3]}"
    uat_explain "Two reads with deliberately different contracts. ctx.store is the checked one: reading a key you never declared, or one whose declaring fiber is no longer live, raises a named error rather than handing back something stale. ctx.get is the unchecked one and answers None. A silent None in the first case is exactly how a stale reference outlives the thing it points at."
    uat_expect "exit 0; UndeclaredAccessError for the undeclared read, InactiveAccessError for the read against an inactive fiber, and ctx.get answering None in both of its miss cases."

    uat_run 0 "${UAT_BASE_CMD[@]}" "$DEMO" --section access
    uat_assert_matches "ctx.store inside a component that declared it +first" \
        "a declared read resolves"
    uat_assert_contains "UndeclaredAccessError" \
        "an undeclared read raises rather than returning None"
    uat_assert_contains "InactiveAccessError" \
        "a read against an inactive declaring fiber raises"
    uat_assert_matches "ctx.get\('store'\) from the root +first" \
        "the unchecked read resolves from the root"
    uat_assert_matches "ctx.get\('missing'\) from the root +None" \
        "the unchecked read answers None for a key nobody provides"
}

step_4() {
    uat_step 4 "${STEP_TITLES[4]}"
    uat_explain "Isolation is what makes the same declaration mean different things in different subtrees - two tenants, a test double beside the real thing - without either side knowing. The realms differ, so the two bindings coexist rather than one overwriting the other."
    uat_expect "exit 0; the root reader sees ['shared'] while the isolated reader sees ['isolated'], the two realms are not the same object, and the store holds 2 bindings."

    uat_run 0 "${UAT_BASE_CMD[@]}" "$DEMO" --section isolation
    uat_assert_contains "['shared']" "the reader on the root context sees the shared binding"
    uat_assert_contains "['isolated']" "the reader on the isolated context sees its own"
    uat_assert_matches "root.get\('store'\) +shared" "the root resolves to the shared provider"
    uat_assert_matches "branch.get\('store'\) +isolated" "the branch resolves to the isolated one"
    uat_assert_matches "realms are the same object +False" "the two realms are distinct"
    uat_assert_matches "bindings in the store +2" "both bindings coexist - neither overwrote the other"
}

step_5() {
    uat_step 5 "${STEP_TITLES[5]}"
    uat_explain "The argument validation. An unknown section name is an invocation error, distinct from a section that ran and disagreed, and it has to be refused before any context is composed rather than printing an empty section."
    uat_expect "exit 2, argparse naming the valid choices, and no section output."

    uat_run 2 "${UAT_BASE_CMD[@]}" "$DEMO" --section "$UNKNOWN_SECTION"
    uat_assert_contains "invalid choice" "the unknown section is reported as an invalid choice"
    uat_assert_contains "$UNKNOWN_SECTION" "the rejected value is named back"
    uat_assert_not_contains "PENDING" "no section was printed"
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
        uat_list_steps "services.sh"
        exit "$UAT_EXIT_OK"
    fi

    uat_resolve_range "$LAST_STEP"

    uat_banner "UAT walkthrough: run_services.py - coeffects"
    printf '  guide  : services.md\n'
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
