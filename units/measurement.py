"""Exact measurement values shared by the deterministic core.

This module intentionally depends on the Python standard library only.  It preserves the
value and unit printed by a drawing while offering a canonical millimetre representation
for storage and display.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from fractions import Fraction


class Unit(StrEnum):
    """Units supported by the V1 deterministic core."""

    MM = "mm"
    INCH = "in"


class MixedUnitError(ValueError):
    """Raised when arithmetic attempts to combine authored unit systems."""


INCH_TO_MM = Decimal("25.4")


def _decimal_from_fraction(value: Fraction) -> Decimal:
    """Return a Decimal representation without passing through binary floating point."""

    return Decimal(value.numerator) / Decimal(value.denominator)


@dataclass(frozen=True, slots=True, order=True)
class Measurement:
    """An exact dimension. Never a float, anywhere, ever.

    ``exact`` and ``unit`` preserve the authored measurement. ``raw_text`` retains the
    source token so later evidence and review layers can show what was actually written.
    """

    exact: Fraction
    unit: Unit
    raw_text: str | None

    def __post_init__(self) -> None:
        """Reject values that would weaken the exact-arithmetic contract."""

        if not isinstance(self.exact, Fraction):
            raise TypeError("exact must be a Fraction")
        if not isinstance(self.unit, Unit):
            raise TypeError("unit must be a Unit")
        if self.raw_text is not None and not isinstance(self.raw_text, str):
            raise TypeError("raw_text must be a string or None")

    @property
    def mm(self) -> Decimal:
        """Return the canonical millimetre value using the exact 25.4 conversion factor."""

        if self.unit is Unit.MM:
            return _decimal_from_fraction(self.exact)
        return _decimal_from_fraction(self.exact) * INCH_TO_MM

    def to(self, unit: Unit) -> Measurement:
        """Convert exactly while retaining the source token for auditability."""

        if not isinstance(unit, Unit):
            raise TypeError("unit must be a Unit")
        if unit is self.unit:
            return self
        if unit is Unit.MM:
            return Measurement(self.exact * Fraction(127, 5), unit, self.raw_text)
        return Measurement(self.exact * Fraction(5, 127), unit, self.raw_text)

    def __add__(self, other: Measurement) -> Measurement:
        """Add measurements authored in the same unit system exactly."""

        self._require_same_unit(other)
        return Measurement(self.exact + other.exact, self.unit, None)

    def __sub__(self, other: Measurement) -> Measurement:
        """Subtract measurements authored in the same unit system exactly."""

        self._require_same_unit(other)
        return Measurement(self.exact - other.exact, self.unit, None)

    def _require_same_unit(self, other: Measurement) -> None:
        if not isinstance(other, Measurement):
            raise TypeError("other must be a Measurement")
        if self.unit is not other.unit:
            raise MixedUnitError(
                f"cannot combine {self.unit.value!r} and {other.unit.value!r} measurements"
            )
