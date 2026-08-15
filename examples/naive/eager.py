"""A plugin module that resolves its dependency at import time.

THIS IS A FAITHFUL CONVENTIONAL PATTERN, NOT A STRAW MAN. Reaching into a
process-wide service table at module scope is what a plugin does when it wants a
module-level constant, a decorator bound to a service, or simply a shorter name
for something it uses everywhere. It works perfectly -- as long as the import
happens after the provider was registered.

That "as long as" is the whole problem. Import order is decided by whoever wrote
the import statements, and it has no relation to the order in which services
become available. When the ordering is wrong the failure is an exception during
import, which is early, loud, and unrecoverable: there is no state to resume
from and nothing to retry, because a half-imported module is not something the
interpreter will finish later.

The cordis answer is not a better error message. It is that a component with an
unsatisfied coeffect is a legitimate, ordinary state -- PENDING -- and the
component activates by itself when the provider appears (paper Algorithm 3 and
Algorithm 5).
"""

from __future__ import annotations

from typing import Any

from .registry import SERVICES

__all__ = ["STORE", "summary"]

#: Resolved as this module is imported. A ``KeyError`` here aborts the import.
STORE: Any = SERVICES["store"]


def summary() -> str:
    """What this module bound itself to."""
    return f"eager tool bound to the {STORE.kind} store"
