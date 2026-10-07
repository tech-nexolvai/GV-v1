"""Exact aggregate operations for variable-length resolved operand lists.

The engine rejects missing required lists before calling these functions. Operations that
need at least one measurement also reject an empty sequence defensively, so an empty sum can
never become a plausible zero. Ordered intermediates preserve every value and running total
for the engine's final provenance-rich trace.
"""

from __future__ import annotations

from collections.abc import Sequence

from units.imperial import format_inches
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


def _abstain(outcome: Outcome, why: str, *intermediates: tuple[str, object]) -> OperationResult:
    return OperationResult(
        outcome=outcome,
        delta=None,
        intermediates=tuple(intermediates),
        comparison=why,
        tolerance=None,
    )


def _written(value: Measurement) -> str:
    """`40 3/8 in`, as the drawing writes it — never `323/8` (see `scalar._value_text`)."""
    return f"{format_inches(value.exact)} {value.unit.value}"


def _total(values: Sequence[Measurement], name: str) -> tuple[Measurement, str]:
    measurements = _require_measurements(values, name)
    total, _running = _running_totals(measurements)
    addends = " + ".join(format_inches(value.exact) for value in measurements)
    return total, f"{name} {addends} = {_written(total)}"


def row_total(
    *,
    pieces: Sequence[Measurement] | None,
    cabinets: Sequence[Measurement] | None,
    fillers: Sequence[Measurement] | None,
) -> DerivationResult | OperationResult:
    """The exact length of one countertop's row, from whichever complete source was given (#991).

    A row can be stated two ways: as its **pieces** left to right, whatever each one is, or as its
    **cabinets** and its **fillers**. The sum is the same either way — which is why the width check
    no longer needs to know which piece is which — and the kinds are the part readers get wrong.

    ``None`` means *nobody supplied that list* (the engine's word for it, `OperationSpec.optional`).
    An empty list never arrives: the engine returns the rule's ``on_missing`` before this runs.

    | pieces | cabinets and fillers   | result                                                    |
    |--------|------------------------|-----------------------------------------------------------|
    | given  | neither                | the pieces' sum                                           |
    | none   | both                   | cabinets + fillers, exactly as `CT-WIDTH-001` 1.0.1 did    |
    | given  | both, same total       | that total, with both sums in the trace                   |
    | given  | both, different totals | REVIEW_REQUIRED — two statements of one row disagree      |
    | given  | only one of the two    | REVIEW_REQUIRED — a half-stated second source can neither |
    |        |                        | confirm the pieces nor be safely ignored                  |
    | none   | one or neither         | NOT_FOUND — no complete statement of the row              |

    **Never picks a source.** Two disagreeing totals are a question about the drawing or the form,
    and preferring one would make whichever we preferred the answer — the failure `AGENTS.md` §2.4
    forbids. Mixed units across the lists raise, and the engine turns that into REVIEW_REQUIRED.
    """
    kinds_given = (cabinets is not None, fillers is not None)
    if pieces is None:
        if cabinets is None or fillers is None:
            absent = [
                name
                for name, value in (("cabinets", cabinets), ("fillers", fillers))
                if value is None
            ]
            return _abstain(
                Outcome.NOT_FOUND,
                "no piece widths were given for the row, and its "
                f"{' and '.join(absent)} widths are missing too, so the row's length is unknown. "
                "A missing piece is not zero.",
            )
        cabinet_total, cabinet_text = _total(cabinets, "cabinets")
        filler_total, filler_text = _total(fillers, "fillers")
        unit = require_same_unit(cabinet_total, filler_total)
        total = Measurement(cabinet_total.exact + filler_total.exact, unit, None)
        return DerivationResult(
            value=total,
            intermediates=(
                ("source", "cabinets and fillers"),
                ("cabinet_total", cabinet_total),
                ("filler_total", filler_total),
            ),
            expression=f"{cabinet_text}; {filler_text}; row = {_written(total)}",
        )

    piece_total, piece_text = _total(pieces, "pieces")
    if kinds_given == (False, False):
        return DerivationResult(
            value=piece_total,
            intermediates=(("source", "pieces"), ("piece_total", piece_total)),
            expression=piece_text,
        )
    if cabinets is None or fillers is None:
        given = "cabinet" if cabinets is not None else "filler"
        return _abstain(
            Outcome.REVIEW_REQUIRED,
            f"the row's pieces add up to {_written(piece_total)}, and a "
            f"{given} list was also given without the "
            f"{'filler' if given == 'cabinet' else 'cabinet'} list. Half a second statement of the "
            "row can neither confirm the pieces nor be ignored: complete it or clear it.",
            ("piece_total", piece_total),
        )

    cabinet_total, cabinet_text = _total(cabinets, "cabinets")
    filler_total, filler_text = _total(fillers, "fillers")
    unit = require_same_unit(piece_total, cabinet_total, filler_total)
    kinds_total = Measurement(cabinet_total.exact + filler_total.exact, unit, None)
    if kinds_total.exact != piece_total.exact:
        return _abstain(
            Outcome.REVIEW_REQUIRED,
            f"the row's pieces add up to {_written(piece_total)}, but its cabinets and fillers "
            f"add up to {_written(kinds_total)}. Two statements of the same row "
            "disagree, and neither is preferred.",
            ("piece_total", piece_total),
            ("cabinet_total", cabinet_total),
            ("filler_total", filler_total),
        )
    return DerivationResult(
        value=piece_total,
        intermediates=(
            ("source", "pieces, confirmed by cabinets and fillers"),
            ("piece_total", piece_total),
            ("cabinet_total", cabinet_total),
            ("filler_total", filler_total),
        ),
        expression=f"{piece_text}; {cabinet_text}; {filler_text}; row = {_written(piece_total)}",
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
    OperationSpec(
        "row_total",
        "1.0.0",
        {"pieces": Arity.LIST, "cabinets": Arity.LIST, "fillers": Arity.LIST},
        row_total,
        OperationKind.DERIVATION,
        optional=frozenset({"pieces", "cabinets", "fillers"}),
    ),
)


def register_aggregate_operations() -> None:
    """Register every reviewed aggregate operation exactly once."""

    for spec in AGGREGATE_SPECS:
        register(spec)
