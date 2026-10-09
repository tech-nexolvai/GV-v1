"""One architect's dimension label: its number, the words printed with it, and whether it may be used.

**Why a separate step from the parser.** `units.normalise.normalise_to_inches` refuses `3'-6" VIF`,
and it must: a parser that quietly dropped `VIF` would turn "verify in field" into a firm number.
But the word is not noise either. It says the architect has not committed to the number, and a
reviewer needs to see it. So the label is split here into the dimension text and the qualifier
words printed with it, each qualifier kept as a flag, and the label is **held** when a qualifier
says the number is not firm (`VIF`, `±`, `HOLD`) or when words nobody recognises are printed with
it (a note such as `6" WOODEN BLOCKING` is not a dimension).

**What it never does.** It never rounds, never guesses a unit, and never reads a value out of a
label it holds: a held label's inches are `None`. Fractions must be over a power of two no larger
than sixty-four, in every form, or the label is held.

Source: issue #1052 · Verification: `tests/extraction/architect/test_labels.py`
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction
from typing import Final

from units.normalise import (
    MAXIMUM_DENOMINATOR,
    UnitNormalisationError,
    normalise_to_inches,
    plain_marks,
)

__all__ = [
    "DIMENSION_PATTERN",
    "HOLDING_QUALIFIERS",
    "LabelReading",
    "Qualifier",
    "read_label",
    "split_label",
]


class Qualifier(StrEnum):
    """A word an architect prints beside a dimension. Kept as a flag, never stripped silently."""

    VIF = "VIF"
    """Verify in field: the number is to be measured on site."""
    PLUS_MINUS = "±"
    """Approximately: the number is not exact."""
    EQ = "EQ"
    """Equal spaces: the number may be a share, not a drawn width."""
    TYP = "TYP"
    """Typical: the same dimension repeats elsewhere."""
    CLR = "CLR"
    """Clear: an opening, not a piece."""
    MIN = "MIN"
    MAX = "MAX"
    HOLD = "HOLD"
    """The architect has not fixed the number yet."""
    AFF = "AFF"
    """Above finished floor: a height."""
    NOM = "NOM"
    """Nominal: the size by name, not as built."""


#: The qualifiers that say the number is not a firm value. A label with one is held for a person.
HOLDING_QUALIFIERS: Final = frozenset({Qualifier.VIF, Qualifier.PLUS_MINUS, Qualifier.HOLD})

#: How each qualifier may be written, once case and dots are removed.
_QUALIFIER_SPELLINGS: Final = {
    "VIF": Qualifier.VIF,
    "±": Qualifier.PLUS_MINUS,
    "+/-": Qualifier.PLUS_MINUS,
    "+-": Qualifier.PLUS_MINUS,
    "EQ": Qualifier.EQ,
    "TYP": Qualifier.TYP,
    "CLR": Qualifier.CLR,
    "MIN": Qualifier.MIN,
    "MAX": Qualifier.MAX,
    "HOLD": Qualifier.HOLD,
    "AFF": Qualifier.AFF,
    "NOM": Qualifier.NOM,
}

#: One written dimension in the plain spelling `plain_marks` gives: feet and inches (`3'-6"`,
#: `3' - 6 1/2"`, `3'6"`, `3'-1-1/2"`, `0'-1/2"`), feet alone (`3'`), or marked inches (`11"`,
#: `13 1/8"`, `1/2"`). Used to find labels in a run of text; the parser decides what each one is.
DIMENSION_PATTERN: Final = re.compile(
    r"\d+\s*'(?:\s*-?\s*(?:\d+(?:(?:\s+|\s*-\s*)\d+/\d+)?|\d+/\d+)\s*\")?"
    r"|\d+(?:(?:\s+|\s*-\s*)\d+/\d+)?\s*\""
    r"|\d+/\d+\s*\""
)

#: `±` written before a number with no space (`±3'-6"`) is split from it first.
_PLUS_MINUS_PREFIX: Final = re.compile(r"^(±|\+/-|\+-)(?=\d)")
_FRACTION: Final = re.compile(r"(\d+)\s*/\s*(\d+)")
_BRACKETS: Final = str.maketrans({"(": " ", ")": " ", "[": " ", "]": " ", ",": " "})


@dataclass(frozen=True, slots=True)
class SplitLabel:
    """A label split into its dimension text and the words printed with it."""

    dimension: str
    """The dimension as printed, qualifiers removed; empty when the label holds no dimension."""
    qualifiers: frozenset[Qualifier]
    other_words: tuple[str, ...]
    """Words that are neither the dimension nor a known qualifier, as printed."""


@dataclass(frozen=True, slots=True)
class LabelReading:
    """What one printed label says, and whether its number may be used."""

    text: str
    """Exactly as printed."""
    dimension: str
    qualifiers: frozenset[Qualifier]
    printed_inches: Fraction | None
    """What the dimension text parses to, exactly, whether or not it may be used — for a person
    reading the report. `None` when it does not parse."""
    held_reason: str | None
    """Why the number may not be used; `None` when it may."""

    @property
    def inches(self) -> Fraction | None:
        """The number, only when nothing holds it. Never a held label's value."""
        return self.printed_inches if self.held_reason is None else None


def _qualifier(word: str) -> Qualifier | None:
    key = word.upper().replace(".", "")
    return _QUALIFIER_SPELLINGS.get(key)


def split_label(text: str) -> SplitLabel:
    """Split a printed label into its one dimension and its qualifier words.

    The dimension is the one run of text `DIMENSION_PATTERN` finds; everything else is a qualifier
    (`VIF`, `(VIF)`, `±`, `+/-`, `EQ.`, `TYP`, ...) or another word. Two dimensions in one label are
    not split apart here: the whole text is returned as `other_words` with no dimension, so the
    caller holds it.
    """
    plain = plain_marks(text.strip())
    plain = _PLUS_MINUS_PREFIX.sub(r"\1 ", plain)
    found = list(DIMENSION_PATTERN.finditer(plain))
    if len(found) != 1:
        return SplitLabel(
            dimension="",
            qualifiers=frozenset(),
            other_words=tuple(plain.split()) if plain else (),
        )
    (match,) = found
    rest = (plain[: match.start()] + " " + plain[match.end() :]).translate(_BRACKETS)
    qualifiers: set[Qualifier] = set()
    others: list[str] = []
    for word in rest.split():
        qualifier = _qualifier(word)
        if qualifier is None:
            others.append(word)
        else:
            qualifiers.add(qualifier)
    return SplitLabel(
        dimension=match.group(0).strip(),
        qualifiers=frozenset(qualifiers),
        other_words=tuple(others),
    )


def _drawn_fractions(dimension: str) -> bool:
    """Whether every written fraction is over a power of two no larger than sixty-four."""
    for _numerator, denominator in _FRACTION.findall(dimension):
        bottom = int(denominator)
        if not (2 <= bottom <= MAXIMUM_DENOMINATOR and bottom & (bottom - 1) == 0):
            return False
    return True


def read_label(text: str) -> LabelReading:
    """Read one architect's label: split, check, parse exactly, and hold it when it is not firm.

    A label is held — its inches `None`, with the reason — when it holds no single dimension, when
    other words are printed with it, when a qualifier says the number is not firm (`VIF`, `±`,
    `HOLD`), when a fraction is not over a power of two up to 64, or when the parser refuses it.
    Qualifiers that do not hold a number (`EQ`, `TYP`, `CLR`, `MIN`, `MAX`, `AFF`, `NOM`) stay as
    flags for whoever uses the number.
    """
    split = split_label(text)
    printed: Fraction | None = None
    reason: str | None = None
    if split.dimension:
        try:
            printed = normalise_to_inches(split.dimension).exact
        except UnitNormalisationError as error:
            reason = f"the dimension does not read exactly: {error}"
        if printed is not None and not _drawn_fractions(plain_marks(split.dimension)):
            printed = None
            reason = "a fraction is not over a power of two up to 64, as a drawing prints them"
    if not split.dimension:
        reason = "the label holds no single dimension"
    elif split.other_words:
        reason = (
            "other words are printed with the dimension ("
            + " ".join(split.other_words)
            + "): a note, not a dimension label"
        )
    elif split.qualifiers & HOLDING_QUALIFIERS:
        names = ", ".join(sorted(q.value for q in split.qualifiers & HOLDING_QUALIFIERS))
        reason = reason or f"the architect marks this number as not firm ({names})"
    return LabelReading(
        text=text,
        dimension=split.dimension,
        qualifiers=split.qualifiers,
        printed_inches=printed,
        held_reason=reason,
    )
