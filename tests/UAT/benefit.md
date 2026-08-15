# UAT: `run_benefit.py`

Manual user-acceptance test for `examples/run_benefit.py`: the head-to-head demo that builds the same
application twice - once on the cordis runtime, once on a conventional plugin registry - and measures what
each leaves behind when a component is retired, replaced, kept waiting, or allowed to fail. This guide
proves the four claims the paradigm rests on, each as a number read out of the live process. The demo only
computes and prints; it writes no files and opens no network connection, so this guide needs no cleanup.

## Prerequisites

The shared setup in [README.md](README.md) - `uv` on PATH and the project environment resolved
(`./benefit.sh --seed` does the second one). Nothing else: the demo builds both applications inside its own
process.

## What this uses

`run_benefit.py` selects one comparison at a time, or runs all four:

| Flag | Value | Why |
| ---- | ----- | --- |
| `--scenario` | `residue`, `hotswap`, `late`, `failure` in steps 1-4, then `all` in step 5 | One comparison per step, so a single failing claim names itself. `all` is the default. |
| `--log-level` | left at its default `critical` | The `failure` scenario makes a component fail on purpose and both runtimes report it through `logging`. The default keeps that expected report out of the results table. |
| `--verbose` | not passed | It narrates each measurement as prose. The table is what this guide asserts against. |

The demo doubles as its own assertion: it checks 22 cordis-side invariants and exits 1 if any of them does
not hold, so a passing exit code is itself a result.

## Run it

From the repo root, under Git Bash:

```bash
uv run python examples/run_benefit.py --scenario residue
```

## What to check

- **Residue after unload is zero on one side and not on the other.** Exit 0, and the table rows show the
  cordis column at 0 in every category while the conventional registry keeps what the component acquired:

  ```text
  | residue  | resources the component acquired            | 13                                           | 13                                             |
  |          | leftover route handlers                     | 0                                            | 1                                              |
  |          | leftover event subscribers                  | 0                                            | 2                                              |
  |          | still-open sqlite connections               | 0                                            | 3                                              |
  |          | still-pending asyncio tasks                 | 0                                            | 3                                              |
  |          | total residue                               | 0                                            | 9                                              |
  ```

  Both sides acquired the same 13 resources, so the difference is what each released, not what each did.
  The run ends with `OK: 5 cordis-side invariants hold`.

- **A swapped provider leaves the consumer serving, not holding a corpse.** Exit 0, and
  `OK: 6 cordis-side invariants hold`:

  ```bash
  uv run python examples/run_benefit.py --scenario hotswap
  ```

  ```text
  | hotswap  | provider the runtime reports after the swap | sqlite                                       | sqlite                                         |
  |          | consumer state after the swap               | ACTIVE                                       | REGISTERED                                     |
  |          | write served after the swap                 | ok                                           | StoreClosedError: the memory store has been... |
  ```

  Both registries report the new provider. Only cordis reloaded the consumer against it; the conventional
  side still holds the closed one, so its next write raises `StoreClosedError`.

- **A missing dependency is a wait, not an error.** Exit 0, and `OK: 5 cordis-side invariants hold`:

  ```bash
  uv run python examples/run_benefit.py --scenario late
  ```

  ```text
  | late     | consumer state with no provider             | PENDING                                      | MissingDependencyError: no service register... |
  |          | consumer state once the provider arrives    | ACTIVE                                       | ABSENT                                         |
  |          | request after the provider arrives          | ok                                           | RouteError: no route mounted at '/kv/put'      |
  ```

  The conventional registry fails at registration and never retries, so the consumer is still absent after
  the provider shows up.

- **A component that fails mid-load rolls back what it already did.** Exit 0, and
  `OK: 6 cordis-side invariants hold`:

  ```bash
  uv run python examples/run_benefit.py --scenario failure
  ```

  ```text
  | failure  | state recorded for the failed component     | FAILED                                       | ABSENT                                         |
  |          | routes the failed component left behind     | 0                                            | 2                                              |
  |          | its half-mounted route still answers        | RouteError: no route mounted at '/broken/go' | ok                                             |
  ```

  The conventional registry leaves two routes mounted by a component that never finished loading, and one
  of them still answers requests.

- **All four together hold every invariant.** Exit 0, the run ends with
  `OK: 22 cordis-side invariants hold`, and no `[FAIL]` line appears anywhere in the output:

  ```bash
  uv run python examples/run_benefit.py --scenario all
  ```

- **An unknown scenario name is refused before anything is built.** Exit 2, with argparse naming the valid
  choices and no table printed:

  ```bash
  uv run python examples/run_benefit.py --scenario bogus
  ```

## Scripted equivalent

[`benefit.sh`](benefit.sh) runs this guide's own commands as a guided walkthrough, asserting each check
above:

```bash
./benefit.sh          # step through it, one keypress per step
./benefit.sh --auto   # run it unattended and print a verdict
```

See [README.md](README.md#scripted-runs) for the shared flags.

## Related automated coverage

`tests/test_benefit.py` runs the very same scenario functions this demo runs and turns their measurements
into assertions, including the case where `report()` is fed a deliberately failing result and has to exit
non-zero. The demo therefore cannot drift away from what it claims here.
