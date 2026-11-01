"""Numbers printed in text with no letter map, read at their spot by two OCR engines: $0, no AI
(#1750 item 3).

Some PDFs keep their text as font codes with nothing that says which letter each code is: a Type3
font (each character a little drawing of its own) or a Type0 font with no `ToUnicode` map. The
page shows `24 1/2"`; its text layer gives `(cid:21)(cid:20)...`, and the text route
(`verify_value._char`) drops every such character, so a dimension printed that way was silently
missing (the health check said so, `extraction/page_health.no_letter_map`, and nothing read it).

**What is read.** The page's characters with no letter (`(cid:N)` from the PDF reader) that show
ink of their own (a code for a space has none) are put in rows: one reading direction, one size,
one baseline, letters no more than `JOIN_GAP` letter heights apart. A row is a spot to read only
when it is of a number's size, has at least two characters (a dimension has a mark) and nothing
else is printed within `CROWD` letter heights of it (a stacked fraction's digits, a second line, a
word: what a part belongs to decides its value, so the `24"` beside a stacked `1/2` is never read
alone). `dim_labels._add_ocr` reads these spots on the vendor's drawings only, with the OCR
route's own reader (`ocr_labels.read_spots`: the page's vendor layer drawn as a picture, GV's
marks emptied, colours whitened; two engines must read the same one value, read only the way
the file says the text runs, and no word or sign beside it).

**What the letters prove.** The codes say how many characters are there and which of them are
the same character, but not which letters they are:

- a reading counts only when it spells exactly as many characters as the row holds (`Fit`);
- one code of one font is one character: when the readings code confirmed on a page give one
  code two different letters, every confirmed reading using that code goes back to a suggestion
  (`consistent`).

**What the label is.** Exactly what the OCR route makes (`route: "drawn"`, `origin: "ocr"`,
`label_origin.ocr_read`): the drawn-ink witness, never typed text, with no dimension line, so it
never passes a geometry, anchor or same-ends check on a line, and a red resting on it needs the
same second kind of reading any OCR label needs (`reading_routes`). It is marked `FIELD` so the
reading coverage counts it as text with no letter map; a reading two engines did not confirm is
listed under the page's `ocr.codes.unconfirmed`, never a label.

Verification: `tests/review/assistant/test_unmapped_text.py` (PDFs made in the test with a Type3
font and a Type0 font without `ToUnicode`).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any, Final

import numpy as np

from app.review.assistant import ocr_labels
from app.review.assistant import picture_labels as pl

__all__ = [
    "CROWD",
    "FIELD",
    "JOIN_GAP",
    "TWO_LETTERS",
    "UNCONFIRMED",
    "Code",
    "Fit",
    "Row",
    "code_of",
    "consistent",
    "inked",
    "rows",
    "spot_of",
]

type Box = tuple[float, float, float, float]
type Shown = Callable[[float, float], tuple[float, float]]

#: A label read here is marked so (`True`): the reading coverage counts it as text with no map.
FIELD: Final = "no_letter_map"
#: Why a number read in such text is not a label (a suggestion only).
UNCONFIRMED: Final = "number in text with no letter map, read by OCR at its spot: not confirmed"
#: Why a confirmed reading goes back to a suggestion (`consistent`).
TWO_LETTERS: Final = "a letter code of its font reads as two different characters on this page"
#: What the PDF reader gives for a character whose font has no letter for it.
_UNMAPPED: Final = "(cid:"
#: Characters on one row: at most this many letter heights apart along it, their middles at most
#: `_ACROSS` letter heights apart across it, their sizes at most `_SIZES` times apart.
JOIN_GAP: Final = 0.6
_ACROSS: Final = 0.35
_SIZES: Final = 1.3
#: Nothing else printed within this many letter heights of a row (`rows`).
CROWD: Final = 0.5
#: A character's direction is read only when it lies this close to an axis (a sine).
_SQUARE: Final = 0.1
#: A code shows ink of its own when the middle of its box, this share of it left off each side
#: along the row, holds ink (`inked`): a space's box holds none, its neighbours' side bearings do.
_OWN_INK_TRIM: Final = 0.2
#: How much darker than white a pixel must be to be ink (`visible_text`'s level).
_INK_LEVEL: Final = 12
#: Readings spell an inch mark as two primes or quotes: one mark.
_TWO_PRIMES: Final = ("''", '"')


@dataclass(frozen=True, slots=True)
class Code:
    """One character with no letter: its font, its code, its box (shown points, origin top-left),
    the way it reads (`turn`: 0 left to right, 90 up the page, 270 down it, 180 upside down) and its
    height across its row (points)."""

    font: str
    code: str
    box: Box
    turn: int
    size: float

    @property
    def along(self) -> float:
        """Its middle's place along its row."""
        x, y = (self.box[0] + self.box[2]) / 2, (self.box[1] + self.box[3]) / 2
        return {0: x, 180: -x, 90: -y, 270: y}[self.turn]

    @property
    def across(self) -> float:
        """Its middle's place across its row."""
        x, y = (self.box[0] + self.box[2]) / 2, (self.box[1] + self.box[3]) / 2
        return y if self.turn in (0, 180) else x

    @property
    def length(self) -> float:
        """Its extent along its row."""
        x0, y0, x1, y1 = self.box
        return (x1 - x0) if self.turn in (0, 180) else (y1 - y0)


def _turn(dx: float, dy: float) -> int | None:
    """The reading direction (shown space, y down) as a turn, or `None` when it is askew."""
    norm = math.hypot(dx, dy)
    if not norm:
        return None
    dx, dy = dx / norm, dy / norm
    if abs(dy) <= _SQUARE:
        return 0 if dx > 0 else 180
    if abs(dx) <= _SQUARE:
        return 90 if dy < 0 else 270
    return None


def code_of(
    char: Mapping[str, Any],
    shown: Shown,
    *,
    colour_rule: bool = True,
    ink: Callable[[dict[str, Any]], bool] | None = None,
) -> Code | None:
    """A pdfplumber character as a `Code` when its font has no letter for it (`(cid:N)`), else
    `None`. `shown`: pdfplumber's `(x, top)` as a shown point (`verify_value._placement`). With
    `colour_rule` (GV's combined sheets only) a character `ink` says is not drawing ink is left
    out, as the text route leaves it out."""
    text = str(char.get("text", ""))
    if not text.startswith(_UNMAPPED):
        return None
    if colour_rule and ink is not None and not ink(dict(char)):
        return None
    matrix = char.get("matrix")
    if not isinstance(matrix, (tuple, list)) or len(matrix) != 6:
        return None
    a, b = float(matrix[0]), float(matrix[1])
    x0, x1, top, bot = (float(char[k]) for k in ("x0", "x1", "top", "bottom"))
    # The text's direction in pdfplumber's frame (y down), then as shown.
    start, end = shown(x0, bot), shown(x0 + a, bot - b)
    turn = _turn(end[0] - start[0], end[1] - start[1])
    if turn is None:
        return None
    corners = [shown(x, y) for x in (x0, x1) for y in (top, bot)]
    xs, ys = [c[0] for c in corners], [c[1] for c in corners]
    box = (min(xs), min(ys), max(xs), max(ys))
    size = (box[3] - box[1]) if turn in (0, 180) else (box[2] - box[0])
    if size <= 0:
        return None
    return Code(str(char.get("fontname") or ""), text, box, turn, size)


def inked(code: Code, grey: np.ndarray, scale: float) -> bool:
    """Whether the page shows ink of the code's own (`visible_text.InkPicture`'s picture): in its
    box with `_OWN_INK_TRIM` of it left off each end along the row, so a space between two digits
    (whose box their side bearings touch) has none."""
    x0, y0, x1, y1 = code.box
    if code.turn in (0, 180):
        trim = _OWN_INK_TRIM * (x1 - x0)
        x0, x1 = x0 + trim, x1 - trim
    else:
        trim = _OWN_INK_TRIM * (y1 - y0)
        y0, y1 = y0 + trim, y1 - trim
    height, width = grey.shape[:2]
    left = max(0, min(width - 1, int(x0 * scale)))
    upper = max(0, min(height - 1, int(y0 * scale)))
    right = max(left + 1, min(width, math.ceil(x1 * scale)))
    lower = max(upper + 1, min(height, math.ceil(y1 * scale)))
    return bool((grey[upper:lower, left:right] < 255 - _INK_LEVEL).any())


@dataclass(frozen=True, slots=True)
class Row:
    """Characters with no letter on one row, in reading order, and their box (shown points)."""

    codes: tuple[Code, ...]
    box: Box
    turn: int

    @property
    def size(self) -> float:
        return max(code.size for code in self.codes)


def _union(boxes: Sequence[Box]) -> Box:
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def _meets(a: Box, b: Box) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _same_row(row: list[Code], code: Code) -> bool:
    last = row[-1]
    sizes = max(code.size, last.size) / max(min(code.size, last.size), 1e-6)
    gap = (code.along - code.length / 2) - (last.along + last.length / 2)
    return (
        code.turn == last.turn
        and sizes <= _SIZES
        and abs(code.across - last.across) <= _ACROSS * max(code.size, last.size)
        and gap <= JOIN_GAP * max(code.size, last.size)
    )


def rows(codes: Sequence[Code], others: Sequence[Box] = ()) -> tuple[list[Row], int]:
    """The codes' rows that are clear to read, and how many rows were not: every row of codes
    (`_same_row`), kept only when nothing else is printed within `CROWD` letter heights of it,
    neither another code nor any of `others` (the page's other characters, shown points)."""
    found: list[list[Code]] = []
    for turn in sorted({code.turn for code in codes}):
        lying = sorted((c for c in codes if c.turn == turn), key=lambda c: (c.across, c.along))
        bands: list[list[Code]] = []
        for code in lying:
            band = bands[-1] if bands else None
            if band is not None and code.across - band[0].across <= _ACROSS * max(
                code.size, band[0].size
            ):
                band.append(code)
            else:
                bands.append([code])
        for band in bands:
            row: list[Code] = []
            for code in sorted(band, key=lambda c: c.along):
                if row and not _same_row(row, code):
                    found.append(row)
                    row = []
                row.append(code)
            if row:
                found.append(row)
    clear: list[Row] = []
    boxes = [_union([code.box for code in row]) for row in found]
    for index, (row, box) in enumerate(zip(found, boxes, strict=True)):
        reach = CROWD * max(code.size for code in row)
        around = (box[0] - reach, box[1] - reach, box[2] + reach, box[3] + reach)
        near = any(_meets(around, other) for n, other in enumerate(boxes) if n != index) or any(
            _meets(around, other) for other in others
        )
        if not near:
            clear.append(Row(tuple(row), box, row[0].turn))
    return clear, len(found) - len(clear)


def spot_of(row: Row) -> ocr_labels.Spot | None:
    """A row as a spot to read: of a number's size and at least two characters (a dimension has a
    mark: inch, foot or bracket), read only the way its text runs (the file says which way: a
    `6` is never read as an upside-down `9`)."""
    x0, y0, x1, y1 = row.box
    if max(x1 - x0, y1 - y0) > ocr_labels.MAX_LENGTH_PT or row.size < ocr_labels.MIN_LETTER_PT:
        return None
    if len(row.codes) < 2:
        return None
    return ocr_labels.Spot(row.box, (row.turn,), direction_known=True)


def letters(text: str) -> str:
    """A reading's characters as the codes count them: no spaces, an inch mark one character
    however the engine spelled it (`″`, `''`)."""
    plain = "".join(text.translate(pl._PRIMES).split())
    return plain.replace(*_TWO_PRIMES)


def _key(box: Sequence[float]) -> tuple[float, ...]:
    return tuple(round(float(v), 2) for v in box)


@dataclass(slots=True)
class Fit:
    """`ocr_labels.Fits` for rows of codes: a reading counts only when it spells as many
    characters as the row holds (`letters`). Every reading it lets count is kept, by spot and
    turn, for `consistent`."""

    counts: dict[tuple[float, ...], int] = field(default_factory=dict)
    kept: dict[tuple[tuple[float, ...], int], list[tuple[str, tuple[str, Decimal]]]] = field(
        default_factory=dict
    )

    def add(self, row: Row, spot: ocr_labels.Spot) -> None:
        self.counts[_key(spot.box)] = len(row.codes)

    def __call__(
        self, spot: ocr_labels.Spot, turn: int, text: str, parsed: tuple[str, Decimal]
    ) -> bool:
        key = _key(spot.box)
        if len(letters(text)) != self.counts.get(key, -1):
            return False
        self.kept.setdefault((key, turn), []).append((letters(text), parsed))
        return True


def consistent(
    found: Sequence[pl.PictureLabel], rows_by_box: Mapping[tuple[float, ...], Row], fit: Fit
) -> list[pl.PictureLabel]:
    """`found` with every confirmed reading that gives a code of a font a letter another confirmed
    reading on the page gives another letter turned back into a suggestion (`TWO_LETTERS`): one
    code of one font is one character, so one of the two readings is wrong and code cannot tell
    which. The letters come from the readings `fit` let count for the decided value."""
    letters_of: dict[tuple[str, str], set[str]] = {}
    spelled: dict[int, list[tuple[tuple[str, str], str]]] = {}
    for index, label in enumerate(found):
        row = rows_by_box.get(_key(label.box))
        if not label.confirmed or row is None:
            continue
        value = (label.kind, label.inches)
        for text, parsed in fit.kept.get((_key(label.box), label.turn), []):
            if parsed != value or len(text) != len(row.codes):
                continue
            for code, letter in zip(row.codes, text, strict=True):
                key = (code.font, code.code)
                letters_of.setdefault(key, set()).add(letter)
                spelled.setdefault(index, []).append((key, letter))
    two = {key for key, seen in letters_of.items() if len(seen) > 1}
    out: list[pl.PictureLabel] = []
    for index, label in enumerate(found):
        if any(key in two for key, _letter in spelled.get(index, [])):
            label = replace(label, confirmed=False, why=TWO_LETTERS)
        out.append(label)
    return out
