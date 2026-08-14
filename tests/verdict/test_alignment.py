"""Verification for issue #51: exact one-dimensional alignment."""

from __future__ import annotations

from fractions import Fraction

import pytest

from units.measurement import Measurement, MixedUnitError, Unit
from verdict.operations.alignment import ALIGNMENT_SPECS, alignment
from verdict.outcomes import Outcome
from verdict.registry import Arity, RuleAuthoringError


def mm(value: int | Fraction) -> Measurement:
    return Measurement(Fraction(value), Unit.MM, str(value))


def inch(value: int | Fraction) -> Measurement:
    return Measurement(Fraction(value), Unit.INCH, str(value))


@pytest.mark.parametrize(
    ("positions", "expected_outcome", "expected_spread"),
    [
        ((mm(100), mm(101)), Outcome.PASS, Fraction(1)),
        ((mm(100), mm(102)), Outcome.PASS, Fraction(2)),
        ((mm(100), mm(103)), Outcome.FAIL, Fraction(3)),
    ],
    ids=["inside", "exact-boundary", "outside"],
)
def test_alignment_uses_an_inclusive_exact_range(
    positions: tuple[Measurement, ...],
    expected_outcome: Outcome,
    expected_spread: Fraction,
) -> None:
    result = alignment(positions=positions, tolerance=mm(2), axis="X")

    assert result.outcome is expected_outcome
    assert result.delta == Measurement(expected_spread, Unit.MM, None)


def test_alignment_is_independent_of_position_order() -> None:
    forward = alignment(positions=(mm(98), mm(100), mm(101)), tolerance=mm(3), axis="Y")
    reordered = alignment(positions=(mm(101), mm(98), mm(100)), tolerance=mm(3), axis="Y")

    assert forward.outcome is reordered.outcome is Outcome.PASS
    assert forward.delta == reordered.delta == Measurement(Fraction(3), Unit.MM, None)
    assert forward.comparison == reordered.comparison


@pytest.mark.parametrize("positions", [(), (mm(100),)], ids=["zero", "one"])
def test_fewer_than_two_positions_is_not_found(
    positions: tuple[Measurement, ...],
) -> None:
    result = alignment(positions=positions, tolerance=mm(1), axis="X")

    assert result.outcome is Outcome.NOT_FOUND
    assert result.outcome is not Outcome.PASS
    assert result.delta is None
    assert "at least two" in result.comparison


def test_trace_facts_record_axis_positions_range_and_spread() -> None:
    positions = (mm(102), mm(100), mm(101))
    result = alignment(positions=positions, tolerance=mm(2), axis="drawer-front vertical")

    assert result.intermediates == (
        ("axis", "drawer-front vertical"),
        ("position[0]", mm(102)),
        ("position[1]", mm(100)),
        ("position[2]", mm(101)),
        ("minimum", mm(100)),
        ("maximum", mm(102)),
        ("spread", Measurement(Fraction(2), Unit.MM, None)),
    )
    assert "drawer-front vertical" in result.comparison


def test_alignment_rejects_mixed_authored_units() -> None:
    with pytest.raises(MixedUnitError):
        alignment(positions=(mm(25), inch(1)), tolerance=mm(1), axis="X")

    with pytest.raises(MixedUnitError):
        alignment(positions=(mm(25), mm(26)), tolerance=inch(1), axis="X")


def test_alignment_rejects_negative_tolerance() -> None:
    with pytest.raises(RuleAuthoringError, match="negative"):
        alignment(positions=(mm(100), mm(101)), tolerance=mm(-1), axis="X")


@pytest.mark.parametrize("axis", ["", "   ", None])
def test_alignment_requires_a_declared_axis(axis: object) -> None:
    with pytest.raises(RuleAuthoringError, match="non-empty"):
        alignment(
            positions=(mm(100), mm(101)),
            tolerance=mm(1),
            axis=axis,  # type: ignore[arg-type]
        )


def test_alignment_rejects_wrong_position_shapes() -> None:
    with pytest.raises(RuleAuthoringError, match="list arity"):
        alignment(positions=mm(100), tolerance=mm(1), axis="X")  # type: ignore[arg-type]

    with pytest.raises(RuleAuthoringError, match=r"positions\[1\]"):
        alignment(
            positions=(mm(100), "101"),  # type: ignore[arg-type]
            tolerance=mm(1),
            axis="X",
        )


def test_alignment_has_a_versioned_registry_spec() -> None:
    assert len(ALIGNMENT_SPECS) == 1
    spec = ALIGNMENT_SPECS[0]
    assert spec.name == "alignment"
    assert spec.version == "1.0.0"
    assert spec.operands == {
        "positions": Arity.LIST,
        "tolerance": Arity.SCALAR,
        "axis": Arity.SCALAR,
    }
