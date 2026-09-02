"""Parse inch values from client rule text without using floating point."""

from __future__ import annotations

import re
from fractions import Fraction


class ImperialParseError(ValueError):
    """Raised when text is not a supported imperial measurement."""


_WHOLE_RE = re.compile(r"\d+")
_FRACTION_RE = re.compile(r"(\d+)/(\d+)")
_MIXED_RE = re.compile(r"(\d+)\s+(\d+)/(\d+)")
_DECIMAL_RE = re.compile(r"\d+\.\d+")


def parse_imperial(text: str) -> Fraction:
    """Convert a supported inch token to an exact Fraction.

    Supports whole numbers, fractions, mixed fractions, decimal text, and one
    optional trailing inch quote. Raises ImperialParseError for invalid input.

    """

    if not isinstance(text, str):
        raise ImperialParseError("imperial measurement must be token")

    token = text.strip()

    if token.endswith('"'):
        token = token[:-1].strip()

    if not token:
        raise ImperialParseError("imperial measurement is empty")

    try:
        mixed = _MIXED_RE.fullmatch(token)
        if mixed:
            whole, numerator, denominator = (int(part) for part in mixed.groups())
            return Fraction(whole) + Fraction(numerator, denominator)

        fraction = _FRACTION_RE.fullmatch(token)
        if fraction:
            numerator, denominator = (int(part) for part in fraction.groups())
            return Fraction(numerator, denominator)

        if _WHOLE_RE.fullmatch(token):
            return Fraction(int(token))

        if _DECIMAL_RE.fullmatch(token):
            return Fraction(token)

    except ZeroDivisionError as error:
        raise ImperialParseError("imperial fraction cannot have a zero denominator") from error

    raise ImperialParseError(f"unsupported imperial measurement: {text!r}")


def format_inches(value: Fraction) -> str:
    """Render an exact inch value the way a drawing writes it.

    `Fraction(7, 2)` is `3 1/2`, not `7/2`. The improper form is correct arithmetic and the wrong
    thing to put in front of a reviewer: dimensions are called out on drawings as whole-and-fraction,
    and somebody checking a report against a page should not have to convert in their head before
    they can tell whether the two agree.

    Exact throughout — this changes how a number is written, never what it is. `parse_imperial` reads
    back everything this produces, which is what keeps the pair from drifting into two different
    ideas of an inch.

    Negative values keep the sign on the whole part (`-3 1/2`), because a dimension that came out
    negative is a real result a reviewer needs to see rather than an absolute value.
    """
    if not isinstance(value, Fraction):
        raise ImperialParseError("value must be a Fraction")

    sign = "-" if value < 0 else ""
    magnitude = abs(value)
    whole = magnitude.numerator // magnitude.denominator
    remainder = magnitude - whole

    if remainder == 0:
        return f"{sign}{whole}"
    if whole == 0:
        return f"{sign}{remainder.numerator}/{remainder.denominator}"
    return f"{sign}{whole} {remainder.numerator}/{remainder.denominator}"
