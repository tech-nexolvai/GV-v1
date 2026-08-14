"""Exact one-dimensional alignment operation.

The caller establishes a common coordinate axis before invoking this module. The
operation receives only exact numbers and cannot detect a mixture of X and Y
coordinates; recording the declared axis keeps that caller-owned fact visible to a
reviewer.
"""

from __future__ import annotations

from collections.abc import Sequence

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


def _require_positions(positions: object) -> tuple[Measurement, ...]:
    """Return a defensively validated sequence of exact coordinates."""

    if isinstance(positions, (str, bytes, bytearray)) or not isinstance(positions, Sequence):
        raise RuleAuthoringError("positions must have list arity")
    checked: list[Measurement] = []
    for index, position in enumerate(positions):
        if not isinstance(position, Measurement):
            raise RuleAuthoringError(f"positions[{index}] must be a Measurement")
        checked.append(position)
    return tuple(checked)


def alignment(
    *,
    positions: Sequence[Measurement],
    tolerance: Measurement,
    axis: str,
) -> OperationResult:
    """Check one-dimensional coordinates using ``max - min <= tolerance``.

    The boundary is inclusive and all arithmetic is exact. Range is used instead of
    comparing every position with a chosen datum: datum comparison can accept a total
    spread of twice the tolerance, and its answer depends on which datum was selected.
    ``max - min`` is reference-free and strictly tighter.

    The caller guarantees that every coordinate belongs to ``axis``. This pure numeric
    operation cannot distinguish an X coordinate from a Y coordinate, so it records the
    declared axis in its trace facts rather than pretending to verify that association.
    Fewer than two coordinates produces ``NOT_FOUND`` instead of a vacuous pass.
    """

    checked = _require_positions(positions)
    if not isinstance(tolerance, Measurement):
        raise RuleAuthoringError("tolerance must be a Measurement")
    if not isinstance(axis, str) or not axis.strip():
        raise RuleAuthoringError("axis must be a non-empty string established by the caller")

    unit = require_same_unit(*checked, tolerance)
    if tolerance.exact < 0:
        raise RuleAuthoringError("tolerance must not be negative")

    position_facts = tuple((f"position[{index}]", value) for index, value in enumerate(checked))
    if len(checked) < 2:
        return OperationResult(
            outcome=Outcome.NOT_FOUND,
            delta=None,
            intermediates=(("axis", axis), *position_facts),
            comparison=(
                f"axis {axis!r}: alignment requires at least two positions; found {len(checked)}"
            ),
            tolerance=tolerance,
        )

    minimum = min(checked, key=lambda value: value.exact)
    maximum = max(checked, key=lambda value: value.exact)
    spread = Measurement(maximum.exact - minimum.exact, unit, None)
    passed = spread.exact <= tolerance.exact
    comparison = (
        f"axis {axis!r}: max {maximum.exact} - min {minimum.exact} = {spread.exact} "
        f"{'<=' if passed else '>'} {tolerance.exact} {unit.value}"
    )
    return OperationResult(
        outcome=Outcome.PASS if passed else Outcome.FAIL,
        delta=spread,
        intermediates=(
            ("axis", axis),
            *position_facts,
            ("minimum", minimum),
            ("maximum", maximum),
            ("spread", spread),
        ),
        comparison=comparison,
        tolerance=tolerance,
    )


ALIGNMENT_SPECS: tuple[OperationSpec, ...] = (
    OperationSpec(
        "alignment",
        "1.0.0",
        {"positions": Arity.LIST, "tolerance": Arity.SCALAR, "axis": Arity.SCALAR},
        alignment,
    ),
)


def register_alignment_operations() -> None:
    """Register the reviewed alignment operation exactly once."""

    for spec in ALIGNMENT_SPECS:
        register(spec)
