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

**The drawn route** (only within a time budget, `drawn_budget_s`): numbers drawn as lines are
grouped into clusters of character-sized ink; a cluster with a dimension line beside it is read by
`verify_value`'s two OCR engines (four reads), and becomes a label only when exactly one value
passes `verify_value`'s own match rule (two reads, both engines, no near-miss, a `1` that looks
like a `1`). A label read this way is the drawn-ink witness, never more (#756 D2).

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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from time import monotonic
from typing import Any, Final

import pdfplumber
from PIL import Image, ImageDraw, ImageFont

from app.review.assistant import verify_value as vv
from app.review.assistant.finding_checks import to_inches, units_check
from app.review.assistant.measure import _pieces
from extraction.stamp_text import path_ink

__all__ = [
    "LABELS_VERSION",
    "PageLabels",
    "build_dims",
    "dimension_line",
    "draw_marks",
    "page_labels",
]

_log = logging.getLogger(__name__)

#: Changes when what a label holds or how ids are given changes.
LABELS_VERSION: Final = 1
#: The most labels one page lists (a schedule page can print hundreds of numbers).
MAX_LABELS: Final = 400
#: A bare number larger than this is not read as inches (it is a count, a note number, or mm).
_BARE_MAX_IN: Final = Decimal(600)
#: A dimension line may be at most this many times longer than its text.
_LINE_MAX_RATIO: Final = 60.0
#: A drawn cluster: its longer side between these (points), and paths at most this far apart.
_CLUSTER_MIN_PT: Final = 4.0
_CLUSTER_MAX_PT: Final = 60.0
_CLUSTER_GAP_PT: Final = 1.5
_MAX_CLUSTERS: Final = 300
#: How far a dimension line's length per inch may be from its drawing's median (`scale_ok`).
_SCALE_TOLERANCE: Final = 0.08
_MM_BARE: Final = re.compile(r"^\d{2,5}$")
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


def _drawn_value(page: Any, box: Box, readers: Sequence[Any]) -> Decimal | None:
    """The one value the drawn ink in `box` reads as under `verify_value`'s match rule, or
    `None` (no value, or more than one passes)."""
    spot = (box[0] - 1, box[1] - 1, box[2] + 1, box[3] + 1)
    reads = vv._read_ink(page, spot, readers)
    values = {v for read in reads for v in read.values if v > 0}
    passing: list[Decimal] = []
    for value in values:
        votes = [read for read in reads if value in read.values]
        engines = {read.engine for read in votes}
        if len(votes) < vv._VOTES_NEEDED or len(engines) < 2:
            continue
        if any(vv.near_miss(value, other) for other in values if other != value):
            continue
        if vv._ones_unsure(votes, value) is not None:
            continue
        passing.append(value)
    return passing[0] if len(passing) == 1 else None


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
    labels = [_label(r, line, _side(r.box, halves)) for r, line in kept[:MAX_LABELS]]
    result = PageLabels(labels=labels)
    if drawn_budget_s > 0 and parsed.page.paths:
        engines = list(vv.default_readers() if readers is None else readers)
        if len(engines) < 2:
            result.drawn = "no_engine"
        else:
            result.drawn = "done"
            skip = [tuple(label["box"]) for label in labels]
            drawn: list[dict[str, Any]] = []
            for cluster in _clusters(parsed.page.paths, skip):
                if len(labels) + len(drawn) >= MAX_LABELS:
                    break
                if clock() - started > drawn_budget_s:
                    result.drawn = "partial"
                    break
                width, height = cluster[2] - cluster[0], cluster[3] - cluster[1]
                found = None
                for orientation, font in (("h", height), ("v", width)):
                    found = dimension_line(cluster, orientation, parsed.segments, font)
                    if found is not None:
                        break
                if found is None:
                    continue
                try:
                    value = _drawn_value(parsed.page, cluster, engines)
                except Exception as failed:  # noqa: BLE001 - an unread cluster is left out
                    _log.warning("dimension labels: drawn read failed: %s", type(failed).__name__)
                    continue
                if value is None:
                    continue
                reading = _Reading(
                    text=vv._inches_text(value),
                    inches=value,
                    box=cluster,
                    along=(1.0, 0.0) if orientation == "h" else (0.0, 1.0),
                    font=min(width, height),
                    bare=False,
                    route="drawn",
                )
                drawn.append(_label(reading, found, _side(cluster, halves)))
            drawn.sort(key=lambda label: (round(label["box"][1] / 4), label["box"][0]))
            labels += drawn
    _scale_checks(labels)
    for number, label in enumerate(labels, start=1):
        label["id"] = f"D{number}"
    result.seconds = round(clock() - started, 2)
    return result


def build_dims(
    pdfs: Mapping[str, Path],
    page_map: Mapping[str, Any] | None,
    *,
    drawn_budget_s: float = 0.0,
    on_page: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """The set pack's `dims.json`: every page of every upload, its labels (text route; the drawn
    route shares `drawn_budget_s` seconds across the set). `on_page` sees the document after each
    page (to write it as it grows)."""
    halves: dict[tuple[str, int], list[Mapping[str, Any]]] = {}
    entries = page_map.get("pages") if isinstance(page_map, Mapping) else None
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, Mapping):
            key = (str(entry.get("file")), int(entry.get("page") or 0))
            halves[key] = [h for h in entry.get("halves") or [] if isinstance(h, Mapping)]
    document: dict[str, Any] = {"version": LABELS_VERSION, "files": list(pdfs), "pages": []}
    left = drawn_budget_s
    for name, pdf in pdfs.items():
        count = 0
        if pdf.is_file():
            with pdfplumber.open(str(pdf)) as opened:
                count = len(opened.pages)
        for number in range(1, count + 1):
            started = monotonic()
            try:
                found = page_labels(
                    pdf, number, halves.get((name, number), ()), drawn_budget_s=max(0.0, left)
                )
            except Exception as failed:  # noqa: BLE001 - a page code cannot read has no labels
                _log.warning("dimension labels: page failed: %s", type(failed).__name__)
                found = PageLabels(drawn="failed")
            if left > 0 and found.drawn != "not_run":
                left -= monotonic() - started
            document["pages"].append(
                {"file": name, "page": number, "drawn": found.drawn, "labels": found.labels}
            )
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
