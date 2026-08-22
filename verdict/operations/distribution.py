"""Exact filler-first distribution for one ordered left/right cabinet run.

The operation calculates a derived expectation for a reviewer. It never selects a cabinet or
issues a fabrication instruction. When the two fillers cannot absorb the site difference inside
their configured bounds, it returns ``REVIEW_REQUIRED`` with the remaining difference recorded.

Source: issue #61; Cabinet_Checks.xlsx H18-H25 and N18-N22; client facts Q8, Q9 and Q21.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from fractions import Fraction

from units.measurement import Measurement
from units.policy import require_same_unit
from verdict.outcomes import Outcome
from verdict.registry import Arity, OperationResult, OperationSpec, RuleAuthoringError, register


class DistributionCondition(StrEnum):
    """Why the operation decided or abstained."""

    FILLERS_ABSORB = "fillers_absorb"
    CABINET_SELECTION_REQUIRED = "cabinet_selection_required"


def _pair(values: Sequence[Measurement], name: str) -> tuple[Measurement, Measurement]:
    if isinstance(values, (str, bytes, bytearray)) or not isinstance(values, Sequence):
        raise RuleAuthoringError(f"{name} must have list arity")
    if len(values) != 2:
        raise RuleAuthoringError(
            f"{name} must contain exactly two ordered values: left filler, then right filler"
        )
    left, right = values
    if not isinstance(left, Measurement) or not isinstance(right, Measurement):
        raise RuleAuthoringError(f"{name} values must be Measurements")
    return left, right


def _measurement(value: object, name: str) -> Measurement:
    if not isinstance(value, Measurement):
        raise RuleAuthoringError(f"{name} must be a Measurement")
    return value


def filler_distribution(
    *,
    field_width: Measurement,
    design_width: Measurement,
    design_fillers: Sequence[Measurement],
    proposed_fillers: Sequence[Measurement],
    filler_min: Measurement,
    filler_max: Measurement,
    allow_asymmetric: int,
) -> OperationResult:
    """Check an exact, filler-first response to a changed site width.

    Both filler sequences are ordered ``(left, right)``. The expected filler total is the
    architectural filler total plus ``field_width - design_width``. In the ordinary U.N.O. branch
    the expected total is divided equally. A reviewer-established noted branch may be asymmetric,
    but its two values must still sum exactly and each remain within the configured bounds.

    If the fillers cannot absorb the difference, the operation abstains and records the unabsorbed
    amount. It deliberately does not accept cabinet operands: Q9 assigns cabinet selection to the
    reviewer, so no code path here can move a non-adjustable cabinet.
    """

    field_width = _measurement(field_width, "field_width")
    design_width = _measurement(design_width, "design_width")
    filler_min = _measurement(filler_min, "filler_min")
    filler_max = _measurement(filler_max, "filler_max")
    design_left, design_right = _pair(design_fillers, "design_fillers")
    proposed_left, proposed_right = _pair(proposed_fillers, "proposed_fillers")
    if type(allow_asymmetric) is not int or allow_asymmetric not in (0, 1):
        raise RuleAuthoringError("allow_asymmetric must be the reviewed integer 0 or 1")

    unit = require_same_unit(
        field_width,
        design_width,
        design_left,
        design_right,
        proposed_left,
        proposed_right,
        filler_min,
        filler_max,
    )
    if filler_min.exact < 0:
        raise RuleAuthoringError("filler_min must not be negative")
    if filler_min.exact > filler_max.exact:
        raise RuleAuthoringError("filler_min must not exceed filler_max")

    site_difference = Measurement(field_width.exact - design_width.exact, unit, None)
    design_total = Measurement(design_left.exact + design_right.exact, unit, None)
    expected_total = Measurement(design_total.exact + site_difference.exact, unit, None)
    lower_total = Measurement(filler_min.exact * 2, unit, None)
    upper_total = Measurement(filler_max.exact * 2, unit, None)
    proposed_total = Measurement(proposed_left.exact + proposed_right.exact, unit, None)

    common_intermediates: tuple[tuple[str, object], ...] = (
        ("ordered_design_fillers", (design_left, design_right)),
        ("ordered_proposed_fillers", (proposed_left, proposed_right)),
        ("site_difference", site_difference),
        ("design_filler_total", design_total),
        ("expected_filler_total", expected_total),
        ("allowed_filler_total", (lower_total, upper_total)),
    )

    if expected_total.exact < lower_total.exact or expected_total.exact > upper_total.exact:
        boundary = lower_total if expected_total.exact < lower_total.exact else upper_total
        remaining = Measurement(expected_total.exact - boundary.exact, unit, None)
        direction = "reduce" if remaining.exact < 0 else "increase"
        return OperationResult(
            outcome=Outcome.REVIEW_REQUIRED,
            delta=Measurement(abs(remaining.exact), unit, None),
            intermediates=(
                *common_intermediates,
                ("condition", DistributionCondition.CABINET_SELECTION_REQUIRED.value),
                ("bounded_filler_total", boundary),
                ("remaining_difference", remaining),
                ("cabinet_adjustment", "reviewer_selection_required; no cabinet selected"),
            ),
            comparison=(
                f"fillers cannot absorb the site difference within {filler_min.exact}.."
                f"{filler_max.exact} {unit.value}; reviewer must select an adjustable cabinet "
                f"to {direction} by {abs(remaining.exact)} {unit.value}"
            ),
            tolerance=None,
        )

    bounds_ok = all(
        filler_min.exact <= filler.exact <= filler_max.exact
        for filler in (proposed_left, proposed_right)
    )
    total_ok = proposed_total.exact == expected_total.exact
    expected_pair: tuple[Measurement, Measurement] | None = None
    symmetry_ok = True
    if not allow_asymmetric:
        each = Measurement(expected_total.exact * Fraction(1, 2), unit, None)
        expected_pair = (each, each)
        symmetry_ok = proposed_left.exact == each.exact and proposed_right.exact == each.exact

    passed = bounds_ok and total_ok and symmetry_ok
    mismatch = Measurement(abs(proposed_total.exact - expected_total.exact), unit, None)
    return OperationResult(
        outcome=Outcome.PASS if passed else Outcome.FAIL,
        delta=mismatch,
        intermediates=(
            *common_intermediates,
            ("condition", DistributionCondition.FILLERS_ABSORB.value),
            ("expected_fillers", expected_pair),
            ("proposed_filler_total", proposed_total),
            ("each_filler_within_bounds", bounds_ok),
            ("asymmetric_note_applied", bool(allow_asymmetric)),
            ("cabinet_adjustment", "not_required; no cabinet selected"),
        ),
        comparison=(
            f"proposed fillers {'satisfy' if passed else 'do not satisfy'} exact total "
            f"{expected_total.exact} {unit.value}, individual bounds {filler_min.exact}.."
            f"{filler_max.exact}, and "
            f"{'the reviewer-noted asymmetric layout' if allow_asymmetric else 'equal U.N.O. split'}"
        ),
        tolerance=None,
    )


DISTRIBUTION_SPECS: tuple[OperationSpec, ...] = (
    OperationSpec(
        "filler_distribution",
        "1.0.0",
        {
            "field_width": Arity.SCALAR,
            "design_width": Arity.SCALAR,
            "design_fillers": Arity.LIST,
            "proposed_fillers": Arity.LIST,
            "filler_min": Arity.SCALAR,
            "filler_max": Arity.SCALAR,
            "allow_asymmetric": Arity.SCALAR,
        },
        filler_distribution,
    ),
)


def register_distribution_operations() -> None:
    """Register the reviewed filler distribution operation."""

    for spec in DISTRIBUTION_SPECS:
        register(spec)
