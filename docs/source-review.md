# Source review: vocabulary and divergences

`cordispy` is built against the paper's algorithms, not against the shipped TypeScript's names or its
exact behavior. This document records why, and exactly where the two diverge. It is the product of a
source review of the cloned `cordiverse/cordis` checkout, `packages/core` v4.0.0-rc.8, HEAD `8cc9e33`.

## Why the vocabulary differs

The review found that the shipped code does not use the paper's own names: `ctx.effect` takes a second
`label` parameter nowhere in the formal model, `ctx.use` is spelled `ctx.plugin`, the paper's `INACTIVE`
sentinel is the magic string `'__INACTIVE__'`, and so on throughout. Since demonstrating the paper is
the point of this port, `cordispy` leads with the paper's vocabulary wherever the two disagree, and
this table exists so a reader coming from the TypeScript implementation can cross-navigate.

| Paper (section 5) | Cordis TS v4.0.0-rc.8 | `cordispy` |
| --- | --- | --- |
| `ctx.effect(callback)` | `ctx.effect(callback, label)` (`fiber.ts:277`) | `ctx.effect(callback)` |
| `ctx.use(component, config)` | `ctx.plugin(plugin, config)` (`registry.ts:193`) | `ctx.use(...)`, alias `ctx.plugin(...)` |
| `fiber.target` | `fiber._runner.epoch`, a `':'`-joined uid string (`fiber.ts:385`) | `fiber.target`, a tuple, `None` when unsatisfied |
| `INACTIVE` sentinel | the magic string `'__INACTIVE__'` (`fiber.ts:101`) | `None` |
| `fiber.committed` | `fiber.store`, the committed snapshot (`fiber.ts:416`) | `fiber.committed` |
| `fiber.state = INACTIVE` | splits into `PENDING` (deps unmet) and `DISPOSED` (`uid is None`) (`fiber.ts:78`) | `PENDING`, `INACTIVE`, `DISPOSED` |
| `fiber.inertia` | `fiber.inertia` (`fiber.ts:399`) | `fiber.inertia`, an `asyncio.Task` |
| `@@store` / `@@isolate` / `@@intercept` | `reflect.store` + `symbols.isolate` / `symbols.intercept` | `_store` / `_isolate` / `_intercept` |
| `INACTIVE_ACCESS` / `UNDECLARED_ACCESS` | untyped `Error`s distinguished only by message (`reflect.ts:81`) | `InactiveAccessError` / `UndeclaredAccessError` |
| entry `url` | entry `name`, resolved against `ctx.baseUrl` (`entry.ts:8`) | entry `name`, an import target |

A few of these are worth a sentence beyond the table. `fiber.target` in the reference implementation is
a string built by concatenating `':' + impl.fiber.uid` per declared key, so two providers can differ by
a colon-adjacency accident that the type system cannot catch; `cordispy` uses a sorted tuple of
`(key, provider_uid)` pairs instead, which is both structurally comparable and impossible to
mis-concatenate. `INACTIVE_ACCESS`/`UNDECLARED_ACCESS` in the reference implementation are the same
`Error` class with different message text (`reflect.ts:81`), so a caller who wants to branch on which
one occurred has to parse the message; `cordispy` gives each its own exception class
(`src/cordispy/errors.py`) precisely so a caller can `except InactiveAccessError` without string matching.

## Deliberate divergences

The review turned up seven real defects in the reference implementation. Each is fixed here, and
documented below with the file:line it lives at, what goes wrong, and what this port does instead.
Nothing else was "improved" -- everywhere the paper and the shipped TypeScript agree, this port follows
the paper exactly, even where a different implementation choice might have looked nicer.

### 1. Aborted async-generator effects are abandoned without closing

**Where:** `fiber.ts:263`.

**What goes wrong:** when the effect guard trips mid-iteration, the reference implementation simply
returns from the driving loop without calling `iter.return()` on the generator it was pulling from. A
`try: ... finally: cleanup()` written inside a generator effect therefore never runs its `finally` if
the effect is interrupted -- the very case an iterator-form effect exists to handle safely.

**What this port does instead:** `_drive_sync_generator` and `_drive_async_generator`
(`src/cordispy/effect.py`) close the generator in a `finally` block regardless of how the driving loop
exits -- guard tripped, exhausted normally, or an exception propagating through. This is both the
correctness fix and what keeps Python from separately reporting "coroutine ignored GeneratorExit" at
garbage-collection time, which a literal port of the TypeScript behavior would trigger.

### 2. No per-disposer error isolation in the effect disposer chain

**Where:** `fiber.ts:281`.

**What goes wrong:** the disposer array is spliced empty *before* any disposer in it runs
(`disposables.splice(0).reverse()`), so if the first inverse run throws, every inverse after it in the
array is simply gone -- it was already removed from the array that would have run it, and nothing
re-raises it or runs it later. One throwing inverse permanently leaks every inverse that was supposed to
run after it.

**What this port does instead:** `DisposerChain.__call__` (`src/cordispy/effect.py`) drains its list once
(making the call idempotent, matching the reference's intent), but then runs *every* item in the drained
list regardless of earlier failures, collecting exceptions as it goes, and re-raises them together as
one `ExceptionGroup` only after every inverse has had its turn. Draining before running keeps the call
idempotent but also means an inverse that performs an effect of its own lands on an already-emptied
chain, so `Fiber._unload` (`src/cordispy/fiber.py`) re-drains until the chain is empty rather than calling
it once -- see `docs/architecture.md` for the bound and the warning.

### 3. `fiber.error` is never cleared on recovery

**Where:** `fiber.ts:482`.

**What goes wrong:** the recorded error is cleared only inside a separate `update()` method, not inside
the ordinary reload path. A fiber that fails once and later reloads successfully -- for instance because
a dependency it needed arrives late -- still reports `FiberState.FAILED` forever afterward, and
`await fiber` (or any code awaiting its settlement) still observes the stale error.

**What this port does instead:** `Fiber._start_reload` (`src/cordispy/fiber.py`) clears `self.error` at the
start of every reload attempt, synchronously, before the reload task is even created. A fiber that
failed and later satisfies its target again reports `ACTIVE` with no error, which is what
`docs/architecture.md`'s state diagram shows as the ordinary `FAILED --> LOADING` transition.

### 4. Fiber-level teardown is concurrent, not LIFO

**Where:** `fiber.ts:438`.

**What goes wrong:** the reversed disposer list is mapped through `Promise.all`, which starts every
disposer's promise in reverse order but then awaits them *concurrently*. `Promise.all` guarantees only
that the promises were created in that order, not that disposer 2 finishes before disposer 1 begins --
so two inverses that are not independent of each other (one releasing a resource the other still holds
open) can interleave in a way the paper's sequential composition `dispose_2 . dispose_1` does not permit.

**What this port does instead:** `DisposerChain.__call__` awaits each inverse strictly one after another,
in a plain `for` loop, so the LIFO order is an order of *completion*, not merely of creation.

### 5. `ctx.set` on an existing binding mutates in place and never notifies

**Where:** `reflect.ts:162`.

**What goes wrong:** `set()` looks up the existing binding for a key and assigns `impl.value = value`
directly, in place. No `notify` is called, because as far as the notification machinery is concerned
nothing about the binding's *identity* changed -- only its value. Any dependent that already resolved
the key observes the new value only the next time it happens to read it, with no reload, no
recomputation of its own state, and no chance to react to the change at all.

**What this port does instead:** `Context.set` (`src/cordispy/context.py`) treats re-setting a key the same
fiber already provides as a withdraw-then-install: the old binding is deleted and `notify` is called,
then the new binding is installed and `notify` is called again. This is a direct consequence of how
`fiber.target` is defined (a digest over *provider uid*, not value) -- a provider that wants its
replacement value to propagate has no choice but to withdraw and reinstall, which is exactly what this
implementation of `set` does automatically. The paper itself notes, and this port's docstrings repeat,
that a provider overwriting its own binding in place is specifically the case that is *not* observed.

### 6. Closure capture bug in the sequence-number disposer

**Where:** `packages/utils/src/index.ts:19` (the `List.push` method).

**What goes wrong:**

```typescript
push(value: T) {
  this.ctx.effect(() => {
    this.inner.set(++this.sn, value)
    return () => this.inner.delete(this.sn)
  }, `${this.trace}.push()`)
}
```

The returned disposer closes over `this.sn` -- the container's *current* sequence number at the moment
the disposer eventually runs -- rather than the value `this.sn` held at the moment this particular entry
was pushed. Push two values and dispose the first entry's disposer: `this.sn` has since advanced to `2`,
so the disposer deletes key `2`, the most recently pushed entry, instead of key `1`, the one it was
actually supposed to remove.

**What this port does instead:** Python closures capture variables the same way JavaScript's do (by
reference to the enclosing scope, not by value at closure-creation time), so the same bug is available
to write by accident in this port too, and three places in this codebase specifically guard against it
with the identical fix: bind the sequence number, task, or subscription to a **local** before returning
the inverse that closes over it. `Context.set`'s inverse (`src/cordispy/context.py`) binds `binding` to a
local before defining `inverse`; `Timer._arm`'s inverse (`src/cordispy/plugins/timer.py`) binds `task` to a
local before defining `retract`; `Bus._attach`'s inverse (`src/cordispy/plugins/events.py`) binds
`subscription` to a local before defining `remove`. Each site carries a comment pointing back to this
divergence.

### 7. `notify` is a full registry sweep per name

**Where:** `reflect.ts:205`, called once per changed name from `fiber.ts:364`.

**What goes wrong:** `notify` iterates every runtime in the registry and every fiber in every runtime,
checking whether each declares the one name it was called with. The call site in `fiber.ts:364` invokes
it once per changed key, so a binding change touching `n` keys costs `n` full sweeps of every fiber in
the process, regardless of how many fibers actually declare any of those keys.

**What this port does instead:** `Registry` (`src/cordispy/registry.py`) keeps a reverse index from
coeffect key to the fibers that declare it, maintained incrementally in `attach`/`detach`, and
`Registry.notify` accepts a whole batch of changed keys in one call, looking up the union of dependents
directly from the index and re-evaluating each fiber at most once even if it declares several of the
changed keys. An item of that batch may be a `(key, realm)` pair, which is how a fiber-level
notification names the realm its binding actually occupies rather than re-deriving one from a context
that may resolve the key elsewhere.

## Sources

- [github.com/cordiverse/paper](https://github.com/cordiverse/paper) -- the paper this port implements.
- [github.com/cordiverse/cordis](https://github.com/cordiverse/cordis) -- the reference TypeScript
  implementation reviewed above.
