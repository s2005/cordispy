"""A calculator whose every operation is a plugin, built twice.

Three modules:

``engine``
    the calculator itself -- the four tables an operation is installed into, the
    tokenizer, the precedence-climbing parser, the memo caches. Shared by both
    implementations, so the comparison is about composition and not about two
    different calculators.

``cordis_side``
    each operation as a cordis component. ``mod`` declares ``op.sub``,
    ``op.mul`` and ``op.div``, and the runtime does the rest.

``naive_side``
    each operation as a conventional plugin with a ``setup``/``teardown`` pair,
    on the registry from ``examples.naive``.

The question the pair answers is narrower than "does it leak": it is whether a
calculator that has lost an operation stops *offering* it. A calculator that
still lists ``%`` in ``help`` and then raises when you type ``22 % 8`` is not
merely leaking, it is lying about what it can do.
"""

from __future__ import annotations

from .cordis_side import CordisCalculator, broken_factorial, calculator_plugin, operation_component
from .engine import (
    ARITHMETIC,
    DERIVED,
    EVICTION_DELAY,
    FACTORIAL,
    OPERATIONS,
    REQUIREMENTS,
    BrokenOperationError,
    Calculator,
    CalculatorError,
    Operation,
    ParseError,
    UnknownSymbolError,
    format_result,
    install,
    requirements_for,
    uninstall,
    with_fn,
)
from .naive_side import NaiveCalculator, OperationPlugin

__all__ = [
    "ARITHMETIC",
    "DERIVED",
    "EVICTION_DELAY",
    "FACTORIAL",
    "OPERATIONS",
    "REQUIREMENTS",
    "BrokenOperationError",
    "Calculator",
    "CalculatorError",
    "CordisCalculator",
    "NaiveCalculator",
    "Operation",
    "OperationPlugin",
    "ParseError",
    "UnknownSymbolError",
    "broken_factorial",
    "calculator_plugin",
    "format_result",
    "install",
    "operation_component",
    "requirements_for",
    "uninstall",
    "with_fn",
]
