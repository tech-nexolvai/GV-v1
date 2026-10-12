"""The headings GV prints on a combined sheet, read by meaning (#1193). The one place they are listed.

On GV's combined sheets each pasted drawing carries a heading GV typed as a text note: whose drawing
it is. The role of a drawing decides what is read as the vendor's (`extraction/panels.py`,
`workflow/stages.py`), so the words are listed here, once, with what each one means.

**What the headings mean.** Read on the whole label (case, spacing and curly apostrophes ignored),
never on part of it:

* **the vendor's shop drawing** (`shop`): `VENDOR'S SHOP DRAWING ELEVATION`, `VENDOR'S SHOP
  DRAWING`, `VENDOR SHOP DRAWING`, `SHOP DRAWING`, and the same with `VENDORS`, a plural, or
  `ELEVATION(S)` after; also `VENDOR('S) DRAWING` and `VENDOR('S) ELEVATION`;
* **the architect's drawing** (`arch`): `ID SET ELEVATION`, `ID SET`, `ARCH SET ELEVATION`, `ARCH
  SET`, `ARCHITECT'S SET`, `ARCHITECTURAL SET`, and `ID`, `ARCH`, `ARCHITECT'S` or `ARCHITECTURAL`
  followed by `ELEVATION(S)`;
* **a reference drawing** (`reference`): `BRAND PROTOTYPE ELEVATION`, `BRAND PROTOTYPE`,
  `PROTOTYPE (ELEVATION)`, `BRAND STANDARD(S) (ELEVATION)`. A brand standard is neither the vendor's
  nor the architect's: it is never read for a countertop and never compared.

Seen on the client's files (2026-10-12): `ID SET ELEVATION`, `VENDOR'S SHOP DRAWING ELEVATION`,
`VENDOR'S SHOP DRAWING`, `VENDOR SHOP DRAWING`, `ARCH SET ELEVATION`, `ARCH SET`, `BRAND PROTOTYPE
ELEVATION`, `BRAND PROTOTYPE`. The rest are the same words written the other ways a person writes
them; nothing wider.

**Not a heading.** A note that only contains these words is not one: `ID SET ELEVATION NOTES`,
`VENDOR TO VERIFY`, `STONE TOP BY VENDOR`, `LED STRIP BY VENDOR`, `SEE SHOP DRAWING`, `SHOP DRAWING
NOTES: …`. They fail the whole-label match. Notes GV writes inside a drawing (`CABINET CUTOUT`,
`WALL`, `(VIF)`, `ELEVATION-B`) are kept out by where and how big they are (`looks_like_heading`
and `extraction/panels.py`).

**A heading we do not know.** A label shaped like a heading (`looks_like_heading`: capitals, no
digits, a few words, printed at heading size) that means none of the above is *unknown*: the drawing
under it gets no role, with the label quoted, and never borrows another drawing's heading. Adding a
new wording is one line here, after someone has looked at the sheet it came from.

Source: issue #1193. Verification: tests/extraction/test_panels.py.
"""

from __future__ import annotations

import re
from decimal import Decimal
from enum import StrEnum
from typing import Final

__all__ = [
    "HEADING_MIN_FONT_PT",
    "HeadingMeaning",
    "heading_meaning",
    "looks_like_heading",
    "normalise_label",
]


class HeadingMeaning(StrEnum):
    """Whose drawing a heading names. `ARCH` and `SHOP` are the two sides of a comparison
    (`app.models.drawing.ViewRole`); `REFERENCE` is a drawing that is neither and is never checked.
    """

    ARCH = "arch"
    SHOP = "shop"
    REFERENCE = "reference"


#: GV's headings are typed at 24 pt on every sheet seen (AI Set 2 and AI Set 3); its notes at 4.7 to
#: 12 pt. A label printed smaller than this is a note, whatever it says.
HEADING_MIN_FONT_PT: Final = Decimal(14)

_ELEVATION = r"(?: ELEVATIONS?)?"
_VENDOR = r"(?:VENDOR'S|VENDORS|VENDOR)"
_PATTERNS: Final[tuple[tuple[HeadingMeaning, re.Pattern[str]], ...]] = (
    (
        HeadingMeaning.SHOP,
        re.compile(
            rf"(?:{_VENDOR} )?SHOP DRAWINGS?{_ELEVATION}"
            rf"|{_VENDOR} DRAWINGS?{_ELEVATION}"
            rf"|{_VENDOR} ELEVATIONS?"
        ),
    ),
    (
        HeadingMeaning.ARCH,
        re.compile(
            rf"(?:ID|ARCH|ARCHITECT'S|ARCHITECTS|ARCHITECTURAL) SET{_ELEVATION}"
            r"|(?:ID|ARCH|ARCHITECT'S|ARCHITECTS|ARCHITECTURAL) ELEVATIONS?"
        ),
    ),
    (
        HeadingMeaning.REFERENCE,
        re.compile(rf"(?:BRAND )?PROTOTYPES?{_ELEVATION}|BRAND STANDARDS?{_ELEVATION}"),
    ),
)

_APOSTROPHES: Final = str.maketrans({"’": "'", "‘": "'", "`": "'", "′": "'"})

#: Capitals, spaces and the punctuation a heading uses; no digits. Starts and ends with a letter (a
#: closing bracket may end it).
_HEADING_SHAPE: Final = re.compile(r"[A-Z][A-Z'&/().\- ]*[A-Z)]")
_MAX_HEADING_WORDS: Final = 8


def normalise_label(text: str) -> str:
    """The label as it is matched: capitals, single spaces, straight apostrophes."""
    return " ".join(text.translate(_APOSTROPHES).upper().split())


def heading_meaning(text: str) -> HeadingMeaning | None:
    """What the whole label names, or `None` when it is not one of the headings above."""
    label = normalise_label(text)
    for meaning, pattern in _PATTERNS:
        if pattern.fullmatch(label):
            return meaning
    return None


def looks_like_heading(text: str, font_size: Decimal | None) -> bool:
    """Whether a label is shaped like a heading, known or not: typed in capitals (no lower-case
    letter in the file), no digits, at most eight words, at least three letters, and printed at
    heading size (`HEADING_MIN_FONT_PT`). A label whose size the file does not give is not one: an
    unknown label is taken for a heading only on evidence. Where it sits is the caller's question
    (`extraction/panels.py`: never inside a drawing)."""
    if font_size is None or font_size < HEADING_MIN_FONT_PT:
        return False
    if any(character.islower() for character in text):
        return False
    label = normalise_label(text)
    return (
        _HEADING_SHAPE.fullmatch(label) is not None
        and len(label.split()) <= _MAX_HEADING_WORDS
        and sum(character.isalpha() for character in label) >= 3
    )
