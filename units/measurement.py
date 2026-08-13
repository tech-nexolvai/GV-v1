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


def to_exact_fraction(value: object) -> Fraction:
    """Convert an authored numeric value into an exact Fraction.

    Accepts what a rule author or a drawing actually writes: ``3``, ``"1/8"``, ``"38 3/4"``,
    ``"2.375"``. This lives in ``units`` rather than in the rule schema because turning
    authored text into an exact number is a units concern, and keeping it here means
    ``rules/`` and ``verdict/`` never need to mention floating point at all.

    A float is rejected rather than converted: accepting one would let binary rounding into
    a tolerance, which is the failure ADR-0001 exists to prevent.

    Every rejection raises ``ValueError``, including the type errors. That is deliberate --
    Pydantic wraps ``ValueError`` into a validation error naming the offending field, while a
    ``TypeError`` escapes as a bare traceback. A rule author needs to be told which field is
    wrong, so correct behaviour wins over the usual type-error convention here.
    """

    from units.imperial import ImperialParseError, parse_imperial

    if isinstance(value, Fraction):
        return value
    if isinstance(value, bool):  # bool subclasses int; it is never a measurement
        raise ValueError("a boolean is not a measurement")  # noqa: TRY004
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, float):
        raise ValueError(  # noqa: TRY004
            "float is not allowed; write the exact value as text, "
            'e.g. "1/8" or "2.375", so it stays exact'
        )
    if isinstance(value, str):
        try:
            return parse_imperial(value)
        except ImperialParseError as error:
            raise ValueError(str(error)) from error
    raise ValueError(f"cannot read {value!r} as an exact numeric value")


def ensure_exact(value: object, *, context: str) -> None:
    """Raise if a value would smuggle floating point into the decision path.

    Lives here rather than at the call site for the same reason as :func:`to_exact_fraction`:
    the exactness contract belongs to ``units``, and keeping the check here means ``rules`` and
    ``verdict`` never need to mention floating point at all — which is what lets the guard in
    ``tests/units/test_measurement.py`` ban the word outright in those packages.

    Raises ``TypeError``, because being handed a float is a caller contract violation rather
    than bad data: the type annotations already forbid it, so reaching here means something
    upstream ignored them.
    """

    if isinstance(value, float):
        raise TypeError(
            f"{context} cannot be a float. ADR-0001 requires exact arithmetic, and a float "
            "would reintroduce the rounding error the unit policy exists to prevent. Use "
            "Measurement or Fraction."
        )
