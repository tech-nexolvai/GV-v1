"""Parse dimensions containing primary and alternate unit readings."""

from __future__ import annotations

import re
from dataclasses import dataclass
from fractions import Fraction

from units.imperial import ImperialParseError, parse_imperial
from units.measurement import Measurement, Unit


class DualDimensionParseError(ValueError):
    """Raised when text is not a supported dual-dimension token."""


@dataclass(frozen=True, slots=True)
class DualDimension:
    """A primary drawing dimension and its optional alternate reading."""

    primary: Measurement
    alternate: Measurement | None


#: A dimension stated twice: a millimetre number and a bracketed imperial reading.
#:
#: Public because the reader needs the same definition to find one inside a line of text, and two
#: regexes for one token shape is how they come to disagree about what a dual dimension is.
DUAL_TOKEN_RE = re.compile(r"(?P<primary>\d+)\s*\[\s*(?P<alternate>[^\[\]]+)\s*\]")

_DUAL_RE = DUAL_TOKEN_RE
_SINGLE_RE = re.compile(r"\d+")


def parse_dual(text: str) -> DualDimension:
    """Parse a millimetre value with an optional bracketed inch value.

    For example, ``984 [38 3/4]`` preserves ``984`` as the primary
    millimetre reading and ``38 3/4`` as the alternate inch reading.
    A single millimetre value is valid and yields ``alternate=None``.
    """

    if not isinstance(text, str):
        raise DualDimensionParseError("dual dimension must be text")

    token = text.strip()
    if not token:
        raise DualDimensionParseError("dual dimension is empty")

    if _SINGLE_RE.fullmatch(token):
        return DualDimension(
            primary=Measurement(
                exact=Fraction(token),
                unit=Unit.MM,
                raw_text=token,
            ),
            alternate=None,
        )

    match = _DUAL_RE.fullmatch(token)

    if match is None:
        raise DualDimensionParseError(f"unsupported dual dimension: {text!r}")

    primary_text = match.group("primary")
    alternate_text = match.group("alternate").strip()

    try:
        alternate_exact = parse_imperial(alternate_text)
    except ImperialParseError as error:
        raise DualDimensionParseError(f"invalid alternate dimension: {alternate_text!r}") from error

    return DualDimension(
        primary=Measurement(
            exact=Fraction(primary_text),
            unit=Unit.MM,
            raw_text=primary_text,
        ),
        alternate=Measurement(
            exact=alternate_exact,
            unit=Unit.INCH,
            raw_text=alternate_text,
        ),
    )
