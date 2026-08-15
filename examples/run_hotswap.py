"""Example 3: replacing a provider while its consumers are running.

The demonstration application (``examples.harness``) has two interchangeable
providers of the ``store`` key: one backed by a dictionary, one by a real
``sqlite3`` connection. Swapping them means retiring one component and composing
the other in. Nothing else is written, and in particular nothing tells the
consumers -- they are reloaded because their *target* changed.

A target is a digest of ``(key, provider uid)`` pairs (paper Definition 46).
A uid is drawn fresh and never reused, so a replacement provider can never be
mistaken for the one it replaced, even when the two hold equal values.

Usage::

    uv run python examples/run_hotswap.py
    uv run python examples/run_hotswap.py --source sqlite --target memory --verbose
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from examples.harness import STORE_PROVIDERS, Harness, open_connections, reset_connections


def heading(title: str) -> None:
    print()
    print(title)
    print("-" * len(title))


def show(label: str, value: Any) -> None:
    print(f"  {label:<44} {value}")


async def demonstrate(source: str, target: str, verbose: bool) -> int:
    reset_connections()
    harness = Harness()
    await harness.start(store=source)

    heading(f"the application, serving on the {source} store")
    kv = harness.fibers["tool_kv"]
    show("store provider", harness.store_kind)
    show("tool_kv state", kv.state.value)
    show("tool_kv target", kv.target)
    written = harness.dispatch("/kv/put", {"key": "alpha", "value": "1"})
    show("a write", written)
    show("open sqlite connections", open_connections())

    old_store = harness.root.get("store")

    heading("step 1: retire the provider")
    await harness.remove("store")
    show("store binding", harness.root.get("store"))
    show("tool_kv state", kv.state.value)
    show("tool_kv target", kv.target)
    server = harness.server
    mounted = [] if server is None else [path for path in server.routes if path.startswith("/kv")]
    show("routes still mounted by tool_kv", mounted)
    show("open sqlite connections", open_connections())
    show("the old store was released", getattr(old_store, "closed", "closed"))

    if verbose:
        print()
        print("  The consumer did not fail and was not asked to do anything. Its required")
        print("  key lost its ACTIVE provider, so its target became undefined, so it")
        print("  unloaded -- running the inverse of every route, journal and timer it had")
        print("  installed, in the reverse of the order it installed them.")

    heading(f"step 2: compose the {target} provider in")
    await harness.add("store", STORE_PROVIDERS[target])
    await harness.root.registry.settle()
    show("store provider", harness.store_kind)
    show("tool_kv state", kv.state.value)
    show("tool_kv target", kv.target)
    show("open sqlite connections", open_connections())

    heading("step 3: serve requests again")
    written = harness.dispatch("/kv/put", {"key": "beta", "value": "2"})
    read = harness.dispatch("/kv/get", {"key": "beta"})
    show("a write", written)
    show("a read", read)
    show("the value written before the swap", harness.dispatch("/kv/get", {"key": "alpha"})["value"])
    show("requests the audit trail recorded", len(harness.audit_log))

    if verbose:
        print()
        print("  The value written before the swap is gone, and that is correct: the")
        print("  binding was replaced, not migrated. Moving data between providers is an")
        print("  application concern; the runtime's concern is that no consumer ever")
        print("  addresses the old provider after the new one is bound.")

    ok = (
        harness.store_kind == target
        and kv.state.value == "ACTIVE"
        and read["backend"] == target
        and read["value"] == "2"
    )

    heading("result")
    show("the consumer reloaded against the new provider", "yes" if ok else "no")
    await harness.shutdown()
    show("after shutting the application down: connections", open_connections())
    show("routes left mounted", len(harness.root.get("server").routes) if harness.root.get("server") else 0)
    reset_connections()
    print()
    return 0 if ok else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run_hotswap.py",
        description="Replace the store provider under a running consumer and watch it reload.",
    )
    parser.add_argument(
        "--source",
        choices=["memory", "sqlite"],
        default="memory",
        help="which store provider to start with (default: memory)",
    )
    parser.add_argument(
        "--target",
        choices=["memory", "sqlite"],
        default="sqlite",
        help="which store provider to swap to (default: sqlite)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="explain what each step shows",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return asyncio.run(demonstrate(args.source, args.target, args.verbose))


if __name__ == "__main__":
    raise SystemExit(main())
