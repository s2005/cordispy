# Limitations

What this port deliberately does not implement, and why. Everywhere in this list the omission is a
choice, not an oversight: each is either a boundary the paper itself draws, or a Python/JavaScript
runtime difference that no amount of porting effort removes.

## No receiver-rebinding for service methods

The reference TypeScript implementation lets a service method write `this.ctx` and have it resolve to
the *calling* context rather than the context the service object was constructed with, through a
tracker/shadow `Proxy` (`packages/utils/src/index.ts:120-155`). That is what lets one shared `Timer` or
`Bus` instance behave, from inside its own methods, as if it belonged separately to whichever component
called it.

This port does not reproduce that machinery. Python has no receiver-rebinding equivalent of a property
`get` trap on `this`, and the nearest available tool -- `contextvars`, threaded through every call site
that might need the calling context -- is a substantial amount of infrastructure for a feature the
paper's formal model does not require: nothing in Algorithms 1 through 10 depends on a service resolving
its caller implicitly. `cordispy` asks a component to pass its own `ctx` explicitly instead:

```python
timer.interval(ctx, 60.0, callback)  # `ctx` is the caller's, passed by hand
```

`src/cordispy/plugins/timer.py` and `src/cordispy/plugins/events.py` both document this at the call site.

## HMR and Python's module registry

Algorithm 10's transactional reload evicts modules from `sys.modules` and re-imports them. Node's
module registry and Python's differ in a way that matters here: evicting a module from `sys.modules`
does not guarantee the old module *object* is collected, because any other module that did
`from x import y` at import time holds a direct reference to `y` that survives the eviction untouched --
only a subsequent `import x; x.y` lookup would see the replacement.

This is exactly why Algorithm 8's classification fixed point treats an importer of a changed module as
`accepted` too, not just the changed module itself (`src/cordispy/loader/hmr.py:classify`): re-importing
only the edited file and leaving its importers alone would leave those importers holding direct
references into the module object that HMR just tried to replace. The bounded import graph exists
partly to make this tractable -- `ImportGraph` reads `.py` sources with `ast` rather than trusting live
module objects, specifically so a module whose source no longer parses is still a graph node (the one
Algorithm 9 has to detect as stale) rather than an import failure that aborts classification.

## Declarative loader: one-directional reconciliation

The paper describes an entry's binding to its fiber as running in both directions: "the loader responds
to a change in an entry's fields by adjusting the fiber, and a component that revises its own
configuration or disables itself has the change written back to its entry" (paper section 5.2.1). This
port implements the first direction in full -- `Loader.reconcile` turns a changed document into fiber
operations -- but not the second: there is no API in this port for a running component to push a
revised configuration back onto its own `Entry` so that a subsequent read of the document (or a write of
it back to disk) would reflect a change the component made to itself. `Entry.updater`
(`src/cordispy/loader/entry.py`) is the mechanism for the loader to hand a *new* configuration down into a
component that knows how to absorb one (which is how `group`'s keyed diff works); nothing plays the
symmetric role of a component handing a revised configuration back up.

## Change detection is polling, not a watcher

`Hmr.poll()` re-stats every file in the bounded import graph on every call rather than subscribing to
filesystem change events. This keeps the runtime free of a third-party watcher dependency -- the
project's dependency budget is `pyyaml` plus the dev tool group and nothing else -- at the cost of
detecting a change only the next time something calls `poll()`, rather than the instant the change
happens on disk.

## No distributed spatial composability

Every mechanism in this port -- the store, the registry, realms -- lives in one Python process. The
paper notes that operating systems already give a coarse substitute for temporal composability at
process granularity, and container orchestrators a coarse substitute for spatial composability at
service granularity (paper section 1.2.3); this port does not attempt to extend fine-grained
composability across a process boundary. A coeffect binding cannot be published in one process and
observed in another.

## The runtime does not verify that an inverse reverses its effect

`ctx.effect(callback)` does not check that the disposer `callback` returns actually undoes what
`callback` did. This is not an omission specific to this port; it is the paper's own stated boundary
(section 5.1.1): the obligation that an inverse recovers its effect rests on the component author, and
the runtime's only guarantee is about *when* the inverse runs -- once, in reverse order, at a clean step
boundary. A component that returns the wrong inverse, or none at all where one was needed, will not be
caught by anything in this runtime.

## Optional injects are an extension, with one reader of their own

The paper's coeffect specification is flat: a component declares the keys it needs, and every one of
them gates activation. `cordispy` adds an `optional` section to `inject` (paper section 5.1.3 has no
counterpart), which never gates activation but still participates in `fiber.target`, so a change of
optional provider still reloads the dependent.

That extension needs a reader the paper does not describe. Algorithm 6 has exactly two outcomes for a
declared key -- the committed value, or `INACTIVE_ACCESS` -- so `ctx.key` cannot answer "declared, and
nothing provides it", which is the ordinary state of an optional dependency. `Context.optional(key)`
(`src/cordispy/context.py`) is that third outcome and nothing more: the same walk up the fiber chain,
against the same committed view, returning `None` where `ctx.key` would raise `InactiveAccessError`. It
is deliberately *not* a general escape hatch -- a key the fiber never declared still raises
`UndeclaredAccessError` -- and it is deliberately not `ctx.get(key)`, which reads the live store and can
answer with a binding whose provider is already unloading. `docs/plugin-authoring.md` shows it in use.

## Cycle detection is diagnostic, not preventive

`Registry.find_cycles()` (`src/cordispy/registry.py`) reports a dependency cycle among live fibers'
`inject`/`provide` declarations and logs a warning; it does not refuse to compose a component that would
join one. Self-edges are included, so a component that injects a key it also provides is reported as the
one-component cycle it is rather than sitting at `PENDING` with no diagnostic. The paper is explicit that a cycle leaves every component involved permanently inactive and
that this is predictable from the declarations alone, unlike a deadlock (paper section 6.5) -- but
`provide` is a declaration of what a component *may* install, not a guarantee that it will, so refusing
composition on the strength of declared cycles alone would reject configurations that a later `ctx.set`
call might never actually make cyclic. The check exists to name the failure mode, not to prevent it.

## Not a port of Koishi or the wider TypeScript ecosystem

The reference TypeScript implementation's Section 5.3 case study (Koishi) and its surrounding packages
carry substantial application-framework machinery beyond the core paper algorithms: an internationalized
logger service, a schema-validated configuration DSL, an event pipeline with `waterfall`/`bail`
middleware chains, and a `check` predicate that can be attached to a binding to revalidate it
independently of its provider's own state. None of that is part of Algorithms 1 through 10, and none of
it is implemented here. `cordispy` implements the core library (paper section 5.1), the declarative
loader and HMR (paper section 5.2), and a demo application built directly on both -- not a full
application framework.

## No thread or executor safety

The whole design assumes run-to-completion between `await` points: `fiber._store`, `fiber.state`,
`fiber.committed` and the disposer chain are all mutated at synchronous points with no locking, because
asyncio's single-threaded cooperative scheduling already provides the guarantee this relies on (the
`if self.inertia: return` check inside `refresh()` *is* the lock). Nothing in this runtime may be moved
onto a thread or run through `asyncio.to_thread` / `loop.run_in_executor` without reintroducing races
these invariants depend on not existing.
