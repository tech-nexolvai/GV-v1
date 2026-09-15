"""Identifier-keyed pairwise measurement comparison.

Mappings arrive from the resolved extraction/evidence pipeline. A missing counterpart is
therefore ``NOT_FOUND``: the operation knows that a comparison input is absent, but cannot
claim whether the drawing omitted it or extraction missed it. Verified pair failures still
dominate the aggregate result, while incomplete pairs can never contribute to a pass.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from units.measurement import Measurement
from units.policy import require_same_unit
from verdict.outcomes import Outcome
from verdict.registry import (
    Arity,
    OperationResult,
    OperationSpec,
    RuleAuthoringError,
    register,
)


@dataclass(frozen=True, slots=True)
class CountComparison:
    """The independently visible comparison of identifier counts."""

    left_count: int
    right_count: int
    outcome: Outcome
    comparison: str


@dataclass(frozen=True, slots=True)
class PairComparison:
    """One identifier's exact comparison or missing-counterpart result."""

    identifier: str
    left: Measurement | None
    right: Measurement | None
    delta: Measurement | None
    outcome: Outcome
    comparison: str


def _require_pairs(
    values: Mapping[str, Measurement] | Sequence[Measurement],
    name: str,
) -> Mapping[str, Measurement]:
    if isinstance(values, Mapping):
        pairs = values
    elif isinstance(values, Sequence) and not isinstance(values, (str, bytes, bytearray)):
        pairs = {f"#{index}": value for index, value in enumerate(values)}
    else:
        raise RuleAuthoringError(f"{name} must be a mapping keyed by identifier or an ordered list")

    for identifier, value in pairs.items():
        if not isinstance(identifier, str) or not identifier:
            raise RuleAuthoringError(f"{name} contains an invalid identifier")
        if not isinstance(value, Measurement):
            raise RuleAuthoringError(f"{name}[{identifier!r}] must be a Measurement")
    return pairs


def _missing_pair(
    identifier: str,
    left: Measurement | None,
    right: Measurement | None,
) -> PairComparison:
    missing_side = "left" if left is None else "right"
    return PairComparison(
        identifier=identifier,
        left=left,
        right=right,
        delta=None,
        outcome=Outcome.NOT_FOUND,
        comparison=f"{identifier}: {missing_side} counterpart not found",
    )


def pairwise_within_tolerance(
    *,
    left: Mapping[str, Measurement] | Sequence[Measurement],
    right: Mapping[str, Measurement] | Sequence[Measurement],
    tolerance: Measurement,
) -> OperationResult:
    """Compare exact identifier-matched pairs using ``delta <= tolerance``.

    Pairing is by mapping key when identifiers are present. Ordered reviewer-entered lists are
    paired by position using stable ``#0``/``#1`` identifiers, preserving the layout order the
    reviewer supplied. A missing counterpart produces an individually addressable ``NOT_FOUND``
    pair. Overall semantics are conjunction-like: any verified pair failure yields ``FAIL``;
    otherwise any missing pair yields ``NOT_FOUND``; only complete passing coverage yields ``PASS``.
    """

    left = _require_pairs(left, "left")
    right = _require_pairs(right, "right")
    if not isinstance(tolerance, Measurement):
        raise RuleAuthoringError("tolerance must be a Measurement")
    if not left and not right:
        raise ValueError("pairwise comparison requires at least one identifier")

    if tolerance.exact < 0:
        raise RuleAuthoringError("tolerance must not be negative")
    measurements = (*left.values(), *right.values())
    unit = require_same_unit(*measurements)
    if tolerance.unit is not unit:
        if tolerance.exact != 0:
            require_same_unit(*measurements, tolerance)
        tolerance = Measurement(tolerance.exact * 0, unit, tolerance.raw_text)

    counts_match = len(left) == len(right)
    count_comparison = CountComparison(
        left_count=len(left),
        right_count=len(right),
        outcome=Outcome.PASS if counts_match else Outcome.NOT_FOUND,
        comparison=f"left count {len(left)} {'==' if counts_match else '!='} right count {len(right)}",
    )
    intermediates: list[tuple[str, object]] = [("count_comparison", count_comparison)]
    pairs: list[PairComparison] = []

    for identifier in sorted(set(left) | set(right)):
        left_value = left.get(identifier)
        right_value = right.get(identifier)
        if left_value is None or right_value is None:
            pair = _missing_pair(identifier, left_value, right_value)
        else:
            delta = Measurement(abs(left_value.exact - right_value.exact), unit, None)
            passed = delta.exact <= tolerance.exact
            pair = PairComparison(
                identifier=identifier,
                left=left_value,
                right=right_value,
                delta=delta,
                outcome=Outcome.PASS if passed else Outcome.FAIL,
                comparison=(
                    f"{identifier}: |{left_value.exact} - {right_value.exact}| = "
                    f"{delta.exact} {'<=' if passed else '>'} {tolerance.exact} {unit.value}"
                ),
            )
        pairs.append(pair)
        intermediates.append((f"pair[{identifier}]", pair))

    if any(pair.outcome is Outcome.FAIL for pair in pairs):
        outcome = Outcome.FAIL
    elif any(pair.outcome is Outcome.NOT_FOUND for pair in pairs):
        outcome = Outcome.NOT_FOUND
    else:
        outcome = Outcome.PASS

    matched_deltas = [pair.delta for pair in pairs if pair.delta is not None]
    maximum_delta = max(matched_deltas, key=lambda value: value.exact) if matched_deltas else None
    comparison = (
        f"{len(pairs)} identifiers: "
        f"{sum(pair.outcome is Outcome.PASS for pair in pairs)} pass, "
        f"{sum(pair.outcome is Outcome.FAIL for pair in pairs)} fail, "
        f"{sum(pair.outcome is Outcome.NOT_FOUND for pair in pairs)} not found"
    )
    return OperationResult(
        outcome=outcome,
        delta=maximum_delta,
        intermediates=tuple(intermediates),
        comparison=comparison,
        tolerance=tolerance,
    )


PAIRWISE_SPECS: tuple[OperationSpec, ...] = (
    OperationSpec(
        "pairwise_within_tolerance",
        "1.0.0",
        {"left": Arity.LIST, "right": Arity.LIST, "tolerance": Arity.SCALAR},
        pairwise_within_tolerance,
    ),
)


def register_pairwise_operations() -> None:
    """Register the reviewed pairwise operation exactly once."""

    for spec in PAIRWISE_SPECS:
        register(spec)
