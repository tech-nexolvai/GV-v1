"""Exact aggregate operations for variable-length resolved operand lists.

The engine rejects missing required lists before calling these functions. Operations that
need at least one measurement also reject an empty sequence defensively, so an empty sum can
never become a plausible zero. Ordered intermediates preserve every value and running total
for the engine's final provenance-rich trace.
"""

from __future__ import annotations

from collections.abc import Sequence

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


def _require_sequence(values: object, name: str) -> Sequence[object]:
    if isinstance(values, (str, bytes, bytearray)) or not isinstance(values, Sequence):
        raise RuleAuthoringError(f"{name} must have list arity")
    return values


def _require_measurements(
    values: Sequence[Measurement], name: str, *, allow_empty: bool = False
) -> tuple[Measurement, ...]:
    sequence = _require_sequence(values, name)
    if not sequence and not allow_empty:
        raise ValueError(f"{name} requires at least one value; an empty sum is not zero")
    measurements: list[Measurement] = []
    for index, value in enumerate(sequence):
        if not isinstance(value, Measurement):
            raise RuleAuthoringError(f"{name}[{index}] must be a Measurement")
        measurements.append(value)
    if measurements:
        require_same_unit(*measurements)
    return tuple(measurements)


def _running_totals(
    values: tuple[Measurement, ...],
) -> tuple[Measurement, tuple[tuple[str, object], ...]]:
    unit = require_same_unit(*values)
    exact_total = values[0].exact * 0
    intermediates: list[tuple[str, object]] = []
    for index, value in enumerate(values):
        exact_total += value.exact
        running = Measurement(exact_total, unit, None)
        intermediates.append((f"addend[{index}]", value))
        intermediates.append((f"running_total[{index}]", running))
    return Measurement(exact_total, unit, None), tuple(intermediates)


def sum(*, values: Sequence[Measurement]) -> DerivationResult:
    """Return the exact sum of one or more same-unit measurements.

    Empty input raises rather than becoming zero; the engine maps a missing required list to
    ``NOT_FOUND`` before derivation execution.
    """

    measurements = _require_measurements(values, "values")
    total, intermediates = _running_totals(measurements)
    expression = " + ".join(str(value.exact) for value in measurements)
    return DerivationResult(
        value=total,
        intermediates=intermediates,
        expression=f"{expression} = {total.exact} {total.unit.value}",
    )


def count(*, values: Sequence[object]) -> DerivationResult:
    """Return the exact number of values; an empty list has count zero."""

    sequence = _require_sequence(values, "values")
    result = len(sequence)
    return DerivationResult(
        value=result,
        intermediates=(("count", result),),
        expression=f"count({result} values) = {result}",
    )


def count_equals(*, values: Sequence[object], n: int) -> OperationResult:
    """Pass when the list length equals the exact non-negative integer ``n``."""

    sequence = _require_sequence(values, "values")
    if type(n) is not int or n < 0:
        raise RuleAuthoringError("n must be a non-negative integer")
    actual = len(sequence)
    passed = actual == n
    return OperationResult(
        outcome=Outcome.PASS if passed else Outcome.FAIL,
        delta=None,
        intermediates=(("count", actual),),
        comparison=f"count = {actual} {'==' if passed else '!='} {n}",
        tolerance=None,
    )


def sum_within_tolerance(
    *,
    target: Measurement,
    addends: Sequence[Measurement],
    tolerance: Measurement,
) -> OperationResult:
    """Pass when ``|target - sum(addends)| <= tolerance``; equality is inclusive."""

    if not isinstance(target, Measurement):
        raise RuleAuthoringError("target must be a Measurement")
    if not isinstance(tolerance, Measurement):
        raise RuleAuthoringError("tolerance must be a Measurement")
    measurements = _require_measurements(addends, "addends")
    total, running = _running_totals(measurements)
    unit = require_same_unit(target, total, tolerance)
    if tolerance.exact < 0:
        raise RuleAuthoringError("tolerance must not be negative")
    delta = Measurement(abs(target.exact - total.exact), unit, None)
    passed = delta.exact <= tolerance.exact
    comparison = (
        f"|{target.exact} - {total.exact}| = {delta.exact} "
        f"{'<=' if passed else '>'} {tolerance.exact} {unit.value}"
    )
    return OperationResult(
        outcome=Outcome.PASS if passed else Outcome.FAIL,
        delta=delta,
        intermediates=(*running, ("sum", total), ("absolute_difference", delta)),
        comparison=comparison,
        tolerance=tolerance,
    )


def all_within_tolerance(
    *,
    values: Sequence[Measurement],
    expected: Measurement,
    tolerance: Measurement,
) -> OperationResult:
    """Pass when every value is within ``<= tolerance`` of ``expected``.

    Empty input raises rather than passing vacuously. The reported delta is the largest
    absolute difference, which is sufficient to reconstruct the boundary decision.
    """

    if not isinstance(expected, Measurement):
        raise RuleAuthoringError("expected must be a Measurement")
    if not isinstance(tolerance, Measurement):
        raise RuleAuthoringError("tolerance must be a Measurement")
    measurements = _require_measurements(values, "values")
    unit = require_same_unit(*measurements, expected, tolerance)
    if tolerance.exact < 0:
        raise RuleAuthoringError("tolerance must not be negative")

    intermediates: list[tuple[str, object]] = []
    deltas: list[Measurement] = []
    for index, value in enumerate(measurements):
        delta = Measurement(abs(value.exact - expected.exact), unit, None)
        intermediates.append((f"value[{index}]", value))
        intermediates.append((f"absolute_difference[{index}]", delta))
        deltas.append(delta)
    maximum_delta = max(deltas, key=lambda item: item.exact)
    passed = maximum_delta.exact <= tolerance.exact
    comparison = (
        f"maximum absolute difference {maximum_delta.exact} "
        f"{'<=' if passed else '>'} {tolerance.exact} {unit.value}"
    )
    return OperationResult(
        outcome=Outcome.PASS if passed else Outcome.FAIL,
        delta=maximum_delta,
        intermediates=tuple(intermediates),
        comparison=comparison,
        tolerance=tolerance,
    )


AGGREGATE_SPECS: tuple[OperationSpec, ...] = (
    OperationSpec("sum", "1.0.0", {"values": Arity.LIST}, sum, OperationKind.DERIVATION),
    OperationSpec("count", "1.0.0", {"values": Arity.LIST}, count, OperationKind.DERIVATION),
    OperationSpec(
        "count_equals",
        "1.0.0",
        {"values": Arity.LIST, "n": Arity.SCALAR},
        count_equals,
    ),
    OperationSpec(
        "sum_within_tolerance",
        "1.0.0",
        {"target": Arity.SCALAR, "addends": Arity.LIST, "tolerance": Arity.SCALAR},
        sum_within_tolerance,
    ),
    OperationSpec(
        "all_within_tolerance",
        "1.0.0",
        {"values": Arity.LIST, "expected": Arity.SCALAR, "tolerance": Arity.SCALAR},
        all_within_tolerance,
    ),
)


def register_aggregate_operations() -> None:
    """Register every reviewed aggregate operation exactly once."""

    for spec in AGGREGATE_SPECS:
        register(spec)
