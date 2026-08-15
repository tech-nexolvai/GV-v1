"""Exact validation for dimension chains assembled by extraction.

A chain closes only when at least two segments sum exactly to its authored overall in
one authored unit system. This is an extraction-quality check, not a tolerance rule:
it reports discrepancies and never adjusts values to make a chain fit.

Source: ``docs/V1_RESEARCH_AND_PLAN.md`` section F3 and issue #122.
Verification: ``tests/extraction/test_chains.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from units.measurement import Measurement
from units.policy import require_same_unit


class ClosureStatus(StrEnum):
    """Whether a dimension chain can be verified and closes exactly."""

    CLOSED = "closed"
    NOT_CLOSED = "not_closed"
    NOT_VERIFIABLE = "not_verifiable"


@dataclass(frozen=True, slots=True)
class DimensionChain:
    """An authored overall and the ordered segments beneath it.

    A nested chain contributes its authored ``overall`` to its parent. Its own
    segments are validated independently and appear in the child result.
    """

    segments: tuple[Measurement | DimensionChain, ...]
    overall: Measurement | None


@dataclass(frozen=True, slots=True)
class ClosureResult:
    """Exact closure details for one chain level and any nested child chains.

    ``difference`` is ``actual - expected``. It is absent when comparison is not
    possible. ``status`` describes this chain level; callers inspect ``children``
    for the independently validated nested levels.
    """

    status: ClosureStatus
    expected: Measurement | None
    actual: Measurement | None
    difference: Measurement | None
    children: tuple[ClosureResult, ...]


def validate_closure(chain: DimensionChain) -> ClosureResult:
    """Validate one dimension chain exactly in its authored unit.

    Empty and single-segment chains are not evidence of closure. Mixed authored
    units raise ``MixedUnitError`` through ``require_same_unit`` rather than being
    converted or misreported as a geometric discrepancy.
    """

    if not isinstance(chain, DimensionChain):
        raise TypeError("chain must be a DimensionChain")

    children = tuple(
        validate_closure(segment)
        for segment in chain.segments
        if isinstance(segment, DimensionChain)
    )
    measurements: list[Measurement] = []
    unresolved_child = False
    for segment in chain.segments:
        if isinstance(segment, Measurement):
            measurements.append(segment)
        elif isinstance(segment, DimensionChain):
            if segment.overall is None:
                unresolved_child = True
            else:
                measurements.append(segment.overall)
        else:
            raise TypeError("segments must contain only Measurement or DimensionChain values")

    values_for_unit_check = [*measurements]
    if chain.overall is not None:
        values_for_unit_check.append(chain.overall)
    if values_for_unit_check:
        require_same_unit(*values_for_unit_check)

    actual = _sum_measurements(measurements) if measurements else None
    if len(chain.segments) < 2 or chain.overall is None or unresolved_child:
        return ClosureResult(
            status=ClosureStatus.NOT_VERIFIABLE,
            expected=chain.overall,
            actual=actual,
            difference=None,
            children=children,
        )

    assert actual is not None
    difference = actual - chain.overall
    status = ClosureStatus.CLOSED if difference.exact == 0 else ClosureStatus.NOT_CLOSED
    return ClosureResult(
        status=status,
        expected=chain.overall,
        actual=actual,
        difference=difference,
        children=children,
    )


def _sum_measurements(measurements: list[Measurement]) -> Measurement:
    """Sum a non-empty, same-unit measurement list exactly."""

    total = measurements[0]
    for measurement in measurements[1:]:
        total = total + measurement
    return total
