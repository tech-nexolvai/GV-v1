"""Read a dimension written in another unit into inches, at read time only.

`CALL_2026_08_25_INPUTS` N3: some drawings label only in millimetres or only in feet, and without
conversion those read as `NOT_FOUND` even though the value is plainly on the page. This turns such a
token into an inch `Measurement` so it can be *read*.

**It does not change the verdict model.** Inches remain authoritative and millimetres are never a
verdict operand (Q12, `ADR-0001`). This is normalisation upstream of the comparison — a different
thing from comparing units against each other, which nothing here does. `units/policy.py` still
refuses to combine measurements authored in different units, and this module never touches that path.

**Nothing is ever rounded.** Every conversion is exact `Fraction` arithmetic: 25.4 mm to the inch as
`5/127`, twelve inches to the foot. Rounding a converted value to the nearest sixteenth would make
most millimetre drawings compare equal, and it would do so by inventing precision the drawing does
not carry — a guess, on the dimension that decides the verdict. See `read_the_docstring_on`
`normalise_to_inches` for what that costs and why it is still the right trade.

Yards are out of scope by decision, not by omission, and are refused explicitly (N3).
"""

from __future__ import annotations

import re
from fractions import Fraction

from units.imperial import ImperialParseError, parse_imperial
from units.measurement import Measurement, Unit


class UnitNormalisationError(ValueError):
    """Raised when a token is not a dimension this module can read into inches.

    A `ValueError` rather than a sentinel return: a caller that forgets to handle it gets a loud
    failure, where a `None` would flow onward and be read as "no dimension here" — the silent
    NOT_FOUND this module exists to remove.
    """


#: Exactly twelve inches. Not a conversion factor so much as the definition of the unit.
INCHES_PER_FOOT: Fraction = Fraction(12)

#: 25.4 mm to the inch, as an exact ratio. `Measurement.to` uses the same number the other way.
INCHES_PER_MM: Fraction = Fraction(5, 127)

#: Refused on sight, so the reason appears in the error rather than as an unsupported-token shrug.
_YARD_RE = re.compile(r"\b(yd|yds|yard|yards)\b", re.IGNORECASE)

#: `984 mm`, `984mm`, `984 MM`.
_MM_RE = re.compile(r"^(?P<value>\d+(?:\.\d+)?)\s*mm$", re.IGNORECASE)

#: `3 ft`, `3ft`, `3'`, and the feet-and-inches forms `3'-6`, `3'-6"`, `3' 6 1/2"`, `3'-6 1/2`.
_FEET_RE = re.compile(
    r"^(?P<feet>\d+)\s*(?:'|ft|feet)"
    r"(?:\s*[-\s]\s*(?P<inches>[\d\s/\.]+?)\s*(?:\"|in|inches)?)?$",
    re.IGNORECASE,
)

#: A bare inch token, with or without its quote — `38 3/4`, `38 3/4"`, `4`, `2.375`.
_INCH_RE = re.compile(r'^[\d\s/\.]+"?$')


def normalise_to_inches(text: str) -> Measurement:
    """Read a dimension token as inches, exactly.

    Accepts millimetres (`984 mm`), feet (`3'`, `3 ft`), feet and inches (`3'-6 1/2"`) and plain
    inches (`38 3/4"`). The returned `Measurement` keeps the original token as `raw_text`, so a
    reviewer is shown what the drawing actually said and not only what it became.

    **What conversion costs under exact match, and why it is still correct.** 984 mm is exactly
    `4920/127` inches — about 38.7402 — and the drawing that means it almost certainly writes
    `38 3/4`, which is `155/4`. Those are not equal, and `Q2` settled that V1 compares exactly with
    no tolerance band, so a millimetre-sourced value checked against an inch one will normally FAIL.

    That is the honest result and not a defect to paper over. The alternative is to round the
    converted value to some denominator, and a rounded value would be the system deciding what the
    drawing *meant* — the guess `AGENTS.md` §2 forbids on precisely the number that gets cut. Under
    `Q4` every finding is flagged for a reviewer anyway, and a flag saying "these two dimensions do
    not agree exactly" is true. What the reviewer must not be shown is a PASS that arithmetic did not
    earn.

    Feet are the clean case: twelve to the inch exactly, so `3'-6"` is `42` and nothing is lost.

    Raises `UnitNormalisationError` for yards (out of scope by decision, N3) and for anything else it
    cannot read. It never returns an approximation and never falls back to a bare number when the
    unit is unrecognised — a token whose unit cannot be established is exactly the ambiguity the
    caller must turn into an abstention.
    """
    if not isinstance(text, str):
        raise UnitNormalisationError("dimension must be text")

    token = text.strip()
    if not token:
        raise UnitNormalisationError("dimension is empty")

    if _YARD_RE.search(token):
        raise UnitNormalisationError(
            f"yards are out of scope for V1 and are not converted: {text!r}. "
            "Refused rather than read, so a yard cannot enter a comparison unnoticed."
        )

    millimetres = _MM_RE.match(token)
    if millimetres is not None:
        exact = Fraction(millimetres.group("value")) * INCHES_PER_MM
        return Measurement(exact=exact, unit=Unit.INCH, raw_text=token)

    feet = _FEET_RE.match(token)
    if feet is not None:
        exact = Fraction(int(feet.group("feet"))) * INCHES_PER_FOOT
        remainder = feet.group("inches")
        if remainder is not None and remainder.strip():
            try:
                exact += parse_imperial(remainder.strip())
            except ImperialParseError as error:
                raise UnitNormalisationError(
                    f"unreadable inch part of a feet-and-inches dimension: {text!r}"
                ) from error
        return Measurement(exact=exact, unit=Unit.INCH, raw_text=token)

    if _INCH_RE.match(token):
        try:
            return Measurement(exact=parse_imperial(token), unit=Unit.INCH, raw_text=token)
        except ImperialParseError as error:
            raise UnitNormalisationError(f"unreadable inch dimension: {text!r}") from error

    raise UnitNormalisationError(
        f"no unit could be established for {text!r}. A dimension whose unit is unknown is an "
        "ambiguity, and the caller turns it into REVIEW_REQUIRED rather than assuming inches."
    )


def inches_from_mm(millimetres: Fraction | int | str) -> Fraction:
    """Convert millimetres to inches exactly.

    Separate from `normalise_to_inches` for callers that already hold a number and its unit and have
    no token to parse. Same ratio, same refusal to round.
    """
    return Fraction(millimetres) * INCHES_PER_MM


def inches_from_feet(feet: Fraction | int | str) -> Fraction:
    """Convert feet to inches exactly."""
    return Fraction(feet) * INCHES_PER_FOOT
