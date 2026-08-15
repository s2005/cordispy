# UAT: `run_calculator.py`

Manual user-acceptance test for `examples/run_calculator.py`: a calculator whose every arithmetic
operation is a separate plugin, built twice - once on the cordis runtime, once on a conventional plugin
registry - and then taken apart. Composing an operation in teaches the calculator a symbol; taking one out
should make the calculator forget it, and this guide proves which of the two designs actually does. The
demo only computes and prints; it writes no files, so this guide needs no cleanup.

## Prerequisites

The shared setup in [README.md](README.md) - `uv` on PATH and the project environment resolved
(`./calculator.sh --seed` does the second one). Nothing else: both calculators are built inside the demo's
own process.

## What this uses

`run_calculator.py` runs one comparison at a time, or all four:

| Flag | Value | Why |
| ---- | ----- | --- |
| `--scenario` | `remove`, `late`, `residue`, `failure` in steps 1-4, then `all` in step 5 | One comparison per step, so a single failing claim names itself. `all` is the default. |
| `--log-level` | left at its default `critical` | The `failure` scenario makes an operation fail on purpose and both runtimes report it through `logging`. The default keeps that expected report out of the results table. |
| `--verbose` | not passed | It narrates each measurement as prose. The table is what this guide asserts against. |

The operation under test in most steps is `mod` (symbol `%`), which is *derived*: a remainder is
*defined* as `a - floor(a / b) * b`, so it works only for as long as subtraction, multiplication and
division all do. That is a property of the operation, not a policy either implementation invented.

The demo doubles as its own assertion: it checks 19 cordis-side invariants and exits 1 if any of them does
not hold, so a passing exit code is itself a result.

## Run it

From the repo root, under Git Bash:

```bash
uv run python examples/run_calculator.py --scenario remove
```

## What to check

- **Removing an operation something else was built on.** Exit 0, and the table shows one calculator that
  stopped offering the remainder and one that did not:

  ```text
  | remove   | operations offered by help                 | * + - ^                                  | % * + - ^             |
  |          | state of the derived operation             | INACTIVE                                 | REGISTERED            |
  |          | 17 % 5 -- a pair already in the memo cache | UnknownSymbolError: unknown operator '%' | 2                     |
  |          | 22 % 8 -- a pair never evaluated before    | UnknownSymbolError: unknown operator '%' | KeyError: '/'         |
  |          | advertised but not performable             | (none)                                   | %                     |
  ```

  The two middle rows are the point. Division has been removed on both sides. The conventional registry
  still lists `%` under `help`, still answers `17 % 5` with **2** - the correct remainder - because that
  pair is in the memo cache, and raises `KeyError: '/'` the moment a pair it has not seen arrives. cordis
  answers both the same way, because the operation is simply gone. The run ends with
  `OK: 4 cordis-side invariants hold`.

- **Composing a derived operation before the ones it needs.** Exit 0, and `OK: 5 cordis-side invariants
  hold`:

  ```bash
  uv run python examples/run_calculator.py --scenario late
  ```

  ```text
  |          | state before its dependencies exist   | PENDING     | REJECTED                                 |
  |          | state once its dependencies arrive    | ACTIVE      | REJECTED                                 |
  |          | 22 % 8                                | 6           | UnknownSymbolError: unknown operator '%' |
  ```

  Neither side advertises `%` while it cannot run - both are honest about that. The difference is what
  happens next: cordis activates it by itself when the three operations it is defined in terms of arrive,
  and the registry stays rejected, because registration was a function call that already failed and
  nothing retries it.

- **What a retired operation leaves behind.** Exit 0, and `OK: 5 cordis-side invariants hold`:

  ```bash
  uv run python examples/run_calculator.py --scenario residue
  ```

  ```text
  |          | memo caches after retiring pow            | 2                                        | 3           |
  |          | eviction timers after retiring pow        | 2                                        | 3           |
  ```

  Both sides removed the symbol from `help` correctly here - the conventional `teardown()` is complete
  with respect to what its `setup()` did. What it cannot reach is the memo cache and the eviction timer,
  because both were created while *evaluating an expression*, after `setup()` had already returned.

- **An operation that installs itself and then fails to load.** Exit 0, and `OK: 5 cordis-side invariants
  hold`:

  ```bash
  uv run python examples/run_calculator.py --scenario failure
  ```

  ```text
  |          | does the tokenizer accept !               | no                                       | yes                                 |
  |          | 5 ! 1                                     | UnknownSymbolError: unknown operator '!' | IndexError: list index out of range |
  |          | tables of 4 it is still installed in      | 0                                        | 4                                   |
  ```

  This is the case no static check can catch: the operation is in all four tables and its arithmetic
  raises from inside its own closure. Rolling the installation back is the only thing that helps.

- **All four together hold every invariant.** Exit 0, the run ends with
  `OK: 19 cordis-side invariants hold`, and no `[FAIL]` line appears anywhere in the output:

  ```bash
  uv run python examples/run_calculator.py --scenario all
  ```

- **An unknown scenario name is refused before anything is built.** Exit 2, with argparse naming the
  valid choices and no table printed:

  ```bash
  uv run python examples/run_calculator.py --scenario bogus
  ```

## Scripted equivalent

[`calculator.sh`](calculator.sh) runs this guide's own commands as a guided walkthrough, asserting each
check above:

```bash
./calculator.sh          # step through it, one keypress per step
./calculator.sh --auto   # run it unattended and print a verdict
```

See [README.md](README.md#scripted-runs) for the shared flags.

## Related automated coverage

`tests/test_calculator.py` runs the same scenario functions this demo runs and turns their measurements
into assertions, including the stale-memo-cache case and the check that `report()` exits non-zero when an
invariant does not hold.
