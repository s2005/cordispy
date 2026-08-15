# UAT: `run_loader.py`

Manual user-acceptance test for `examples/run_loader.py`: realizing a declarative configuration document
as a tree of fibers, reconciling four revisions of that document incrementally, and then hot-replacing a
plugin module on disk twice - once successfully, once into a rollback. This is the guide that shows the
paradigm at the level an operator touches it: edit a config entry or a source file, and only what changed
is rebuilt. **This is the one guide in the suite that writes to disk**: part B generates a throw-away
plugin package, and step 6 removes it.

## Prerequisites

The shared setup in [README.md](README.md) - `uv` on PATH and the project environment resolved
(`./loader.sh --seed` does the second one). The two configuration documents the guide reads,
`examples/config/app.yaml` and `examples/config/app.json`, are committed in this checkout.

## What this uses

`run_loader.py` realizes one configuration document, then optionally runs the hot-module-replacement half:

| Flag | Value | Why |
| ---- | ----- | --- |
| `--config` | `examples/config/app.yaml` in steps 1 and 3, `examples/config/app.json` in step 2, `examples/config/nope.yaml` in step 5 | The document to realize. The YAML and JSON files describe the same application, which is what step 2 checks. |
| `--skip-hmr` | passed in steps 1 and 2 | Runs only part A, the declarative reconciliation, so a reconciliation failure cannot be confused with a reload failure. |
| `--workspace` | `output/uat-hmr` in step 3 | Where part B writes its generated plugin package. Without it the demo uses a system temporary directory and removes it, which would leave step 4 nothing to inspect. |
| `--keep-workspace` | passed in step 3 | Leaves the generated package on disk so step 4 can read what the failed reload left there. Step 6 removes it. |

## Run it

From the repo root, under Git Bash:

```bash
uv run python examples/run_loader.py --config examples/config/app.yaml --skip-hmr
```

## What to check

- **Reconciliation is incremental: each revision replaces only the entries that changed.** Exit 0, no
  `PART B` section, and across the four revisions the demo names exactly which fibers were replaced and
  which were left alone:

  ```text
  fibers replaced       counter
  fibers untouched      store, tools, greeter

  fibers replaced       greeter
  fibers untouched      store, tools, counter

  fibers replaced       echo
  fibers untouched      store, tools, greeter, counter
  fibers in runtime     4 -> 5
  ```

  The fiber ids in the final snapshot are the proof, because an id only changes when a fiber was rebuilt:
  `{'store': 1, 'tools': 2, 'greeter': 5, 'counter': None, 'echo': 6}`. `store` and `tools` still carry
  the ids they had at the first revision, so disabling one entry, reconfiguring another and adding a
  third never restarted them. The run ends with `every claim above held`.

- **The JSON document produces the same application as the YAML one.** Exit 0, and the demo names the
  companion document and confirms both parse identically:

  ```bash
  uv run python examples/run_loader.py --config examples/config/app.json --skip-hmr
  ```

  ```text
  PART A -- declarative reconciliation from app.json
      companion document    app.yaml
      claim holds           the YAML and JSON documents parse identically
  ```

- **A hot reload rebuilds only the edited module's entry, and a failed reload rolls back.** Exit 0, and
  part B does two replacements. The first succeeds: only `hot_plugins.greeter` is accepted, `state` keeps
  its fiber and the value it was holding, and `/greet` starts answering with the new code. The second
  introduces a syntax error, and the reload is transactional - the error reaches the caller, the previous
  modules are restored from the backup, and `/greet` keeps answering:

  ```bash
  uv run python examples/run_loader.py --config examples/config/app.yaml --workspace output/uat-hmr --keep-workspace
  ```

  ```text
  accepted modules      hot_plugins.greeter
  stale entries         greeter
  fibers replaced       greeter
  fibers untouched      state
  reply from /greet     welcome, world

  runtime log [cordispy.hmr] hot reload failed; rolling back to the previous modules
  re-import raised      SyntaxError: expected ':' (line 6)
  reply from /greet     welcome, world
  rollbacks so far      1
  workspace kept at     output\uat-hmr
  ```

  The run ends with `every claim above held`. The `workspace kept at` line prints the path with the
  platform's own separator - the block above was captured under Git Bash on Windows, so it reads
  `output\uat-hmr` there and `output/uat-hmr` on Linux. The walkthrough asserts the label, not the
  separator.

- **The rollback restored modules, not files.** Exit 0, and the generated `greeter.py` left on disk is
  still the broken version the demo wrote - while the fiber above went on serving the previous good one
  out of the module backup:

  ```bash
  cat output/uat-hmr/hot_plugins/greeter.py
  ```

  ```text
  """A deliberate syntax error, to provoke the transactional rollback."""

  from cordispy import plugin


  def greeter(ctx, config)
      return None
  ```

  This is worth seeing directly: the runtime's transaction covers the module graph it owns, and makes no
  claim to undo an operator's edit.

- **A configuration file that does not exist is refused.** Exit 2, naming the resolved path, with no
  fiber tree printed:

  ```bash
  uv run python examples/run_loader.py --config examples/config/nope.yaml
  ```

  ```text
  no such configuration file: examples/config/nope.yaml
  ```

## Cleanup

Step 6 of the walkthrough does this, and `./loader.sh --keep-output` skips it. By hand:

```bash
rm -rf output/uat-hmr
```

## Scripted equivalent

[`loader.sh`](loader.sh) runs this guide's own commands as a guided walkthrough, asserting each check
above:

```bash
./loader.sh          # step through it, one keypress per step
./loader.sh --auto   # run it unattended and print a verdict
```

See [README.md](README.md#scripted-runs) for the shared flags.

## Related automated coverage

`tests/test_loader.py` covers the keyed diff and the incremental reconciliation part A walks;
`tests/test_hmr.py` covers the module-graph traversal, the accept/decline split and the transactional
rollback part B walks.
