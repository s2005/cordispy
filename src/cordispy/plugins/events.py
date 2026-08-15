"""The ``bus`` service: an event bus whose subscriptions are revertible.

A subscription is the archetypal leak in a conventional plugin system. It is
cheap to add, it is added from anywhere -- including from inside a request
handler, long after the plugin was set up -- and the only record that it exists
is a closure sitting in somebody else's list. Teardown code written by hand can
only remove the subscriptions its author remembered.

Here a subscription is an effect (paper section 5.1.1, Algorithm 1), so the
record of its existence is the fiber's accumulator, which is complete by
construction:

* :meth:`Bus.on` goes through ``ctx.effect`` and is therefore owned by a fiber.
* :meth:`Bus.subscribe` is the raw, unmanaged form. It exists so that a
  conventional implementation can be written against the same bus for
  comparison, and so that non-component code can still use it.

Handlers are synchronous. An event is dispatched by ``emit`` returning to its
caller, not by a task, so a handler that returned a coroutine would leave one
nobody awaits; :meth:`Bus.emit` rejects that outright rather than let it become
a warning at collection time.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeAlias

from ..component import plugin
from ..effect import AsyncDisposer, Disposer
from ..errors import CordisError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..context import Context

__all__ = ["Bus", "EventHandler", "Subscription", "events_plugin"]

#: An event handler. Called with whatever ``emit`` was given.
EventHandler: TypeAlias = Callable[..., Any]


class Subscription:
    """One handler attached to one event, compared by identity.

    Identity is what makes removal exact: the same handler may be subscribed to
    the same event more than once, and removing one of those subscriptions must
    remove *that* one rather than the first equal-looking entry.
    """

    __slots__ = ("event", "handler")

    def __init__(self, event: str, handler: EventHandler) -> None:
        self.event = event
        self.handler = handler

    def __repr__(self) -> str:
        return f"<Subscription {self.event!r}>"


class Bus:
    """The value bound to the ``bus`` coeffect key."""

    __slots__ = ("_handlers",)

    def __init__(self) -> None:
        self._handlers: dict[str, list[Subscription]] = {}

    def __repr__(self) -> str:
        return f"<Bus {self.subscribers()} subscribers>"

    # ------------------------------------------------------------- inspection

    def events(self) -> tuple[str, ...]:
        """Every event with at least one subscriber, in subscription order."""
        return tuple(self._handlers)

    def subscribers(self, event: str | None = None) -> int:
        """How many handlers are attached -- to one event, or to all of them."""
        if event is None:
            return sum(len(entries) for entries in self._handlers.values())
        return len(self._handlers.get(event, ()))

    # ------------------------------------------------------------ subscription

    def on(self, ctx: Context, event: str, handler: EventHandler) -> AsyncDisposer:
        """Subscribe ``handler`` to ``event`` as an effect of ``ctx``'s fiber.

        The returned disposer removes the handler, and the same disposer is
        prepended to the fiber's accumulator, so the subscription also goes away
        when the component unloads -- including a subscription made from inside
        a request handler minutes after the component was loaded.
        """
        return ctx.effect(lambda: self._attach(event, handler))

    def subscribe(self, event: str, handler: EventHandler) -> Disposer:
        """Subscribe outside the runtime, returning the removal function.

        Nothing tracks the returned function: whoever calls this owns the
        obligation to call it, which is precisely the obligation ``on`` moves
        into the runtime.
        """
        return self._attach(event, handler)

    def _attach(self, event: str, handler: EventHandler) -> Disposer:
        # The subscription is bound to a local *before* the inverse closes over
        # it. The reference implementation closes over the container's current
        # sequence number instead (packages/utils/src/index.ts:19), so removing
        # an earlier subscription removes the most recent one; Python closures
        # capture the same way, so the binding has to be explicit here too.
        subscription = Subscription(event, handler)
        self._handlers.setdefault(event, []).append(subscription)

        def remove() -> None:
            self._detach(subscription)

        return remove

    def _detach(self, subscription: Subscription) -> None:
        entries = self._handlers.get(subscription.event)
        if entries is None:
            return
        for index, candidate in enumerate(entries):
            if candidate is subscription:
                del entries[index]
                break
        if not entries:
            del self._handlers[subscription.event]

    def clear(self) -> None:
        """Drop every subscription. The inverse of the component itself."""
        self._handlers.clear()

    # -------------------------------------------------------------- dispatch

    def emit(self, event: str, *args: Any) -> list[Any]:
        """Call every handler of ``event`` in subscription order.

        The handler list is copied first, so a handler that subscribes or
        unsubscribes while the event is being dispatched cannot change which
        handlers this dispatch calls.
        """
        results: list[Any] = []
        for subscription in tuple(self._handlers.get(event, ())):
            result = subscription.handler(*args)
            if inspect.iscoroutine(result):
                result.close()
                raise CordisError(
                    f"the handler for event {event!r} is a coroutine function; "
                    "bus handlers are synchronous -- schedule asynchronous work "
                    "with the timer service instead"
                )
            results.append(result)
        return results


@plugin(name="events", provide=["bus"])
def events_plugin(ctx: Context, config: Any) -> Disposer:
    """Provide the ``bus`` service."""
    bus = Bus()
    ctx.set("bus", bus)
    return bus.clear
