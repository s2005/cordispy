#!/usr/bin/env bash
#
# loader.sh - guided, self-checking walkthrough of loader.md.
#
# Runs every check the guide describes for examples/run_loader.py: each step
# explains what it exercises, echoes and runs the guide's exact command, shows
# the real output and the observed vs expected exit code, asserts the
# expectation, and waits for a keypress before the next step.
#
# Prerequisites - the shared setup in README.md:
#   uv sync
# or, equivalently, run this script once with --seed.
#
# This guide WRITES: step 3 passes --workspace/--keep-workspace so the demo
# leaves its generated plugin package on disk for step 4 to read. Step 6 removes
# exactly that package, and --keep-output skips the removal.
#
# Exit codes: 0 all assertions passed, 1 an assertion failed, 2 bad invocation,
# 3 a prerequisite is missing, 130 interrupted.
#
# No `set -e`: step 5 expects a non-zero exit code (see common.sh).
set -uo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

# The demo and the documents it realizes. Every path is inside this checkout -
# the demo reaches nothing outside the process it runs in, so there is no host
# or account name to keep synthetic. MISSING_CONFIG names a file that must NOT
# exist; it is the negative case in step 5.
readonly DEMO="examples/run_loader.py"
readonly YAML_CONFIG="examples/config/app.yaml"
readonly JSON_CONFIG="examples/config/app.json"
readonly MISSING_CONFIG="examples/config/nope.yaml"

# The package name run_loader.py generates inside the workspace. The workspace
# itself is UAT_WORKSPACE, set in project.sh and overridable with --workspace.
readonly HOT_PACKAGE="hot_plugins"

STEP_TITLES=(
    "Preflight - the runner, the demo, and both configuration documents"
    "Reconciliation is incremental: only changed entries are replaced"
    "The JSON document produces the same application as the YAML one"
    "A hot reload rebuilds one entry, and a failed reload rolls back"
    "The rollback restored modules, not files"
    "A configuration file that does not exist is refused"
    "Cleanup - remove the generated plugin package"
)
readonly LAST_STEP=$((${#STEP_TITLES[@]} - 1))

usage() {
    cat <<'USAGE'
loader.sh - guided walkthrough of loader.md

Usage:
  ./loader.sh [options]

Options:
  --auto, --no-pause     Run every step back-to-back with no keypress prompt
  --stop-on-fail         Abort at the first failed step (default: run them all)
  --from-step N          Start at step N, skipping the earlier ones
  --only-step N          Run exactly one step
  --list-steps           Print the step index and exit
  --runner CMD           How to launch the demo (default: "uv run python")
  --workspace PATH       Where the demo writes its generated plugin package
                         (default: output/uat-hmr)
  --keep-output          Keep that generated package instead of removing it
  --seed                 Resolve the project environment (uv sync) first
  --cleanup              Remove the generated package after the walkthrough
  --help                 Show this help and exit

Prerequisites: uv on PATH and the project environment resolved, or pass --seed.
See README.md.

This guide writes: step 3 leaves a generated plugin package under the workspace
so step 4 can read it, and step 6 removes exactly that package. To remove it by
hand: rm -rf output/uat-hmr
USAGE
}

# The generated package path. Resolved through a helper rather than a readonly
# constant because --workspace can change UAT_WORKSPACE, and because each step
# has to resolve its own inputs for --only-step N to work alone.
hot_package_dir() {
    printf '%s/%s' "$UAT_WORKSPACE" "$HOT_PACKAGE"
}

# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

step_0() {
    uat_step 0 "${STEP_TITLES[0]}"
    uat_explain "Everything below needs a Python launcher, the demo file, an environment where the cordis package imports, and both committed configuration documents. Checking them once here turns a missing prerequisite into one clear message instead of six confusing step failures."
    uat_expect "the runner is on PATH, $DEMO and both configuration documents exist, and the demo answers --help. If the environment is not resolved, this exits 3 naming the seed command."

    uat_require_cmd "${UAT_BASE_CMD[0]}" "Install uv (https://docs.astral.sh/uv/), or pass --runner with an interpreter that can import cordis, e.g. --runner python."
    uat_pass "runner is on PATH: ${UAT_BASE_CMD[*]}"

    uat_require_file "$DEMO" "Run this from a cordispy checkout; the guide resolves paths from the repo root."
    uat_pass "demo exists: $DEMO"

    uat_require_file "$YAML_CONFIG" "It is committed in this checkout; a missing copy means the working tree is incomplete."
    uat_require_file "$JSON_CONFIG" "It is committed in this checkout; a missing copy means the working tree is incomplete."
    uat_pass "both configuration documents exist: $YAML_CONFIG, $JSON_CONFIG"

    uat_run_quiet "${UAT_BASE_CMD[@]}" "$DEMO" --help
    if [[ "$UAT_RC" -ne 0 ]]; then
        printf '%s\n' "$UAT_OUT"
        uat_fatal "$UAT_EXIT_PREREQ" \
            "the demo could not start (exit $UAT_RC). Resolve the environment first:
    ./loader.sh --seed"
    fi
    uat_pass "the demo starts and the runtime imports"
}

step_1() {
    uat_step 1 "${STEP_TITLES[1]}"
    uat_explain "Four revisions of one document: disable an entry, reconfigure another, add a child to a group. A registry that rebuilds the world on every config change would pass a functional test of this and still be useless in production. The fiber ids are the evidence, because an id only changes when a fiber was actually rebuilt - store and tools keep the ids they were given at the first revision throughout."
    uat_expect "exit 0; each revision names exactly the fibers it replaced and the ones it left alone, the runtime goes from 4 fibers to 5 when one child is added, and the run ends with 'every claim above held'."

    uat_run 0 "${UAT_BASE_CMD[@]}" "$DEMO" --config "$YAML_CONFIG" --skip-hmr
    uat_assert_matches "fibers replaced +counter" "disabling an entry replaced only that entry"
    uat_assert_matches "fibers untouched +store, tools, greeter" "its sibling and its group were untouched"
    uat_assert_matches "fibers replaced +greeter" "reconfiguring an entry replaced only that entry"
    uat_assert_matches "fibers in runtime +4 -> 5" "adding one child created exactly one fiber"
    uat_assert_contains "{'store': 1, 'tools': 2, 'greeter': 5, 'counter': None, 'echo': 6}" \
        "store and tools kept their original ids - they were never restarted"
    uat_assert_not_contains "PART B" "--skip-hmr stopped after the reconciliation half"
    uat_assert_contains "every claim above held" "every reconciliation claim held"
}

step_2() {
    uat_step 2 "${STEP_TITLES[2]}"
    uat_explain "The configuration format is not part of the paradigm. Realizing the JSON document has to produce the same fiber tree as the YAML one, and the demo checks that itself by parsing the companion document alongside whichever one it was given."
    uat_expect "exit 0, the header naming app.json, the companion document named as app.yaml, and the claim that both parse identically."

    uat_run 0 "${UAT_BASE_CMD[@]}" "$DEMO" --config "$JSON_CONFIG" --skip-hmr
    uat_assert_contains "PART A -- declarative reconciliation from app.json" \
        "the JSON document was the one realized"
    uat_assert_matches "companion document +app.yaml" "the YAML document was parsed alongside it"
    uat_assert_matches "claim holds +the YAML and JSON documents parse identically" \
        "both documents describe the same application"
    uat_assert_contains "every claim above held" "every reconciliation claim held for JSON too"
}

step_3() {
    uat_step 3 "${STEP_TITLES[3]}"
    uat_explain "Hot module replacement over a bounded import graph. The first reload edits one source file: only that module is accepted, cordis and typing are declined as externals, and the entry that nobody edited keeps both its fiber and the state it was holding. The second reload introduces a syntax error, and the point is that the failure is transactional - it reaches the caller instead of being swallowed, the previous modules come back from the backup, and the route keeps answering."
    uat_expect "exit 0; only hot_plugins.greeter is accepted and only greeter is stale, state keeps its fiber, /greet answers with the new code, then the SyntaxError reaches the caller, one rollback is recorded, /greet still answers, and the run ends with 'every claim above held'."

    local workspace="$UAT_WORKSPACE"
    uat_run 0 "${UAT_BASE_CMD[@]}" "$DEMO" --config "$YAML_CONFIG" \
        --workspace "$workspace" --keep-workspace
    uat_assert_contains "PART B -- hot module replacement over a bounded import graph" \
        "the hot-reload half ran"
    uat_assert_matches "accepted modules +hot_plugins.greeter" "only the edited module was accepted"
    uat_assert_matches "declined modules +cordis, cordis.Context" "the externals were declined"
    uat_assert_matches "stale entries +greeter" "only the edited module's entry was stale"
    uat_assert_matches "fibers untouched +state" "the module nobody edited kept its fiber"
    uat_assert_matches "reply from /greet +welcome, world" "the new code is the one serving"
    uat_assert_contains "hot reload failed; rolling back to the previous modules" \
        "the failed reload announced the rollback"
    uat_assert_matches "re-import raised +SyntaxError" \
        "the failure reached the caller instead of being swallowed"
    uat_assert_matches "rollbacks so far +1" "exactly one rollback was recorded"
    uat_assert_contains "workspace kept at" "the generated package was left on disk for step 4"
    uat_assert_contains "every claim above held" "every reconciliation and reload claim held"
}

step_4() {
    uat_step 4 "${STEP_TITLES[4]}"
    uat_explain "The scope of the transaction, stated precisely. Step 3's rollback restored the module graph the runtime owns; it did not rewrite the operator's file. Reading the file back is the only way to see that distinction, and it is the one an operator most needs to understand before trusting a hot reload in production."
    uat_expect "exit 0, and the generated greeter.py on disk is still the deliberately broken version - a def line with no colon - even though the fiber went on serving the previous good one."

    local greeter
    greeter="$(hot_package_dir)/greeter.py"
    if [[ ! -f "$greeter" ]]; then
        uat_skip "no generated package at $greeter - run step 3 first (it is what writes it)"
        return
    fi

    uat_run 0 cat "$greeter"
    uat_assert_contains "A deliberate syntax error" "the file is the broken version the demo wrote"
    uat_assert_contains "def greeter(ctx, config)" "the def line is still missing its colon"
    uat_assert_not_contains "GREETING = " "it is not the good version that was serving"
}

step_5() {
    uat_step 5 "${STEP_TITLES[5]}"
    uat_explain "The argument validation. A configuration path that does not resolve is an invocation error, distinct from a document that parsed and reconciled badly, and it has to be refused before any fiber is built rather than realizing an empty tree."
    uat_expect "exit 2, the resolved path named in the message, and no fiber tree printed."

    uat_run 2 "${UAT_BASE_CMD[@]}" "$DEMO" --config "$MISSING_CONFIG"
    uat_assert_contains "no such configuration file" "the missing document is reported"
    uat_assert_contains "nope.yaml" "the path it tried is named back"
    uat_assert_not_contains "fiber tree" "no fiber tree was realized"
}

step_6() {
    uat_step 6 "${STEP_TITLES[6]}"
    uat_explain "Step 3 asked the demo to leave its generated package on disk so step 4 could read it. Removing it here keeps the guide rerunnable and the checkout clean. The package directory is created wholesale by the demo and holds nothing else, including a __pycache__ whose filenames the interpreter chooses, so it is removed as the one exact path this guide wrote."
    uat_expect "exit 0, and no generated package remains under the workspace. With --keep-output this step is skipped instead."

    local package
    package="$(hot_package_dir)"

    if [[ "$UAT_KEEP_OUTPUT" -eq 1 ]]; then
        uat_skip "--keep-output was passed, so $package is left in place"
        return
    fi

    if [[ ! -e "$package" ]]; then
        uat_note "nothing to remove: $package does not exist, so step 3 either did not run or already cleaned up."
        uat_pass "no generated package is left behind"
        return
    fi

    uat_run 0 rm -rf -- "$package"
    # The workspace and its parent are removed only when empty, so a --workspace
    # pointing at a directory that holds something else is left alone.
    rmdir -- "$UAT_WORKSPACE" 2>/dev/null
    rmdir -- "$(dirname "$UAT_WORKSPACE")" 2>/dev/null

    if [[ -e "$package" ]]; then
        uat_fail "$package is still present after the removal"
    else
        uat_pass "the generated package under $UAT_WORKSPACE is gone"
    fi
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
        uat_list_steps "loader.sh"
        exit "$UAT_EXIT_OK"
    fi

    uat_resolve_range "$LAST_STEP"

    uat_banner "UAT walkthrough: run_loader.py - declarative reconciliation and hot reload"
    printf '  guide     : loader.md\n'
    printf '  runner    : %s\n' "${UAT_BASE_CMD[*]}"
    printf '  workspace : %s\n' "$UAT_WORKSPACE"
    printf '  steps     : %s\n' "$(uat_step_range)"

    [[ "$UAT_DO_SEED" -eq 1 ]] && uat_seed

    uat_run_steps

    # Step 6 is the cleanup, so --cleanup is only a belt-and-braces repeat for an
    # operator who ran a partial range that stopped before it.
    [[ "$UAT_DO_CLEANUP" -eq 1 ]] && uat_cleanup

    uat_summary
}

main "$@"
