# UAT: `run_effects.py`

Manual user-acceptance test for `examples/run_effects.py`: the demo of revertible effects - the five
callback forms a component may use to acquire something, the last-applied-first recovery that gives it
back, what an interrupted effect does with what it had already accumulated, and the fact that an effect
belongs to the fiber that created it rather than to the call that ran. This is the mechanism the residue
numbers in [benefit.md](benefit.md) come out of. The demo only computes and prints; it writes no files, so
this guide needs no cleanup.

## Prerequisites

The shared setup in [README.md](README.md) - `uv` on PATH and the project environment resolved
(`./effects.sh --seed` does the second one). Nothing else.

## What this uses

`run_effects.py` prints one section at a time, or all of them:

| Flag | Value | Why |
| ---- | ----- | --- |
| `--section` | `forms`, `order`, `guard`, `fiber`, `plugins` in steps 1-5 | One claim per step, so a single failing section names itself. `all` is the default and prints every section in this order. |
| `--verbose` | not passed | It adds a prose note under each section. The measured lines are what this guide asserts against. |

## Run it

From the repo root, under Git Bash:

```bash
uv run python examples/run_effects.py --section forms
```

## What to check

- **All five callback forms are accepted, and a generator recovers its steps in reverse.** Exit 0, and:

  ```text
  def cb() -> None                             applied ['applied'] then []
  def cb() -> Disposer                         applied ['applied'] then ['recovered']
  async def cb() -> Disposer | None            applied ['applied'] then ['recovered']
  def cb() -> Generator[Disposer]              applied ['applied step 1', 'applied step 2'] then ['recovered step 2', 'recovered step 1']
  async def cb() -> AsyncGenerator[Disposer]   applied ['applied step 1', 'applied step 2'] then ['recovered step 2', 'recovered step 1']
  ```

  A callback returning `None` acquired something revertible-by-nothing and recovers `[]`; the two
  generator forms yield two steps and unwind them `step 2` before `step 1`.

- **Recovery is last-applied-first and happens at most once.** Exit 0, with the applied order and the
  recovered order exactly reversed, and a disposer called three times running one recovery:

  ```bash
  uv run python examples/run_effects.py --section order
  ```

  ```text
  applied                                      effect-0 effect-1 effect-2 effect-3
  recovered                                    undo-3 undo-2 undo-1 undo-0
  three calls to the same disposer             1 recovery run
  ```

- **An interrupted effect keeps only what it had accumulated.** Exit 0, and the guard trips partway
  through a generator effect: the two inverses already accumulated run, the step after the guard never
  does:

  ```bash
  uv run python examples/run_effects.py --section guard
  ```

  ```text
  after the guard tripped                      generator closed
  inverses that ran                            undo-b undo-a
  the step after the guard                     never ran
  ```

- **An effect belongs to the fiber that created it, not to the call that ran.** Exit 0, and an effect
  created long after `apply` returned is still recovered when the component is retired, before the fiber
  reaches `DISPOSED`:

  ```bash
  uv run python examples/run_effects.py --section fiber
  ```

  ```text
  a second effect created after loading        registered nowhere but on the fiber
  recovered on unload                          undo-created-later undo-at-load-time
  component state                              DISPOSED
  ```

- **The built-in timer and bus services leave nothing behind.** Exit 0, and every count that was non-zero
  while the worker was `ACTIVE` is zero once it is `DISPOSED` - two armed timers, one subscriber and two
  pending asyncio tasks all go to zero:

  ```bash
  uv run python examples/run_effects.py --section plugins
  ```

  ```text
  worker state                                 ACTIVE
  timers armed                                 2
  bus subscribers                              1
  pending asyncio tasks (delta)                2
  after retiring the worker: state             DISPOSED
  timers armed                                 0
  bus subscribers                              0
  pending asyncio tasks (delta)                0
  ```

- **An unknown section name is refused.** Exit 2, with argparse naming the valid choices and no section
  printed:

  ```bash
  uv run python examples/run_effects.py --section bogus
  ```

## Scripted equivalent

[`effects.sh`](effects.sh) runs this guide's own commands as a guided walkthrough, asserting each check
above:

```bash
./effects.sh          # step through it, one keypress per step
./effects.sh --auto   # run it unattended and print a verdict
```

See [README.md](README.md#scripted-runs) for the shared flags.

## Related automated coverage

`tests/test_effect.py` covers the five callback forms, the LIFO recovery order and the at-most-once
guarantee; `tests/test_fiber_lifecycle.py` covers the fiber-ownership path this guide's `fiber` section
walks; `tests/test_plugins.py` covers the timer and bus services from the `plugins` section.
