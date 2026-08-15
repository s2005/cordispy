"""Components and their coeffect specification -- paper section 5.1.3.

A component pairs a coeffect specification (``inject``) with an effect function
(``apply``). Instantiating one produces a :class:`~cordispy.fiber.Fiber`.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeAlias

from .errors import CordisError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .context import Context

__all__ = ["ApplyFn", "Component", "Inject", "InjectSpec", "plugin", "to_component"]

#: An effect function. It receives the fiber's own context, and optionally the
#: configuration the component was instantiated with.
ApplyFn: TypeAlias = Callable[..., Any]


@dataclass(frozen=True)
class Inject:
    """A component's coeffect specification.

    Required keys gate activation: while any one of them is unsatisfied the
    fiber's target is undefined and the component never loads. Optional keys
    never gate activation, but they *do* participate in the target, so a change
    of optional provider still reloads the dependent.
    """

    required: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        overlap = set(self.required) & set(self.optional)
        if overlap:
            names = ", ".join(sorted(overlap))
            raise CordisError(f"inject keys declared both required and optional: {names}")

    def __contains__(self, key: object) -> bool:
        return key in self.required or key in self.optional

    def __iter__(self) -> Iterator[str]:
        yield from self.required
        yield from self.optional

    def __len__(self) -> int:
        return len(self.required) + len(self.optional)

    def __bool__(self) -> bool:
        return bool(self.required or self.optional)

    @classmethod
    def parse(cls, spec: InjectSpec) -> Inject:
        """Normalize any accepted spelling of an inject declaration."""
        if spec is None:
            return cls()
        if isinstance(spec, Inject):
            return spec
        if isinstance(spec, Mapping):
            unknown = set(spec) - {"required", "optional"}
            if unknown:
                names = ", ".join(sorted(unknown))
                raise CordisError(f"unknown inject sections: {names}")
            return cls(
                required=_as_keys(spec.get("required", ())),
                optional=_as_keys(spec.get("optional", ())),
            )
        if isinstance(spec, str):
            return cls(required=(spec,))
        if isinstance(spec, Sequence):
            return cls(required=_as_keys(spec))
        raise CordisError(f"cannot interpret {spec!r} as an inject declaration")


#: Everything accepted where an inject declaration is expected.
InjectSpec: TypeAlias = "Inject | str | Sequence[str] | Mapping[str, Sequence[str]] | None"


def _as_keys(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, Sequence):
        raise CordisError(f"expected a sequence of coeffect keys, got {value!r}")
    keys = tuple(value)
    for key in keys:
        if not isinstance(key, str):
            raise CordisError(f"coeffect keys must be strings, got {key!r}")
    return keys


@dataclass(frozen=True)
class Component:
    """A normalized component: an effect function plus its declarations."""

    apply: ApplyFn
    name: str
    inject: Inject = field(default_factory=Inject)
    provide: tuple[str, ...] = ()
    takes_config: bool = True

    def invoke(self, ctx: Context, config: Any) -> Any:
        """Call the effect function, passing config only if it accepts one."""
        if self.takes_config:
            return self.apply(ctx, config)
        return self.apply(ctx)

    def __call__(self, ctx: Context, config: Any = None) -> Any:
        return self.invoke(ctx, config)


def plugin(
    *,
    name: str | None = None,
    inject: InjectSpec = None,
    provide: str | Sequence[str] | None = None,
) -> Callable[[ApplyFn], Component]:
    """Decorate an effect function into a :class:`Component`.

    ``provide`` is a declaration only: the binding is still installed by calling
    ``ctx.set``. It exists so dependency cycles can be detected from the
    declarations alone (paper section 6.5).
    """

    def decorate(fn: ApplyFn) -> Component:
        return Component(
            apply=fn,
            name=name or _name_of(fn),
            inject=Inject.parse(inject),
            provide=_as_keys(provide) if provide is not None else (),
            takes_config=_takes_config(fn),
        )

    return decorate


def to_component(value: Any) -> Component:
    """Normalize anything usable with ``ctx.use`` into a :class:`Component`."""
    if isinstance(value, Component):
        return value

    apply = getattr(value, "apply", None)
    if apply is None and callable(value):
        apply = value
    if apply is None or not callable(apply):
        raise CordisError(
            f"a component must be callable or expose a callable 'apply', got {type(value).__name__}"
        )

    return Component(
        apply=apply,
        name=getattr(value, "name", None) or _name_of(value),
        inject=Inject.parse(getattr(value, "inject", None)),
        provide=_as_keys(getattr(value, "provide", None) or ()),
        takes_config=_takes_config(apply),
    )


def _name_of(value: Any) -> str:
    name = getattr(value, "__name__", None)
    if isinstance(name, str) and name:
        return name
    return type(value).__name__


def _takes_config(fn: ApplyFn) -> bool:
    """Report whether ``fn`` accepts a configuration argument after the context."""
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):
        return True
    positional = 0
    for parameter in signature.parameters.values():
        if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
            return True
        if parameter.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            positional += 1
    return positional >= 2
