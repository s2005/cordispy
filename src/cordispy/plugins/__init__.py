"""Built-in plugins: the temporal claim, made concrete.

Two components ship with the runtime, and both exist to be *unloaded*. Between
them they cover the two resources that conventional plugin systems leak most
reliably -- a scheduled task and an event subscription -- and each is
implemented as nothing but a wrapper over ``ctx.effect``:

``timer_plugin``
    Provides ``timer``. ``timeout`` and ``interval`` arm a task whose inverse
    cancels *and awaits* it, so unloading the consumer leaves exactly zero
    pending tasks.

``events_plugin``
    Provides ``bus``. ``on`` attaches a handler whose inverse removes it, so
    unloading the consumer leaves exactly zero stale handlers -- including
    handlers attached long after the component was loaded.

Both are used by ``examples/harness``, where the residue after unloading a
component is measured rather than asserted by inspection::

    from cordispy import Context
    from cordispy.plugins import events_plugin, timer_plugin

    root = Context()
    root.use(timer_plugin)
    root.use(events_plugin)
    await root.registry.settle()
"""

from __future__ import annotations

from .events import Bus, EventHandler, Subscription, events_plugin
from .timer import Timer, TimerCallback, timer_plugin

__all__ = [
    "Bus",
    "EventHandler",
    "Subscription",
    "Timer",
    "TimerCallback",
    "events_plugin",
    "timer_plugin",
]
