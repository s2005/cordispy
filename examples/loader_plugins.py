"""The handful of plugins the declarative loader example composes.

They exist only to give ``run_loader.py`` something legible to reconcile, so
each one is a few lines: it registers one route in a shared store on load, and
removes exactly that route on unload. Reading the set of live routes is then a
direct measurement of which fibers are currently serving.

Only ``store`` provides a coeffect; the rest declare it in ``inject`` and read it
as ``ctx.store``. A provider does not read its own binding that way -- a
committed view is ``resolve(inject)`` and nothing else -- so ``store`` keeps the
dictionary it published in a local instead.
"""

from __future__ import annotations

from typing import Any

from cordispy import Context, plugin


@plugin(name="store", provide=["store"])
def store(ctx: Context, config: Any) -> Any:
    """Provide the shared store every other plugin here registers into."""
    data: dict[str, Any] = {"routes": {}, "served": 0}
    ctx.set("store", data)

    def undo() -> None:
        data["routes"].clear()
        data["served"] = 0

    return undo


@plugin(name="greeter", inject=["store"])
def greeter(ctx: Context, config: Any) -> Any:
    """Serve ``/greet``. Its greeting comes from the entry's configuration."""
    greeting = (config or {}).get("greeting", "hello")
    routes = ctx.store["routes"]
    routes["/greet"] = lambda who: f"{greeting}, {who}"
    return lambda: routes.pop("/greet", None)


@plugin(name="counter", inject=["store"])
def counter(ctx: Context, config: Any) -> Any:
    """Serve ``/count``, advancing the store's counter by a configured step."""
    step = int((config or {}).get("step", 1))
    data = ctx.store

    def bump() -> int:
        data["served"] += step
        return int(data["served"])

    data["routes"]["/count"] = bump
    return lambda: data["routes"].pop("/count", None)


@plugin(name="echo", inject=["store"])
def echo(ctx: Context, config: Any) -> Any:
    """Serve ``/echo``. Added part way through the example by a keyed diff."""
    prefix = (config or {}).get("prefix", "echo")
    routes = ctx.store["routes"]
    routes["/echo"] = lambda text: f"{prefix}: {text}"
    return lambda: routes.pop("/echo", None)
