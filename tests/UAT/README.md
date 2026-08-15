# Hand-run demos of the cordis plugin system

These are hand-run acceptance tests, and they double as the demo of what the paradigm buys. Each one pairs
a copy-paste markdown guide with a script that runs the guide's own commands and asserts every expectation,
so neither can rot without the other noticing. They complement the automated suite in `tests/` rather than
replacing it: `pytest` proves the runtime behaves; these show a person what that behavior looks like, with
the numbers read out of a live process while they watch.

Everything here runs in one process against this checkout. There is no database, no service, no container
and no network access anywhere in the suite.

Start with [`benefit.md`](benefit.md) - it is the claim the other five explain. [`calculator.md`](calculator.md) is the one to read next if you would rather see the argument at the scale of a small application than as a table of counters.

## The guides

| Command | Guide | Script | What it shows | Needs |
| ------- | ----- | ------ | ------------- | ----- |
| `examples/run_benefit.py` | [benefit.md](benefit.md) | [benefit.sh](benefit.sh) | The same application built twice - on cordis and on a conventional plugin registry - and what each leaves behind when a component is retired, replaced, kept waiting, or allowed to fail | Nothing beyond the shared setup |
| `examples/run_calculator.py` | [calculator.md](calculator.md) | [calculator.sh](calculator.sh) | A calculator whose every arithmetic operation is a plugin: removing one, adding a derived one before its dependencies, and one that fails mid-load | Nothing beyond the shared setup |
| `examples/run_effects.py` | [effects.md](effects.md) | [effects.sh](effects.sh) | Revertible effects: the five callback forms, last-applied-first recovery, an interrupted effect, and fiber ownership | Nothing beyond the shared setup |
| `examples/run_services.py` | [services.md](services.md) | [services.sh](services.sh) | Coeffects: declaration-driven activation, the ordering guarantee, the two refusal paths, and key isolation | Nothing beyond the shared setup |
| `examples/run_hotswap.py` | [hotswap.md](hotswap.md) | [hotswap.sh](hotswap.sh) | Replacing a provider under a live consumer, in both directions | Nothing beyond the shared setup |
| `examples/run_loader.py` | [loader.md](loader.md) | [loader.sh](loader.sh) | Declarative reconciliation across four config revisions, then hot module replacement with a transactional rollback | Writes a generated plugin package; its step 6 removes it |

## Shared prerequisites

Once per session, from the repo root:

```bash
uv sync
```

That is the whole setup. Any guide will do it for you if you pass `--seed`:

```bash
./benefit.sh --seed --auto
```

If `uv` is not available and the environment is already provisioned some other way, point the guides at an
interpreter that can import `cordispy`:

```bash
./benefit.sh --auto --runner "python"
```

There is no teardown, because only [loader.md](loader.md) writes anything and it removes its own artifact
in its last step. To remove it by hand:

```bash
rm -rf output/uat-hmr
```

## Scripted runs

Run one guide interactively, one keypress per step:

```bash
./benefit.sh
```

Run it unattended and get a verdict:

```bash
./benefit.sh --auto
```

Run the whole roster, in order, one at a time:

```bash
./run-all.sh
```

Each step prints why it exists, the exact command it is about to run, that command's real output, the
observed exit code against the expected one, and a `[PASS]` or `[FAIL]` line per assertion. The run ends
with a summary and a `RESULT:` line.

### Shared flags

Every guide script takes the same named flags. There are no positional arguments.

| Flag | Meaning |
| ---- | ------- |
| `--auto`, `--no-pause` | Run every step back-to-back with no keypress prompt |
| `--stop-on-fail` | Abort at the first failed step instead of continuing |
| `--from-step N` | Start at step `N` |
| `--only-step N` | Run exactly one step |
| `--list-steps` | Print the step index and exit |
| `--runner CMD` | How to launch the demos (default: `uv run python`) |
| `--workspace PATH` | Where the loader guide writes its generated package (default: `output/uat-hmr`) |
| `--seed` | Resolve the project environment (`uv sync`) before the walkthrough |
| `--cleanup` | Remove the generated package afterwards |
| `--keep-output` | Keep the files a guide writes instead of removing them |
| `--help`, `-h` | Usage and exit 0 |

`UAT_AUTO=1` in the environment is equivalent to `--auto`.

`run-all.sh` takes `--runner`, `--workspace`, `--stop-on-fail`, `--keep-output`, `--only NAME` and
`--list`. It refuses `--seed`, `--cleanup`, `--from-step` and `--only-step`, which address one guide's own
fixture or step numbers rather than the roster.

### Exit codes

| Code | Meaning |
| ---- | ------- |
| 0 | Every executed assertion passed |
| 1 | At least one assertion failed |
| 2 | Bad invocation - unknown flag, missing value, out-of-range step |
| 3 | A missing prerequisite; the message names the command that fixes it |
| 130 | Interrupted |

`--help` always works, with nothing provisioned.

## Run one at a time

The guides share one checkout and one generated workspace - `output/uat-hmr` - and nothing serializes
concurrent access to it. Two overlapping runs would have [loader.md](loader.md) writing and removing the
same plugin package underneath each other, so never run two walkthroughs at once. `run-all.sh` is a plain
sequential loop for exactly this reason.

Running a guide alongside `pytest` is safe: each demo is a separate process, and `tests/test_hmr.py`
builds its throw-away packages under uniquely numbered names in a `tmp_path`, never in the workspace this
suite uses.

## Skipped steps

A step that cannot run reports `[SKIP]` with a reason. A skip is not a pass: it never changes the exit
code, but it moves the step out of `steps run` and qualifies the verdict as
`RESULT: PASS (N step(s) skipped)`, so a run that mostly did not happen can never read as clean.

Two steps can skip legitimately:

- `loader.sh` step 4 skips when no generated package is present, because step 3 is what writes it - that
  happens under `--only-step 4`.
- `loader.sh` step 6 skips under `--keep-output`, because that flag asks for the package to be kept.

## What the guides name

Every value the guides name is either a path inside this checkout or an argument the demos accept. Nothing
here names a host, an account, a credential or an environment, because none of these demos reaches outside
the process it runs in - which is also why the suite needs no fixture to seed.

| Value | What it is |
| ----- | ---------- |
| `examples/config/app.yaml`, `examples/config/app.json` | The two committed configuration documents [loader.md](loader.md) realizes. They describe the same application; the demo checks that itself. |
| `examples/config/nope.yaml` | A path that deliberately does not exist, used for the missing-file case in [loader.md](loader.md) step 5. |
| `output/uat-hmr` | The workspace the loader guide writes its generated plugin package into. Git-ignored, and removed by that guide's last step. |
| `hot_plugins` | The package name `run_loader.py` generates inside that workspace, with the modules `state.py` and `greeter.py`. |
| `memory`, `sqlite` | The two store providers [hotswap.md](hotswap.md) swaps between. The sqlite one is in-process; no file and no server. |
| `bogus` | The invalid argument value every guide uses for its exit-2 case. |
| `+ - * / ^ % !` | The operator symbols [calculator.md](calculator.md) composes in and out. `%` is a remainder, derived from `-`, `*` and `/` because `a % b` is `a - floor(a / b) * b`; `!` is the operation that fails mid-load. |
| `DEMO-1`-style fixture ids | Not used. These demos plant no data, so there is no fixture to name. |

## Adding a guide

1. Write `<command>.md` first, completely - it is the source of truth. Run every command in it and paste
   the real output.
2. Transcribe it into `<command>.sh`: one step per `## What to check` bullet, numbered from 0, step 0 the
   preflight. Source `common.sh`; never copy its plumbing and never redefine a `uat_*` helper.
3. Add a row to the guide table above, and the guide's name to the `ROSTER` in
   [`run-all.sh`](run-all.sh), positioned for its dependencies.
4. Mark the script executable in the git index - a local `chmod +x` records nothing on Windows:

   ```bash
   git update-index --chmod=+x tests/UAT/<command>.sh
   ```

5. Run it: `./<command>.sh --auto` must reach the summary with the verdict the guide claims.

Project values - the runner, the workspace, the seed and the cleanup - live in
[`project.sh`](project.sh); `common.sh` is copied between repositories unchanged and must not be edited
here.
