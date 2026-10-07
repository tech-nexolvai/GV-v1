"""A label's value from its printed text, including the two worded forms the admin approved (#992).

**Only text two sources printed identically reaches this module** (`seal.py`), and nothing here is a
model: it is exact arithmetic on characters. What it accepts, and nothing else:

- **A plain dimension** — digits, a fraction, inch and foot marks, an mm `[inch]` pair — parsed by
  `units/` (unchanged from #987).
- **A sum, `a"+b"`**, optionally followed by the word `Filler` (the vendor prints `4"+1" Filler` for
  a filler made of two strips). One piece whose width is a + b exactly. Every part must carry its
  own inch mark and parse alone through `units/`; a bare `4+1"` is not read as inches.
- **Equal shares, `N"(K EQ)`** — K equal cabinets that together are N wide. One row entry of width
  N: the countertop's width needs only the total, and each share is never offered as a cabinet.

**Two words hold the whole countertop for the person** (admin, 2026-10-07): `INCLUDING FIELD CUT`
(the printed width already contains the allowance CT-WIDTH-001 would add again) and `VIF` (the
dimension is provisional until verified in the field). They are looked for in *every* text any
source gave for any label of the row — the file's own, either reader's — because holding is always
safe and a word one source missed is still on the drawing.

Any other word, a sum without inch marks, a share count below two: not expanded, so the person
decides, as before.

Source: issue #992 · Verification: `tests/extraction/slot_reader/test_labels.py`
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction
from typing import Final

from units.measurement import Measurement, Unit
from units.normalise import UnitNormalisationError, normalise_to_inches
from units.notation import canonical_notation, is_compound

__all__ = [
    "FIELD_CUT_INCLUDED",
    "VIF",
    "ExpandedLabel",
    "Expansion",
    "RowHold",
    "expand_label",
    "plain_dimension",
    "row_hold",
]

#: A plain dimension: digits, spaces, a fraction slash, inch and foot marks, a dash, a decimal
#: point, and the brackets of an mm [inch] pair. Anything else is words, a sum or a count.
_PLAIN: Final = re.compile(r"[0-9 /\"'\-.\[\]]+")

#: One part of a sum, or the total of equal shares: a number written with its inch mark.
_INCH_PART: Final = r"[0-9][0-9 /.\-]*\""
_SUM: Final = re.compile(
    rf"(?P<parts>{_INCH_PART}(?:\s*\+\s*{_INCH_PART})+)(?:\s+(?P<word>[A-Za-z]+))?"
)
_SHARES: Final = re.compile(
    rf"(?P<total>{_INCH_PART})\s*\(\s*(?P<count>[0-9]+)\s*EQ\s*\)", re.IGNORECASE
)
#: The one word a sum may carry: it names the piece's kind and changes nothing about its width.
_SUM_WORDS: Final = frozenset({"FILLER"})

_FIELD_CUT: Final = re.compile(r"(?<![A-Za-z])INCLUDING\s+FIELD\s+CUT(?![A-Za-z])", re.IGNORECASE)
_VIF: Final = re.compile(r"(?<![A-Za-z])VIF(?![A-Za-z])", re.IGNORECASE)

FIELD_CUT_INCLUDED: Final = ("field-cut-included", "width already includes the field cut")
VIF: Final = ("vif", "VIF: provisional, verify in field")


def plain_dimension(text: str) -> Measurement | None:
    """The exact value of a plain dimension's text, or `None` where it is not one.

    `None` for text with anything but the characters of a dimension, for a compound, and for text
    `units/` cannot give a unit — a bare `30` is not assumed to be inches.
    """
    if not text or _PLAIN.fullmatch(text) is None or is_compound(text):
        return None
    try:
        notation, _millimetres = canonical_notation(text)
        value = normalise_to_inches(notation)
    except (UnitNormalisationError, TypeError, ValueError, ArithmeticError):
        return None
    return Measurement(value.exact, value.unit, text)


class Expansion(StrEnum):
    SUM = "sum"
    EQUAL_SHARES = "equal-shares"


@dataclass(frozen=True, slots=True)
class ExpandedLabel:
    """A worded or summed label's one value, and how it was reached."""

    value: Measurement
    how: Expansion
    parts: tuple[Fraction, ...]
    """The sum's parts in inches, left to right; for equal shares, the total alone."""
    shares: int | None
    """K in `(K EQ)`; `None` for a sum."""


def _inches(part: str) -> Fraction | None:
    value = plain_dimension(part.strip())
    if value is None or value.unit is not Unit.INCH:
        return None
    return value.exact


def expand_label(text: str) -> ExpandedLabel | None:
    """The value of an agreed `a"+b"[ Filler]` or `N"(K EQ)`, exactly; `None` for anything else.

    `text` is the agreed text after `seal.normalise_text` (quote glyphs to `"`, whitespace runs to one
    space). A text holding `INCLUDING FIELD CUT` or `VIF` is never expanded: `row_hold` holds it.
    """
    text = text.strip()
    if not text or row_hold([text]) is not None:
        return None
    total = _SUM.fullmatch(text)
    if total is not None:
        word = total.group("word")
        if word is not None and word.upper() not in _SUM_WORDS:
            return None
        parts: list[Fraction] = []
        for part in total.group("parts").split("+"):
            value = _inches(part)
            if value is None or value <= 0:
                return None
            parts.append(value)
        return ExpandedLabel(
            Measurement(sum(parts, Fraction(0)), Unit.INCH, text), Expansion.SUM, tuple(parts), None
        )
    shares = _SHARES.fullmatch(text)
    if shares is not None:
        count = int(shares.group("count"))
        value = _inches(shares.group("total"))
        if value is None or value <= 0 or count < 2:
            return None
        return ExpandedLabel(
            Measurement(value, Unit.INCH, text), Expansion.EQUAL_SHARES, (value,), count
        )
    return None


@dataclass(frozen=True, slots=True)
class RowHold:
    """Why a whole countertop row goes to the person, whatever else sealed."""

    code: str
    reason: str
    text: str
    """The text it was found in, as the source gave it."""


def row_hold(texts: Iterable[str]) -> RowHold | None:
    """`INCLUDING FIELD CUT` first, then `VIF`, in any of the texts; `None` when neither appears."""
    seen = [" ".join(text.split()) for text in texts if text]
    for pattern, (code, reason) in ((_FIELD_CUT, FIELD_CUT_INCLUDED), (_VIF, VIF)):
        for text in seen:
            if pattern.search(text):
                return RowHold(code, reason, text)
    return None
