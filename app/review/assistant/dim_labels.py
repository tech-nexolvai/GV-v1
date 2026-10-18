"""Numbered dimension labels: code lists every printed dimension on a page, $0, no AI (#1558).

The agent used to read each number off a picture and type it into a finding, with a box it
guessed. A misread digit became a false Wrong and a guessed box a loose cloud. Most printed
dimensions in GV's drawing sets are real text inside the pasted drawings, so code can list them
exactly, once per set (the set pack's `dims.json`), and give each one an id (`D17`). The agent
then picks a label (Set-of-Mark: the model chooses a numbered mark that code drew, instead of
guessing coordinates) and code supplies the value, the inches and the box (`dim_index.py`).

**What a label holds:** its id (`D1`, `D2`... per page), the value as printed (`text`), its
`inches` (`to_inches`), its `box` (PDF points of the page as shown, origin top-left), whose drawing
it is on (`side`: architect / vendor / brand / unknown, from the page map's drawings), its
`orientation` (h / v / turned), the `route` that read it (`text`, or `drawn` for the vendor's
drawn ink), the millimetres printed with it (`mm`, and whether they agree with the inches), and,
when code finds them, its dimension line (`dim_line`, two points) and the far ends of its two
extension lines (`extension`).

**The text route** reads the drawing's characters exactly as `verify_value` does (the vendor
layer: black or grey characters of the page's own content and its pasted drawings; GV's markup is
never read): rows read the longest way first, stacked fractions composed from the characters'
places, the symbol font decoded. On top of that:

- a millimetre value over its inches in brackets (`457` over `[18]`) is one label, `457 [18]`;
- a bare number (no inch mark, no fraction, no bracket) is a label only when code finds a dimension
  line beside it, and never on a drawing that writes millimetres over inches (there a lone bare
  number is millimetres);
- a scale note (`1/2" = 1'-0"`) is not a dimension.

**The drawn route** (the vendor's numbers drawn as lines, within a time budget,
`drawn_budget_s`; the set pack runs it after every page's text): character-sized ink is grouped
into clusters, each split into labels by its vectors (`ink_labels`, as `verify_value`'s label route
does), each label gathered again until it is whole. A label is read only when it is on the
vendor's drawing, ends in an inch mark, has no other ink in line with it a word space away, and
sits beside a dimension line of its own. Each row is re-typeset on one line and read by two OCR
engines (RapidOCR's recogniser alone, `LineReader`, then Tesseract), and the label is kept only
when exactly one value is read as a whole row by both, no reading of it is a near-miss, a `1` in
it is shaped as a 1, and no reading holds a word, a `+` or a leading zero. A label read this way is
the drawn-ink witness, never more (#756 D2).

**Ids are stable:** text labels are numbered in reading order (top to bottom, left to right), then
drawn labels after them, so a drawn label found or not never moves a text label's id.

Loaded by name (`set_pack`, `dim_index`), like `verify_value`: it parses PDFs and may run OCR.
Verification: `tests/review/assistant/test_dim_labels.py` (synthetic PDFs).
"""

from __future__ import annotations

import io
import logging
import math
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from importlib import import_module
from itertools import pairwise
from pathlib import Path
from time import monotonic
from typing import Any, Final

import cv2
import pdfplumber
from PIL import Image, ImageDraw, ImageFont

from app.review.assistant import ink_labels
from app.review.assistant import verify_value as vv
from app.review.assistant.finding_checks import to_inches, units_check
from app.review.assistant.measure import _pieces
from extraction.stamp_text import path_ink

__all__ = [
    "LABELS_VERSION",
    "LineReader",
    "PageLabels",
    "build_dims",
    "dimension_line",
    "draw_marks",
    "drawn_labels",
    "drawn_readers",
    "page_labels",
]

_log = logging.getLogger(__name__)

#: Changes when what a label holds or how ids are given changes.
LABELS_VERSION: Final = 1
#: The most labels one page lists (a schedule page can print hundreds of numbers).
MAX_LABELS: Final = 400
#: The most phrases one page lists.
MAX_PHRASES: Final = 1500
#: A bare number larger than this is not read as inches (it is a count, a note number, or mm).
_BARE_MAX_IN: Final = Decimal(600)
#: A dimension line may be at most this many times longer than its text.
_LINE_MAX_RATIO: Final = 60.0
#: A drawn cluster: its longer side between these (points), and paths at most this far apart.
_CLUSTER_MIN_PT: Final = 4.0
_CLUSTER_MAX_PT: Final = 60.0
_CLUSTER_GAP_PT: Final = 1.5
_MAX_CLUSTERS: Final = 300
#: A label this far inside the edge of the ink gathered round its cluster is whole: a glyph that
#: joins a label is at most 1.1 glyph sizes (a word space) away, within this for a drawn number.
_WHOLE_INSIDE_PT: Final = 8.0
#: A drawn label with more character-sized paths round it than this is in hatching or a fill.
_MAX_NEAR_PATHS: Final = 150
#: How far a dimension line's length per inch may be from its drawing's median (`scale_ok`).
_SCALE_TOLERANCE: Final = 0.08
_MM_BARE: Final = re.compile(r"^\d{2,5}$")
#: A drawn label read with a leading zero (`03`), or read as a word of letters only (`EQ`).
_LEADING_ZERO: Final = re.compile(r"(?<![\d/.])0\d")
_WORD: Final = re.compile(r"[A-Za-z]{2,}")
_SCALE_WORDS: Final = re.compile(r"\bSCALE\b|=", re.IGNORECASE)

type Box = tuple[float, float, float, float]
type Point = tuple[float, float]
type Segment = tuple[Point, Point]


@dataclass(frozen=True, slots=True)
class _Reading:
    text: str
    inches: Decimal
    box: Box
    along: Point
    """The reading direction on the page as shown (unit vector)."""
    font: float
    bare: bool
    bracket: bool = False
    mm: str | None = None
    units: str | None = None
    route: str = "text"


@dataclass(slots=True)
class PageLabels:
    """One page's labels, and how far the drawn route got."""

    labels: list[dict[str, Any]] = field(default_factory=list)
    drawn: str = "not_run"
    """`not_run`, `done`, `partial` (the time budget ran out) or `no_engine`."""
    seconds: float = 0.0
    scale_notes: list[dict[str, Any]] = field(default_factory=list)
    """The printed scale notes (`1/2" = 1'-0"`): `{text, box}` (`view_scales` sets them beside
    the scale measured from the dimensions)."""
    phrases: list[dict[str, Any]] = field(default_factory=list)
    """Every printed phrase: `{text, box, side}` (`drawing_notes` finds the notes and tags)."""


# ---- reading the page once -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Parsed:
    page: Any
    """`verify_value._Page`: characters, character-sized ink paths, pictures."""
    segments: tuple[Segment, ...]
    """Every straight piece of the page's black or grey line work (shown points)."""


def _parse(pdf: Path, page_index: int) -> _Parsed:
    """The page's vendor layer read once: `verify_value`'s characters and glyph paths, and
    `measure`'s line work (both from the same pass)."""
    stat = pdf.stat()
    key = (str(pdf.resolve()), stat.st_mtime_ns, stat.st_size, page_index)
    vendor = vv._vendor_page(pdf.read_bytes(), page_index)
    chars: list[Any] = []
    paths: list[Any] = []
    segments: list[Segment] = []
    with pdfplumber.open(io.BytesIO(vendor)) as document:
        page = document.pages[0]
        shown = vv._placement(page)
        corner = shown(float(page.width), float(page.height))
        start = shown(0.0, 0.0)
        size = (abs(corner[0] - start[0]), abs(corner[1] - start[1]))
        for char in page.chars:
            made = vv._char(char, shown)
            if made is not None:
                chars.append(made)
        for item in (*page.curves, *page.lines, *page.rects):
            made_path = vv._path(item, shown)
            if made_path is not None:
                paths.append(made_path)
            if not path_ink(dict(item)):
                continue
            for piece in _pieces(item):
                points = [shown(x, y) for x, y in piece]
                segments.extend((a, b) for a, b in pairwise(points) if a != b)
        pictures = tuple(
            vv._shown_box(shown, *(float(image[k]) for k in ("x0", "top", "x1", "bottom")))
            for image in page.images
        )
    parsed = vv._Page(key, vendor, tuple(chars), tuple(paths), pictures, size)
    return _Parsed(parsed, tuple(segments))


# ---- the text route ------------------------------------------------------------------------------


def _union(boxes: Sequence[Box]) -> Box:
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def _direction(chars: Sequence[Any]) -> Point:
    """The reading direction on the page as shown: from the first character's centre to the last
    one's, else from the characters' own direction."""
    ordered = sorted(chars, key=lambda c: c.along)
    first, last = ordered[0].box, ordered[-1].box
    dx = (last[0] + last[2]) / 2 - (first[0] + first[2]) / 2
    dy = (last[1] + last[3]) / 2 - (first[1] + first[3]) / 2
    if len(ordered) > 1 and math.hypot(dx, dy) > 0.3 * max(c.size for c in ordered):
        norm = math.hypot(dx, dy)
        return dx / norm, dy / norm
    box = _union([c.box for c in ordered])
    return (1.0, 0.0) if box[2] - box[0] >= box[3] - box[1] else (0.0, 1.0)


def _reading(text: str, inches: Decimal, chars: Sequence[Any], **more: Any) -> _Reading:
    return _Reading(
        text=" ".join(text.split()),
        inches=inches,
        box=_union([c.box for c in chars]),
        along=_direction(chars),
        font=max(c.size for c in chars),
        **more,
    )


def _tokens(chars: Sequence[Any], font: float) -> list[tuple[str, tuple[Any, ...]]]:
    """A row's characters as words (split where `verify_value._row_text` puts a space)."""
    out: list[tuple[str, list[Any]]] = []
    previous: Any = None
    for char in sorted(chars, key=lambda c: c.along):
        gap = None if previous is None else char.along - (previous.along + previous.length)
        if previous is None or (gap is not None and gap > 0.18 * font):
            out.append((char.text, [char]))
        else:
            out[-1] = (out[-1][0] + char.text, [*out[-1][1], char])
        previous = char
    return [(vv._plain(text).strip(), tuple(group)) for text, group in out if text.strip()]


def _bare(text: str) -> bool:
    return not any(mark in text for mark in ('"', "'", "/", "["))


def _row_readings(chars: Sequence[Any], font: float) -> list[_Reading]:
    """The dimensions in one row: brackets as inches, else runs read the longest way first."""
    tokens = _tokens(chars, font)
    if not tokens or _SCALE_WORDS.search(" ".join(text for text, _ in tokens)):
        return []
    out: list[_Reading] = []
    start = 0
    while start < len(tokens):
        text = tokens[start][0]
        if text.startswith("["):
            end = start
            while end < len(tokens) - 1 and not tokens[end][0].rstrip(".,;:").endswith("]"):
                end += 1
            inside = " ".join(t for t, _ in tokens[start : end + 1]).strip('[]".,;: ')
            value = to_inches(inside.rstrip('"') + '"') if inside else None
            if value is not None and value > 0 and end - start < 4:
                group = [c for _, part in tokens[start : end + 1] for c in part]
                out.append(_reading(f"[{inside}]", value, group, bare=False, bracket=True))
                start = end + 1
                continue
        for size in range(min(vv._LONGEST_RUN, len(tokens) - start), 0, -1):
            run = tokens[start : start + size]
            if not all(any(ch.isdigit() for ch in t) or t in ("'", '"', "-") for t, _ in run):
                continue
            joined = " ".join(t for t, _ in run).rstrip(".,;:")
            value = to_inches(joined)
            if value is not None and value > 0:
                group = [c for _, part in run for c in part]
                out.append(_reading(joined, value, group, bare=_bare(joined)))
                start += size
                break
        else:
            start += 1
    return out


def _whole(
    groups: Sequence[Sequence[Any]], upper: Sequence[Any], lower: Sequence[Any], taken: set[int]
) -> tuple[int, str] | None:
    """The whole number written just before a stacked fraction, and its group (as
    `verify_value._whole_before`, which gives the text only)."""
    size = max(c.size for c in (*upper, *lower))
    start = min(c.along for c in (*upper, *lower))
    low, high = lower[0].across - 0.5 * size, upper[0].across + 0.5 * size
    for index, group in enumerate(groups):
        if index in taken or group[0].direction != upper[0].direction:
            continue
        last = max(group, key=lambda c: c.along)
        gap = start - (last.along + last.length)
        if last.text.isdigit() and -0.2 * size <= gap <= 1.0 * size and low <= last.across <= high:
            row = [c for c in group if abs(c.across - last.across) <= 0.2 * size]
            text = vv._row_text(row, size).strip()
            if vv._WHOLE.fullmatch(text) and len(row) == len(group):
                return index, text
            return None
    return None


def _text_readings(page: Any) -> list[_Reading]:
    """Every dimension the page's text prints (before pairing and filtering)."""
    groups = vv._labels(list(page.chars))
    out: list[_Reading] = []
    taken: set[int] = set()
    for upper, lower in vv._stacked_pairs(groups):
        if upper in taken or lower in taken:
            continue
        taken |= {upper, lower}
        top, bottom = groups[upper], groups[lower]
        numerator = "".join(c.text for c in sorted(top, key=lambda c: c.along))
        denominator = "".join(c.text for c in sorted(bottom, key=lambda c: c.along))
        if not vv._fraction_ok(numerator, denominator):
            continue
        chars = [*top, *bottom]
        text = f'{numerator}/{denominator}"'
        whole = _whole(groups, top, bottom, taken)
        if whole is not None:
            taken.add(whole[0])
            chars += list(groups[whole[0]])
            text = f'{whole[1]} {numerator}/{denominator}"'
        value = to_inches(text)
        if value is not None and value > 0:
            out.append(_reading(text, value, chars, bare=False))
    for index, group in enumerate(groups):
        if index in taken:
            continue
        font = max(c.size for c in group)
        parts, _doubtful = vv.compose_parts(group)
        for text, fraction, part in parts:
            if not part:
                continue
            if fraction:
                value = to_inches(text)
                if value is not None and value > 0:
                    out.append(_reading(text, value, part, bare=False))
            else:
                out.extend(_row_readings(part, font))
    return out


def _centre(box: Box) -> Point:
    return (box[0] + box[2]) / 2, (box[1] + box[3]) / 2


def _paired(readings: list[_Reading]) -> tuple[list[_Reading], bool]:
    """A bare millimetre value right over (or under) its inches in brackets becomes one label
    (`457 [18]`). Also whether the page writes millimetres over inches at all."""
    used: set[int] = set()
    out: list[_Reading] = []
    any_pair = False
    for b_index, bracket in enumerate(readings):
        if not bracket.bracket:
            continue
        best: tuple[float, int] | None = None
        bx, by = _centre(bracket.box)
        ux, uy = bracket.along
        for a_index, bare in enumerate(readings):
            if a_index in used or not bare.bare or not _MM_BARE.match(bare.text):
                continue
            if abs(bare.along[0] * uy - bare.along[1] * ux) > 0.2:
                continue
            ax, ay = _centre(bare.box)
            dx, dy = ax - bx, ay - by
            along = abs(dx * ux + dy * uy)
            across = abs(-dx * uy + dy * ux)
            span = max(
                abs((bare.box[2] - bare.box[0]) * ux) + abs((bare.box[3] - bare.box[1]) * uy),
                abs((bracket.box[2] - bracket.box[0]) * ux)
                + abs((bracket.box[3] - bracket.box[1]) * uy),
            )
            font = max(bare.font, bracket.font)
            if (
                along <= 0.5 * span + 0.5 * font
                and 0.4 * font <= across <= 2.6 * font
                and (best is None or across < best[0])
            ):
                best = (across, a_index)
        if best is None:
            continue
        bare = readings[best[1]]
        used |= {best[1], b_index}
        any_pair = True
        inside = bracket.text.strip("[]")
        joined = f"{bare.text} [{inside}]"
        out.append(
            _Reading(
                text=joined,
                inches=bracket.inches,
                box=_union([bare.box, bracket.box]),
                along=bracket.along,
                font=max(bare.font, bracket.font),
                bare=False,
                bracket=True,
                mm=bare.text,
                units=units_check(joined),
            )
        )
    out += [r for i, r in enumerate(readings) if i not in used]
    return out, any_pair


def _overlap(a: Box, b: Box) -> float:
    """How much of the smaller box the two boxes share (0 to 1)."""
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    if w <= 0 or h <= 0:
        return 0.0
    smaller = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1])) or 1.0
    return w * h / smaller


# ---- dimension lines -----------------------------------------------------------------------------


def _orientation(along: Point) -> str:
    dx, dy = abs(along[0]), abs(along[1])
    if dx >= 2 * dy:
        return "h"
    if dy >= 2 * dx:
        return "v"
    return "turned"


def _flip(point: Point) -> Point:
    return point[1], point[0]


def _boundaries(
    y: float, lo: float, hi: float, segments: Sequence[Segment], font: float
) -> list[float]:
    """Where the dimension line at height `y` is cut: extension lines that reach it, and the
    ticks or arrows that cross it (a chain's segments share one line)."""
    out = [lo, hi]
    for a, b in segments:
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dy)
        mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
        if not lo - 1.5 <= mx <= hi + 1.5 or length < 0.8:
            continue
        if abs(dx) <= 0.05 * length:
            if min(a[1], b[1]) - 1.5 <= y <= max(a[1], b[1]) + 1.5:
                out.append(mx)
        elif length <= 3 * font and 0.4 <= abs(dy / dx) <= 2.5 and abs(my - y) <= 1.5:
            out.append(mx)
    return sorted(out)


def _extension(x: float, y: float, segments: Sequence[Segment], font: float) -> list[Point] | None:
    """The extension line at `x` that reaches the dimension line at `y` (dashes followed): its far
    end first, then its end at the dimension line."""
    pieces: list[tuple[float, float]] = []
    for a, b in segments:
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dy)
        if length < 0.8 or abs(dx) > 0.05 * length or abs((a[0] + b[0]) / 2 - x) > 0.8:
            continue
        pieces.append((min(a[1], b[1]), max(a[1], b[1])))
    gap = max(3.5, 0.5 * font)
    touching = [p for p in pieces if p[0] - 1.5 <= y <= p[1] + 1.5]
    if not touching:
        return None
    top, bottom = min(p[0] for p in touching), max(p[1] for p in touching)
    grew = True
    while grew:
        grew = False
        for p0, p1 in pieces:
            if p0 < top and p1 >= top - gap:
                top, grew = p0, True
            if p1 > bottom and p0 <= bottom + gap:
                bottom, grew = p1, True
    if bottom - top < 1.5:
        return None
    far = top if abs(top - y) > abs(bottom - y) else bottom
    return [(x, far), (x, y)]


def dimension_line(
    box: Box, orientation: str, segments: Sequence[Segment], font: float
) -> dict[str, Any] | None:
    """The dimension line a label sits on (or in a gap of), cut at the extension lines or ticks
    on each side of the label, and its two extension lines when code finds them: `{dim_line:
    [p, q], extension: [[far, near] | None, [far, near] | None], span_pt}`. Horizontal and
    vertical labels only; `None` when no line with at least one extension line or tick is found."""
    if orientation not in ("h", "v"):
        return None
    flip = orientation == "v"
    if flip:
        box = (box[1], box[0], box[3], box[2])
        segments = [(_flip(a), _flip(b)) for a, b in segments]
    x0, y0, x1, y1 = box
    width = x1 - x0
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    rows: dict[float, list[tuple[float, float]]] = {}
    for a, b in segments:
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dy)
        if length < 2.0 or abs(dy) > 0.02 * length:
            continue
        y = (a[1] + b[1]) / 2
        if not y0 - 0.7 * font <= y <= y1 + 0.7 * font:
            continue
        rows.setdefault(round(y * 2) / 2, []).append((min(a[0], b[0]), max(a[0], b[0])))
    candidates: list[tuple[float, float, float, float]] = []
    for y, pieces in rows.items():
        merged: list[list[float]] = []
        for lo, hi in sorted(pieces):
            if merged and lo - merged[-1][1] <= max(2.0, width + font):
                merged[-1][1] = max(merged[-1][1], hi)
            else:
                merged.append([lo, hi])
        for lo, hi in merged:
            if hi < cx - 0.25 * width or lo > cx + 0.25 * width:
                continue
            if hi - lo < 0.9 * width or hi - lo > _LINE_MAX_RATIO * max(width, font):
                continue
            candidates.append((abs(y - cy), y, lo, hi))
    for _score, y, lo, hi in sorted(candidates):
        cuts = _boundaries(y, lo, hi, segments, font)
        left = max((c for c in cuts if c <= cx), default=None)
        right = min((c for c in cuts if c >= cx), default=None)
        if left is None or right is None or right - left < 0.8 * width:
            continue
        ends = [_extension(x, y, segments, font) for x in (left, right)]
        inner = len(cuts) > 2 and (left != lo or right != hi)
        if not any(ends) and not inner:
            continue
        line: list[Point] = [(left, y), (right, y)]
        if flip:
            line = [_flip(p) for p in line]
            ends = [None if e is None else [_flip(p) for p in e] for e in ends]
        return {
            "dim_line": [[round(p[0], 2), round(p[1], 2)] for p in line],
            "extension": [
                None if e is None else [[round(p[0], 2), round(p[1], 2)] for p in e] for e in ends
            ],
            "span_pt": round(right - left, 2),
        }
    return None


# ---- the drawn route -----------------------------------------------------------------------------


def _clusters(paths: Sequence[Any], skip: Sequence[Box]) -> list[Box]:
    """Character-sized ink grouped into clusters (a drawn number), outside the text labels."""
    boxes = [p.box for p in paths if not any(_overlap(p.box, s) > 0.5 for s in skip)]
    parent = list(range(len(boxes)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    order = sorted(range(len(boxes)), key=lambda i: boxes[i][0])
    for n, i in enumerate(order):
        a = boxes[i]
        for j in order[n + 1 :]:
            b = boxes[j]
            if b[0] > a[2] + _CLUSTER_GAP_PT:
                break
            if b[1] <= a[3] + _CLUSTER_GAP_PT and a[1] <= b[3] + _CLUSTER_GAP_PT:
                parent[root(i)] = root(j)
    groups: dict[int, list[Box]] = {}
    for i, box in enumerate(boxes):
        groups.setdefault(root(i), []).append(box)
    out: list[Box] = []
    for members in groups.values():
        if len(members) < 2:
            continue
        box = _union(members)
        longer, shorter = max(box[2] - box[0], box[3] - box[1]), min(
            box[2] - box[0], box[3] - box[1]
        )
        if _CLUSTER_MIN_PT <= longer <= _CLUSTER_MAX_PT and 2.0 <= shorter <= 12.0:
            out.append(box)
    return sorted(out, key=lambda b: (round(b[1] / 4), b[0]))[:_MAX_CLUSTERS]


class LineReader:
    """RapidOCR reading one re-typeset row as one line: its recogniser only, no text detection.

    `ink_labels` has already cut the row out and set it on one line, so the detector (most of
    RapidOCR's time: about a second a picture, against a twentieth for the recogniser) has
    nothing left to find. Used by the drawn route only; `verify_value` keeps its own engines."""

    name = "RapidOCR"
    reads_inch_marks = False
    """As `verify_value.RapidReader`: it often reads an inch mark as `99`."""

    def __init__(self) -> None:
        engine = import_module("rapidocr_onnxruntime")
        self._engine = engine.RapidOCR()
        self._lock = threading.Lock()

    def __call__(self, picture: Any, *, single_line: bool) -> list[str]:
        rgb = cv2.cvtColor(picture, cv2.COLOR_GRAY2RGB)
        with self._lock:
            result, _elapsed = self._engine(rgb, use_det=not single_line, use_cls=False)
        lines: list[str] = []
        for item in result or ():
            text = str(item[1] if len(item) == 3 else item[0])
            if text.strip():
                lines.append(text)
        return lines


_DRAWN_READERS: list[Any] | None = None
_DRAWN_READERS_LOCK: Final = threading.Lock()


def drawn_readers() -> list[Any]:
    """The drawn route's engines, loaded once: the one-line RapidOCR first (fast: a label it
    cannot read as a dimension is not shown to Tesseract), then Tesseract."""
    global _DRAWN_READERS
    with _DRAWN_READERS_LOCK:
        if _DRAWN_READERS is None:
            found: list[Any] = []
            try:
                found.append(LineReader())
            except Exception as missing:  # noqa: BLE001 - an engine not installed
                _log.warning("dimension labels: RapidOCR is not available: %s", missing)
            found += [reader for reader in vv.default_readers() if reader.name != "RapidOCR"]
            _DRAWN_READERS = found
        return list(_DRAWN_READERS)


def _label_reads(label: ink_labels.Label, engines: Sequence[Any]) -> list[Any]:
    """One drawn label read by each engine the way `verify_value`'s label route reads it: each
    row re-typeset on one line (`ink_labels`), at two sizes, in each plausible reading direction.
    An engine after the first runs only when an earlier one read a whole dimension: a label that
    no engine reads as one can never have two engines agree."""
    reads: list[Any] = []
    for reader in engines:
        if reads and not any(read.whole for read in reads):
            break
        read = vv._InkRead(f"drawn label, {reader.name}", reader.name, 1)
        for turn in ink_labels.turns(label):
            for row in ink_labels.rows(label, turn):
                layout = _layout(row)
                marked = bool(ink_labels.inch_mark_at_end(row, turn))
                # An engine that reads `"` as `99` reads the row without its inch mark.
                for mark in (not marked or getattr(reader, "reads_inch_marks", True),):
                    for height in ink_labels.HEIGHTS_PX:
                        picture = ink_labels.row_picture(row, turn, height, inch_mark=mark)
                        if picture is None:
                            continue
                        line = " ".join(reader(picture, single_line=True)).strip()
                        read.add(reader, picture, [line] if line else [], True)
                        # The digits laid out as the vectors draw them first: an engine that
                        # loses a space (`11/2` for a 1 and a stacked 1 over 2) still reads the
                        # value. When the digits do not match the drawing (a digit drawn in two
                        # strokes counts twice), the line as read.
                        whole = None if layout is None else _laid_out(line, layout)
                        if whole is None:
                            whole = vv._row_value(line, inch_mark=mark)
                        if whole is not None:
                            read.whole.add(whole)
                            # This picture gave it (the `1` shape check reads it per picture).
                            values, ones = read.passes[-1]
                            read.passes[-1] = (values | {whole}, ones)
                            read.values.add(whole)
        reads.append(read)
    return reads


type _Layout = tuple[int, int, int]


def _layout(row: Sequence[ink_labels.Token]) -> _Layout | None:
    """How many glyphs the row's vectors draw for its whole number, its stacked fraction's
    numerator and its denominator (`(2, 1, 1)` is `17 3/8"`), or `None` when the row is not one
    number with at most one stacked fraction at its end. A digit drawn in two strokes that do not
    touch counts twice, so a layout only ever confirms digits, never adds one."""
    whole = numerator = denominator = 0
    for token in row:
        if token.kind == "mark":
            continue
        if numerator:  # a character after the fraction: not one plain dimension
            return None
        if token.kind == "fraction":
            numerator, denominator = len(token.glyphs), len(token.below)
        else:
            whole += 1
    if not whole and not numerator:
        return None
    return whole, numerator, denominator


def _laid_out(line: str, layout: _Layout) -> Decimal | None:
    """The value an OCR line gives when its digits are laid out as the vectors draw them: the
    engine need only read the digits right, never the spaces or the slash (`11/2` drawn as a 1
    and a stacked 1 over 2 is 1 1/2"). Any other count of digits, a numerator not below its
    denominator or a denominator a drawing never uses: `None`."""
    whole, numerator, denominator = layout
    digits = "".join(ch for ch in vv._plain(line) if ch.isdigit())
    if len(digits) != whole + numerator + denominator or not digits:
        return None
    value = Decimal(int(digits[:whole])) if whole else Decimal(0)
    if numerator:
        top, bottom = int(digits[whole : whole + numerator]), int(digits[whole + numerator :])
        if bottom not in vv._DENOMINATORS or not 0 < top < bottom:
            return None
        value += Decimal(top) / Decimal(bottom)
    return value


def _inch_marked(label: ink_labels.Label) -> bool:
    """Whether the label's own strokes end a row in an inch mark (two ticks), in some reading
    direction: how a drawn dimension is written. A bare drawn number (a tag, a count, a symbol
    an engine reads as a digit) is never a label: its vectors, not OCR, decide."""
    return any(
        ink_labels.inch_mark_at_end(row, turn)
        for turn in ink_labels.turns(label)
        for row in ink_labels.rows(label, turn)
    )


def _line_work(path: Any, turn: ink_labels.Turn) -> bool:
    """Whether a path is a dimension's own line work, never a character beside the label: a
    filled arrowhead or dot, or one straight stroke across the reading direction or slanted (an
    extension line, a tick). A straight stroke along it is a dash (`4'-6"`) and still counts."""
    glyph = ink_labels.Glyph((path,), path.box)
    if ink_labels._arrowhead(glyph):
        return True
    ends = ink_labels._ends(glyph)
    if ends is None:
        return False
    (x0, y0), (x1, y1) = (float(v) for v in ends[0]), (float(v) for v in ends[1])
    length = math.hypot(x1 - x0, y1 - y0) or 1.0
    along = abs(x1 - x0) / length if turn == 0 else abs(y1 - y0) / length
    return bool(along < 0.9)


def _crowded(page: Any, label: ink_labels.Label) -> bool:
    """Whether other character-sized ink sits in line with the label, closer than 1.2 times its
    height (a word space), in a direction it may read: a word or a number the label belongs to
    (`WD-` before `02`, `SCALE :` before `1"`, `2'-` before `10"`). Such a label is not read: what
    it is part of decides its value. The dimension's own ticks, arrowheads and extension lines do
    not count (`_line_work`), nor the next dimension of a chain (a label with its own inch mark).
    """
    own = {id(path) for glyph in label.glyphs for path in glyph.paths}
    near = [path for path in vv._ink_paths_near(page, label.box) if id(path) not in own]
    # The next dimension of a chain (its own number and inch mark) is no word round this one.
    x0, y0, x1, y1 = label.box
    reach = 1.2 * max(x1 - x0, y1 - y0)
    around = (x0 - reach, y0 - reach, x1 + reach, y1 + reach)
    dimensions = {
        id(path)
        for other in ink_labels.labels(vv._ink_paths_near(page, around), around)
        if _inch_marked(other)
        for glyph in other.glyphs
        for path in glyph.paths
    }
    near = [path for path in near if id(path) not in dimensions]
    for turn in ink_labels.turns(label):
        frame = ink_labels._frame(label.box, turn)
        tall = frame[3] - frame[1]
        for path in near:
            other = ink_labels._frame(path.box, turn)
            if other[3] - other[1] < 0.15 * tall:  # a piece of the dimension line it sits on
                continue
            if _line_work(path, turn):
                continue
            across = min(frame[3], other[3]) - max(frame[1], other[1])
            gap = max(other[0] - frame[2], frame[0] - other[2])
            if across >= 0.5 * min(tall, other[3] - other[1]) and gap < 1.2 * tall:
                return True
    return False


def _whole_label(
    page: Any, label: ink_labels.Label, gathered: Box | None = None
) -> ink_labels.Label | None:
    """The label gathered again round its own box until it stops growing, or `None`. A cluster
    gathers the ink a few points round it, so a label at the edge of what it gathered may have
    lost a digit (`51"` cut to `1"`): only a label that gathers to itself is read. A label that
    grows past a written number's size (hatching, a dotted fill) is none. A label well inside
    what was gathered round `gathered` (further than a word space from its edge) is whole."""
    if gathered is not None:
        reach = ink_labels.GATHER_PT - _WHOLE_INSIDE_PT
        x0, y0, x1, y1 = label.box
        if (
            x0 >= gathered[0] - reach
            and y0 >= gathered[1] - reach
            and x1 <= gathered[2] + reach
            and y1 <= gathered[3] + reach
        ):
            return label
    for _ in range(4):
        box = label.box
        if max(box[2] - box[0], box[3] - box[1]) > _CLUSTER_MAX_PT:
            return None
        near = vv._ink_paths_near(page, box)
        if len(near) > _MAX_NEAR_PATHS:
            return None
        again = [other for other in ink_labels.labels(near, box) if _overlap(other.box, box) > 0.5]
        if not again:
            return None
        grown = max(again, key=lambda other: _overlap(other.box, label.box))
        if grown.box == label.box:
            return label
        label = grown
    return None


def _drawn_value(reads: Sequence[Any]) -> Decimal | None:
    """The one value a drawn label reads as, or `None`: `verify_value`'s label rule with no value
    to expect. Exactly one value read as a whole row by two engines, no reading of the label a
    near-miss of it (one digit off, or inside a longer number), and a `1` in it shaped as a 1. A
    zero or a number too big for inches is no dimension, and neither is a sum written out
    (`2"+3" FILLER`: the line it sits on spans both)."""
    lines = [line for read in reads for line in read.lines]
    if any("+" in line or _LEADING_ZERO.search(line) or _WORD.search(line) for line in lines):
        # A sum written out; a number with a leading zero, which no dimension has (`EQ` read
        # upside down is `03`); or a word read in it (a tag `LA-002`, a note `(2) ADJ.
        # SHELVES`, or a dimension with its note, left to the AI): no label then.
        return None
    engines: dict[Decimal, set[str]] = {}
    for read in reads:
        for value in read.whole:
            engines.setdefault(value, set()).add(read.engine)
    agreed = [value for value, names in engines.items() if len(names) >= 2]
    if len(agreed) != 1:
        return None
    value = agreed[0]
    if not 0 < value <= _BARE_MAX_IN:
        return None
    # A line whose digits are exactly the value's (`11/2` for 1 1/2") is the same reading with
    # its spaces lost, not a near-miss: only the other lines' values are checked.
    digits = vv._digit_string(value)
    seen = {
        v
        for read in reads
        for line in read.lines
        if "".join(ch for ch in vv._plain(line) if ch.isdigit()) != digits
        for v in vv._ocr_values([line])
    } | set(engines)
    if any(vv.near_miss(value, other) for other in seen):
        return None
    if vv._ones_unsure(list(reads), value) is not None:
        return None
    return value


# ---- one page ------------------------------------------------------------------------------------


_SIDES: Final = {"architect": "architect", "vendor": "vendor", "brand_prototype": "brand"}


def _side(box: Box, halves: Sequence[Mapping[str, Any]]) -> str:
    cx, cy = _centre(box)
    best: tuple[float, str] | None = None
    for half in halves:
        hb = half.get("box")
        if not isinstance(hb, Sequence) or len(hb) != 4:
            continue
        x0, y0, x1, y1 = (float(v) for v in hb)
        distance = math.hypot(max(x0 - cx, 0, cx - x1), max(y0 - cy, 0, cy - y1))
        role = _SIDES.get(str(half.get("role")), "unknown")
        if best is None or distance < best[0]:
            best = (distance, role)
    if best is None or best[0] > 36:
        return "unknown"
    return best[1]


def _label(reading: _Reading, line: dict[str, Any] | None, side: str) -> dict[str, Any]:
    box = reading.box
    return {
        "id": "",
        "text": reading.text,
        "inches": format(reading.inches.normalize(), "f"),
        "box": [round(v, 2) for v in box],
        "side": side,
        "orientation": _orientation(reading.along),
        "route": reading.route,
        "mm": reading.mm,
        "units": reading.units,
        "dim_line": None if line is None else line["dim_line"],
        "extension": None if line is None else line["extension"],
        "span_pt": None if line is None else line["span_pt"],
    }


def _scale_checks(labels: Sequence[dict[str, Any]]) -> None:
    """`scale_ok` on each label with a dimension line: its drawn length per inch within
    `_SCALE_TOLERANCE` of the median of its drawing's labels (at least three), else `False`;
    `None` when there are too few to tell. A line off the drawing's scale may be the wrong line,
    or a dimension not drawn to scale."""
    by_side: dict[str, list[float]] = {}
    for label in labels:
        inches = float(label["inches"])
        if label.get("span_pt") and inches >= 2:
            by_side.setdefault(label["side"], []).append(label["span_pt"] / inches)
    for label in labels:
        label["scale_ok"] = None
        ratios = sorted(by_side.get(label["side"], []))
        inches = float(label["inches"])
        if len(ratios) < 3 or not label.get("span_pt") or inches < 2:
            continue
        median = ratios[len(ratios) // 2]
        label["scale_ok"] = abs(label["span_pt"] / inches / median - 1) <= _SCALE_TOLERANCE


def page_labels(
    pdf: Path,
    page: int,
    halves: Sequence[Mapping[str, Any]] = (),
    *,
    drawn_budget_s: float = 0.0,
    readers: Sequence[Any] | None = None,
    clock: Callable[[], float] = monotonic,
) -> PageLabels:
    """The labels of page `page` (from 1) of `pdf`. `halves` are the page map's drawings on the
    page (`{box, role}`), for each label's side. The drawn route runs only while
    `drawn_budget_s` seconds last (0: not at all); `readers` are its OCR engines (default: the
    installed ones)."""
    started = clock()
    parsed = _parse(Path(pdf), page - 1)
    phrases = _phrases(parsed)
    result = PageLabels(
        labels=_text_labels(parsed, halves),
        scale_notes=_scale_notes(phrases),
        phrases=[
            {"text": text, "box": [round(v, 2) for v in box], "side": _side(box, halves)}
            for text, box in phrases[:MAX_PHRASES]
        ],
    )
    if drawn_budget_s > 0:
        _add_drawn(
            result,
            parsed,
            halves,
            budget_s=drawn_budget_s - (clock() - started),
            readers=readers,
            clock=clock,
        )
    _number(result.labels)
    result.seconds = round(clock() - started, 2)
    return result


def drawn_labels(
    pdf: Path,
    page: int,
    halves: Sequence[Mapping[str, Any]],
    text_labels: Sequence[Mapping[str, Any]],
    *,
    budget_s: float,
    readers: Sequence[Any] | None = None,
    clock: Callable[[], float] = monotonic,
) -> PageLabels:
    """Page `page`'s text labels (as `page_labels` gave them) with its drawn labels added after
    them, read within `budget_s` seconds: the pack's second pass, after every page's text."""
    started = clock()
    result = PageLabels(labels=[dict(label) for label in text_labels if label["route"] == "text"])
    _add_drawn(result, _parse(Path(pdf), page - 1), halves, budget_s, readers, clock)
    _number(result.labels)
    result.seconds = round(clock() - started, 2)
    return result


def _number(labels: list[dict[str, Any]]) -> None:
    _scale_checks(labels)
    for number, label in enumerate(labels, start=1):
        label["id"] = f"D{number}"


_SCALE_NOTE: Final = re.compile(
    r"(\d+(?:\s+\d+/\d+)?|\d+/\d+)\s*\"\s*=\s*(\d+\s*'\s*-?\s*\d+(?:\s+\d+/\d+)?\s*\"?)"
)


def _phrases(parsed: _Parsed) -> list[tuple[str, Box]]:
    """Every phrase the page's text prints (the vendor layer: its own content and its pasted
    drawings, black or grey; GV's markup is never read), with its box: characters grouped into
    labels (`verify_value._labels`), each split into rows by baseline and letter size (a title
    printed over a note is not one text) and each row into phrases where a gap is wider than a
    letter (`ID5.1` is not part of `1/4" = 1'-0"`)."""
    out: list[tuple[str, Box]] = []
    for group in vv._labels(list(parsed.page.chars)):
        rows: list[list[Any]] = []
        for char in sorted(group, key=lambda c: (c.size, c.across)):
            row = next(
                (
                    r
                    for r in rows
                    if abs(r[0].size - char.size) <= 0.1 * char.size
                    and abs(r[0].across - char.across) <= 0.5 * char.size
                ),
                None,
            )
            if row is None:
                rows.append([char])
            else:
                row.append(char)
        for row in rows:
            phrases: list[list[Any]] = []
            for char in sorted(row, key=lambda c: c.along):
                last = phrases[-1][-1] if phrases else None
                if last is not None and char.along - (last.along + last.length) <= char.size:
                    phrases[-1].append(char)
                else:
                    phrases.append([char])
            for phrase in phrases:
                text = " ".join(
                    vv._plain(vv._row_text(phrase, max(c.size for c in phrase))).split()
                )
                if text:
                    out.append((text, _union([c.box for c in phrase])))
    out.sort(key=lambda item: (round(item[1][1] / 4), item[1][0]))
    return out


def _scale_notes(phrases: Sequence[tuple[str, Box]]) -> list[dict[str, Any]]:
    """The scale notes among the page's phrases (`1/2" = 1'-0"`), each with its box."""
    out: list[dict[str, Any]] = []
    for text, box in phrases:
        found = _SCALE_NOTE.search(text)
        if found is not None:
            note = f'{found.group(1)}" = {found.group(2).rstrip(chr(34)).strip()}"'
            out.append({"text": note, "box": [round(v, 2) for v in box]})
    return out


def _text_labels(parsed: _Parsed, halves: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    readings, millimetres = _paired(_text_readings(parsed.page))
    kept: list[tuple[_Reading, dict[str, Any] | None]] = []
    for reading in sorted(readings, key=lambda r: (not r.bracket, r.bare, -len(r.text))):
        if any(
            _overlap(reading.box, other.box) > 0.6 and other.inches == reading.inches
            for other, _ in kept
        ):
            continue
        orientation = _orientation(reading.along)
        line = dimension_line(reading.box, orientation, parsed.segments, reading.font)
        if reading.bare and (line is None or millimetres or reading.inches > _BARE_MAX_IN):
            continue
        kept.append((reading, line))
    kept.sort(key=lambda item: (round(item[0].box[1] / 4), item[0].box[0]))
    return [_label(r, line, _side(r.box, halves)) for r, line in kept[:MAX_LABELS]]


def _add_drawn(
    result: PageLabels,
    parsed: _Parsed,
    halves: Sequence[Mapping[str, Any]],
    budget_s: float,
    readers: Sequence[Any] | None,
    clock: Callable[[], float],
) -> None:
    """The drawn route: character-sized ink outside the text labels, grouped into clusters, each
    split into labels by its vectors (`ink_labels`); a label beside a dimension line of its own is
    read (`_label_reads`) and kept when `_drawn_value` gives its one value. Added after the text
    labels (sorted among themselves), so a text label's id never moves."""
    started = clock()
    if not parsed.page.paths:
        result.drawn = "done"
        return
    engines = list(drawn_readers() if readers is None else readers)
    if len(engines) < 2:
        result.drawn = "no_engine"
        return
    result.drawn = "done"
    labels = result.labels
    skip = [tuple(label["box"]) for label in labels]
    seen: list[Box] = []
    drawn: list[dict[str, Any]] = []
    for cluster in _clusters(parsed.page.paths, skip):
        near = vv._ink_paths_near(parsed.page, cluster)
        if len(near) > _MAX_NEAR_PATHS:  # hatching or a dotted fill, not a written number
            continue
        for piece in ink_labels.labels(near, cluster):
            if len(labels) + len(drawn) >= MAX_LABELS:
                break
            if clock() - started > budget_s:
                result.drawn = "partial"
                break
            label = _whole_label(parsed.page, piece, cluster)
            if label is None:
                continue
            box = label.box
            # Clusters gather their neighbours' ink too: a label is read once.
            if any(_overlap(box, other) > 0.5 for other in seen):
                continue
            seen.append(box)
            side = _side(box, halves)
            if side != "vendor" or not _inch_marked(label) or _crowded(parsed.page, label):
                continue
            width, height = box[2] - box[0], box[3] - box[1]
            found = None
            for orientation, font in (("h", height), ("v", width)):
                found = dimension_line(box, orientation, parsed.segments, font)
                if found is not None:
                    break
            if found is None:
                continue
            try:
                value = _drawn_value(_label_reads(label, engines))
            except Exception as failed:  # noqa: BLE001 - an unread label is left out
                _log.warning("dimension labels: drawn read failed: %s", type(failed).__name__)
                continue
            if value is None:
                continue
            reading = _Reading(
                text=vv._inches_text(value),
                inches=value,
                box=box,
                along=(1.0, 0.0) if orientation == "h" else (0.0, 1.0),
                font=min(width, height),
                bare=False,
                route="drawn",
            )
            drawn.append(_label(reading, found, side))
        if result.drawn == "partial" or len(labels) + len(drawn) >= MAX_LABELS:
            break
    drawn.sort(key=lambda label: (round(label["box"][1] / 4), label["box"][0]))
    labels += drawn


def build_dims(
    pdfs: Mapping[str, Path],
    page_map: Mapping[str, Any] | None,
    *,
    drawn_budget_s: float = 0.0,
    on_page: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """The set pack's `dims.json`: every page of every upload and its labels. Two passes: the text
    route on every page first (the agent has every printed label in minutes), then the drawn
    route page by page, sharing `drawn_budget_s` seconds across the set (a page it does not reach
    stays `not_run`; one it leaves half read is `partial`). `on_page` sees the document after each
    page of each pass (to write it as it grows)."""
    halves: dict[tuple[str, int], list[Mapping[str, Any]]] = {}
    entries = page_map.get("pages") if isinstance(page_map, Mapping) else None
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, Mapping):
            key = (str(entry.get("file")), int(entry.get("page") or 0))
            halves[key] = [h for h in entry.get("halves") or [] if isinstance(h, Mapping)]
    document: dict[str, Any] = {"version": LABELS_VERSION, "files": list(pdfs), "pages": []}
    for name, pdf in pdfs.items():
        count = 0
        if pdf.is_file():
            with pdfplumber.open(str(pdf)) as opened:
                count = len(opened.pages)
        for number in range(1, count + 1):
            try:
                found = page_labels(pdf, number, halves.get((name, number), ()))
            except Exception as failed:  # noqa: BLE001 - a page code cannot read has no labels
                _log.warning("dimension labels: page failed: %s", type(failed).__name__)
                found = PageLabels(drawn="failed")
            document["pages"].append(
                {
                    "file": name,
                    "page": number,
                    "drawn": found.drawn,
                    "labels": found.labels,
                    "scale_notes": found.scale_notes,
                    "phrases": found.phrases,
                }
            )
            if on_page is not None:
                on_page(document)
    left = drawn_budget_s
    for entry in document["pages"]:
        if left <= 0:
            break
        if entry["drawn"] != "not_run":
            continue
        name, number = entry["file"], entry["page"]
        started = monotonic()
        try:
            found = drawn_labels(
                pdfs[name],
                number,
                halves.get((name, number), ()),
                entry["labels"],
                budget_s=left,
            )
        except Exception as failed:  # noqa: BLE001 - the page keeps its text labels
            _log.warning("dimension labels: drawn pass failed: %s", type(failed).__name__)
            entry["drawn"] = "failed"
            continue
        finally:
            left -= monotonic() - started
        entry["drawn"], entry["labels"] = found.drawn, found.labels
        if on_page is not None:
            on_page(document)
    return document


# ---- marks on a zoom crop ------------------------------------------------------------------------

_MARK_COLOUR: Final = (0, 92, 230)
_GUIDE_COLOUR: Final = (214, 0, 160)


def _font(size: int) -> Any:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # an older Pillow without sizes
        return ImageFont.load_default()


def draw_marks(
    png: bytes,
    crop_box: Sequence[float],
    labels: Sequence[Mapping[str, Any]],
    guides: Sequence[Mapping[str, Any]] = (),
) -> bytes:
    """A copy of a zoom crop with each label's box outlined and its id in a small tag beside it
    (Set-of-Mark), and for each label in `guides` its dimension line and its two extension lines
    drawn right across the crop, so what the dimension spans is plain to see."""
    with Image.open(io.BytesIO(png)) as opened:
        picture = opened.convert("RGB")
    x0, y0, x1, y1 = (float(v) for v in crop_box)
    sx, sy = picture.width / max(x1 - x0, 1e-6), picture.height / max(y1 - y0, 1e-6)

    def px(point: Sequence[float]) -> tuple[float, float]:
        return (float(point[0]) - x0) * sx, (float(point[1]) - y0) * sy

    draw = ImageDraw.Draw(picture)
    width = max(1, round(picture.width / 900))
    for label in guides:
        ends = [e for e in label.get("extension") or [] if e]
        line = label.get("dim_line")
        if line:
            draw.line([px(line[0]), px(line[1])], fill=_GUIDE_COLOUR, width=2 * width)
        lines = [[px(e[0]), px(e[1])] for e in ends]
        if not lines and line:
            # No extension line found: across the dimension line at its two ends.
            (ax, ay), (bx, by) = px(line[0]), px(line[1])
            nx, ny = -(by - ay), bx - ax
            lines = [[(ax, ay), (ax + nx, ay + ny)], [(bx, by), (bx + nx, by + ny)]]
        for (ax, ay), (bx, by) in lines:
            dx, dy = bx - ax, by - ay
            norm = math.hypot(dx, dy) or 1.0
            reach = picture.width + picture.height
            draw.line(
                [
                    (ax - dx / norm * reach, ay - dy / norm * reach),
                    (ax + dx / norm * reach, ay + dy / norm * reach),
                ],
                fill=_GUIDE_COLOUR,
                width=width,
            )
    size = max(11, min(22, round(picture.height / 45)))
    font = _font(size)
    taken: list[tuple[float, float, float, float]] = []
    for label in labels:
        bx0, by0 = px(label["box"][:2])
        bx1, by1 = px(label["box"][2:])
        draw.rectangle((bx0 - 1, by0 - 1, bx1 + 1, by1 + 1), outline=_MARK_COLOUR, width=width)
        text = str(label["id"])
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
        tw, th = right - left + 6, bottom - top + 4
        spots = [
            (bx0, by0 - th - 1),
            (bx1 - tw, by0 - th - 1),
            (bx0, by1 + 1),
            (bx0 - tw - 1, by0),
            (bx1 + 1, by0),
        ]
        chosen = spots[0]
        for sx0, sy0 in spots:
            tag = (sx0, sy0, sx0 + tw, sy0 + th)
            if not any(
                tag[0] < t[2] and t[0] < tag[2] and tag[1] < t[3] and t[1] < tag[3] for t in taken
            ):
                chosen = (sx0, sy0)
                break
        tag = (chosen[0], chosen[1], chosen[0] + tw, chosen[1] + th)
        taken.append(tag)
        draw.rectangle(tag, fill=_MARK_COLOUR)
        draw.text((tag[0] + 3 - left, tag[1] + 2 - top), text, fill=(255, 255, 255), font=font)
    buffer = io.BytesIO()
    picture.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def main(arguments: Sequence[str]) -> int:
    """`python -m app.review.assistant.dim_labels <pack folder> <uploads folder> <name>...`: the
    set pack's `dims.json` (`set_pack.write_dims`), in a process of its own at low priority."""
    import json
    import os

    from app.review.assistant import set_pack

    if len(arguments) < 3:
        print("usage: dim_labels <pack folder> <uploads folder> <name>...")
        return 2
    try:
        os.nice(10)
    except OSError:  # a platform without it: the labels still come, at normal priority
        pass
    folder, uploads, names = Path(arguments[0]), Path(arguments[1]), list(arguments[2:])
    try:
        document = json.loads((folder / "page_map.json").read_text(encoding="utf-8"))
        pages = document.get("pages") or []
    except (OSError, ValueError):
        pages = []
    set_pack.write_dims(folder, uploads, names, pages, parent=os.getppid())
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
