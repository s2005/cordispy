"""Example 2: coeffects -- the spatial dimension.

A component declares what it needs (``inject``) and publishes what it offers
(``ctx.set``). It never looks anything up, never waits for anything and never
checks whether a dependency is ready: the runtime activates it when its
declarations are satisfied and deactivates it when they stop being satisfied
(paper section 5.1.2, Algorithms 2, 3 and 6).

Four sections::

    uv run python examples/run_services.py --section activation
    uv run python examples/run_services.py --section all --verbose

``activation``
    composition order does not matter, and withdrawal deactivates dependents.
``ordering``
    the guarantee everything else rests on: a dependency is still readable while
    the dependent it triggered is running its own inverses.
``access``
    ``ctx.key`` enforces the declaration; ``ctx.get(key)`` is the reflective
    read that never fails. The two rejections of Algorithm 6.
``isolation``
    two contexts isolating the same key resolve to independent bindings and do
    not notify each other.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cordispy import Context, InactiveAccessError, UndeclaredAccessError, plugin


def heading(title: str) -> None:
    print()
    print(title)
    print("-" * len(title))


def show(label: str, value: Any) -> None:
    print(f"  {label:<46} {value}")


@plugin(name="store_provider", provide=["store"])
def store_provider(ctx: Context, config: Any) -> None:
    """Publish a ``store``. The value is the config, so callers can tell them apart."""
    store = {"label": config or "first", "rows": []}
    ctx.set("store", store)


# --------------------------------------------------------------------------
# activation
# --------------------------------------------------------------------------


async def section_activation(verbose: bool) -> None:
    heading("activation follows the declarations, not the composition order")
    root = Context()
    loads: list[str] = []

    @plugin(name="consumer", inject=["store"])
    def consumer(ctx: Context, config: Any) -> None:
        loads.append(ctx.store["label"])

    # The consumer is composed in first, before anything provides `store`.
    dependent = root.use(consumer)
    await root.registry.settle()
    show("consumer composed before the provider", dependent.state.value)
    show("its target", dependent.target)

    provider = root.use(store_provider, "first")
    await root.registry.settle()
    show("after the provider is composed in", dependent.state.value)
    show("its target (key, provider uid)", dependent.target)
    show("times its apply has run", len(loads))

    await provider.retire()
    show("after the provider is retired", dependent.state.value)
    show("its target", dependent.target)

    replacement = root.use(store_provider, "second")
    await root.registry.settle()
    show("after a replacement provider arrives", dependent.state.value)
    show("values it has loaded against", loads)
    await replacement.retire()

    if verbose:
        print()
        print("  PENDING means 'declared but never loaded'; INACTIVE means 'loaded once,")
        print("  now fully recovered'. The consumer moved between them without anybody")
        print("  calling it, and its apply ran once per satisfied target.")


# --------------------------------------------------------------------------
# the ordering guarantee
# --------------------------------------------------------------------------


async def section_ordering(verbose: bool) -> None:
    heading("a dependency stays readable through the dependent's own teardown")
    root = Context()
    trace: list[str] = []

    @plugin(name="consumer", inject=["store"])
    def consumer(ctx: Context, config: Any) -> Any:
        ctx.store["rows"].append("row from the consumer")
        trace.append(f"acquired against {ctx.store['label']}")

        def release() -> None:
            # The provider's withdrawal is what started this teardown, and the
            # binding is still readable here. Without that, no component could
            # ever hand a resource back to the dependency it took it from.
            trace.append(f"released against {ctx.store['label']}")
            ctx.store["rows"].remove("row from the consumer")

        return release

    provider = root.use(store_provider, "first")
    dependent = root.use(consumer)
    await root.registry.settle()
    show("rows the consumer added", root.get("store")["rows"])

    await provider.retire()
    show("trace", " | ".join(trace))
    show("consumer state", dependent.state.value)
    show("the store binding afterwards", root.get("store"))

    if verbose:
        print()
        print("  Two things make this work: `unload` drains every dependent before it")
        print("  runs its own inverses, and it discards the committed view only after")
        print("  the last inverse has run (paper Algorithm 5, lines 25-28).")


# --------------------------------------------------------------------------
# property access
# --------------------------------------------------------------------------


async def section_access(verbose: bool) -> None:
    heading("two ways to read a coeffect, and the two ways to be refused")
    root = Context()
    observed: dict[str, str] = {}

    @plugin(name="declared", inject=["store"])
    def declared(ctx: Context, config: Any) -> None:
        observed["while active"] = str(ctx.store["label"])

    @plugin(name="undeclared")
    def undeclared(ctx: Context, config: Any) -> None:
        try:
            _ = ctx.store
        except UndeclaredAccessError as error:
            observed["undeclared"] = f"UndeclaredAccessError: {error}"

    provider = root.use(store_provider, "first")
    fiber = root.use(declared)
    root.use(undeclared)
    await root.registry.settle()

    show("ctx.store inside a component that declared it", observed["while active"])
    show("ctx.store inside one that did not", observed["undeclared"])
    show("ctx.get('store') from the root", root.get("store")["label"])
    show("ctx.get('missing') from the root", root.get("missing"))

    await provider.retire()
    try:
        _ = fiber.ctx.store
        refused = "no error"
    except InactiveAccessError as error:
        refused = f"InactiveAccessError: {error}"
    show("ctx.store once the declaring fiber is inactive", refused)
    show("ctx.get('store') at the same moment", root.get("store"))

    if verbose:
        print()
        print("  `ctx.key` reads the fiber's committed view and enforces the declaration.")
        print("  `ctx.get(key)` reads the store and never raises. A component that")
        print("  provides a key does not thereby get to read it as `ctx.key`: a committed")
        print("  view is resolve(inject) and nothing else.")


# --------------------------------------------------------------------------
# isolation
# --------------------------------------------------------------------------


async def section_isolation(verbose: bool) -> None:
    heading("isolating a key gives two contexts independent bindings")
    root = Context()

    @plugin(name="reader", inject=["store"])
    def reader(ctx: Context, config: Any) -> None:
        sink: list[str] = config
        sink.append(str(ctx.store["label"]))

    shared_sink: list[str] = []
    isolated_sink: list[str] = []

    # The shared realm.
    root.use(store_provider, "shared")
    root.use(reader, shared_sink)
    await root.registry.settle()

    # A derived context that resolves `store` to a realm of its own.
    branch = root.isolate("store")
    branch.use(store_provider, "isolated")
    branch.use(reader, isolated_sink)
    await root.registry.settle()

    show("reader on the root context", shared_sink)
    show("reader on the isolated context", isolated_sink)
    show("root.get('store')", root.get("store")["label"])
    show("branch.get('store')", branch.get("store")["label"])
    show("realms are the same object", root.realm_of("store") is branch.realm_of("store"))
    show("bindings in the store", len(root.bindings()))

    if verbose:
        print()
        print("  A key is not looked up in the store directly: it is mapped through the")
        print("  isolation table to a realm, and the realm indexes the store. Isolation")
        print("  is derived realization -- the parent is untouched and recovery is")
        print("  discarding the child context.")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

SECTIONS = {
    "activation": section_activation,
    "ordering": section_ordering,
    "access": section_access,
    "isolation": section_isolation,
}


async def run(names: list[str], verbose: bool) -> None:
    for name in names:
        await SECTIONS[name](verbose)
    print()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run_services.py",
        description="Coeffects: declaration-driven activation, the ordering guarantee, and isolation.",
    )
    parser.add_argument(
        "--section",
        choices=[*SECTIONS, "all"],
        default="all",
        help="which part to demonstrate (default: all)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="add a note explaining what each section shows",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    names = list(SECTIONS) if args.section == "all" else [args.section]
    asyncio.run(run(names, args.verbose))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
