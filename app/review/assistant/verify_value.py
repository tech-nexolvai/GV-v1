"""Is this number really printed at this spot? Code reads it, $0, no AI (`verify_value`, #1531).

A chat finding is the AI's claim that a value is printed inside a box on a page. This module reads
what the drawing itself prints there and compares it with the claimed value in inches (`to_inches`),
one value at a time. It answers `match`, `mismatch` or `cannot_read`, says which route read it, what
it read, and why, in plain words. The same answer every time for the same file.

**Route 1: the drawing's text.** Most numbers in GV's pasted drawings are real text characters
inside the `/Stamp` annotations. A private copy of the page keeps its own content and its pasted
drawings (merged in, `stamp_text.carries_drawing`), and drops every other annotation: GV's markup.
Only black or grey characters are read (`stamp_text.drawing_ink`, the vendor-layer rule). The
characters are put back together by position, whichever way they turn:

- one line of text is read the longest way first (`1'-9"` never also yields `9`);
- a millimetre value over its inches on the next line (`610` over `[24]`): the bracket is inches;
- a stacked fraction (`71` with `1` over `2`) is composed from the characters' places;
- a symbol font that stores `4'-3"` as private-use characters (U+F020 to U+F07E) is decoded;
- a digit that is part of a stacked fraction is never read as a number of its own: the `3` of a
  stacked `3/4` is not `3"`. Two stacked digit rows code cannot put together leave the box unsure.

**Route 2: the vendor's drawn ink.** When the box holds numbers drawn as lines (no text), code
redraws only the vendor's black or grey character-sized paths, which drops GV's coloured boxes and
marks and shows a number hidden under GV's yellow box. Two OCR engines (Tesseract and RapidOCR)
read that redrawing upright and turned both ways, with a stacked fraction split at its bar and each
part read alone; both also read a picture of the vendor's layer with coloured pixels whitened. That
is four reads. **`match` only when at least two of the four read exactly the expected value, both
engines among them, and no read gives a near-miss of it** (one digit different, or the expected value
as a fragment of a longer number). A box over `BIG_BOX_PT2` holding several numbers is refused: draw
the box round the one dimension. And a `1` must look like a `1`: both engines read one architect's
hand-lettered `7 5/8"` as `1 5/8"`, so every glyph Tesseract read as `1` in the value must be a
narrow stroke (`_ONE_MAX_WIDTH`), and a value with a `1` that no engine could show the shape of is
not a match.

**What it never does.** It never calls a model, never reads GV's coloured markup as a value, and
never says `mismatch` from OCR alone: only when the box's text was read, none of it is the value,
and no ink reader read the value. A `match` proves the number is on the drawing at that spot, not
that the comparison built on it is right.

**Loaded by name, never imported** by `app/api/` (`tests/api/test_no_heavy_work.py`): it renders
pages and runs OCR. pdfium is not thread-safe: every render holds `chat_drawing.PDFIUM_LOCK`.

Source: `RESEARCH-stroke-witness.md` (local study, 2026-10-17) · Verification:
`tests/review/assistant/test_verify_value.py`
"""

from __future__ import annotations

import hashlib
import io
import logging
import math
import os
import re
import shlex
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from importlib import import_module
from pathlib import Path
from typing import Any, Final, Literal, Protocol

import cv2
import numpy as np
import pdfplumber
import pikepdf

from app.review.assistant.finding_checks import claim_value, dimension_tokens, to_inches
from extraction.reader import pixel_placement
from extraction.stamp_text import carries_drawing, drawing_ink, path_ink

__all__ = [
    "BIG_BOX_PT2",
    "InkReader",
    "Verified",
    "claim_value",
    "default_readers",
    "dimension_tokens",
    "near_miss",
    "printed_spots",
    "verify_value",
]

_log = logging.getLogger(__name__)

type VerifyResult = Literal["match", "mismatch", "cannot_read"]
type Route = Literal["text", "drawn", "none"]

#: A box larger than this (square points) that holds several numbers is refused by the drawn route.
BIG_BOX_PT2: Final = 2500.0
#: How far outside the box a character may start and still count as at the spot (points): a box
#: drawn tight round a dimension often clips its inch mark or its last digit.
_NEAR_PT: Final = 4.0
#: How far outside the box a drawn path may reach and still be read (points).
_INK_PAD_PT: Final = 3.0
#: A drawn path counts as a character's stroke when its longer side is between these (points):
#: longer is a dimension line or a box, shorter a speck of hatching.
_GLYPH_MAX_PT: Final = 10.0
_GLYPH_MIN_PT: Final = 0.45
#: The vector redrawing: pixels per point, and the largest picture drawn (pixels).
_VECTOR_PX_PER_PT: Final = 12.0
_MAX_PIXELS: Final = 4_000_000
#: The vendor-layer picture: pixels per point, between these, aiming at this many pixels a side.
_PICTURE_SIDE_PX: Final = 1600.0
_PICTURE_MIN_SCALE: Final = 3.0
_PICTURE_MAX_SCALE: Final = 10.0
#: How many reads must give exactly the expected value for a drawn match.
_VOTES_NEEDED: Final = 2
#: A glyph read as `1` that is at least this wide for its height may be a hand-lettered `7` (both
#: engines read one architect's `7 5/8"` as `1 5/8"`): a drawn `1` must be a narrow stroke.
_ONE_MAX_WIDTH: Final = 0.45
#: Fractions a drawing writes: halves to sixty-fourths.
_DENOMINATORS: Final = frozenset({2, 4, 8, 16, 32, 64})
#: The most tokens read together as one value: `3'` `-` `1` `1/2"` is four.
_LONGEST_RUN: Final = 5

_PENDING: Final = "the drawn ink was not read yet"
_NOT_ONE_NUMBER: Final = "not one number"


# ---- the answer ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Verified:
    """What code read at one spot for one expected value."""

    result: VerifyResult
    route: Route
    """`text` (the drawing's characters), `drawn` (the vendor's ink, by OCR) or `none`."""
    read: str | None
    """What code read there: the matching label, or on a mismatch what the drawing prints."""
    reads: tuple[str, ...] = ()
    """Every reading behind the answer, each labelled with its route and engine."""
    reason: str = ""
    """One line, plain words: why this result."""
    value: str | None = None
    """The value that was checked (one dimension, taken from a sentence when it was one)."""
    pending: bool = False
    """The text route ran but the box holds drawn ink still to read (the drawn route was off)."""

    def words(self) -> str:
        """The evidence detail's words."""
        if self.result == "match" and self.route == "text":
            return f"Read from the drawing's text: {self.read}"
        if self.result == "match":
            return f"Read from the vendor's ink by two OCR engines: {self.read}"
        if self.result == "mismatch":
            return f"The drawing prints {self.read} here, not {self.value}"
        return f"Code could not read this spot: {self.reason}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "result": self.result,
            "route": self.route,
            "read": self.read,
            "reads": list(self.reads),
            "reason": self.reason,
            "value": self.value,
            "pending": self.pending,
            "words": self.words(),
        }


# ---- values from text ----------------------------------------------------------------------------

_MARKS: Final = (
    ("−", "-"),
    ("–", "-"),
    ("—", "-"),
    ("’", "'"),
    ("‘", "'"),
    ("′", "'"),
    ("”", '"'),
    ("“", '"'),
    ("″", '"'),
    ("''", '"'),
    ("½", " 1/2"),
    ("¼", " 1/4"),
    ("¾", " 3/4"),
)


def _plain(text: str) -> str:
    """Symbol-font characters (U+F020 to U+F07E, a font that stores `4'-3"` as private-use
    characters) as the ASCII they stand for, and a drawing's marks in their plain spelling."""
    text = "".join(chr(ord(ch) - 0xF000) if 0xF020 <= ord(ch) <= 0xF07E else ch for ch in text)
    for spelling, plain in _MARKS:
        text = text.replace(spelling, plain)
    return text


_BRACKETED: Final = re.compile(r"\[\s*([0-9][0-9 /\-.]*)\s*\"?\s*\]")


def _values_of_tokens(tokens: Sequence[str]) -> set[Decimal]:
    """Values in tokens in reading order, read the longest way first and consumed, so `1' - 9"`
    never also yields `9`; plus inches in brackets (`[24]` on the line under `610`)."""
    plain = [_plain(token).rstrip(".,;:") for token in tokens if token.strip()]
    plain = [token for token in plain if token]
    values: set[Decimal] = set()
    start = 0
    while start < len(plain):
        for size in range(min(_LONGEST_RUN, len(plain) - start), 0, -1):
            value = to_inches(" ".join(plain[start : start + size]))
            if value is not None:
                values.add(value)
                start += size
                break
        else:
            start += 1
    for bracket in _BRACKETED.finditer(" ".join(plain)):
        value = to_inches(bracket.group(1).strip() + '"')
        if value is not None:
            values.add(value)
    return values


def _fraction_ok(numerator: str, denominator: str) -> bool:
    return (
        numerator.isdigit()
        and denominator.isdigit()
        and int(denominator) in _DENOMINATORS
        and 0 < int(numerator) < int(denominator)
    )


# ---- the page, read once -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Char:
    text: str
    box: tuple[float, float, float, float]
    """Shown points, origin top-left."""
    direction: tuple[float, float]
    along: float
    """Position along the reading direction (points)."""
    across: float
    """Position up across the line (points)."""
    size: float
    """The character's height across its line."""
    length: float
    """The character's length along its line."""


@dataclass(frozen=True, slots=True)
class _Path:
    box: tuple[float, float, float, float]
    """Shown points, origin top-left."""
    polylines: tuple[tuple[tuple[float, float], ...], ...]
    """In shown points."""
    filled: bool
    width: float


@dataclass(slots=True)
class _Page:
    key: tuple[str, int, int, int]
    vendor_pdf: bytes
    """A one-page PDF: the page's own content and its pasted drawings, no other annotation."""
    chars: tuple[_Char, ...]
    paths: tuple[_Path, ...]
    pictures: tuple[tuple[float, float, float, float], ...]
    """Where the page shows a pasted picture (shown points): only OCR can read one."""
    size: tuple[float, float]
    """Shown width and height (points)."""


_CACHE_LOCK: Final = threading.Lock()
_PAGE_CACHE: OrderedDict[tuple[str, int, int, int], _Page] = OrderedDict()
_PAGE_CACHE_SIZE: Final = 16
_PAGE_LOCKS: dict[tuple[str, int, int, int], threading.Lock] = {}


def _vendor_page(data: bytes, page_index: int) -> bytes:
    """A one-page copy of page `page_index` with its own content and only its pasted drawings
    (`carries_drawing`), merged into it."""
    with pikepdf.open(io.BytesIO(data)) as source:
        page = source.pages[page_index]
        out = pikepdf.new()
        out.pages.append(page)
        copy = out.pages[0]
        annotations = copy.obj.get("/Annots") or pikepdf.Array()
        copy.obj["/Annots"] = pikepdf.Array([a for a in annotations if carries_drawing(a)])
        out.flatten_annotations(mode="all")
        buffer = io.BytesIO()
        out.save(buffer)
        return buffer.getvalue()


def _placement(page: Any) -> Callable[[float, float], tuple[float, float]]:
    """pdfplumber's `(x, top)` as a shown point: the repo's placement (`pixel_placement`), which
    is affine (crop box, rotation), taken at 1/100 point and applied in floats."""
    place = pixel_placement(page, 7200)
    origin, right, down = place(0, 0), place(1000, 0), place(0, 1000)
    ax, ay = (right.x - origin.x) / 1000, (right.y - origin.y) / 1000
    bx, by = (down.x - origin.x) / 1000, (down.y - origin.y) / 1000

    def shown(x: float, top: float) -> tuple[float, float]:
        return (
            (origin.x + ax * x + bx * top) / 100,
            (origin.y + ay * x + by * top) / 100,
        )

    return shown


def _shown_box(
    shown: Callable[[float, float], tuple[float, float]],
    x0: float,
    top: float,
    x1: float,
    bot: float,
) -> tuple[float, float, float, float]:
    corners = [shown(x, y) for x in (x0, x1) for y in (top, bot)]
    xs, ys = [c[0] for c in corners], [c[1] for c in corners]
    return min(xs), min(ys), max(xs), max(ys)


def _polylines(item: Mapping[str, Any]) -> list[list[tuple[float, float]]]:
    """A pdfplumber line, curve or rectangle as polylines in its `(x, top)` frame."""
    path = item.get("path")
    out: list[list[tuple[float, float]]] = []
    if path:
        current: list[tuple[float, float]] = []
        for command in path:
            op = command[0]
            if op == "m":
                if current:
                    out.append(current)
                current = [(float(command[1][0]), float(command[1][1]))]
            elif op == "l":
                current.append((float(command[1][0]), float(command[1][1])))
            elif op == "c":
                start = current[-1] if current else (float(command[1][0]), float(command[1][1]))
                c1, c2, end = ((float(p[0]), float(p[1])) for p in command[1:4])
                for step in range(1, 9):
                    t = step / 8
                    u = 1 - t
                    current.append(
                        (
                            u**3 * start[0]
                            + 3 * u * u * t * c1[0]
                            + 3 * u * t * t * c2[0]
                            + t**3 * end[0],
                            u**3 * start[1]
                            + 3 * u * u * t * c1[1]
                            + 3 * u * t * t * c2[1]
                            + t**3 * end[1],
                        )
                    )
            elif op == "h" and current:
                current.append(current[0])
        if current:
            out.append(current)
        return out
    points = item.get("pts")
    if points:
        return [[(float(x), float(y)) for x, y in points]]
    x0, x1, top, bot = (float(item[k]) for k in ("x0", "x1", "top", "bottom"))
    return [[(x0, top), (x1, top), (x1, bot), (x0, bot), (x0, top)]]


def _read_page(path: Path, page_index: int) -> _Page:
    """The page's vendor-layer characters and character-sized paths (cached per file version)."""
    stat = path.stat()
    key = (str(path.resolve()), stat.st_mtime_ns, stat.st_size, page_index)
    with _CACHE_LOCK:
        cached = _PAGE_CACHE.get(key)
        if cached is not None:
            _PAGE_CACHE.move_to_end(key)
            return cached
        lock = _PAGE_LOCKS.setdefault(key, threading.Lock())
    with lock:
        with _CACHE_LOCK:
            cached = _PAGE_CACHE.get(key)
            if cached is not None:
                return cached
        vendor = _vendor_page(path.read_bytes(), page_index)
        chars: list[_Char] = []
        paths: list[_Path] = []
        with pdfplumber.open(io.BytesIO(vendor)) as document:
            page = document.pages[0]
            shown = _placement(page)
            corner = shown(float(page.width), float(page.height))
            start = shown(0.0, 0.0)
            size = (abs(corner[0] - start[0]), abs(corner[1] - start[1]))
            for char in page.chars:
                made = _char(char, shown)
                if made is not None:
                    chars.append(made)
            for item in (*page.curves, *page.lines, *page.rects):
                made_path = _path(item, shown)
                if made_path is not None:
                    paths.append(made_path)
            pictures = tuple(
                _shown_box(shown, *(float(image[k]) for k in ("x0", "top", "x1", "bottom")))
                for image in page.images
            )
        result = _Page(key, vendor, tuple(chars), tuple(paths), pictures, size)
        with _CACHE_LOCK:
            _PAGE_CACHE[key] = result
            while len(_PAGE_CACHE) > _PAGE_CACHE_SIZE:
                old, _ = _PAGE_CACHE.popitem(last=False)
                _PAGE_LOCKS.pop(old, None)
        return result


def _char(
    char: Mapping[str, Any], shown: Callable[[float, float], tuple[float, float]]
) -> _Char | None:
    text = _plain(str(char.get("text", "")))
    if not text.strip() or text.startswith("(cid:") or not drawing_ink(dict(char)):
        return None
    matrix = char.get("matrix")
    if not isinstance(matrix, (tuple, list)) or len(matrix) != 6:
        return None
    a, b, _c, _d, e, f = (float(v) for v in matrix)
    norm = math.hypot(a, b) or 1.0
    dx, dy = a / norm, b / norm
    ux, uy = -dy, dx
    x0, x1, top, bot = (float(char[k]) for k in ("x0", "x1", "top", "bottom"))
    sideways = abs(dx) < 0.5
    size = (x1 - x0) if sideways else (bot - top)
    length = (bot - top) if sideways else (x1 - x0)
    return _Char(
        text=text,
        box=_shown_box(shown, x0, top, x1, bot),
        direction=(round(dx, 2), round(dy, 2)),
        along=e * dx + f * dy,
        across=e * ux + f * uy,
        size=size or 1.0,
        length=length,
    )


def _path(
    item: Mapping[str, Any], shown: Callable[[float, float], tuple[float, float]]
) -> _Path | None:
    if not path_ink(dict(item)):
        return None
    x0, x1, top, bot = (float(item[k]) for k in ("x0", "x1", "top", "bottom"))
    longest = max(x1 - x0, bot - top)
    if longest > _GLYPH_MAX_PT or longest < _GLYPH_MIN_PT:
        return None
    lines = tuple(tuple(shown(x, y) for x, y in line) for line in _polylines(item) if line)
    return _Path(
        box=_shown_box(shown, x0, top, x1, bot),
        polylines=lines,
        filled=bool(item.get("fill")),
        width=float(item.get("linewidth") or 0.3),
    )


# ---- route 1: the drawing's text -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _TextRead:
    values: frozenset[Decimal]
    labels: tuple[str, ...]
    """What each label prints, as composed."""
    doubtful: bool
    """Stacked digits code could not put together: the box may print more than it read."""


def _labels(chars: Sequence[_Char]) -> list[list[_Char]]:
    """Characters grouped into labels: same direction, near each other along and across."""
    by_direction: dict[tuple[float, float], list[_Char]] = {}
    for char in chars:
        by_direction.setdefault(char.direction, []).append(char)
    labels: list[list[_Char]] = []
    for group in by_direction.values():
        used = [False] * len(group)
        for first in range(len(group)):
            if used[first]:
                continue
            label = [group[first]]
            used[first] = True
            grew = True
            while grew:
                grew = False
                for other in range(len(group)):
                    if used[other]:
                        continue
                    candidate = group[other]
                    for member in label:
                        size = max(member.size, candidate.size)
                        if (
                            abs(candidate.across - member.across) <= 0.9 * size
                            and abs(candidate.along - member.along) <= 1.25 * size
                        ):
                            label.append(candidate)
                            used[other] = True
                            grew = True
                            break
            labels.append(label)
    return labels


def _row_text(chars: Iterable[_Char], font: float) -> str:
    text = ""
    previous: _Char | None = None
    for char in sorted(chars, key=lambda c: c.along):
        if previous is not None and char.along - (previous.along + previous.length) > 0.18 * font:
            text += " "
        text += char.text
        previous = char
    return text


_WHOLE: Final = re.compile(r"(\d+'\s*-?\s*)?\d+")


def _compose(label: Sequence[_Char]) -> tuple[list[tuple[str, bool]], bool]:
    """A label's readings: `(text, composed fraction)` per row or stacked fraction, and whether
    stacked digits were left that code could not put together."""
    parts, doubtful = compose_parts(label)
    return [(text, fraction) for text, fraction, _chars in parts], doubtful


def compose_parts(
    label: Sequence[_Char],
) -> tuple[list[tuple[str, bool, tuple[_Char, ...]]], bool]:
    """`_compose` with the characters behind each reading (the dimension labels' boxes, #1558): a
    composed fraction's text with its characters, and each row's characters left over (its text
    is `_row_text` of them)."""
    font = max(char.size for char in label)
    rows: list[list[_Char]] = []
    for char in sorted(label, key=lambda c: -c.across):
        for row in rows:
            if abs(row[0].across - char.across) <= 0.2 * font:
                row.append(char)
                break
        else:
            rows.append([char])
    out: list[tuple[str, bool, tuple[_Char, ...]]] = []
    consumed: set[int] = set()
    for base in rows:
        level = base[0].across
        above = [
            c
            for row in rows
            for c in row
            if level + 0.2 * font < row[0].across < level + 0.9 * font and c.text.isdigit()
        ]
        below = [
            c
            for row in rows
            for c in row
            if level - 0.9 * font < row[0].across < level - 0.2 * font and c.text.isdigit()
        ]
        if not above or not below:
            continue
        start = min(c.along for c in above + below)
        end = max(c.along for c in above + below)
        if abs(min(c.along for c in above) - min(c.along for c in below)) > 0.8 * font:
            continue
        numerator = "".join(c.text for c in sorted(above, key=lambda c: c.along))
        denominator = "".join(c.text for c in sorted(below, key=lambda c: c.along))
        if not _fraction_ok(numerator, denominator):
            continue
        before = _row_text([c for c in base if c.along < start], font).strip()
        after = _row_text([c for c in base if c.along > end], font).strip()
        chars = (*above, *below, *base)
        if before and _WHOLE.fullmatch(before):
            out.append((f"{before} {numerator}/{denominator}{after or chr(34)}", True, chars))
        elif not before:
            out.append((f"{numerator}/{denominator}{after or chr(34)}", True, chars))
        else:
            continue
        consumed |= {id(c) for c in above + below} | {id(c) for c in base}
    # Stacked digits with no base row: a fraction on its own (`3` over `4`), else unsure. Its
    # digits are never read as numbers of their own (the `3` of a stacked `3/4` is not `3"`).
    doubtful = False
    left = [[c for c in row if id(c) not in consumed] for row in rows]
    digit_rows = [row for row in left if row and all(c.text.isdigit() for c in row)]
    for upper in digit_rows:
        for lower in digit_rows:
            if upper is lower or not 0.2 * font < upper[0].across - lower[0].across < 1.3 * font:
                continue
            if any(id(c) in consumed for c in upper + lower):
                continue
            u0, u1 = min(c.along for c in upper), max(c.along + c.length for c in upper)
            l0, l1 = min(c.along for c in lower), max(c.along + c.length for c in lower)
            if u1 < l0 or l1 < u0:
                continue
            numerator = "".join(c.text for c in sorted(upper, key=lambda c: c.along))
            denominator = "".join(c.text for c in sorted(lower, key=lambda c: c.along))
            if _fraction_ok(numerator, denominator):
                out.append((f'{numerator}/{denominator}"', True, (*upper, *lower)))
            else:
                doubtful = True
            consumed |= {id(c) for c in upper + lower}
    for row in rows:
        rest = [c for c in row if id(c) not in consumed]
        if rest:
            out.append((_row_text(rest, font), False, tuple(rest)))
    return out, doubtful


def _near(box: tuple[float, float, float, float], spot: tuple[float, float, float, float]) -> bool:
    return not (
        box[2] < spot[0] - _NEAR_PT
        or box[0] > spot[2] + _NEAR_PT
        or box[3] < spot[1] - _NEAR_PT
        or box[1] > spot[3] + _NEAR_PT
    )


def _digit_row(label: Sequence[_Char]) -> bool:
    """A label that is one short row of digits only (what a stacked fraction's part looks like)."""
    if not label or len(label) > 2 or not all(char.text.isdigit() for char in label):
        return False
    size = max(char.size for char in label)
    return max(c.across for c in label) - min(c.across for c in label) <= 0.2 * size


def _stacked_pairs(labels: Sequence[Sequence[_Char]]) -> list[tuple[int, int]]:
    """Pairs of short digit rows, one right above the other: a stacked fraction's numerator and
    denominator that the label grouping put apart (`(upper, lower)` label indexes)."""
    rows = [i for i, label in enumerate(labels) if _digit_row(label)]
    pairs: list[tuple[int, int]] = []
    for upper in rows:
        for lower in rows:
            a, b = labels[upper], labels[lower]
            if upper == lower or a[0].direction != b[0].direction:
                continue
            size = max(c.size for c in (*a, *b))
            if not 0.5 * size < a[0].across - b[0].across < 1.8 * size:
                continue
            a0, a1 = min(c.along for c in a), max(c.along + c.length for c in a)
            b0, b1 = min(c.along for c in b), max(c.along + c.length for c in b)
            if a1 >= b0 and b1 >= a0:
                pairs.append((upper, lower))
    return pairs


def _whole_before(
    labels: Sequence[Sequence[_Char]],
    upper: Sequence[_Char],
    lower: Sequence[_Char],
    taken: set[int],
) -> str | None:
    """The whole number written just before a stacked fraction (`27` of `27 1/4"`), if any."""
    size = max(c.size for c in (*upper, *lower))
    start = min(c.along for c in (*upper, *lower))
    low, high = lower[0].across - 0.5 * size, upper[0].across + 0.5 * size
    for index, label in enumerate(labels):
        if index in taken or label[0].direction != upper[0].direction:
            continue
        last = max(label, key=lambda c: c.along)
        gap = start - (last.along + last.length)
        if last.text.isdigit() and -0.2 * size <= gap <= 1.0 * size and low <= last.across <= high:
            text = _row_text([c for c in label if abs(c.across - last.across) <= 0.2 * size], size)
            return text.strip() if _WHOLE.fullmatch(text.strip()) else None
    return None


def _text_read(page: _Page, spot: tuple[float, float, float, float]) -> _TextRead:
    inside = [char for char in page.chars if _near(char.box, spot)]
    values: set[Decimal] = set()
    labels: list[str] = []
    doubtful = False
    grouped = _labels(inside)
    # A stacked fraction's parts are never numbers of their own: the `3` of `3/4` is not `3"`.
    stacked: set[int] = set()
    for upper, lower in _stacked_pairs(grouped):
        if upper in stacked or lower in stacked:
            continue
        stacked |= {upper, lower}
        numerator = "".join(c.text for c in sorted(grouped[upper], key=lambda c: c.along))
        denominator = "".join(c.text for c in sorted(grouped[lower], key=lambda c: c.along))
        if not _fraction_ok(numerator, denominator):
            doubtful = True
            continue
        values.add(Decimal(int(numerator)) / Decimal(int(denominator)))
        labels.append(f'{numerator}/{denominator}"')
        whole = _whole_before(grouped, grouped[upper], grouped[lower], stacked)
        if whole is not None:
            value = to_inches(f'{whole} {numerator}/{denominator}"')
            if value is not None:
                values.add(value)
                labels.append(f'{whole} {numerator}/{denominator}"')
    for index, label in enumerate(grouped):
        if index in stacked:
            continue
        composed, unsure = _compose(label)
        doubtful = doubtful or unsure
        for text, fraction in composed:
            if fraction:
                value = to_inches(text)
                if value is not None:
                    values.add(value)
            else:
                values |= _values_of_tokens(text.split())
            labels.append(" ".join(text.split()))
    return _TextRead(frozenset(values), tuple(label for label in labels if label), doubtful)


# ---- route 2: the vendor's drawn ink -------------------------------------------------------------


class InkReader(Protocol):
    """One OCR engine: the text lines it reads in a grey picture (dark ink on white)."""

    name: str

    def __call__(self, picture: np.ndarray, *, single_line: bool) -> list[str]: ...


#: The characters a dimension is written with: what Tesseract is told to look for.
_WHITELIST: Final = "0123456789/'\"-[]. "


class TesseractReader:
    """Tesseract (Apache-2.0) through pytesseract, sparse text or one line."""

    name = "Tesseract"

    def __init__(self) -> None:
        self._tesseract = import_module("pytesseract")
        self._tesseract.get_tesseract_version()  # raises when the program is not installed

    def __call__(self, picture: np.ndarray, *, single_line: bool) -> list[str]:
        config = f"--psm {7 if single_line else 11} -c " + shlex.quote(
            f"tessedit_char_whitelist={_WHITELIST}"
        )
        text = self._tesseract.image_to_string(picture, config=config, timeout=60)
        return [line for line in str(text).splitlines() if line.strip()]

    def one_shapes(self, picture: np.ndarray, *, single_line: bool) -> list[float]:
        """The width-to-height of the ink of every glyph Tesseract reads as `1` here."""
        config = f"--psm {7 if single_line else 11} -c " + shlex.quote(
            f"tessedit_char_whitelist={_WHITELIST}"
        )
        boxes = self._tesseract.image_to_boxes(picture, config=config, timeout=60)
        height = picture.shape[0]
        out: list[float] = []
        for line in str(boxes).splitlines():
            parts = line.split(" ")
            if len(parts) < 5 or parts[0] != "1":
                continue
            x0, y0, x1, y1 = (int(v) for v in parts[1:5])
            ink = np.nonzero(picture[height - y1 : height - y0, x0:x1] < 128)
            if len(ink[0]):
                tall = int(ink[0].max() - ink[0].min() + 1)
                wide = int(ink[1].max() - ink[1].min() + 1)
                out.append(wide / tall)
        return out


class RapidReader:
    """RapidOCR (Apache-2.0, ONNX models inside the package), one instance, one call at a time."""

    name = "RapidOCR"

    def __init__(self) -> None:
        ocr = import_module("extraction.ocr")
        self._engine = ocr.RapidOcrEngine()
        self._lock = threading.Lock()

    def __call__(self, picture: np.ndarray, *, single_line: bool) -> list[str]:
        rgb = np.ascontiguousarray(cv2.cvtColor(picture, cv2.COLOR_GRAY2RGB))
        height, width = picture.shape[:2]
        with self._lock:
            items = self._engine.read(rgb.tobytes(), width=width, height=height)
        return [item.text for item in items]


_READERS_LOCK: Final = threading.Lock()
_READERS: list[InkReader] | None = None


def default_readers() -> list[InkReader]:
    """The OCR engines installed here (loaded once). An engine that is not installed is left out
    and said in the log: fewer engines can only give fewer reads, never a looser match."""
    global _READERS
    with _READERS_LOCK:
        if _READERS is None:
            found: list[InkReader] = []
            for make in (TesseractReader, RapidReader):
                try:
                    found.append(make())
                except Exception as missing:  # noqa: BLE001 - an engine not installed
                    _log.warning("verify_value: %s is not available: %s", make.name, missing)
            _READERS = found
        return list(_READERS)


def _ink_paths(page: _Page, spot: tuple[float, float, float, float]) -> list[_Path]:
    x0, y0, x1, y1 = spot
    return [
        path
        for path in page.paths
        if path.box[0] >= x0 - _INK_PAD_PT
        and path.box[2] <= x1 + _INK_PAD_PT
        and path.box[1] >= y0 - _INK_PAD_PT
        and path.box[3] <= y1 + _INK_PAD_PT
    ]


def _draw(paths: Sequence[_Path], turn: int = 0, margin: float = 1.5) -> np.ndarray | None:
    """Only these paths, black on white, then turned (degrees clockwise)."""
    if not paths:
        return None
    left = min(p.box[0] for p in paths) - margin
    top = min(p.box[1] for p in paths) - margin
    right = max(p.box[2] for p in paths) + margin
    bottom = max(p.box[3] for p in paths) + margin
    scale = _VECTOR_PX_PER_PT
    width, height = (right - left) * scale, (bottom - top) * scale
    if width * height > _MAX_PIXELS:
        scale *= math.sqrt(_MAX_PIXELS / (width * height))
    picture = np.full(
        (int((bottom - top) * scale) + 2, int((right - left) * scale) + 2), 255, np.uint8
    )
    outlines: list[np.ndarray] = []
    for path in paths:
        thickness = max(1, round(max(path.width, 0.2) * scale))
        shapes = [
            np.round(np.array([[(x - left) * scale, (y - top) * scale] for x, y in line])).astype(
                np.int32
            )
            for line in path.polylines
        ]
        if path.filled and any(len(points) >= 3 for points in shapes):
            outlines.extend(points for points in shapes if len(points) >= 3)
            continue
        for points in shapes:
            if len(points) >= 2:
                cv2.polylines(picture, [points], False, 0, thickness)
    if outlines:
        # One even-odd fill for every outline: a glyph's hole (the inside of a 6) is its own
        # subpath, often its own path, and stays open.
        mask = np.zeros_like(picture)
        for points in outlines:
            x, y, w, h = cv2.boundingRect(points)
            single = np.zeros((h, w), np.uint8)
            cv2.fillPoly(single, [points - np.array([x, y], np.int32)], 1)
            x0, y0 = max(x, 0), max(y, 0)
            part = mask[y0 : y + h, x0 : x + w]
            part ^= single[y0 - y : y0 - y + part.shape[0], x0 - x : x0 - x + part.shape[1]]
        picture[mask > 0] = 0
    framed: np.ndarray = cv2.copyMakeBorder(picture, 20, 20, 20, 20, cv2.BORDER_CONSTANT, value=255)
    return _turned(framed, turn)


def _turned(picture: np.ndarray, turn: int) -> np.ndarray:
    if turn == 90:
        return cv2.rotate(picture, cv2.ROTATE_90_CLOCKWISE)
    if turn == 270:
        return cv2.rotate(picture, cv2.ROTATE_90_COUNTERCLOCKWISE)
    return picture


def _bars(paths: Sequence[_Path]) -> list[tuple[_Path, str, list[_Path], list[_Path]]]:
    """Fraction bars: a short thin stroke with character-sized ink right above and below it (or
    left and right of it, for a sideways fraction)."""
    out = []
    for bar in paths:
        width, height = bar.box[2] - bar.box[0], bar.box[3] - bar.box[1]
        flat = height <= 0.25 and 0.8 <= width <= 5
        upright = width <= 0.25 and 0.8 <= height <= 5
        if not (flat or upright):
            continue
        if flat:
            above = [
                p
                for p in paths
                if p is not bar
                and bar.box[1] - 1.2 <= p.box[3] <= bar.box[1] + 0.2
                and p.box[0] >= bar.box[0] - 0.5
                and p.box[2] <= bar.box[2] + 0.5
                and p.box[3] - p.box[1] > 0.6
            ]
            below = [
                p
                for p in paths
                if p is not bar
                and bar.box[3] - 0.2 <= p.box[1] <= bar.box[3] + 1.2
                and p.box[0] >= bar.box[0] - 0.5
                and p.box[2] <= bar.box[2] + 0.5
                and p.box[3] - p.box[1] > 0.6
            ]
        else:
            above = [
                p
                for p in paths
                if p is not bar
                and bar.box[0] - 1.2 <= p.box[2] <= bar.box[0] + 0.2
                and p.box[1] >= bar.box[1] - 0.5
                and p.box[3] <= bar.box[3] + 0.5
                and p.box[2] - p.box[0] > 0.6
            ]
            below = [
                p
                for p in paths
                if p is not bar
                and bar.box[2] - 0.2 <= p.box[0] <= bar.box[2] + 1.2
                and p.box[1] >= bar.box[1] - 0.5
                and p.box[3] <= bar.box[3] + 0.5
                and p.box[2] - p.box[0] > 0.6
            ]
        if above and below:
            out.append((bar, "flat" if flat else "upright", above, below))
    return out


def _digits(lines: Iterable[str]) -> str:
    return re.sub(r"\D", "", "".join(lines))


def _fractions(
    paths: Sequence[_Path], reader: InkReader
) -> list[tuple[str, list[tuple[np.ndarray, list[str]]]]]:
    """Each stacked fraction read part by part: the whole number before the bar, the numerator
    and the denominator, each drawn and read alone. `(text, [(part picture, lines read)])`."""
    found: list[tuple[str, list[tuple[np.ndarray, list[str]]]]] = []
    for bar, way, above, below in _bars(paths):
        size = max(bar.box[2] - bar.box[0], bar.box[3] - bar.box[1])
        parts = set(map(id, above + below)) | {id(bar)}
        if way == "flat":
            whole = [
                p
                for p in paths
                if id(p) not in parts
                and bar.box[0] - 4 * size <= p.box[2] <= bar.box[0] + 0.3
                and p.box[1] <= bar.box[1] + 1.5 * size
                and p.box[3] >= bar.box[1] - 1.5 * size
            ]
            turn = 0
        else:
            whole = [
                p
                for p in paths
                if id(p) not in parts
                and bar.box[3] - 0.3 <= p.box[1] <= bar.box[3] + 4 * size
                and p.box[0] <= bar.box[2] + 1.5 * size
                and p.box[2] >= bar.box[0] - 1.5 * size
            ]
            turn = 90
        read: list[tuple[np.ndarray, list[str]]] = []

        def one(
            group: Sequence[_Path],
            turn: int = turn,
            read: list[tuple[np.ndarray, list[str]]] = read,
        ) -> str:
            picture = _draw(group, turn)
            if picture is None:
                return ""
            lines = reader(picture, single_line=True)
            read.append((picture, lines))
            return _digits(lines)

        numerator, denominator = one(above), one(below)
        if not _fraction_ok(numerator, denominator):
            continue
        whole_number = one(whole) if whole else ""
        found.append((f'{whole_number} {numerator}/{denominator}"'.strip(), read))
    return found


#: The hi-res page cache folder of the running `verify_value` call (#1548), or `None`.
_CACHE_FOLDER: ContextVar[Path | None] = ContextVar("verify_value_page_cache", default=None)


def _cached_layer(
    page: _Page, area: tuple[float, float, float, float], scale: float
) -> np.ndarray | None:
    """The vendor layer's area cut from its 300-dpi picture (`page_cache`), when a cache folder is
    given and the picture wanted is 300 dpi or less; else `None` (rendered as before). The layer is
    stored once as its own one-page PDF, named by its hash, so the cache keys it like any PDF."""
    folder = _CACHE_FOLDER.get()
    page_cache = import_module("app.review.assistant.page_cache")
    if folder is None or scale * 72 > page_cache.CACHE_DPI:
        return None
    try:
        layers = folder / "layers"
        layers.mkdir(parents=True, exist_ok=True)
        layer = layers / f"{hashlib.sha256(page.vendor_pdf).hexdigest()[:24]}.pdf"
        if not layer.is_file():
            temporary = layer.with_name(f".{layer.name}.{threading.get_ident()}.tmp")
            temporary.write_bytes(page.vendor_pdf)
            os.replace(temporary, layer)
        picture = page_cache.cut(folder, layer, 1, area, scale)
    except Exception as failed:  # noqa: BLE001 - rendered directly instead
        _log.warning("verify_value page cache failed: %s", type(failed).__name__)
        return None
    return None if picture is None else np.array(picture.convert("RGB"))


def _picture(page: _Page, spot: tuple[float, float, float, float]) -> np.ndarray | None:
    """The vendor's layer at the spot as a grey picture, coloured pixels whitened (GV's marks)."""
    pdfium = import_module("pypdfium2")
    lock = import_module("app.review.assistant.chat_drawing").PDFIUM_LOCK
    width, height = page.size
    x0, y0, x1, y1 = spot
    left, top = max(0.0, x0 - _INK_PAD_PT), max(0.0, y0 - _INK_PAD_PT)
    right, bottom = min(width, x1 + _INK_PAD_PT), min(height, y1 + _INK_PAD_PT)
    if right - left < 1 or bottom - top < 1:
        return None
    scale = float(
        np.clip(
            _PICTURE_SIDE_PX / max(right - left, bottom - top),
            _PICTURE_MIN_SCALE,
            _PICTURE_MAX_SCALE,
        )
    )
    if (right - left) * (bottom - top) * scale * scale > _MAX_PIXELS:
        scale = math.sqrt(_MAX_PIXELS / ((right - left) * (bottom - top)))
    cached = _cached_layer(page, (left, top, right, bottom), scale)
    if cached is not None:
        rgb = cached
    else:
        with lock:
            document = pdfium.PdfDocument(page.vendor_pdf)
            try:
                rendered = document[0].render(
                    scale=scale,
                    crop=(left, height - bottom, width - right, top),
                    may_draw_forms=True,
                )
                rgb = np.array(rendered.to_pil().convert("RGB"))
            finally:
                document.close()
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    rgb[(hsv[:, :, 1] > 60) & (hsv[:, :, 2] > 60)] = 255
    grey: np.ndarray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    return grey


_DASHED_FRACTION: Final = re.compile(r"(\d)\s*-\s*(\d+/\d+)")
_BARE: Final = re.compile(r"\d+(?:\s+\d+/\d+)?|\d+/\d+")


def _ocr_values(lines: Iterable[str]) -> set[Decimal]:
    """Values in OCR lines: dimensions as written, and every bare number as inches."""
    values: set[Decimal] = set()
    for line in lines:
        clean = _DASHED_FRACTION.sub(r"\1 \2", _plain(line))
        values |= _values_of_tokens(clean.split())
        for bare in _BARE.finditer(clean):
            value = to_inches(bare.group(0) + '"')
            if value is not None:
                values.add(value)
    return values


def _digit_string(value: Decimal) -> str:
    exact = Fraction(value)
    whole = exact.numerator // exact.denominator
    rest = exact - whole
    return (str(whole) if whole else "") + (f"{rest.numerator}{rest.denominator}" if rest else "")


def near_miss(expected: Decimal, read: Decimal) -> bool:
    """Whether `read` is a near-miss of `expected`: the same number of digits with one different,
    or `expected`'s digits inside a longer number (`5"` inside `56"`)."""
    if read == expected:
        return False
    want, got = _digit_string(expected), _digit_string(read)
    if len(got) > len(want) and want in got:
        return True
    return len(got) == len(want) and sum(a != b for a, b in zip(got, want, strict=True)) == 1


@dataclass(slots=True)
class _InkRead:
    name: str
    engine: str
    lines: list[str] = field(default_factory=list)
    values: set[Decimal] = field(default_factory=set)
    passes: list[tuple[frozenset[Decimal], tuple[float, ...] | None]] = field(default_factory=list)
    """Each picture read: its values, and the width-to-height of every glyph it read as `1`
    (`None` when the engine cannot say where its characters are)."""

    def add(self, reader: InkReader, picture: np.ndarray, lines: list[str], single: bool) -> None:
        values = frozenset(_ocr_values(lines))
        self.lines.extend(lines)
        self.values |= values
        shapes = getattr(reader, "one_shapes", None)
        ones = None
        if shapes is not None and any("1" in line for line in lines):
            ones = tuple(shapes(picture, single_line=single))
        self.passes.append((values, ones))


_READS_CACHE: OrderedDict[tuple[int, tuple[float, ...], tuple[int, ...]], list[_InkRead]] = (
    OrderedDict()
)
_READS_CACHE_SIZE: Final = 256


def _drawn_reads(
    page: _Page, spot: tuple[float, float, float, float], readers: Sequence[InkReader]
) -> list[_InkRead]:
    """The four reads (cached per page, spot and engines: a second value at the same spot reads
    the same ink)."""
    key = (id(page), spot, tuple(id(reader) for reader in readers))
    with _CACHE_LOCK:
        cached = _READS_CACHE.get(key)
        if cached is not None and _PAGE_CACHE.get(_page_key(page)) is page:
            _READS_CACHE.move_to_end(key)
            return cached
    reads = _read_ink(page, spot, readers)
    with _CACHE_LOCK:
        _READS_CACHE[key] = reads
        while len(_READS_CACHE) > _READS_CACHE_SIZE:
            _READS_CACHE.popitem(last=False)
    return reads


def _page_key(page: _Page) -> tuple[str, int, int, int]:
    return page.key


def _read_ink(
    page: _Page, spot: tuple[float, float, float, float], readers: Sequence[InkReader]
) -> list[_InkRead]:
    """The four reads: each engine on the vector redrawing and on the vendor-layer picture."""
    paths = _ink_paths(page, spot)
    picture = _picture(page, spot)
    reads: list[_InkRead] = []
    for reader in readers:
        vector = _InkRead(f"vendor ink, {reader.name}", reader.name)
        if paths:
            for fraction, parts in _fractions(paths, reader):
                value = to_inches(fraction)
                vector.lines.append(fraction)
                if value is not None:
                    vector.values.add(value)
                    for part, lines in parts:
                        ones = getattr(reader, "one_shapes", None)
                        shapes = (
                            tuple(ones(part, single_line=True))
                            if ones is not None and any("1" in line for line in lines)
                            else None
                        )
                        vector.passes.append((frozenset({value}), shapes))
            for turn in (0, 90, 270):
                drawn = _draw(paths, turn)
                if drawn is not None:
                    vector.add(reader, drawn, reader(drawn, single_line=False), False)
        reads.append(vector)
        shown = _InkRead(f"vendor layer picture, {reader.name}", reader.name)
        if picture is not None:
            for turn in (0, 90, 270):
                turned = _turned(picture, turn)
                shown.add(reader, turned, reader(turned, single_line=False), False)
        reads.append(shown)
    return reads


def _ones_unsure(votes: Sequence[_InkRead], wanted: Decimal) -> str | None:
    """Why a drawn match with a `1` in it is not sure, or `None`: every glyph an engine read as `1`
    in the pictures that gave the value must be a narrow stroke, and at least one engine must have
    said where its `1` is (Tesseract does; a hand-lettered `7` reads as `1` to both engines)."""
    if "1" not in _digit_string(wanted):
        return None
    shaped = False
    for read in votes:
        for values, ones in read.passes:
            if wanted not in values or ones is None:
                continue
            if any(width >= _ONE_MAX_WIDTH for width in ones):
                return "a glyph read as 1 here is as wide as a 7: it may be a hand-lettered 7"
            shaped = shaped or bool(ones)
    if shaped:
        return None
    return "a drawn 1 may be a hand-lettered 7, and no engine could show its shape"


# ---- the one entry point -------------------------------------------------------------------------


def _spot(box: Mapping[str, Any]) -> tuple[float, float, float, float] | None:
    try:
        x0, y0, x1, y1 = (float(box[key]) for key in ("x0", "y0", "x1", "y1"))
    except (KeyError, TypeError, ValueError):
        return None
    if not (x1 > x0 and y1 > y0) or not all(math.isfinite(v) for v in (x0, y0, x1, y1)):
        return None
    return x0, y0, x1, y1


def _inches_text(value: Decimal) -> str:
    exact = Fraction(value)
    whole = exact.numerator // exact.denominator
    rest = exact - whole
    if not rest:
        return f'{whole}"'
    return (
        f'{whole} {rest.numerator}/{rest.denominator}"'
        if whole
        else f'{rest.numerator}/{rest.denominator}"'
    )


def verify_value(
    pdf: Path,
    page: int,
    box: Mapping[str, Any],
    expected: str,
    *,
    drawn: bool = True,
    readers: Sequence[InkReader] | None = None,
    page_cache: Path | None = None,
) -> Verified:
    """Whether `expected` is printed inside `box` on `page` (from 1) of `pdf`.

    `page_cache` (the set pack's `hires/` folder, #1548): a vendor-layer picture of 300 dpi or
    less is cut from the layer's 300-dpi picture, drawn once, instead of rendered again.

    `box` is `{x0, y0, x1, y1}` in PDF points of the page as shown, origin top-left. `expected` is
    one value (`46"`, `3'-10"`, `1168 [46]`) or a sentence whose first dimension is checked.
    `drawn=False` runs the text route only: a box that also holds drawn ink then answers
    `cannot_read` with `pending` set. `readers` are the OCR engines (default: those installed).
    """
    token = _CACHE_FOLDER.set(page_cache)
    try:
        return _verify(pdf, page, box, expected, drawn=drawn, readers=readers)
    finally:
        _CACHE_FOLDER.reset(token)


def _verify(
    pdf: Path,
    page: int,
    box: Mapping[str, Any],
    expected: str,
    *,
    drawn: bool,
    readers: Sequence[InkReader] | None,
) -> Verified:
    value = claim_value(expected)
    wanted = to_inches(value) if value is not None else None
    if value is None or wanted is None:
        return Verified("cannot_read", "none", None, reason=_NOT_ONE_NUMBER)
    spot = _spot(box)
    if spot is None or isinstance(page, bool) or not isinstance(page, int) or page < 1:
        return Verified("cannot_read", "none", None, reason="the box cannot be placed", value=value)
    try:
        read = _read_page(Path(pdf), page - 1)
    except (OSError, IndexError, pikepdf.PdfError, ValueError) as failed:
        _log.warning("verify_value could not read the page: %s", type(failed).__name__)
        return Verified("cannot_read", "none", None, reason="the page cannot be read", value=value)
    text = _text_read(read, spot)
    text_reads = tuple(f"text: {label}" for label in text.labels)
    if wanted in text.values:
        label = next(
            (lab for lab in text.labels if wanted in _label_values(lab)), _inches_text(wanted)
        )
        return Verified("match", "text", label, text_reads, "printed as text here", value)
    has_ink = bool(_ink_paths(read, spot))
    printed = ", ".join(text.labels) if text.labels else None
    if printed and not has_ink and not text.doubtful:
        return Verified(
            "mismatch", "text", printed, text_reads, f"the drawing prints {printed} here", value
        )
    if not drawn:
        if printed and not has_ink:
            reason = "stacked digits here that code could not put together"
            return Verified("cannot_read", "text", None, text_reads, reason, value)
        if not printed and not has_ink and not _has_picture(read, spot):
            return Verified(
                "cannot_read", "none", None, text_reads, "nothing is printed at this spot", value
            )
        return Verified("cannot_read", "none", None, text_reads, _PENDING, value, pending=True)
    return _drawn(read, spot, wanted, value, text, text_reads, readers)


def _union(chars: Iterable[_Char]) -> tuple[float, float, float, float]:
    boxes = [char.box for char in chars]
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def printed_spots(
    pdf: Path, page: int, box: Mapping[str, Any], expected: str, *, near: float = 18.0
) -> list[tuple[float, float, float, float]]:
    """Where the drawing's text prints `expected` inside `box` or within `near` points of it: each
    label's box (shown points, origin top-left), nearest the box's centre first. The cloud drawing
    snaps its cloud to it (#1537). Text only, no OCR: a value drawn as lines is not found here."""
    value = claim_value(expected)
    wanted = to_inches(value) if value is not None else None
    spot = _spot(box)
    if wanted is None or spot is None or isinstance(page, bool) or page < 1:
        return []
    try:
        read = _read_page(Path(pdf), page - 1)
    except (OSError, IndexError, pikepdf.PdfError, ValueError) as failed:
        _log.warning("printed_spots could not read the page: %s", type(failed).__name__)
        return []
    grown = (spot[0] - near, spot[1] - near, spot[2] + near, spot[3] + near)
    grouped = _labels([char for char in read.chars if _near(char.box, grown)])
    found: list[tuple[float, float, float, float]] = []
    stacked: set[int] = set()
    for upper, lower in _stacked_pairs(grouped):
        if upper in stacked or lower in stacked:
            continue
        stacked |= {upper, lower}
        numerator = "".join(c.text for c in sorted(grouped[upper], key=lambda c: c.along))
        denominator = "".join(c.text for c in sorted(grouped[lower], key=lambda c: c.along))
        if not _fraction_ok(numerator, denominator):
            continue
        parts = [*grouped[upper], *grouped[lower]]
        pair_values = {Decimal(int(numerator)) / Decimal(int(denominator))}
        whole = _whole_before(grouped, grouped[upper], grouped[lower], stacked)
        if whole is not None:
            joined = to_inches(f'{whole} {numerator}/{denominator}"')
            if joined is not None:
                pair_values.add(joined)
            size = max(c.size for c in parts)
            start = min(c.along for c in parts)
            parts += [
                c
                for index, label in enumerate(grouped)
                if index not in stacked
                for c in label
                if c.direction == parts[0].direction and 0 <= start - c.along <= 6 * size
            ]
        if wanted in pair_values:
            found.append(_union(parts))
    for index, label in enumerate(grouped):
        if index in stacked:
            continue
        composed, _unsure = _compose(label)
        values: set[Decimal] = set()
        for text, fraction in composed:
            if fraction:
                one = to_inches(text)
                if one is not None:
                    values.add(one)
            else:
                values |= _values_of_tokens(text.split())
        if wanted in values:
            found.append(_union(label))
    cx, cy = (spot[0] + spot[2]) / 2, (spot[1] + spot[3]) / 2
    return sorted(found, key=lambda b: math.hypot((b[0] + b[2]) / 2 - cx, (b[1] + b[3]) / 2 - cy))


def _label_values(label: str) -> set[Decimal]:
    whole = to_inches(label)
    return {whole} if whole is not None else _values_of_tokens(label.split())


def _has_picture(page: _Page, spot: tuple[float, float, float, float]) -> bool:
    """Whether a pasted picture covers part of the spot (only OCR can read a picture)."""
    return any(_near(picture, spot) for picture in page.pictures)


def _drawn(
    page: _Page,
    spot: tuple[float, float, float, float],
    wanted: Decimal,
    value: str,
    text: _TextRead,
    text_reads: tuple[str, ...],
    readers: Sequence[InkReader] | None,
) -> Verified:
    engines = list(default_readers() if readers is None else readers)
    printed = ", ".join(text.labels) if text.labels else None
    if not engines:
        return Verified(
            "cannot_read", "none", None, text_reads, "no OCR engine is installed here", value
        )
    reads = _drawn_reads(page, spot, engines)
    all_reads = text_reads + tuple(
        f"{read.name}: {' | '.join(line.strip() for line in read.lines)}"
        for read in reads
        if read.lines
    )
    votes = [read for read in reads if wanted in read.values]
    misses = sorted({v for read in reads for v in read.values if near_miss(wanted, v)})
    others = {v for read in reads for v in read.values if v != wanted}
    area = (spot[2] - spot[0]) * (spot[3] - spot[1])
    engines_voting = {read.engine for read in votes}
    if len(votes) >= _VOTES_NEEDED and len(engines_voting) >= 2 and not misses:
        if area > BIG_BOX_PT2 and others:
            reason = (
                "the box is too big and holds several numbers: draw the box round the one "
                "dimension"
            )
            return Verified("cannot_read", "drawn", None, all_reads, reason, value)
        ones = _ones_unsure(votes, wanted)
        if ones is not None:
            return Verified("cannot_read", "drawn", None, all_reads, ones, value)
        engines_agreeing = " and ".join(read.name for read in votes)
        return Verified(
            "match",
            "drawn",
            _inches_text(wanted),
            all_reads,
            f"read from the vendor's drawn ink ({engines_agreeing})",
            value,
        )
    if printed and not votes and not text.doubtful:
        return Verified(
            "mismatch", "text", printed, all_reads, f"the drawing prints {printed} here", value
        )
    if misses:
        near = ", ".join(_inches_text(v) for v in misses[:3])
        reason = f"an OCR engine read a near-miss of it here ({near})"
    elif len(votes) == 1:
        reason = "only one reading of the drawn ink gives it"
    elif votes:
        reason = "only one OCR engine reads it here"
    elif not any(read.lines for read in reads):
        reason = "no number could be read at this spot"
    else:
        reason = "the drawn numbers here could not be read as it by two OCR engines"
    return Verified(
        "cannot_read",
        "drawn" if any(r.lines for r in reads) else "none",
        None,
        all_reads,
        reason,
        value,
    )
