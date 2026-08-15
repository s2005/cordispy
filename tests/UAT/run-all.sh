#!/usr/bin/env bash
#
# run-all.sh - sequential roster runner for every guided UAT walkthrough beside
# it.
#
# Runs the roster below strictly one guide at a time - never in parallel, never
# in the background. The guides share one checkout and one generated workspace,
# and nothing serializes concurrent access to them, so two overlapping runs
# would corrupt each other. That is the whole reason this is a plain sequential
# loop rather than anything fancier.
#
# For each guide: print a banner, run ./<name>.sh --auto <passthrough>, capture
# its exit code, and carry on to the next guide regardless of the result. At the
# end print a roster table - one row per guide with its exit code and a verdict -
# then exit.
#
# Exit-code semantics per guide (see common.sh for where these codes come from):
#   0   -> PASS
#   3   -> SKIPPED (prerequisite) - does NOT fail the roster. A guide that
#          cannot find uv or a resolved environment reports 3, and treating
#          that as a failure would hide the one thing the operator has to fix.
#   130 -> the operator pressed Ctrl-C. Stop the whole run immediately and exit
#          130 - do not carry on through the remaining guides.
#   *   -> FAIL, recorded, and the loop continues to the next guide.
#
# run-all.sh itself exits 0 when no guide FAILed, 1 otherwise.
#
# No `set -e`: a guide's own non-zero exit is the normal, expected case this
# script exists to observe and report, not an error to abort on.
set -uo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

# The roster order is deliberate, not alphabetical:
#   benefit  - the headline comparison; run it first so a roster that is going
#              to fail fails on the claim everything else supports.
#   calculator - the same claim at the scale of one small application, where the
#              failure is a calculator that lies about what it can do. Second,
#              because it assumes the benefit comparison is understood.
#   effects  - the mechanism behind benefit's residue numbers.
#   services - the mechanism behind benefit's activation and swap numbers.
#   hotswap  - those two mechanisms at the scale an operator sees.
#   loader   - the same, plus hot module replacement. Last because it is the
#              only guide that writes to disk, so an interrupted roster leaves
#              the checkout clean.
readonly ROSTER=(
    benefit
    calculator
    effects
    services
    hotswap
    loader
)
readonly TOTAL_GUIDES=${#ROSTER[@]}

# Set out of UAT_REST in main(): non-empty restricts the run to exactly one
# guide instead of the full roster.
RUN_ALL_ONLY=""

usage() {
    cat <<'USAGE'
run-all.sh - run every guided UAT walkthrough in this directory, one at a time

Runs the roster below strictly one guide at a time - never in parallel, never
in the background - because the guides share one checkout and one generated
workspace, and nothing serializes concurrent access to them.

Usage:
  ./run-all.sh [options]

Options:
  --auto, --no-pause     Accepted for consistency with every other UAT script.
                         run-all.sh always passes --auto to each guide it runs,
                         regardless of this flag: a roster run is unattended by
                         definition, and a per-step keypress prompt in the
                         middle of the roster would hang it.
  --stop-on-fail         Passed through to every guide, so each guide's OWN step
                         loop stops at its first failed step. Does NOT stop the
                         roster itself: the roster always runs every guide and
                         reports every result (use --only to run just one).
  --runner CMD           Passed through to every guide: how to launch the demos
                         (default: "uv run python").
  --workspace PATH       Passed through to the loader guide, the only one that
                         writes (default: output/uat-hmr).
  --keep-output          Passed through to every guide: keep the files a guide
                         writes instead of removing them.
  --only NAME            Run exactly one guide from the roster instead of all
                         of them.
  --list, --list-steps   Print the roster order and exit 0.
  --help                 Show this help and exit

Prerequisites: the same shared setup every guide in the roster assumes - uv on
PATH and the project environment resolved. See README.md.

--seed, --cleanup, --from-step and --only-step are guide-internal flags - they
address one guide's own fixture or step numbers, not the roster as a whole -
and are not accepted here. Run the specific guide script directly for those
(e.g. ./benefit.sh --seed), or use --only NAME to run just one guide through
this runner.

Exit codes: 0 when no guide FAILed (a SKIPPED guide does not count as a
failure), 1 when at least one guide FAILed, 130 if interrupted (stops the
roster immediately; no further guides run).
USAGE
}

print_roster() {
    printf 'UAT guide roster (run in this order):\n\n'
    local name
    for name in "${ROSTER[@]}"; do
        printf '  %s\n' "$name"
    done
}

# ---------------------------------------------------------------------------
# Roster tracking and the final report
#
# Populated by the run loop in main(). Declared at top level (not `local`) so
# print_report() - called both mid-run on an interrupt and at the very end -
# always sees the same arrays and counters.
# ---------------------------------------------------------------------------
GUIDE_NAMES=()
GUIDE_EXITS=()
GUIDE_VERDICTS=()
GUIDE_PASSED=0
GUIDE_SKIPPED=0
GUIDE_FAILED=0

print_report() {
    uat_banner "UAT roster summary"
    printf '%-17s %4s  %s\n' "Guide" "Exit" "Verdict"
    printf '%-17s %4s  %s\n' "-----------------" "----" "-------------------------"
    local i
    for ((i = 0; i < ${#GUIDE_NAMES[@]}; i++)); do
        printf '%-17s %4s  %s\n' "${GUIDE_NAMES[$i]}" "${GUIDE_EXITS[$i]}" "${GUIDE_VERDICTS[$i]}"
    done
    printf '\n%s guides: %s passed, %s skipped, %s failed\n' \
        "${#GUIDE_NAMES[@]}" "$GUIDE_PASSED" "$GUIDE_SKIPPED" "$GUIDE_FAILED"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

main() {
    uat_init
    # run-all.sh has no steps of its own - it drives the per-guide scripts, each
    # of which owns its own step loop and step count.

    local arg
    for arg in "$@"; do
        if [[ "$arg" == "--help" || "$arg" == "-h" ]]; then
            usage
            exit "$UAT_EXIT_OK"
        fi
    done

    uat_parse_common_args "$@"

    if [[ ${#UAT_REST[@]} -gt 0 ]]; then
        local i=0
        while [[ $i -lt ${#UAT_REST[@]} ]]; do
            case "${UAT_REST[$i]}" in
                --only)
                    i=$((i + 1))
                    if [[ $i -ge ${#UAT_REST[@]} ]]; then
                        uat_fatal "$UAT_EXIT_USAGE" "--only needs a guide name (see --help)"
                    fi
                    RUN_ALL_ONLY="${UAT_REST[$i]}"
                    ;;
                --list)
                    UAT_LIST_ONLY=1
                    ;;
                *)
                    uat_fatal "$UAT_EXIT_USAGE" "unknown argument: ${UAT_REST[$i]} (see --help)"
                    ;;
            esac
            i=$((i + 1))
        done
    fi

    if [[ "$UAT_LIST_ONLY" -eq 1 ]]; then
        print_roster
        exit "$UAT_EXIT_OK"
    fi

    # --seed/--cleanup/--from-step/--only-step address one guide's own fixture or
    # step numbers, not the roster as a whole. A silent no-op here would be more
    # confusing than refusing outright - see usage().
    if [[ "$UAT_DO_SEED" -eq 1 || "$UAT_DO_CLEANUP" -eq 1 ]]; then
        uat_fatal "$UAT_EXIT_USAGE" \
            "--seed/--cleanup are guide-internal flags and are not accepted by run-all.sh; run the specific guide script directly (e.g. ./benefit.sh --seed), see --help"
    fi
    if [[ -n "$UAT_FROM_STEP" || -n "$UAT_ONLY_STEP" ]]; then
        uat_fatal "$UAT_EXIT_USAGE" \
            "--from-step/--only-step address steps inside one guide and are not accepted by run-all.sh; use --only NAME to run a single guide, see --help"
    fi

    if [[ -n "$RUN_ALL_ONLY" ]]; then
        local valid=0 candidate
        for candidate in "${ROSTER[@]}"; do
            if [[ "$candidate" == "$RUN_ALL_ONLY" ]]; then
                valid=1
                break
            fi
        done
        if [[ "$valid" -eq 0 ]]; then
            uat_fatal "$UAT_EXIT_USAGE" \
                "--only: '$RUN_ALL_ONLY' is not a guide in the roster. Valid names: ${ROSTER[*]}"
        fi
    fi

    local -a names=("${ROSTER[@]}")
    [[ -n "$RUN_ALL_ONLY" ]] && names=("$RUN_ALL_ONLY")

    uat_banner "UAT roster: all guided walkthroughs"
    printf '  runner : %s\n' "${UAT_BASE_CMD[*]}"
    if [[ -n "$RUN_ALL_ONLY" ]]; then
        printf '  only   : %s\n' "$RUN_ALL_ONLY"
    else
        printf '  guides : %s (in roster order)\n' "$TOTAL_GUIDES"
    fi

    local name guide_path rc verdict
    local -a guide_args
    for name in "${names[@]}"; do
        guide_path="$UAT_LIB_DIR/${name}.sh"

        guide_args=(--runner "$UAT_RUNNER")
        [[ "$UAT_STOP_ON_FAIL" -eq 1 ]] && guide_args+=(--stop-on-fail)
        [[ "$UAT_KEEP_OUTPUT" -eq 1 ]] && guide_args+=(--keep-output)
        # loader is the only guide that writes, so it is the only one the
        # workspace path means anything to.
        if [[ "$name" == "loader" ]]; then
            guide_args+=(--workspace "$UAT_WORKSPACE")
        fi

        uat_banner "Guide: $name"
        uat_show_command bash "$guide_path" --auto "${guide_args[@]}"
        printf '\n'

        bash "$guide_path" --auto "${guide_args[@]}"
        rc=$?

        GUIDE_NAMES+=("$name")
        GUIDE_EXITS+=("$rc")

        if [[ "$rc" -eq 130 ]]; then
            GUIDE_VERDICTS+=("INTERRUPTED")
            printf '\n%sinterrupted - guide %s exited 130; stopping the roster immediately%s\n' \
                "$UAT_YELLOW" "$name" "$UAT_NC" >&2
            print_report
            exit 130
        fi

        case "$rc" in
            0)
                verdict="PASS"
                GUIDE_PASSED=$((GUIDE_PASSED + 1))
                ;;
            3)
                verdict="SKIPPED (prerequisite)"
                GUIDE_SKIPPED=$((GUIDE_SKIPPED + 1))
                ;;
            *)
                verdict="FAIL"
                GUIDE_FAILED=$((GUIDE_FAILED + 1))
                ;;
        esac
        GUIDE_VERDICTS+=("$verdict")
    done

    print_report

    if [[ "$GUIDE_FAILED" -gt 0 ]]; then
        exit "$UAT_EXIT_FAIL"
    fi
    exit "$UAT_EXIT_OK"
}

main "$@"
