"""The calculator itself: everything the two implementations share.

Nothing in this module knows about cordis or about a plugin registry. It is the
leaf the two composition mechanisms both drive, so that
``examples/run_calculator.py`` compares the mechanisms rather than two different
calculators.

The shape worth understanding before reading either side:

* an :class:`Operation` is one binary operator -- a symbol, a precedence, a
  description for ``help``, and the function that does the arithmetic;
* :func:`install` puts one into service, which means four separate places: the
  operator table the evaluator dispatches through, the precedence table the
  parser climbs, the alphabet the tokenizer accepts, and the catalog ``help``
  reads. :func:`uninstall` removes exactly those four;
* a memo cache and its eviction timer are **not** created by ``install``. They
  are created the first time an operation is actually evaluated, which is the
  detail the whole comparison turns on: they come into existence after a
  conventional plugin's ``setup`` has returned, in a place its ``teardown``
  cannot see.

The catalog is what makes the comparison legible. A calculator that has lost an
operation should stop offering it; one that still lists ``^`` in ``help`` and
then raises when you type ``2 ^ 3`` is not merely leaking, it is lying.
"""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace

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
    "Operation",
    "ParseError",
    "UnknownSymbolError",
    "derived_modulo",
    "describe",
    "evict_later",
    "format_result",
    "install",
    "memo_key",
    "memoized",
    "operation_for",
    "requirements_for",
    "uninstall",
    "with_fn",
]

#: How long an idle memo cache waits before it is evicted. Deliberately longer
#: than any demo run, so a task still pending when the measurement is taken is
#: unambiguously residue rather than a race with the scheduler.
EVICTION_DELAY = 30.0


class CalculatorError(Exception):
    """Base for everything this module raises at the user."""


class UnknownSymbolError(CalculatorError):
    """The text contains a character the tokenizer does not accept.

    This is the *good* failure: the calculator does not recognize the operator,
    says so, and nothing was half-done. It is what a cleanly removed operation
    leaves behind.
    """


class ParseError(CalculatorError):
    """The tokens do not form an expression."""


class BrokenOperationError(CalculatorError):
    """An operation was advertised, tokenized and dispatched -- and could not run.

    Reaching this means the calculator's four tables disagree with each other:
    something is in the alphabet and the catalog but not in the operator table,
    or its function depends on state that was never built. It is the failure
    mode a conventional registry produces when a plugin goes away and nothing
    tells the things that were counting on it.
    """


@dataclass(frozen=True)
class Operation:
    """One binary operator."""

    name: str
    symbol: str
    precedence: int
    describe: str
    fn: Callable[[float, float], float]
    #: Symbols this operation's arithmetic dispatches through. Empty for the
    #: primitives, which compute directly. A derived operation lists what it
    #: composes itself out of, which is what makes :meth:`Calculator.unsound`
    #: computable rather than a matter of opinion.
    depends: tuple[str, ...] = ()


@dataclass
class Calculator:
    """The four tables an operation has to be installed into, plus its caches."""

    #: symbol -> operation. What the evaluator can actually dispatch.
    operators: dict[str, Operation] = field(default_factory=dict)
    #: symbol -> precedence. What the parser climbs.
    precedence: dict[str, int] = field(default_factory=dict)
    #: What the tokenizer will accept as an operator character.
    alphabet: set[str] = field(default_factory=set)
    #: symbol -> description. What ``help`` tells the user it can do.
    catalog: dict[str, str] = field(default_factory=dict)

    #: operation name -> memo cache. Created on first evaluation, not on install.
    caches: dict[str, dict[tuple[float, float], float]] = field(default_factory=dict)
    #: operation name -> its eviction timer. Likewise.
    evictions: dict[str, asyncio.Task[None]] = field(default_factory=dict)

    evaluated: int = 0

    # ------------------------------------------------------------- inspection

    def advertised(self) -> set[str]:
        """The symbols ``help`` offers."""
        return set(self.catalog)

    def usable(self) -> set[str]:
        """The symbols the evaluator can dispatch."""
        return set(self.operators)

    def unsound(self) -> set[str]:
        """Installed operations whose own dependencies are no longer installed.

        These are the dangerous ones. They are in the operator table, so they
        dispatch; they are in the catalog, so ``help`` offers them; and they
        raise the moment they run, because something they compose themselves out
        of has gone away.
        """
        return {
            symbol
            for symbol, operation in self.operators.items()
            if any(needed not in self.operators for needed in operation.depends)
        }

    def broken(self) -> set[str]:
        """Advertised but cannot actually be performed -- the calculator's lies.

        The headline measurement, and it has two halves: symbols the catalog
        offers that the evaluator cannot dispatch at all, and symbols it can
        dispatch that are certain to fail. An honest calculator keeps this
        empty: it offers exactly what it can do, no more.
        """
        return (self.advertised() - self.usable()) | self.unsound()

    def installed_in(self, symbol: str) -> int:
        """How many of the four tables still carry ``symbol`` (0 to 4).

        The blunt instrument, and the only honest one for an operation whose
        breakage lives inside its own closure rather than in a missing
        dependency. :meth:`broken` cannot see that case -- no static check can --
        so a scenario about a half-loaded operation measures presence instead.
        """
        return sum(
            (
                symbol in self.operators,
                symbol in self.precedence,
                symbol in self.alphabet,
                symbol in self.catalog,
            )
        )

    def tokenizable(self) -> set[str]:
        """Symbols the tokenizer accepts but the evaluator cannot dispatch.

        Distinct from :meth:`broken`: these do not even reach a nice error, they
        parse and then fail inside evaluation.
        """
        return self.alphabet - self.usable()

    def help(self) -> list[str]:
        """The ``help`` listing, in symbol order."""
        return [f"{symbol}  {self.catalog[symbol]}" for symbol in sorted(self.catalog)]

    def live_caches(self) -> int:
        """Memo caches currently allocated."""
        return len(self.caches)

    def pending_evictions(self) -> int:
        """Eviction timers that have not been cancelled."""
        return sum(1 for task in self.evictions.values() if not task.done())

    # -------------------------------------------------------------- evaluation

    def tokenize(self, text: str) -> list[str]:
        """Split ``text`` into numbers and operator symbols.

        The alphabet is read fresh on every call, so the tokenizer can never be
        the thing that is out of date. That is deliberate: it keeps the
        comparison about composition rather than about a caching choice either
        implementation could have made differently.
        """
        symbols = sorted(self.alphabet, key=len, reverse=True)
        pattern = re.compile(
            r"\s*(?:(?P<number>\d+(?:\.\d+)?)|(?P<symbol>"
            + ("|".join(re.escape(s) for s in symbols) if symbols else r"(?!)")
            + r")|(?P<bad>\S))"
        )
        tokens: list[str] = []
        position = 0
        while position < len(text):
            if text[position].isspace():
                position += 1
                continue
            match = pattern.match(text, position)
            if match is None or match.group("bad"):
                bad = text[position]
                raise UnknownSymbolError(f"unknown operator {bad!r}")
            tokens.append(match.group("number") or match.group("symbol"))
            position = match.end()
        return tokens

    def evaluate(self, text: str) -> float:
        """Tokenize, parse by precedence climbing, and dispatch."""
        tokens = self.tokenize(text)
        if not tokens:
            raise ParseError("empty expression")
        value, rest = self._expression(tokens, 0)
        if rest:
            raise ParseError(f"unexpected trailing input: {' '.join(rest)}")
        self.evaluated += 1
        return value

    def _expression(self, tokens: list[str], minimum: int) -> tuple[float, list[str]]:
        left, tokens = self._atom(tokens)
        while tokens:
            symbol = tokens[0]
            level = self.precedence.get(symbol)
            if level is None or level < minimum:
                break
            right, tokens = self._expression(tokens[1:], level + 1)
            operation = self.operators.get(symbol)
            if operation is None:
                # Tokenized, given a precedence, and then found to have no
                # implementation. Nothing in this file caused that: the symbol
                # is in two tables and absent from a third.
                raise BrokenOperationError(f"operator {symbol!r} is advertised but not installed")
            left = operation.fn(left, right)
        return left, tokens

    def _atom(self, tokens: list[str]) -> tuple[float, list[str]]:
        if not tokens:
            raise ParseError("expected a number")
        head, rest = tokens[0], tokens[1:]
        try:
            return float(head), rest
        except ValueError as error:
            raise ParseError(f"expected a number, got {head!r}") from error


# --------------------------------------------------------------------------
# putting an operation into and out of service
# --------------------------------------------------------------------------


def install(calculator: Calculator, operation: Operation) -> None:
    """Put one operation into service.

    Four tables, because an operator is four things to four different readers:
    something to dispatch, something to give a precedence, something to
    tokenize, and something to tell the user about.
    """
    calculator.operators[operation.symbol] = operation
    calculator.precedence[operation.symbol] = operation.precedence
    calculator.alphabet.add(operation.symbol)
    calculator.catalog[operation.symbol] = operation.describe


def uninstall(calculator: Calculator, operation: Operation) -> None:
    """Remove exactly what :func:`install` added."""
    calculator.operators.pop(operation.symbol, None)
    calculator.precedence.pop(operation.symbol, None)
    calculator.alphabet.discard(operation.symbol)
    calculator.catalog.pop(operation.symbol, None)


async def evict_later(calculator: Calculator, name: str) -> None:
    """Drop an idle memo cache. Long enough that it never fires during a demo."""
    await asyncio.sleep(EVICTION_DELAY)
    calculator.caches.pop(name, None)


def memo_key(left: float, right: float) -> tuple[float, float]:
    """The cache key for one evaluation of a binary operator."""
    return (left, right)


def memoized(
    operation: Operation,
    calculator: Calculator,
    allocate: Callable[[], dict[tuple[float, float], float]],
) -> Operation:
    """Wrap ``operation`` so its first evaluation allocates a memo cache.

    ``allocate`` is where the two implementations differ, and it is the only
    place they differ. On the cordis side it opens the cache as an effect of the
    fiber; on the conventional side it opens the same cache in the same way and
    files it on the plugin instance. Everything else about this function -- when
    the cache is created, what it holds, that it is created *during evaluation*
    rather than during setup -- is identical, which is what makes the residue
    numbers a property of the composition mechanism.
    """

    def call(left: float, right: float) -> float:
        cache = calculator.caches.get(operation.name)
        if cache is None:
            cache = allocate()
        key = memo_key(left, right)
        if key not in cache:
            cache[key] = operation.fn(left, right)
        return cache[key]

    return replace(operation, fn=call)


# --------------------------------------------------------------------------
# the operations themselves
#
# Pure data, shared by both implementations so that neither can be accused of
# doing different arithmetic. Only the composition mechanism differs.
# --------------------------------------------------------------------------

ARITHMETIC: tuple[Operation, ...] = (
    Operation("add", "+", 1, "add two numbers", lambda a, b: a + b),
    Operation("sub", "-", 1, "subtract the right from the left", lambda a, b: a - b),
    Operation("mul", "*", 2, "multiply two numbers", lambda a, b: a * b),
    Operation("div", "/", 2, "divide the left by the right", lambda a, b: a / b),
    Operation("pow", "^", 3, "raise the left to the right", lambda a, b: a**b),
)

#: The derived operation: remainder, which is *defined* as
#: ``a - floor(a / b) * b`` rather than computed directly. Its dependence on
#: division, multiplication and subtraction is a fact about the operation, not a
#: policy either implementation invented, which is what makes it the honest
#: subject of the removal scenario. :func:`derived_modulo` supplies the body.
DERIVED: Operation = Operation(
    "mod", "%", 2, "remainder after division", lambda a, b: a % b, depends=("-", "*", "/")
)

OPERATIONS: dict[str, Operation] = {op.name: op for op in (*ARITHMETIC, DERIVED)}

#: What each operation needs from the others. Only ``mod`` needs anything, and
#: both sides read this same table, so the declaration is identical and only its
#: consequences differ.
REQUIREMENTS: dict[str, tuple[str, ...]] = {"mod": ("op.sub", "op.mul", "op.div")}


#: The operation the failing plugin installs just before it fails. Shared, so
#: both sides install and remove exactly the same thing; only the arithmetic is
#: supplied locally, because each side closes it over its own broken table.
FACTORIAL: Operation = Operation(
    "factorial", "!", 3, "factorial of the left, times the right", lambda a, b: a * b
)


def with_fn(operation: Operation, fn: Callable[[float, float], float]) -> Operation:
    """The same operation, with different arithmetic behind it."""
    return replace(operation, fn=fn)


def requirements_for(name: str) -> tuple[str, ...]:
    """The operation keys ``name`` cannot work without."""
    return REQUIREMENTS.get(name, ())


def derived_modulo(calculator: Calculator) -> Operation:
    """Remainder, worked out the way the definition says: a - floor(a / b) * b.

    It looks each operation up in the table at call time rather than capturing
    it, which is the *charitable* implementation: a captured reference would go
    on working after its provider was removed and hide the problem. Looking them
    up live means this operation genuinely stops working when any of the three
    goes away -- and that is the point, because a calculator has no business
    advertising a remainder it cannot compute.
    """

    def modulo(left: float, right: float) -> float:
        div = calculator.operators["/"]
        mul = calculator.operators["*"]
        sub = calculator.operators["-"]
        whole = math.floor(div.fn(left, right))
        return sub.fn(left, mul.fn(whole, right))

    return replace(DERIVED, fn=modulo)


def operation_for(name: str, calculator: Calculator) -> Operation:
    """The operation to install for ``name``, bound to ``calculator``.

    Both implementations call this, so neither can be accused of installing a
    different ``mod`` from the other.
    """
    if name == DERIVED.name:
        return derived_modulo(calculator)
    return OPERATIONS[name]


def format_result(value: float) -> str:
    """Render a result the way a calculator would, without trailing zeros."""
    if value == int(value):
        return str(int(value))
    return f"{value:g}"


def describe(operations: Iterable[Operation]) -> dict[str, str]:
    """A plain symbol-to-name summary, for a demo to print."""
    return {operation.symbol: operation.name for operation in operations}
