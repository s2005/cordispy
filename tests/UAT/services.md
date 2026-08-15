# UAT: `run_services.py`

Manual user-acceptance test for `examples/run_services.py`: the demo of coeffects - a component declaring
what it needs rather than fetching it, the runtime activating it when that need is met and deactivating it
when the need goes away, the ordering guarantee that keeps a dependency readable through its dependent's
own teardown, and the two ways a read is refused. This is the half of the paradigm that makes the residue
numbers in [benefit.md](benefit.md) reachable at all: a component the runtime can activate, it can also
deactivate. The demo only computes and prints; it writes no files, so this guide needs no cleanup.

## Prerequisites

The shared setup in [README.md](README.md) - `uv` on PATH and the project environment resolved
(`./services.sh --seed` does the second one). Nothing else.

## What this uses

`run_services.py` prints one section at a time, or all of them:

| Flag | Value | Why |
| ---- | ----- | --- |
| `--section` | `activation`, `ordering`, `access`, `isolation` in steps 1-4 | One claim per step, so a single failing section names itself. `all` is the default and prints every section in this order. |
| `--verbose` | not passed | It adds a prose note under each section. The measured lines are what this guide asserts against. |

## Run it

From the repo root, under Git Bash:

```bash
uv run python examples/run_services.py --section activation
```

## What to check

- **Activation follows the declarations, not the composition order.** Exit 0, and the consumer walks
  `PENDING` -> `ACTIVE` -> `INACTIVE` -> `ACTIVE` without anyone driving it, having loaded against both
  providers in turn:

  ```text
  consumer composed before the provider          PENDING
  after the provider is composed in              ACTIVE
  times its apply has run                        1
  after the provider is retired                  INACTIVE
  after a replacement provider arrives           ACTIVE
  values it has loaded against                   ['first', 'second']
  ```

  A consumer composed *before* its provider is a waiting component, not an error, and `apply` had run
  exactly once at the point it was measured - the runtime does not re-run it speculatively.

- **A dependency stays readable through the dependent's own teardown.** Exit 0, and the trace shows the
  consumer releasing against the same provider it acquired against, with the binding cleared only
  afterwards:

  ```bash
  uv run python examples/run_services.py --section ordering
  ```

  ```text
  rows the consumer added                        ['row from the consumer']
  trace                                          acquired against first | released against first
  consumer state                                 INACTIVE
  the store binding afterwards                   None
  ```

  This is the ordering that makes a correct teardown writable at all: a component being retired can still
  read what it was using.

- **There are two ways to read a coeffect and two ways to be refused.** Exit 0, and both refusals are
  distinct, named errors rather than a `None` that propagates:

  ```bash
  uv run python examples/run_services.py --section access
  ```

  ```text
  ctx.store inside a component that declared it  first
  ctx.store inside one that did not              UndeclaredAccessError: cannot read coeffect 'store' without declaring it in inject
  ctx.get('store') from the root                 first
  ctx.get('missing') from the root               None
  ctx.store once the declaring fiber is inactive InactiveAccessError: cannot read coeffect 'store': <declared> declares it but is INACTIVE
  ctx.get('store') at the same moment            None
  ```

  `ctx.store` is the checked read - it raises rather than hand back something the caller never declared or
  that is no longer live. `ctx.get` is the unchecked one and answers `None`.

- **Isolating a key gives two contexts independent bindings.** Exit 0, and the same declaration resolves
  differently on each side while both remain live:

  ```bash
  uv run python examples/run_services.py --section isolation
  ```

  ```text
  reader on the root context                     ['shared']
  reader on the isolated context                 ['isolated']
  root.get('store')                              shared
  branch.get('store')                            isolated
  realms are the same object                     False
  bindings in the store                          2
  ```

- **An unknown section name is refused.** Exit 2, with argparse naming the valid choices and no section
  printed:

  ```bash
  uv run python examples/run_services.py --section bogus
  ```

## Scripted equivalent

[`services.sh`](services.sh) runs this guide's own commands as a guided walkthrough, asserting each check
above:

```bash
./services.sh          # step through it, one keypress per step
./services.sh --auto   # run it unattended and print a verdict
```

See [README.md](README.md#scripted-runs) for the shared flags.

## Related automated coverage

`tests/test_coeffects.py` covers the declaration-driven activation and the ordering guarantee;
`tests/test_access.py` covers both refusal paths this guide's `access` section walks;
`tests/test_isolation.py` covers the isolated-key behavior from the `isolation` section.
