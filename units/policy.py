"""Rounding-aware policies for exact authored measurements."""

from __future__ import annotations

import re
from decimal import Decimal
from enum import StrEnum
from fractions import Fraction

from units.dual import DualDimension
from units.errors import UnknownRoundingError
from units.measurement import INCH_TO_MM, Measurement, Unit

_WHOLE_RE = re.compile(r"\d+")
_DECIMAL_RE = re.compile(r"\d+\.(?P<places>\d+)")
_FRACTION_RE = re.compile(r"(?:\d+\s+)?\d+/(?P<denominator>\d+)")


class Consistency(StrEnum):
    """Result of comparing two independently authored unit readings."""

    CONSISTENT_WITHIN_ROUNDING = "consistent_within_rounding"
    INCONSISTENT = "inconsistent"
    NOT_CORROBORATED = "not_corroborated"


def _authored_quantum(raw_text: str) -> Fraction:
    """Return the smallest increment expressed by an authored numeric token."""

    token = raw_text.strip()
    if token.endswith('"'):
        token = token[:-1].strip()

    decimal = _DECIMAL_RE.fullmatch(token)
    if decimal is not None:
        return Fraction(1, 10 ** len(decimal.group("places")))

    fraction = _FRACTION_RE.fullmatch(token)
    if fraction is not None:
        denominator = int(fraction.group("denominator"))
        if denominator == 0:
            raise UnknownRoundingError(f"cannot derive rounding quantum from {raw_text!r}")
        return Fraction(1, denominator)

    if _WHOLE_RE.fullmatch(token):
        return Fraction(1)

    raise UnknownRoundingError(f"cannot derive rounding quantum from {raw_text!r}")


def _decimal_from_fraction(value: Fraction) -> Decimal:
    """Convert an exact fraction to Decimal without binary floating point."""

    return Decimal(value.numerator) / Decimal(value.denominator)


def rounding_band(m: Measurement) -> Decimal:
    """Return half the rounding quantum implied by the authored token, in millimetres.

    The quantum comes from the written form, not the reduced numeric denominator:
    ``2.375`` expresses thousandths even though its exact value is ``19/8``. A computed
    measurement produced by addition or subtraction has no authored token and therefore
    no rounding band; asking for one raises ``UnknownRoundingError``.
    """

    if m.raw_text is None:
        raise UnknownRoundingError("cannot derive a rounding quantum without the authored token")

    half_quantum = _authored_quantum(m.raw_text) / 2
    band = _decimal_from_fraction(half_quantum)
    if m.unit is Unit.INCH:
        band *= INCH_TO_MM
    return band


def check_dual(d: DualDimension) -> Consistency:
    """Classify whether independently authored readings agree within their precision.

    A consistent pair corroborates only the numeric reading, not its semantic association.
    An inconsistent pair must become conflicting evidence and require review. With no
    alternate reading, this lane supplies no corroboration and no conflict.
    """

    if d.alternate is None:
        return Consistency.NOT_CORROBORATED

    difference = abs(d.primary.mm - d.alternate.mm)
    allowance = rounding_band(d.primary) + rounding_band(d.alternate)
    if difference <= allowance:
        return Consistency.CONSISTENT_WITHIN_ROUNDING
    return Consistency.INCONSISTENT
