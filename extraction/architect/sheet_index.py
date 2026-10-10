"""What a sheet prints about itself, by code from its real text: its sheet number, and the phrases a
vendor's sheet prints (#1166).

**The sheet number** (`read_sheet_labels`): an architect issues every sheet with its number in the
title block (`A-401`). Code reads it from the page's real text, black and grey ink only:

1. a phrase saying so — `SHEET A-401`, `SHEET NO. A-401`, `SHEET NUMBER: A-401` — names it;
2. else a sheet-number-shaped phrase standing alone (`A-401`) printed just below or right of a
   `SHEET` label names it;
3. else nothing: a number that cannot be tied to a label is never guessed from the drawing's own
   notes. Two different labelled numbers on one sheet also give nothing, with the reason.

**A sheet's phrases** (`read_page_phrases`): every phrase printed in black or grey on the page's own
content and inside its pasted drawings, for finding a printed reference to an architect's view
(`view_matching.find_references`). Coloured ink — a reviewer's markup — is never read.

Pure: PDF bytes in, plain values out, no database. Source: issue #1166 · Verification:
`tests/extraction/architect/test_sheet_index.py`
"""

from __future__ import annotations

import io
import re
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

import pdfplumber

from extraction.architect.text import (
    Orientation,
    PrintedPhrase,
    TextChar,
    TextSettings,
    find_phrases,
    orientation_of,
)
from extraction.geometry.rows import Box
from extraction.reader import UnreadablePdf
from extraction.stamp_text import drawing_ink, stamps_only

__all__ = [
    "SheetLabels",
    "read_page_phrases",
    "read_pages_phrases",
    "read_sheet_labels",
    "read_sheets_labels",
    "sheet_labels_on",
]

#: A sheet number: one to three letters, an optional dash, dot or space, one to four digits, an
#: optional decimal part and an optional letter (`A-401`, `ID 7.4`, `A501`, `A-401A`).
_SHEET: Final = r"[A-Z]{1,3}[-. ]?\d{1,4}(?:\.\d{1,3})?[A-Z]?"
_LABELLED: Final = re.compile(
    rf"\bSHEET(?:\s*(?:NO\.?|NUMBER|#))?\s*:?\s*({_SHEET})\b", re.IGNORECASE
)
_ALONE: Final = re.compile(rf"^\s*({_SHEET})\s*$")
_LABEL_ONLY: Final = re.compile(r"^\s*SHEET(?:\s*(?:NO\.?|NUMBER|#))?\s*:?\s*$", re.IGNORECASE)
#: A lone number belongs to a `SHEET` label printed this many label heights above or left of it.
_LABEL_REACH_EM: Final = Decimal(4)


@dataclass(frozen=True, slots=True)
class SheetLabels:
    sheet_number: str | None
    """As printed (`A-401`); `None` when the sheet does not say, with the reason."""
    reason: str


def _decimal(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float, str)) and not isinstance(value, bool):
        return Decimal(str(value))
    raise TypeError(f"not a number: {value!r}")


def _chars(page: Any) -> list[TextChar]:
    chars: list[TextChar] = []
    for char in page.chars:
        text = str(char.get("text", ""))
        if not text.strip() or not drawing_ink(char):
            continue
        x0, x1 = _decimal(char["x0"]), _decimal(char["x1"])
        top, bottom = _decimal(char["top"]), _decimal(char["bottom"])
        chars.append(
            TextChar(
                text=text,
                box=Box(min(x0, x1), min(top, bottom), max(x0, x1), max(top, bottom)),
                orientation=orientation_of(char.get("matrix"), char.get("upright")),
            )
        )
    return chars


def _pages_phrases(
    data: bytes, page_indices: Sequence[int], text: TextSettings
) -> dict[int, tuple[PrintedPhrase, ...]]:
    """Each page's phrases, the document opened once."""
    found: dict[int, tuple[PrintedPhrase, ...]] = {}
    try:
        with pdfplumber.open(io.BytesIO(data)) as document:
            for page_index in page_indices:
                if not 0 <= page_index < len(document.pages):
                    raise UnreadablePdf(f"page {page_index} is beyond the document's pages")
                found[page_index] = find_phrases(_chars(document.pages[page_index]), text)
    except UnreadablePdf:
        raise
    except Exception as error:
        raise UnreadablePdf(f"the pages' text could not be read: {error}") from error
    return found


def _page_phrases(data: bytes, page_index: int, text: TextSettings) -> tuple[PrintedPhrase, ...]:
    return _pages_phrases(data, (page_index,), text)[page_index]


def read_pages_phrases(
    data: bytes, page_indices: Sequence[int], *, text: TextSettings
) -> dict[int, tuple[tuple[str, Box], ...]]:
    """Every black or grey phrase, with where it is printed (pdfplumber's frame), on each page's
    content and inside its pasted drawings. The file is opened once for the pages' content; each
    page's pasted drawings are flattened on their own (`stamp_text.stamps_only`)."""
    content = _pages_phrases(data, page_indices, text)
    found: dict[int, tuple[tuple[str, Box], ...]] = {}
    for page_index in page_indices:
        pasted = _page_phrases(stamps_only(data, page_index), page_index, text)
        found[page_index] = tuple(
            (phrase.text, phrase.box) for phrase in (*content[page_index], *pasted)
        )
    return found


def read_page_phrases(data: bytes, page_index: int, *, text: TextSettings) -> tuple[str, ...]:
    """Every black or grey phrase on the page's content and inside its pasted drawings."""
    return tuple(
        phrase for phrase, _box in read_pages_phrases(data, (page_index,), text=text)[page_index]
    )


def _near_label(number: PrintedPhrase, labels: Sequence[PrintedPhrase]) -> bool:
    """Whether a `SHEET` label is printed just above or just left of a lone number."""
    for label in labels:
        reach = _LABEL_REACH_EM * label.height
        above = (
            label.box.bottom <= number.box.top
            and number.box.top - label.box.bottom <= reach
            and label.box.x0 - reach <= number.box.x0 <= label.box.x1 + reach
        )
        left = (
            label.box.x1 <= number.box.x0
            and number.box.x0 - label.box.x1 <= reach
            and not (number.box.bottom < label.box.top or number.box.top > label.box.bottom)
        )
        if above or left:
            return True
    return False


def read_sheet_labels(data: bytes, page_index: int, *, text: TextSettings) -> SheetLabels:
    """The sheet number printed in the title block, from real text, or `None` with the reason.

    Raises `UnreadablePdf` when the page's text cannot be read.
    """
    return _labels(_page_phrases(data, page_index, text))


def read_sheets_labels(
    data: bytes, page_indices: Sequence[int], *, text: TextSettings
) -> dict[int, SheetLabels]:
    """`read_sheet_labels` for many pages, the file opened once."""
    return {
        page_index: _labels(phrases)
        for page_index, phrases in _pages_phrases(data, page_indices, text).items()
    }


def sheet_labels_on(page: Any, *, text: TextSettings) -> SheetLabels:
    """`read_sheet_labels` for a pdfplumber page the caller already has open."""
    return _labels(find_phrases(_chars(page), text))


def _labels(printed: Sequence[PrintedPhrase]) -> SheetLabels:
    phrases = [phrase for phrase in printed if phrase.orientation is Orientation.UPRIGHT]
    labelled = {
        match.group(1).upper() for phrase in phrases for match in _LABELLED.finditer(phrase.text)
    }
    if len(labelled) == 1:
        (number,) = labelled
        return SheetLabels(number, "printed after the word SHEET")
    if len(labelled) > 1:
        return SheetLabels(
            None, f"the sheet prints {len(labelled)} different numbers after the word SHEET"
        )
    labels = [phrase for phrase in phrases if _LABEL_ONLY.match(phrase.text)]
    near = {
        match.group(1).upper()
        for phrase in phrases
        if (match := _ALONE.match(phrase.text)) is not None and _near_label(phrase, labels)
    }
    if len(near) == 1:
        (number,) = near
        return SheetLabels(number, "printed beside the title block's SHEET label")
    if len(near) > 1:
        return SheetLabels(
            None, f"{len(near)} different numbers are printed beside the SHEET label"
        )
    return SheetLabels(None, "no sheet number is printed beside the word SHEET")
