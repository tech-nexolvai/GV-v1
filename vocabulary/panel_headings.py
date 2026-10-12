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
  SET`, `ARCHITECT('S) SET`, `ARCHITECTURAL SET`, and `ID`, `ARCH`, `ARCHITECT('S)` or
  `ARCHITECTURAL` followed by `ELEVATION(S)`; `I.D.` is read as `ID`;
* **a reference drawing** (`reference`): `BRAND PROTOTYPE ELEVATION`, `BRAND PROTOTYPE`,
  `PROTOTYPE (ELEVATION)`, `BRAND STANDARD(S) (ELEVATION)`. A brand standard is neither the vendor's
  nor the architect's: it is never read for a countertop and never compared.

Seen on the client's files (2026-10-12): `ID SET ELEVATION`, `VENDOR'S SHOP DRAWING ELEVATION`,
`VENDOR'S SHOP DRAWING`, `VENDOR SHOP DRAWING`, `ARCH SET ELEVATION`, `ARCH SET`, `BRAND PROTOTYPE
ELEVATION`, `BRAND PROTOTYPE`. The rest are the same words written the other ways a person writes
them; nothing wider.

Apostrophes of every kind (curly, modifier-letter U+02BC, full-width U+FF07) are one apostrophe,
and a hyphen between two words is a space (`ID-SET`, `SHOP-DRAWING`).

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
    "NO_HEADINGS_ON_PAGE",
    "Box",
    "HeadingMeaning",
    "heading_meaning",
    "looks_like_heading",
    "normalise_label",
    "place_words",
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
            rf"(?:ID|ARCH|ARCHITECT'S|ARCHITECTS|ARCHITECT|ARCHITECTURAL) SET{_ELEVATION}"
            r"|(?:ID|ARCH|ARCHITECT'S|ARCHITECTS|ARCHITECT|ARCHITECTURAL) ELEVATIONS?"
        ),
    ),
    (
        HeadingMeaning.REFERENCE,
        re.compile(rf"(?:BRAND )?PROTOTYPES?{_ELEVATION}|BRAND STANDARDS?{_ELEVATION}"),
    ),
)

#: Every apostrophe a typed label may carry: curly, backtick, prime, modifier letter (U+02BC) and
#: full-width (U+FF07).
_APOSTROPHES: Final = str.maketrans(
    {"’": "'", "‘": "'", "`": "'", "′": "'", "\u02bc": "'", "\uff07": "'"}
)

#: `I.D.` and `I.D` are `ID`; a hyphen between words is a space (`ID-SET`, `SHOP-DRAWING`).
_INITIALS: Final = re.compile(r"\bI\.\s?D\b\.?")
_HYPHEN: Final = re.compile(r"(?<=[A-Z])-(?=[A-Z])")

#: Capitals, spaces and the punctuation a heading uses; no digits. Starts and ends with a letter (a
#: closing bracket may end it).
_HEADING_SHAPE: Final = re.compile(r"[A-Z][A-Z'&/().\- ]*[A-Z)]")
_MAX_HEADING_WORDS: Final = 8


def normalise_label(text: str) -> str:
    """The label as it is matched: capitals, single spaces, straight apostrophes."""
    return " ".join(text.translate(_APOSTROPHES).upper().split())


def _matched(text: str) -> str:
    """The label as the headings are matched: `normalise_label`, with `I.D.` read as `ID` and a
    hyphen between two words as a space."""
    label = _HYPHEN.sub(" ", _INITIALS.sub("ID", normalise_label(text)))
    return " ".join(label.split())


def heading_meaning(text: str) -> HeadingMeaning | None:
    """What the whole label names, or `None` when it is not one of the headings above."""
    label = _matched(text)
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


#: The reason a drawing on a page that prints no heading at all carries (`extraction/panels.py`):
#: there, an unconfirmed drawing is read as before. Any other reason with no role is a drawing on a
#: headed page that is not read as the vendor's (`app/api/visual_countertops.py`, "drawings not
#: read").
NO_HEADINGS_ON_PAGE: Final = "no label naming the drawings is printed on this page"

Box = tuple[Decimal, Decimal, Decimal, Decimal]
"""A drawing's box in stored page space: left, top, right, bottom (`y` runs down the page)."""


def place_words(boxes: list[Box]) -> list[str]:
    """Each drawing's place on the page in words, among the page's drawings: "the drawing", "the
    upper drawing", "the right drawing", "the top-left drawing". One rule for the reasons code
    writes (`extraction/panels.py`) and the lists the reviewer reads (`app/api`)."""
    if len(boxes) == 1:
        return ["the drawing"]
    if len(boxes) == 2:
        (l1, t1, r1, b1), (l2, t2, r2, b2) = boxes
        shared = min(b1, b2) - max(t1, t2)
        smaller = min(b1 - t1, b2 - t2)
        if shared <= smaller / 2:
            upper = (t1 + b1) <= (t2 + b2)
            return (
                ["the upper drawing", "the lower drawing"]
                if upper
                else ["the lower drawing", "the upper drawing"]
            )
        left = (l1 + r1) <= (l2 + r2)
        return (
            ["the left drawing", "the right drawing"]
            if left
            else ["the right drawing", "the left drawing"]
        )
    words = []
    third, two_thirds = Decimal(1) / 3, Decimal(2) / 3
    for x0, y0, x1, y1 in boxes:
        middle, centre = (y0 + y1) / 2, (x0 + x1) / 2
        vertical = "top" if middle < third else "bottom" if middle > two_thirds else ""
        across = "left" if centre < third else "right" if centre > two_thirds else ""
        where = "-".join(part for part in (vertical, across) if part) or "middle"
        words.append(f"the {where} drawing")
    return words
