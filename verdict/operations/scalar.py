"""Exact scalar operations selected through the verdict registry.

Except for the two presence checks, operations receive resolved, qualified values.
The engine owns evidence absence and ambiguity, and later combines these calculation
facts with provenance and version metadata to build the final trace.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from enum import StrEnum
from fractions import Fraction

from units.measurement import Measurement
from units.policy import require_same_unit
from verdict.outcomes import Outcome
from verdict.registry import (
    Arity,
    DerivationResult,
    OperationKind,
    OperationResult,
    OperationSpec,
    RuleAuthoringError,
    register,
)

type ScalarValue = Measurement | str | StrEnum | int | Fraction
type ExactNumber = Measurement | int | Fraction


def _is_absent(value: object | None) -> bool:
    """Return whether a presence operation considers a value absent."""

    if value is None:
        return True
    return isinstance(value, Collection) and len(value) == 0


def _reject_inexact_number(value: object, name: str) -> None:
    """Reject binary floating-point input without admitting it to verdict types."""

    if type(value).__name__ == "float":
        raise RuleAuthoringError(f"{name} must never be a float; provide an exact value")


def _require_value(value: object | None, name: str) -> object:
    if value is None:
        raise TypeError(f"{name} must be resolved before the operation runs")
    _reject_inexact_number(value, name)
    return value


def _require_measurement(value: object | None, name: str) -> Measurement:
    resolved = _require_value(value, name)
    if not isinstance(resolved, Measurement):
        raise RuleAuthoringError(f"{name} must be a Measurement")
    return resolved


def _value_text(value: object) -> str:
    if isinstance(value, Measurement):
        return f"{value.exact} {value.unit.value}"
    if isinstance(value, StrEnum):
        return repr(value.value)
    return repr(value)


def _result(
    outcome: Outcome,
    comparison: str,
    *,
    tolerance: Measurement | None,
    delta: Measurement | None = None,
    intermediates: tuple[tuple[str, object], ...] = (),
) -> OperationResult:
    return OperationResult(outcome, delta, intermediates, comparison, tolerance)


def _validate_equal_types(actual: object, expected: object) -> None:
    supported = (Measurement, str, StrEnum, int, Fraction)
    if isinstance(actual, bool) or isinstance(expected, bool):
        raise RuleAuthoringError("equals does not accept bool operands")
    _reject_inexact_number(actual, "actual")
    _reject_inexact_number(expected, "expected")
    if not isinstance(actual, supported) or not isinstance(expected, supported):
        raise RuleAuthoringError("equals received an unsupported operand type")
    if type(actual) is not type(expected):
        raise RuleAuthoringError("equals operands must have the same type")
    if isinstance(actual, Measurement) and isinstance(expected, Measurement):
        require_same_unit(actual, expected)


def exists(*, value: object | None) -> OperationResult:
    """Pass when a value is present; ``None`` and empty text/collections fail.

    Numeric zero is present. Float input is rejected because the verdict path permits only
    exact authored numbers.
    """

    if value is not None:
        _reject_inexact_number(value, "value")
    present = not _is_absent(value)
    return _result(
        Outcome.PASS if present else Outcome.FAIL,
        "value is present" if present else "value is absent",
        tolerance=None,
    )


def equals(*, actual: ScalarValue, expected: ScalarValue) -> OperationResult:
    """Pass on exact, same-type equality; measurements require one authored unit."""

    _require_value(actual, "actual")
    _require_value(expected, "expected")
    _validate_equal_types(actual, expected)
    matched = actual == expected
    return _result(
        Outcome.PASS if matched else Outcome.FAIL,
        f"{_value_text(actual)} {'==' if matched else '!='} {_value_text(expected)}",
        tolerance=None,
    )


def within_tolerance(
    *, actual: Measurement, expected: Measurement, tolerance: Measurement
) -> OperationResult:
    """Pass when ``|actual - expected| <= tolerance``; equality is inclusive."""

    actual = _require_measurement(actual, "actual")
    expected = _require_measurement(expected, "expected")
    tolerance = _require_measurement(tolerance, "tolerance")
    unit = require_same_unit(actual, expected, tolerance)
    if tolerance.exact < 0:
        raise RuleAuthoringError("tolerance must not be negative")
    delta = Measurement(abs(actual.exact - expected.exact), unit, None)
    passed = delta.exact <= tolerance.exact
    comparison = (
        f"|{actual.exact} - {expected.exact}| = {delta.exact} "
        f"{'<=' if passed else '>'} {tolerance.exact} {unit.value}"
    )
    return _result(
        Outcome.PASS if passed else Outcome.FAIL,
        comparison,
        delta=delta,
        intermediates=(("absolute_difference", delta),),
        tolerance=tolerance,
    )


def minimum(*, x: Measurement, bound: Measurement) -> OperationResult:
    """Pass when ``x >= bound``; equality at the minimum is a pass."""

    x = _require_measurement(x, "x")
    bound = _require_measurement(bound, "bound")
    unit = require_same_unit(x, bound)
    passed = x.exact >= bound.exact
    return _result(
        Outcome.PASS if passed else Outcome.FAIL,
        f"{x.exact} {'>=' if passed else '<'} {bound.exact} {unit.value}",
        tolerance=None,
    )


def maximum(*, x: Measurement, bound: Measurement) -> OperationResult:
    """Pass when ``x <= bound``; equality at the maximum is a pass."""

    x = _require_measurement(x, "x")
    bound = _require_measurement(bound, "bound")
    unit = require_same_unit(x, bound)
    passed = x.exact <= bound.exact
    return _result(
        Outcome.PASS if passed else Outcome.FAIL,
        f"{x.exact} {'<=' if passed else '>'} {bound.exact} {unit.value}",
        tolerance=None,
    )


def between(*, x: Measurement, lo: Measurement, hi: Measurement) -> OperationResult:
    """Pass when ``lo <= x <= hi``; both boundaries are inclusive."""

    x = _require_measurement(x, "x")
    lo = _require_measurement(lo, "lo")
    hi = _require_measurement(hi, "hi")
    unit = require_same_unit(x, lo, hi)
    if lo.exact > hi.exact:
        raise RuleAuthoringError("between lower bound must not exceed upper bound")
    passed = lo.exact <= x.exact <= hi.exact
    return _result(
        Outcome.PASS if passed else Outcome.FAIL,
        f"{lo.exact} <= {x.exact} <= {hi.exact} {unit.value} is {passed}",
        tolerance=None,
    )


def one_of(*, x: ScalarValue, set: Sequence[ScalarValue]) -> OperationResult:
    """Pass when ``x`` exactly matches a member of a non-empty allowed sequence."""

    _require_value(x, "x")
    if isinstance(set, (str, bytes, bytearray)) or not isinstance(set, Sequence):
        raise RuleAuthoringError("set must have list arity")
    if not set:
        raise RuleAuthoringError("one_of allowed values must not be empty")
    for candidate in set:
        _require_value(candidate, "allowed value")
        _validate_equal_types(x, candidate)
    matched = x in set
    return _result(
        Outcome.PASS if matched else Outcome.FAIL,
        f"{_value_text(x)} {'is' if matched else 'is not'} one of {len(set)} allowed values",
        tolerance=None,
    )


def contains(*, text: str, substr: str) -> OperationResult:
    """Pass on a literal, case-sensitive substring match with no normalization."""

    _require_value(text, "text")
    _require_value(substr, "substr")
    if type(text) is not str or type(substr) is not str:
        raise RuleAuthoringError("contains operands must be strings")
    matched = substr in text
    return _result(
        Outcome.PASS if matched else Outcome.FAIL,
        f"{substr!r} {'is' if matched else 'is not'} contained in {text!r}",
        tolerance=None,
    )


def difference_between(*, a: ExactNumber, b: ExactNumber) -> DerivationResult:
    """Return exact ``a - b`` for use by a later verdict operation."""

    _require_value(a, "a")
    _require_value(b, "b")
    if isinstance(a, bool) or isinstance(b, bool):
        raise RuleAuthoringError("difference_between does not accept bool operands")
    if type(a) is not type(b):
        raise RuleAuthoringError("difference_between operands must have the same type")
    if isinstance(a, Measurement) and isinstance(b, Measurement):
        require_same_unit(a, b)
        value: Measurement | Fraction | int = a - b
    elif (type(a) is int and type(b) is int) or (
        isinstance(a, Fraction) and isinstance(b, Fraction)
    ):
        value = a - b
    else:
        raise RuleAuthoringError("difference_between requires exact numeric operands")
    return DerivationResult(
        value=value,
        intermediates=(("difference", value),),
        expression=f"{_value_text(a)} - {_value_text(b)} = {_value_text(value)}",
    )


def conditional_required(*, when: bool, value: object | None) -> OperationResult:
    """Require a present value only when ``when`` is true.

    A false condition passes because the rule applied and found no requirement. The explicit
    ``requirement_exercised`` intermediate lets reporting distinguish that pass from a value
    that was actually checked. A true condition with an absent value yields ``NOT_FOUND``.
    """

    if type(when) is not bool:
        raise RuleAuthoringError("when must be a bool")
    if value is not None:
        _reject_inexact_number(value, "value")
    if not when:
        return _result(
            Outcome.PASS,
            "requirement not exercised because condition is false",
            tolerance=None,
            intermediates=(("requirement_exercised", False),),
        )
    present = not _is_absent(value)
    return _result(
        Outcome.PASS if present else Outcome.NOT_FOUND,
        "required value is present" if present else "required value is absent",
        tolerance=None,
        intermediates=(("requirement_exercised", True),),
    )


SCALAR_SPECS: tuple[OperationSpec, ...] = (
    OperationSpec("exists", "1.0.0", {"value": Arity.SCALAR}, exists),
    OperationSpec("equals", "1.0.0", {"actual": Arity.SCALAR, "expected": Arity.SCALAR}, equals),
    OperationSpec(
        "within_tolerance",
        "1.0.0",
        {"actual": Arity.SCALAR, "expected": Arity.SCALAR, "tolerance": Arity.SCALAR},
        within_tolerance,
    ),
    OperationSpec("minimum", "1.0.0", {"x": Arity.SCALAR, "bound": Arity.SCALAR}, minimum),
    OperationSpec("maximum", "1.0.0", {"x": Arity.SCALAR, "bound": Arity.SCALAR}, maximum),
    OperationSpec(
        "between",
        "1.0.0",
        {"x": Arity.SCALAR, "lo": Arity.SCALAR, "hi": Arity.SCALAR},
        between,
    ),
    OperationSpec("one_of", "1.0.0", {"x": Arity.SCALAR, "set": Arity.LIST}, one_of),
    OperationSpec("contains", "1.0.0", {"text": Arity.SCALAR, "substr": Arity.SCALAR}, contains),
    OperationSpec(
        "difference_between",
        "1.0.0",
        {"a": Arity.SCALAR, "b": Arity.SCALAR},
        difference_between,
        OperationKind.DERIVATION,
    ),
    OperationSpec(
        "conditional_required",
        "1.0.0",
        {"when": Arity.SCALAR, "value": Arity.SCALAR},
        conditional_required,
    ),
)


def register_scalar_operations() -> None:
    """Register every reviewed scalar operation exactly once."""

    for spec in SCALAR_SPECS:
        register(spec)
