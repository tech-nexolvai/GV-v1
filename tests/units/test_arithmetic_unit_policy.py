"""Verification for issue #43: authored-unit arithmetic policy enforcement.

Source: ``docs/V1_RESEARCH_AND_PLAN.md`` section F1 and ADR-0001.
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

import pytest

from units.measurement import Measurement, MixedUnitError, Unit
from units.policy import require_same_unit


@pytest.mark.parametrize("unit", [Unit.MM, Unit.INCH])
def test_matching_operands_return_their_common_authored_unit(unit: Unit) -> None:
    """An operation may proceed when every operand uses one authored unit system."""

    first = Measurement(Fraction(1), unit, "1")
    second = Measurement(Fraction(2), unit, "2")
    third = Measurement(Fraction(3), unit, "3")

    assert require_same_unit(first, second, third) is unit


@pytest.mark.parametrize("unit", [Unit.MM, Unit.INCH])
def test_one_operand_returns_its_own_unit(unit: Unit) -> None:
    """A single operand is trivially consistent with its authored unit."""

    assert require_same_unit(Measurement(Fraction(1), unit, "1")) is unit


def test_zero_operands_raise_instead_of_inventing_a_unit() -> None:
    """An empty operand collection has no common unit and is a caller error."""

    with pytest.raises(ValueError, match="at least one measurement"):
        require_same_unit()


@pytest.mark.parametrize(
    ("first_unit", "second_unit"),
    [(Unit.MM, Unit.INCH), (Unit.INCH, Unit.MM)],
)
def test_mixed_authored_units_raise_without_conversion(first_unit: Unit, second_unit: Unit) -> None:
    """Mismatch detection is independent of operand order and never converts values."""

    first = Measurement(Fraction(100), first_unit, "100")
    second = Measurement(Fraction(4), second_unit, "4")
    original = (first, second)

    with pytest.raises(MixedUnitError):
        require_same_unit(first, second)

    assert (first, second) == original


def test_f1_rounding_trap_is_rejected_before_arithmetic() -> None:
    """The 100 mm/4 inch pair cannot consume a 1/16 inch tolerance silently."""

    millimetres = Measurement(Fraction(100), Unit.MM, "100")
    inches = Measurement(Fraction(4), Unit.INCH, '4"')
    tolerance_mm = Decimal(1) / Decimal(16) * Decimal("25.4")
    apparent_difference = inches.mm - millimetres.mm

    assert apparent_difference == Decimal("1.6")
    assert tolerance_mm == Decimal("1.5875")
    assert apparent_difference > tolerance_mm

    with pytest.raises(MixedUnitError):
        require_same_unit(millimetres, inches)
