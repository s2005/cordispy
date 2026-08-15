"""Realms and bindings -- the two-layer coeffect store of paper section 5.1.2.

A coeffect key is not looked up in the store directly. It is first mapped
through the isolation table to a *realm*, and only the realm indexes the store::

    key --(isolate)--> realm --(store)--> (value, provider fiber)

The indirection is what lets two sibling contexts isolate the same key to
independent bindings. A key with no entry in the isolation table resolves to its
own default realm, so ``sigma(k) = k`` outside ``dom(sigma)``.

Realms are identified by object identity, never by their label: two realms with
the same label are different realms.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeAlias

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .fiber import Fiber

__all__ = ["Binding", "Realm", "Store"]


class Realm:
    """An opaque coeffect realm, compared by identity.

    The reference implementation uses a JavaScript ``Symbol``; this port uses a
    plain object with a label for debugging, which has the same identity
    semantics and works as a dictionary key.
    """

    __slots__ = ("label",)

    def __init__(self, label: str) -> None:
        self.label = label

    def __repr__(self) -> str:
        return f"<realm {self.label} at {id(self):#x}>"

    @staticmethod
    def default(key: str) -> Realm:
        """Return the realm a key resolves to when no isolation applies.

        The mapping is stable for the lifetime of the process, so unrelated
        roots agree on the default realm of a key. They still hold separate
        stores, so agreeing on the realm does not share any binding.
        """
        realm = _DEFAULTS.get(key)
        if realm is None:
            realm = Realm(key)
            _DEFAULTS[key] = realm
        return realm

    @staticmethod
    def fresh(key: str) -> Realm:
        """Return a brand new realm for a key -- the default for ``ctx.isolate``."""
        return Realm(f"{key}#isolated")


_DEFAULTS: dict[str, Realm] = {}


@dataclass(frozen=True)
class Binding:
    """A value bound into the store, together with the fiber that installed it.

    Recording the provider is what makes ``fiber.target`` a reliable digest: a
    binding is identified by its provider's uid rather than by its value, and a
    uid is drawn fresh and never reused.
    """

    key: str
    value: Any
    provider: Fiber

    def __repr__(self) -> str:
        return f"Binding({self.key!r}, provider=#{self.provider.uid})"


#: The value store: realm -> binding. Owned by the root and shared by every
#: context derived from it.
Store: TypeAlias = dict[Realm, Binding]
