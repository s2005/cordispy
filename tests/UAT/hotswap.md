# UAT: `run_hotswap.py`

Manual user-acceptance test for `examples/run_hotswap.py`: replacing the store provider underneath a
running consumer and watching the consumer reload against the new binding by itself. Where
[benefit.md](benefit.md) measures the swap against a conventional registry, this guide walks the cordis
side alone in detail - what the consumer does in the window where no provider exists, and what it does
once one appears. The demo only computes and prints; it writes no files, so this guide needs no cleanup.

## Prerequisites

The shared setup in [README.md](README.md) - `uv` on PATH and the project environment resolved
(`./hotswap.sh --seed` does the second one). Nothing else: the sqlite store the demo uses is in-process.

## What this uses

`run_hotswap.py` runs one swap per invocation, from a starting provider to a target provider:

| Flag | Value | Why |
| ---- | ----- | --- |
| `--source` | `memory` in steps 1 and 3, `sqlite` in step 2 | Which store provider the application starts on. `memory` is the default. |
| `--target` | `sqlite` in steps 1 and 3, `memory` in step 2 | Which one to swap to. `sqlite` is the default. Running it in both directions shows the behavior is not a property of one backend. |
| `--verbose` | passed in step 3 only | Adds prose explaining what each step of the swap shows. The measured lines are what steps 1 and 2 assert against. |

## Run it

From the repo root, under Git Bash:

```bash
uv run python examples/run_hotswap.py --source memory --target sqlite
```

## What to check

- **The consumer survives the window where its dependency does not exist, then reloads against the new
  one.** Exit 0, and the run reports, in order: the application serving on `memory`; the provider retired,
  leaving the consumer `INACTIVE` with no routes and no open connections; the sqlite provider composed in
  and the consumer `ACTIVE` again; and requests served by the new backend.

  ```text
  store provider                               memory
  a write                                      {'stored': 'alpha', 'backend': 'memory'}

  routes still mounted by tool_kv              []
  open sqlite connections                      0
  the old store was released                   True

  a write                                      {'stored': 'beta', 'backend': 'sqlite'}
  the value written before the swap            None

  the consumer reloaded against the new provider yes
  after shutting the application down: connections 0
  routes left mounted                          0
  ```

  `the value written before the swap` is `None` on purpose: the swap moved the consumer to a genuinely
  different backend rather than silently keeping the old data around. The consumer's own code was never
  asked to do any of this.

- **The same holds in the other direction.** Exit 0, with `sqlite` serving the first write and `memory`
  serving the one after the swap - so the behavior belongs to the runtime, not to one backend:

  ```bash
  uv run python examples/run_hotswap.py --source sqlite --target memory
  ```

  ```text
  a write                                      {'stored': 'alpha', 'backend': 'sqlite'}
  a write                                      {'stored': 'beta', 'backend': 'memory'}
  the consumer reloaded against the new provider yes
  ```

- **`--verbose` explains the window rather than only measuring it.** Exit 0, and the narration after the
  retire step states that the consumer was neither failed nor asked to do anything:

  ```bash
  uv run python examples/run_hotswap.py --source memory --target sqlite --verbose
  ```

  ```text
  The consumer did not fail and was not asked to do anything. Its required
  key lost its ACTIVE provider, so its target became undefined, so it
  ```

- **An unknown provider name is refused.** Exit 2, with argparse naming the valid choices and no swap
  performed:

  ```bash
  uv run python examples/run_hotswap.py --source bogus
  ```

- **A flag given without its value is refused.** Exit 2, with argparse reporting the missing argument:

  ```bash
  uv run python examples/run_hotswap.py --target
  ```

## Scripted equivalent

[`hotswap.sh`](hotswap.sh) runs this guide's own commands as a guided walkthrough, asserting each check
above:

```bash
./hotswap.sh          # step through it, one keypress per step
./hotswap.sh --auto   # run it unattended and print a verdict
```

See [README.md](README.md#scripted-runs) for the shared flags.

## Related automated coverage

`tests/test_coeffects.py` covers the deactivate-on-provider-loss and reactivate-on-provider-arrival path
this swap walks; `tests/test_fiber_lifecycle.py` covers the reload itself; `tests/test_benefit.py` asserts
the same swap against the conventional registry that [benefit.md](benefit.md) step 2 compares it to.
