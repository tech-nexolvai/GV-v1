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
