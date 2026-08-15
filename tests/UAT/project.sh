#!/usr/bin/env bash
#
# project.sh - the cordispy half of the UAT harness.
#
# common.sh sources this file; it is never executed directly. Everything here is
# this repository's business: how the demo scripts are launched, the one
# directory the suite writes into, and the seed/cleanup around it. common.sh
# stays project-agnostic so it can be copied between repositories unchanged.
#
# There is no external fixture. Every guide in this suite runs a self-contained
# Python demo out of examples/ - no database, no service, no network - so the
# only "seeding" is resolving the project's own dependencies, and the only
# artifact is the plugin package the loader guide writes on disk.

# ---------------------------------------------------------------------------
# Shared values
#
# Every value here is either a path inside this checkout or a synthetic name the
# suite invents. Nothing names a real host, account or credential, because the
# demos never reach outside the process they run in.
# ---------------------------------------------------------------------------

# uat_project_defaults
# Called at the end of uat_init, after the generic defaults and before any flag
# is parsed, so a flag parsed later overrides what is set here.
uat_project_defaults() {
    # How the examples are launched. `uv run python` resolves the project's
    # dependencies and its virtualenv on the fly, which is how README.md tells a
    # reader to run them. Override with --runner when uv is unavailable and the
    # environment is already provisioned, e.g. --runner "python".
    UAT_RUNNER="uv run python"

    # The directory the loader guide writes its throw-away plugin package into.
    # It is the only path this suite creates, and loader.sh removes exactly it.
    # Kept inside the checkout (and git-ignored) rather than in the system temp
    # directory so an operator can look at what the hot-reload demo produced.
    UAT_WORKSPACE="output/uat-hmr"

    # uat_assert_json_file needs an interpreter that can import this project;
    # point it at the same launcher the guides use.
    _uat_project_compose
}

# Recompose everything derived from a value a flag can change. Called from
# uat_project_defaults and again from uat_project_after_args, so the derived
# values always match the resolved flags.
#
# UAT_RUNNER carries a command line ("uv run python"), not a single path, so it
# has to be split into words before it can be executed. `read -ra` does that
# explicitly, which is both clearer and safer than leaving an unquoted expansion
# to the shell: no glob in the string can expand into a filename here.
_uat_project_compose() {
    read -ra UAT_BASE_CMD <<<"$UAT_RUNNER"
    UAT_PYTHON_CMD=("${UAT_BASE_CMD[@]}")
}

# uat_project_parse_arg "$@"
# Called for every argument the shared parser does not recognize, with the
# remaining arguments still in place. Claim one by setting UAT_ARG_CONSUMED to
# the number of arguments taken; leave it at 0 and the argument falls through to
# UAT_REST for the guide script to reject.
uat_project_parse_arg() {
    case "$1" in
        --runner)
            [[ $# -ge 2 ]] || uat_fatal "$UAT_EXIT_USAGE" "--runner needs a command, e.g. --runner python"
            UAT_RUNNER="$2"
            UAT_ARG_CONSUMED=2
            ;;
        --workspace)
            [[ $# -ge 2 ]] || uat_fatal "$UAT_EXIT_USAGE" "--workspace needs a directory path"
            UAT_WORKSPACE="$2"
            UAT_ARG_CONSUMED=2
            ;;
    esac
}

# uat_project_after_args
# Called once every flag is parsed. Rebuild whatever depends on a flag value.
uat_project_after_args() {
    _uat_project_compose
}

# ---------------------------------------------------------------------------
# Fixture
#
# Both must be idempotent: a guide is rerun constantly, and a seeder that fails
# against an already-seeded environment would make the whole suite single-shot.
# ---------------------------------------------------------------------------

# There is no data fixture to plant. What every guide does need is a resolved
# environment, and `uv sync` is idempotent - on an already-synced checkout it
# reports the resolution and changes nothing.
uat_seed() {
    printf 'Resolving the project environment (uv sync)...\n'
    if ! uv sync; then
        uat_fatal "$UAT_EXIT_PREREQ" \
            "uv sync failed. Install uv (https://docs.astral.sh/uv/) or provision the
    environment yourself and pass --runner with an interpreter that can import cordis."
    fi
    printf '\n'
}

# Removes the exact paths the loader guide writes and nothing else. Never fatal:
# a cleanup failure after a completed walkthrough must not mask the run's own
# verdict.
uat_cleanup() {
    printf '\nRemoving the generated plugin package under %s...\n' "$UAT_WORKSPACE"
    rm -rf -- "$UAT_WORKSPACE/hot_plugins" 2>/dev/null
    rmdir -- "$UAT_WORKSPACE" 2>/dev/null
    rmdir -- "$(dirname "$UAT_WORKSPACE")" 2>/dev/null
    if [[ -e "$UAT_WORKSPACE/hot_plugins" ]]; then
        printf '%swarning:%s %s/hot_plugins is still present; remove it by hand\n' \
            "$UAT_YELLOW" "$UAT_NC" "$UAT_WORKSPACE" >&2
    else
        printf 'removed.\n'
    fi
}
