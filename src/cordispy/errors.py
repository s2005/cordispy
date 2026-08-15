"""Error taxonomy for the cordis runtime.

The reference TypeScript implementation distinguishes access failures only by
message text (``reflect.ts:81``). This port gives every failure mode a distinct
class so callers can branch on it, which is what the paper's ``INACTIVE_ACCESS``
and ``UNDECLARED_ACCESS`` outcomes (section 5.1.4, Algorithm 6) actually mean.
"""

from __future__ import annotations

__all__ = [
    "AccessError",
    "CordisError",
    "InactiveAccessError",
    "InactiveEffectError",
    "InvalidEffectError",
    "UndeclaredAccessError",
]


class CordisError(Exception):
    """Base class for every error the runtime raises deliberately."""


class AccessError(CordisError):
    """Base class for the two property-access rejections of Algorithm 6."""


class InactiveAccessError(AccessError):
    """A declared coeffect was read while the declaring fiber is not loaded.

    Algorithm 6, line 5: the fiber-chain walk reached a fiber that declares the
    key in its ``inject`` without having committed a view that binds it.
    """


class UndeclaredAccessError(AccessError):
    """A coeffect was read (or a property written) without being declared.

    Algorithm 6, line 6: the fiber-chain walk reached the root without finding
    any declaration of the key.
    """


class InactiveEffectError(CordisError):
    """An effect was created on a fiber that has already been dropped.

    A dropped fiber has ``uid is None``; it owns no accumulator any more, so an
    effect registered on it could never be recovered.
    """


class InvalidEffectError(CordisError, TypeError):
    """An effect callback produced something that is not an inverse.

    An effect callback may return ``None`` or a callable inverse, or yield those
    from a (possibly asynchronous) generator. Anything else is a programming
    error in the component, not a runtime condition.
    """
