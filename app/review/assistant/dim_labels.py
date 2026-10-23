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
layer: the characters of the page's own content and its pasted drawings, in every colour except on
GV's combined sheets, where only black or grey is read; GV's markup is never read): rows read the longest way first, stacked fractions composed from the characters'
places, the symbol font decoded. On top of that:

- a millimetre value over its inches in brackets (`457` over `[18]`) is one label, `457 [18]`;
- a bare number (no inch mark, no fraction, no bracket) is a label only when code finds a dimension
  line beside it, and never on a drawing that writes millimetres over inches (there a lone bare
  number is millimetres), nor inside a tag-sized closed shape (a circle, an oval, a box: the `01`
  of a drawing-reference bubble is the tag's name, not 1 inch);
- a scale note (`1/2" = 1'-0"`) is not a dimension.

**The drawn route** (the vendor's numbers drawn as lines, within a time budget,
`drawn_budget_s`; the set pack runs it after every page's text): character-sized ink is grouped
into clusters (and a lone character-sized path seeds one too: a `2"` lettered wide), each split
into labels by its vectors (`ink_labels`, as `verify_value`'s label route does), each label
gathered again until it is whole and stripped of its dimension line's own pieces and ticks. A label
is read only when it is on the vendor's drawing, sits beside a dimension line of its own, has no
other ink in line with it a word space away, and is a dimension by its vectors: a row ending in an
inch mark whose two ticks are twins (one the other moved along the row; never two pieces of a
circle), read in the direction that mark gives; or one such row per dimension when a chain stacks
two labels into one; or the value before its inch mark when only words follow (`48" (FIELD TRIM)`,
the note read as words by RapidOCR); or millimetres over inches in brackets (`305` over `[12]`).
Each row is re-typeset on one line and read by two OCR engines (RapidOCR's recogniser alone,
`LineReader`, then Tesseract). A stacked fraction's digits are placed by the vectors (how many
characters the whole number, the numerator and the denominator have), so a row with one is also
read with no slash drawn, Tesseract only that way (a slash beside a `1` it reads as a `7`) and a
fraction alone as a raw line with no inch mark (its line model reads that `3` as a `5`); an inch
mark an engine reads as `9`s is put back where the vectors drew it. The label is kept only when
exactly one value is read as a whole row by both, no reading of it is a near-miss, a `1` in it is
shaped as a 1 (on its vectors when they lay out the value's digits), and no reading holds a word,
a `+` or a leading zero; millimetres over inches need the bracketed inches and the millimetres each
read the same by both engines, and the two agreeing (`units_check`). On this route a dimension line
ends at its ticks (a centre line's dash or a letter's stroke does not cut it), a label stacked over
a span narrower than itself (a `3/4"` of a chain) takes that span, and one line has one label (the
one centred on it). A label read this way is the drawn-ink witness, never more (#756 D2).

**An architect's hand lettering** (#1662): on the architect's drawings the drawn route reads
dimensions lettered on one line in a CAD hand-lettering font (`2'-6 1/2"`, `3'-10"`, `5 3/8"`),
found on the vectors by `hand_labels` (characters, rows, each character's role, a dimension's
shape, the digits' layout). Both engines must read the same value from the row's picture, every
reading laid out by the vectors (a digit the vectors know, a bare-stroke `1` or a slashed zero,
read otherwise counts for nothing; so does a `1` where the vectors show a hand-lettered `7`). The
label needs room round it and a dimension line of its own running the way it reads. The vendor's
drawings keep the route above.

**Equal parts** (#1668): an `EQ` mark on a dimension line is a part of a printed overall shared
equally (`_equal_parts`: 36" wall to wall marked `EQ` / `EQ` is 18" each side). Code derives the
part only when the chain's ends meet the overall's and every other part is printed, and lists it
as a label with `derived` naming the overall. Derived labels never set a drawing's scale.

**Pasted pictures** (#1830): a drawing pasted as a picture (a raster snapshot, or a scanned page)
has no characters and no vectors, so the routes above read nothing in it. `picture_labels` reads
its numbers with the same two OCR engines (each line found by RapidOCR's text detector, upright or
sideways, and read by both engines both ways along it); a label is kept only when both engines
read exactly one and the same value the way the detector found it, and nothing the other way
round. It is the OCR trust class (`route: "drawn"`, the drawn-ink witness, never more), marked
`origin: "picture"`, with no dimension line (a raster's line work is not measured). A number read
in a picture that is not confirmed is never a label: the page lists it under `pictures`
(`unconfirmed`, with the reason `picture_labels.UNCONFIRMED`). Its side (#1835) is the role of
GV's heading over the picture (the page map lists a headed picture as a drawing, `picture: true`);
in a picture with no heading, or inside both an architect's and a vendor's region, it has none
(`_picture_side`). A headed picture is never the drawing of any other label.

**Numbers the readers missed** (#1848, decided by Anant 2026-10-22): on the vendor's drawings,
vector ink no label and no phrase covers is read at its spot by the same two OCR engines from the
page's picture (`ocr_labels`: GV's marks emptied, its colours whitened); a label is kept only when
both engines read exactly one and the same value in a direction the vectors allow, and nothing the
other way round. It is the picture route's trust class (`route: "drawn"`, `origin: "ocr"`, no
dimension line: `label_origin.ocr_read`); anything less is listed under the page's `ocr`
(`unconfirmed`), a suggestion only.

**Ids are stable:** text labels are numbered in reading order (top to bottom, left to right), then
picture labels, then drawn labels after them, then derived parts, then the OCR route's, so a label
found later never moves an earlier label's id.

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
from fractions import Fraction
from importlib import import_module
from itertools import pairwise
from pathlib import Path
from time import monotonic
from typing import Any, Final

import cv2
import numpy as np
import pdfplumber
from PIL import Image, ImageDraw, ImageFont

from app.review.assistant import (
    dims_pool,
    hand_labels,
    ink_labels,
    ocr_labels,
    picture_labels,
    vendor_alphabet,
)
from app.review.assistant import verify_value as vv
from app.review.assistant.finding_checks import (
    inch_first_mm,
    printed_mm,
    to_inches,
    units_check,
    value_flags,
)
from app.review.assistant.measure import _pieces
from extraction.visible_text import ink_picture

__all__ = [
    "LABELS_VERSION",
    "OCR_BUDGET_S",
    "LineReader",
    "PageLabels",
    "build_dims",
    "dimension_line",
    "draw_marks",
    "drawn_labels",
    "drawn_readers",
    "ocr_pass",
    "page_labels",
    "picture_pass",
]

_log = logging.getLogger(__name__)

#: Changes when what a label holds or how ids are given changes.
LABELS_VERSION: Final = 3
#: The most labels one page lists (a schedule page can print hundreds of numbers).
MAX_LABELS: Final = 400
#: Time limits for ONE page (seconds; #1872: per page, so a set of any size reads in about its
#: pages' time over the workers): the picture route (#1830), the OCR route (#1848: a few seconds a
#: page) and `read_number` at pack time (#1860). The drawn route's is the set pack's
#: (`set_pack.DIMS_DRAWN_BUDGET_S`).
PICTURES_BUDGET_S: Final = 60.0
OCR_BUDGET_S: Final = 60.0
OCR_PAGE_S: Final = 60.0
SWEEP_BUDGET_S: Final = 30.0
#: The most phrases one page lists.
MAX_PHRASES: Final = 1500
#: A bare number larger than this is not read as inches (it is a count, a note number, or mm).
_BARE_MAX_IN: Final = Decimal(600)
#: A tag's closed shape (`_ring`): each side between these (points), at most this much longer one
#: way than the other, its ends this close, and at most this many text sizes across.
_RING_MIN_PT: Final = 6.0
_RING_MAX_PT: Final = 120.0
_RING_ASPECT: Final = 2.5
_RING_GAP_PT: Final = 0.5
_RING_FONTS: Final = 6.0
#: A dimension line may be at most this many times longer than its text.
_LINE_MAX_RATIO: Final = 60.0
#: A drawn cluster: its longer side between these (points), and paths at most this far apart.
_CLUSTER_MIN_PT: Final = 4.0
_CLUSTER_MAX_PT: Final = 60.0
_CLUSTER_GAP_PT: Final = 1.5
_MAX_CLUSTERS: Final = 600
#: A lone path (no ink within the cluster gap) seeds a label when its longer side is between these.
_LONE_MIN_PT: Final = 3.0
_LONE_MAX_PT: Final = 12.0
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
    flags: tuple[str, ...] = ()
    """The notes written with the value (`finding_checks.value_flags`, #1778): `reference`
    (`(24")`), `vif` (`VIF 24"`), `approx` (`24"+/-`)."""
    mm_ignored: bool = False
    """Typed text whose millimetres disagree with its inches (`660 [27"]`): the inches count and
    the millimetres are ignored (settled with GV), so `units` is left unset: never a doubt."""


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
    metric: list[dict[str, Any]] = field(default_factory=list)
    """Millimetre-only values (`794mm`, #1778): `{text, mm, box, side}`. Never a label: inches
    count, millimetres are never a verdict operand, and 794 mm is no exact number of inches; listed
    so the page is known to print dimensions."""
    pictures: dict[str, Any] | None = None
    """The page's pasted pictures and what code read in them (#1830): `{status, boxes,
    unconfirmed}`; `None` when the picture route did not run on the page. `unconfirmed` lists the
    numbers read in a picture that two engines did not confirm (`{text, box, orientation, side,
    why}`): never labels, never a red."""
    sweep_held: list[Any] = field(default_factory=list)
    """Spots `read_number` read with its OCR pair alone (`SweepHeld`), for the alphabet."""
    alphabet: list[Any] = field(default_factory=list)
    """The page's samples of the vendor's alphabet (`alphabet_samples`)."""
    ocr_labels: list[dict[str, Any]] = field(default_factory=list)
    """The OCR route's labels when read with the drawn pass (`drawn_labels`), not yet numbered:
    the pack adds them after every other label of the page (#1848)."""
    ocr: dict[str, Any] | None = None
    """The OCR route's spots (#1848): `{status, spots, seconds, unconfirmed}`; `None` when it did
    not run on the page. `unconfirmed` lists what one engine read, or the two read differently:
    a suggestion only, never a label, never a red."""
    ink: Any = None
    """With `drawn_labels(count_ink=True)`: the page's pen-stroke ink that may be a printed number
    code did not read (`reading_ink.PageInk`), for the reading coverage (#1862). Counts only."""


# ---- reading the page once -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Parsed:
    page: Any
    """`verify_value._Page`: characters, character-sized ink paths, pictures."""
    segments: tuple[Segment, ...]
    """Every straight piece of the page's black or grey line work (shown points)."""
    weight: float = 0.3
    """The line work's usual stroke width (the median, points): how far apart two line ends may be
    and still meet (`vendor_consistency`'s joints)."""
    rings: tuple[Box, ...] = ()
    """Closed tag-sized shapes (circles, ovals, boxes, polygons), shown points: a bare number inside
    one is a tag (`01` in a drawing-reference bubble), never a dimension."""


def _parse(pdf: Path, page_index: int) -> _Parsed:
    """The page's vendor layer read once: `verify_value`'s characters and glyph paths, and
    `measure`'s line work (both from the same pass)."""
    stat = pdf.stat()
    key = (str(pdf.resolve()), stat.st_mtime_ns, stat.st_size, page_index)
    data = pdf.read_bytes()
    colour_rule = vv.combined_sheet(data)  # the colour rule: GV's combined sheets only
    vendor = vv._vendor_page(data, page_index)
    with import_module("app.review.assistant.chat_drawing").PDFIUM_LOCK:
        ink = ink_picture(vendor)
    chars: list[Any] = []
    paths: list[Any] = []
    segments: list[Segment] = []
    widths: list[float] = []
    rings: list[Box] = []
    with pdfplumber.open(io.BytesIO(vendor)) as document:
        page = document.pages[0]
        shown = vv._placement(page)
        corner = shown(float(page.width), float(page.height))
        start = shown(0.0, 0.0)
        size = (abs(corner[0] - start[0]), abs(corner[1] - start[1]))
        for char in page.chars:
            made = vv._char(char, shown, colour_rule=colour_rule)
            if made is not None and vv._visible(made.box, size, ink):
                chars.append(made)
        for item in (*page.curves, *page.lines, *page.rects):
            made_path = vv._path(item, shown, colour_rule=colour_rule)
            if made_path is not None:
                paths.append(made_path)
            if not vv.path_shown(item, colour_rule=colour_rule):
                continue
            width = float(item.get("linewidth") or 0)
            if width > 0:
                widths.append(width)
            for piece in _pieces(item):
                points = [shown(x, y) for x, y in piece]
                segments.extend((a, b) for a, b in pairwise(points) if a != b)
            ring = _ring(item, shown)
            if ring is not None:
                rings.append(ring)
        pictures = tuple(
            vv._shown_box(shown, *(float(image[k]) for k in ("x0", "top", "x1", "bottom")))
            for image in page.images
        )
    parsed = vv._Page(key, vendor, tuple(chars), tuple(paths), pictures, size, colour_rule)
    weight = float(np.median(widths)) if widths else 0.3
    return _Parsed(parsed, tuple(segments), weight, tuple(rings))


def _ring(
    item: Mapping[str, Any], shown: Callable[[float, float], tuple[float, float]]
) -> Box | None:
    """The shown box of a closed tag-sized shape: a rectangle, or a path that ends where it starts
    (a circle, an oval, a hexagon), between `_RING_MIN_PT` and `_RING_MAX_PT` on each side and no
    longer than `_RING_ASPECT` times its width. `None` for anything else."""
    x0, x1, top, bot = (float(item[k]) for k in ("x0", "x1", "top", "bottom"))
    across, down = x1 - x0, bot - top
    if not (_RING_MIN_PT <= min(across, down) and max(across, down) <= _RING_MAX_PT):
        return None
    if max(across, down) > _RING_ASPECT * min(across, down):
        return None
    if item.get("object_type") != "rect":
        pieces = _pieces(item)
        if len(pieces) != 1 or len(pieces[0]) < 4:
            return None
        start, end = pieces[0][0], pieces[0][-1]
        if abs(start[0] - end[0]) > _RING_GAP_PT or abs(start[1] - end[1]) > _RING_GAP_PT:
            return None
    a, b = shown(x0, top), shown(x1, bot)
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]))


def _in_ring(reading: _Reading, rings: Sequence[Box]) -> bool:
    """Whether a reading sits inside a tag-sized closed shape no wider than `_RING_FONTS` of its
    text size: the shape is the tag's (a bubble, a box), the number its name."""
    x0, y0, x1, y1 = reading.box
    return any(
        r[0] - 0.5 <= x0
        and x1 <= r[2] + 0.5
        and r[1] - 0.5 <= y0
        and y1 <= r[3] + 0.5
        and max(r[2] - r[0], r[3] - r[1]) <= _RING_FONTS * reading.font
        for r in rings
    )


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


def _text_units(text: str, *, check: bool | None = None) -> dict[str, Any]:
    """A typed value's millimetres as printed (`mm`) and their check against its inches
    (`units`), kept as before for a value written inches first (#1778) or a stacked pair
    (`check`). Millimetres that disagree with the inches, whichever comes first (`660 [27"]`,
    `31 1/4" [800]`), are kept and marked `mm_ignored`, with no `units` result: the inches count
    (settled with GV, "inches count; ignore millimetres"), so the label is never a doubt because
    of them. Empty for a value in one unit, and for millimetres first in one row that agree."""
    mm = printed_mm(text)
    if mm is None:
        return {}
    units = units_check(text)
    if units == "disagrees":
        return {"mm": mm, "units": None, "mm_ignored": True}
    if check is None and inch_first_mm(text) is None:
        return {}  # millimetres first in one row that agree: read as before (the inches)
    return {"mm": mm, "units": units}


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
                before = tokens[start - 1][0] if start > 0 else ""
                after = tokens[start + size][0] if start + size < len(tokens) else ""
                out.append(
                    _reading(
                        joined,
                        value,
                        group,
                        bare=_bare(joined),
                        flags=_flags(joined, before, after),
                        **_text_units(joined),
                    )
                )
                start += size
                break
        else:
            start += 1
    return out


#: A word or sign written just before a value that notes it (`VIF 24"`, `± 24"`), or just after
#: it (`24" +/-`, `24" VIF`): its flag is kept on the label (#1778).
_NOTE_BEFORE: Final = re.compile(r"^(?:V\.?I\.?F\.?|APPROX\.?|\+/-|\+-|±)$", re.IGNORECASE)
_NOTE_AFTER: Final = re.compile(r"^(?:V\.?I\.?F\.?|\(V\.?I\.?F\.?\)|\+/-|\+-|±)$", re.IGNORECASE)
#: A millimetre-only value: `794mm`, `794 mm` (25 mm or more: smaller is a gap or a reveal note).
_METRIC: Final = re.compile(r"(?<![\d./\[])(\d{2,5}(?:\.\d+)?)\s?mm\b\.?(?!\s*\[)", re.IGNORECASE)
_METRIC_MIN_MM: Final = Decimal(25)


def _flags(text: str, before: str, after: str) -> tuple[str, ...]:
    """The value's own flags (`value_flags`) and those of the note words beside it."""
    found = list(value_flags(text))
    for word, pattern in ((before, _NOTE_BEFORE), (after, _NOTE_AFTER)):
        if word and pattern.match(word):
            for flag in value_flags(word):
                if flag not in found:
                    found.append(flag)
    return tuple(found)


def _metric(
    phrases: Sequence[tuple[str, Box]], halves: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """The page's millimetre-only values (`PageLabels.metric`), each with its phrase's box."""
    out: list[dict[str, Any]] = []
    for text, box in phrases:
        if "[" in text or "(" in text:
            continue  # millimetres with their inches: the label's own
        for found in _METRIC.finditer(text):
            if Decimal(found.group(1)) < _METRIC_MIN_MM:
                continue
            out.append(
                {
                    "text": found.group(0).strip(),
                    "mm": found.group(1),
                    "box": [round(v, 2) for v in box],
                    "side": _side(box, halves),
                }
            )
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
                **{"mm": bare.text, **_text_units(joined, check=True)},
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
    y: float,
    lo: float,
    hi: float,
    segments: Sequence[Segment],
    font: float,
    drawn: bool = False,
) -> list[float]:
    """Where the dimension line at height `y` is cut: extension lines that reach it, and the
    ticks or arrows that cross it (a chain's segments share one line). `drawn` (the drawn route,
    where the characters are line work too): a tick cuts the line only where an upright crosses
    it; a line with such ticks is cut at its ticks only, and on one without, a short upright piece
    (a dash of a centre line) does not cut it."""
    out = [lo, hi]
    uprights: list[tuple[float, float]] = []
    ticks: list[float] = []
    for a, b in segments:
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dy)
        mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
        if not lo - 1.5 <= mx <= hi + 1.5 or length < 0.8:
            continue
        if abs(dx) <= 0.05 * length:
            if min(a[1], b[1]) - 1.5 <= y <= max(a[1], b[1]) + 1.5:
                uprights.append((mx, length))
        elif length <= 3 * font and 0.4 <= abs(dy / dx) <= 2.5 and abs(my - y) <= 1.5:
            if drawn and not _crosses(a[1], b[1], y):
                # A slash tick crosses its line. A drawn `1`'s foot beside the line (its stroke
                # touching an extension-like stroke) only meets it, on one side (#1629: a tall
                # overall was cut at its neighbours' `1`s).
                continue
            ticks.append(mx)
    if drawn:
        # A tick cuts the line where an extension line crosses it too: a character's slanted
        # stroke beside the line is no tick, and a centre line's dash no extension line.
        ticks = [t for t in ticks if any(abs(t - mx) <= 1.0 for mx, _ in uprights)]
        if len(ticks) >= 2:
            # A line drawn with ticks ends at its ticks: the drawing's own edges and shelves
            # crossing it are no ends of it.
            return sorted(out + ticks)
    for mx, length in uprights:
        if drawn and length < 1.5 * font and not any(abs(t - mx) <= 1.0 for t in ticks):
            continue
        out.append(mx)
    return sorted(out + ticks)


#: A tick crosses its line when its two ends lie on either side of it, each at least this share of
#: the tick's height away (the line's place across is known to half a point: rows are rounded).
_CROSS_SHARE: Final = 0.25


def _crosses(a: float, b: float, y: float) -> bool:
    """A stroke from `a` to `b` (across the line) crosses the line at `y`."""
    low, high = min(a, b), max(a, b)
    margin = _CROSS_SHARE * (high - low)
    return low <= y - margin and high >= y + margin


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


def _span_at(
    cuts: Sequence[float],
    cx: float,
    x0: float,
    x1: float,
    lo: float,
    hi: float,
    width: float,
    drawn: bool,
    taken: Sequence[tuple[float, float]],
) -> tuple[float, float] | None:
    """The span of a dimension line (between two cuts) a label at `x0..x1` (middle `cx`) sits on,
    or `None`: the span under its middle, at least 0.8 of its width. On the drawn route also a
    span between two inner cuts narrower than the label (down to 0.3 of it), under its middle or,
    when that one is another label's (`taken`), one beside it at least half under the label (a
    `3/4"` stacked over its ticks, its middle just past one of them), the nearest first."""
    left = max((c for c in cuts if c <= cx), default=None)
    right = min((c for c in cuts if c >= cx), default=None)
    if left is None or right is None:
        return None
    spans = [(left, right)]
    if drawn:
        ordered = sorted(set(cuts))
        beside = [
            (a, b)
            for a, b in pairwise(ordered)
            if (a, b) != (left, right) and min(b, x1) - max(a, x0) >= 0.5 * (b - a)
        ]
        spans += sorted(beside, key=lambda s: abs((s[0] + s[1]) / 2 - cx))
    for a, b in spans:
        if any(abs(a - t[0]) <= 0.6 and abs(b - t[1]) <= 0.6 for t in taken):
            continue
        narrow = drawn and (a not in (lo, hi) or b not in (lo, hi))
        if b - a < (0.3 if narrow else 0.8) * width:
            continue
        if (a, b) != (left, right) and not narrow:
            continue
        return a, b
    return None


def dimension_line(
    box: Box,
    orientation: str,
    segments: Sequence[Segment],
    font: float,
    *,
    drawn: bool = False,
    taken: Sequence[tuple[float, float, float]] = (),
) -> dict[str, Any] | None:
    """The dimension line a label sits on (or in a gap of), cut at the extension lines or ticks
    on each side of the label, and its two extension lines when code finds them: `{dim_line:
    [p, q], extension: [[far, near] | None, [far, near] | None], span_pt}`. Horizontal and
    vertical labels only; `None` when no line with at least one extension line or tick is found.

    `drawn` (the drawn route): a centre line's dashes do not cut a line (`_boundaries`), and a
    span between two ticks narrower than the label (a `3/4"` of a chain, its label stacked over
    its ticks) is its line when the label's middle is over it. `taken`: spans (`(across, start,
    end)`, the frame of `orientation`) another label is centred on, never this one's."""
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
        cuts = _boundaries(y, lo, hi, segments, font, drawn)
        found = _span_at(
            cuts, cx, x0, x1, lo, hi, width, drawn, [t[1:] for t in taken if abs(y - t[0]) <= 0.6]
        )
        if found is None:
            continue
        left, right = found
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


def _clusters(paths: Sequence[Any], skip: Sequence[Box]) -> tuple[list[Box], list[Box]]:
    """Character-sized ink grouped into clusters (a drawn number), outside the text labels; and
    the lone paths, with no other ink within the clusters' gap. A lone character-sized path seeds
    a label drawn with wide letter spacing (a `2"` whose inch mark is 1.8 points away, a large
    `90"`): `ink_labels` gathers its label with a gap relative to the characters' size."""
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
    lone: list[Box] = []
    for members in groups.values():
        box = _union(members)
        longer, shorter = (
            max(box[2] - box[0], box[3] - box[1]),
            min(box[2] - box[0], box[3] - box[1]),
        )
        if len(members) < 2:
            if _LONE_MIN_PT <= longer <= _LONE_MAX_PT:
                lone.append(box)
            continue
        if _CLUSTER_MIN_PT <= longer <= _CLUSTER_MAX_PT and 2.0 <= shorter <= 12.0:
            out.append(box)

    def order_of(b: Box) -> tuple[int, float]:
        return round(b[1] / 4), b[0]

    return sorted(out, key=order_of)[:_MAX_CLUSTERS], sorted(lone, key=order_of)[:_MAX_CLUSTERS]


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


def _passes(
    reader: Any, row: Sequence[ink_labels.Token], layout: _Layout | None, marked: bool
) -> list[list[tuple[bool, bool, bool]]]:
    """How an engine reads one row: `(inch mark drawn, fraction slash drawn, raw line)` per pass.

    A row is read as `verify_value`'s label route reads it: the slash drawn between a stacked
    fraction's parts, and an engine that reads `"` as `99` reads the row without its inch mark. A
    row with a stacked fraction whose characters the vectors count (`_layout`) is also read with no
    slash, the engine reading only the digits the layout places. Tesseract reads such a row only
    that way (a slash beside a `1` it reads as a `7`: `17 1/8"` as `17 7/8"`, measured on a real set),
    and a stacked fraction alone (no whole number) without its inch mark, as a raw line (its line
    model reads that `3` as a `5` and the mark as a `9`). Each list after the first is read only
    when the ones before it gave the row no value."""
    fraction = layout is not None and any(t.kind == "fraction" for t in row)
    reads_marks = getattr(reader, "reads_inch_marks", True)
    mark = not marked or reads_marks
    if fraction and reads_marks and layout is not None:
        if not layout[0]:
            return [[(not marked, False, True)]]
        # When the digits alone give no reading, the row as `verify_value` reads it.
        return [[(mark, False, False)], [(mark, True, False)]]
    out = [(mark, True, False)]
    if fraction:
        out.append((mark, False, False))
    return [out]


def _mark_as_nines(line: str, layout: _Layout | None) -> str:
    """The line with an inch mark read as `9`s put back as `"`: when the vectors drew the mark and
    count the row's characters (`layout`), trailing `9`s beyond that count are the mark (`3/4"`
    read `349`, `34"` read `3499`). Any other line is returned as read."""
    if layout is None:
        return line
    wanted = sum(layout)
    text = line.rstrip()
    while text and text[-1] in "9\"' ":
        if sum(ch.isdigit() for ch in text) == wanted:
            break
        text = text[:-1].rstrip()
    stripped = line.rstrip()[len(text) :]
    if text == line.rstrip() or set(stripped) - set('9" ') or not text[-1:].isdigit():
        return line
    if sum(ch.isdigit() for ch in text) != wanted:
        return line
    return text + '"'


def _label_reads(
    label: ink_labels.Label, engines: Sequence[Any], turn: ink_labels.Turn | None = None
) -> list[Any]:
    """One drawn label read by each engine the way `verify_value`'s label route reads it: each
    row re-typeset on one line (`ink_labels`), at two sizes, in each plausible reading direction,
    in the passes `_passes` gives. An engine after the first runs only when an earlier one read a
    whole dimension: a label that no engine reads as one can never have two engines agree."""
    reads: list[Any] = []
    for reader in engines:
        if reads and not any(read.whole for read in reads):
            break
        read = vv._InkRead(f"drawn label, {reader.name}", reader.name, 1)
        for way in (_marked_turns(label) or ink_labels.turns(label)) if turn is None else (turn,):
            for row in ink_labels.rows(label, way):
                layout = _layout(row, way)
                marked = bool(ink_labels.inch_mark_at_end(row, way))
                gave = len(read.whole)
                for group in _passes(reader, row, layout, marked):
                    if len(read.whole) > gave:
                        break
                    for mark, slash, raw in group:
                        for height in ink_labels.HEIGHTS_PX:
                            picture = ink_labels.row_picture(
                                row, way, height, inch_mark=mark, slash=slash
                            )
                            if picture is None:
                                continue
                            engine = getattr(reader, "raw_line", None) if raw else None
                            found = engine(picture) if engine else reader(picture, single_line=True)
                            line = " ".join(found).strip()
                            if mark and marked:
                                line = _mark_as_nines(line, layout)
                            if not slash:
                                # Read for its digits only: a line with any other count of digits
                                # says nothing (it is no reading of another number either).
                                whole = None if layout is None else _laid_out(line, layout)
                                if whole is None and sum(ch.isdigit() for ch in line) != sum(
                                    layout or (0,)
                                ):
                                    continue
                            read.add(reader, picture, [line] if line else [], True)
                            # The digits laid out as the vectors draw them first: an engine that
                            # loses a space (`11/2` for a 1 and a stacked 1 over 2) still reads the
                            # value. When the digits do not match the drawing, the line as read.
                            whole = None if layout is None else _laid_out(line, layout)
                            if whole is None and slash:
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


type _Characters = tuple[list[list[ink_labels.Glyph]], ...]


def _characters(
    glyphs: Sequence[ink_labels.Glyph], turn: ink_labels.Turn
) -> list[list[ink_labels.Glyph]]:
    """Glyphs grouped into characters, in reading order: a glyph lying (four fifths or more) within
    another's extent along the reading direction is part of the same character (a `4` drawn as
    two strokes that do not touch). Characters side by side never overlap so far along the row."""
    out: list[list[ink_labels.Glyph]] = []
    extents: list[tuple[float, float]] = []
    for glyph in sorted(glyphs, key=lambda g: ink_labels._frame(g.box, turn)[0]):
        frame = ink_labels._frame(glyph.box, turn)
        start, end = frame[0], frame[2]
        if extents:
            low, high = extents[-1]
            shared = min(end, high) - max(start, low)
            narrower = min(end - start, high - low)
            if shared >= 0.8 * narrower:
                out[-1].append(glyph)
                extents[-1] = (min(low, start), max(high, end))
                continue
        out.append([glyph])
        extents.append((start, end))
    return out


def _row_characters(row: Sequence[ink_labels.Token], turn: ink_labels.Turn) -> _Characters | None:
    """The row's characters: its whole number's, its stacked fraction's numerator's and its
    denominator's, or `None` when the row is not one number with at most one stacked fraction at
    its end."""
    whole: list[ink_labels.Glyph] = []
    fraction: ink_labels.Token | None = None
    for token in row:
        if token.kind == "mark":
            continue
        if fraction is not None:  # a character after the fraction: not one plain dimension
            return None
        if token.kind == "fraction":
            fraction = token
        else:
            whole.extend(token.glyphs)
    if not whole and fraction is None:
        return None
    upper = _characters(fraction.glyphs, turn) if fraction is not None else []
    lower = _characters(fraction.below, turn) if fraction is not None else []
    return _characters(whole, turn), upper, lower


def _layout(row: Sequence[ink_labels.Token], turn: ink_labels.Turn = 0) -> _Layout | None:
    """How many characters the row's vectors draw for its whole number, its stacked fraction's
    numerator and its denominator (`(2, 1, 1)` is `17 3/8"`), or `None` when the row is not one
    number with at most one stacked fraction at its end. Strokes of one character are counted
    once (`_characters`), so a layout only ever confirms digits, never adds one."""
    found = _row_characters(row, turn)
    if found is None:
        return None
    whole, numerator, denominator = (len(part) for part in found)
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


def _twin_ticks(row: Sequence[ink_labels.Token], turn: ink_labels.Turn) -> bool:
    """Whether the row ends in an inch mark drawn as one: two strokes of the same size and shape,
    side by side along the reading direction (one is the other moved along the row). Two pieces of
    a broken circle or of a dashed line are not."""
    count = ink_labels.inch_mark_at_end(row, turn)
    if not count:
        return False
    strokes = [
        [ink_labels._point(x, y, turn) for x, y in line]
        for token in row[len(row) - count :]
        for path in token.glyphs[0].paths
        for line in path.polylines
        if line
    ]
    if len(strokes) != 2:
        return False
    boxes = [
        (min(x for x, _ in s), min(y for _, y in s), max(x for x, _ in s), max(y for _, y in s))
        for s in strokes
    ]
    (a0, a1, a2, a3), (b0, b1, b2, b3) = boxes
    size = max(a2 - a0, a3 - a1, b2 - b0, b3 - b1)
    slack = 0.25 * size + 0.1
    if abs((a2 - a0) - (b2 - b0)) > slack or abs((a3 - a1) - (b3 - b1)) > slack:
        return False
    if abs(a1 - b1) > slack or abs(a3 - b3) > slack:  # side by side, not one above the other
        return False
    if max(a0, b0) < min(a2, b2) - 0.1 * size:  # overlapping along the row: no pair of ticks
        return False
    dx, dy = b0 - a0, b1 - a1
    moved = np.array([(x + dx, y + dy) for x, y in strokes[0]])
    other = np.array(strokes[1])
    if len(moved) < 2 or len(other) < 2:
        return False
    distance = np.sqrt(((moved[:, None, :] - other[None, :, :]) ** 2).sum(axis=2))
    hausdorff = max(float(distance.min(axis=1).max()), float(distance.min(axis=0).max()))
    return hausdorff <= slack


def _marked_turns(label: ink_labels.Label) -> tuple[ink_labels.Turn, ...]:
    """The reading directions in which a row of the label ends in an inch mark (two ticks): how a
    drawn dimension is written, so the direction it reads in. A lone digit gives `ink_labels` no
    neighbour to tell its direction by (a `4"` written up the page); its inch mark does."""
    return tuple(
        turn
        for turn in (0, 90, 270)
        if any(_twin_ticks(row, turn) for row in ink_labels.rows(label, turn))
    )


def _inch_marked(label: ink_labels.Label) -> bool:
    """Whether the label's own strokes end a row in an inch mark (two ticks), in some reading
    direction: how a drawn dimension is written. A bare drawn number (a tag, a count, a symbol
    an engine reads as a digit) is never a label: its vectors, not OCR, decide."""
    return bool(_marked_turns(label))


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


def _crowded(
    page: Any,
    label: ink_labels.Label,
    also: Sequence[ink_labels.Glyph] = (),
    turn: ink_labels.Turn | None = None,
) -> bool:
    """Whether other character-sized ink sits in line with the label, closer than 1.2 times its
    height (a word space), in a direction it may read: a word or a number the label belongs to
    (`FN-` before `02`, `SCALE :` before `1"`, `2'-` before `10"`). Such a label is not read: what
    it is part of decides its value. The dimension's own ticks, arrowheads and extension lines do
    not count (`_line_work`), nor the next dimension of a chain (a label with its own inch mark, or
    millimetres over inches). `also`: glyphs of the label's own note (`48" (FIELD TRIM)`).
    """
    own = {id(path) for glyph in (*label.glyphs, *also) for path in glyph.paths}
    near = [path for path in vv._ink_paths_near(page, label.box) if id(path) not in own]
    # The next dimension of a chain (its own number and inch mark) is no word round this one.
    x0, y0, x1, y1 = label.box
    reach = 1.2 * max(x1 - x0, y1 - y0)
    around = (x0 - reach, y0 - reach, x1 + reach, y1 + reach)
    dimensions = {
        id(path)
        for other in ink_labels.labels(vv._ink_paths_near(page, around), around)
        if _inch_marked(other) or _mm_rows(other) is not None
        for glyph in other.glyphs
        for path in glyph.paths
    }
    near = [path for path in near if id(path) not in dimensions]
    ways = (_marked_turns(label) or ink_labels.turns(label)) if turn is None else (turn,)
    for way in ways:
        frame = ink_labels._frame(label.box, way)
        tall = frame[3] - frame[1]
        for path in near:
            other = ink_labels._frame(path.box, way)
            if other[3] - other[1] < 0.15 * tall:  # a piece of the dimension line it sits on
                continue
            if _line_work(path, way):
                continue
            across = min(frame[3], other[3]) - max(frame[1], other[1])
            gap = max(other[0] - frame[2], frame[0] - other[2])
            if across >= 0.5 * min(tall, other[3] - other[1]) and gap < 1.2 * tall:
                return True
    return False


def _line_ends(segments: Sequence[Segment]) -> np.ndarray:
    """The page's line work as an array of `(x0, y0, x1, y1)` rows, for `_without_line_work`."""
    if not segments:
        return np.zeros((0, 4))
    return np.array([(a[0], a[1], b[0], b[1]) for a, b in segments], dtype=float)


def _between_glyphs(
    label: ink_labels.Label,
    stroke: ink_labels.Glyph,
    start: np.ndarray,
    unit: np.ndarray,
    length: float,
) -> bool:
    """Whether a straight stroke has the label's glyphs on both sides of it, over its length: a
    stacked fraction's bar (on a dimension line it may line up with it), never line work."""
    sides = set()
    for glyph in label.glyphs:
        if glyph is stroke:
            continue
        centre = np.array(((glyph.box[0] + glyph.box[2]) / 2, (glyph.box[1] + glyph.box[3]) / 2))
        along = float(np.dot(centre - start, unit))
        if -0.1 * length <= along <= 1.1 * length:
            side = ink_labels._cross(unit, centre - start)
            if abs(side) > 0.3:
                sides.add(side > 0)
    return len(sides) == 2


def _without_line_work(label: ink_labels.Label, lines: np.ndarray) -> ink_labels.Label | None:
    """The label without the dimension line's own pieces and ticks, or `None` when nothing else
    is left. A dimension line cut short by its ticks leaves pieces as small as a character, so they
    join the label they pass beside: a straight stroke on a line that runs on past the label (the
    line work continues it, touching or overlapping it) is line work, and so is a small mark
    centred on such a line (a tick)."""
    x0, y0, x1, y1 = label.box
    if not len(lines):
        return label
    near = lines[
        (np.maximum(lines[:, 0], lines[:, 2]) >= x0 - 8)
        & (np.minimum(lines[:, 0], lines[:, 2]) <= x1 + 8)
        & (np.maximum(lines[:, 1], lines[:, 3]) >= y0 - 8)
        & (np.minimum(lines[:, 1], lines[:, 3]) <= y1 + 8)
    ]
    axes: list[tuple[np.ndarray, np.ndarray]] = []
    dropped: set[int] = set()
    for index, glyph in enumerate(label.glyphs):
        ends = ink_labels._ends(glyph)
        if ends is None:
            continue
        start, end = ends
        length = float(np.linalg.norm(end - start))
        if length < 0.5:
            continue
        unit = (end - start) / length
        runs_on = False
        for sx, sy, ex, ey in near:
            a, b = np.array((sx, sy)), np.array((ex, ey))
            if (
                abs(ink_labels._cross(unit, a - start)) > 0.3
                or abs(ink_labels._cross(unit, b - start)) > 0.3
            ):
                continue
            ta, tb = float(np.dot(a - start, unit)), float(np.dot(b - start, unit))
            low, high = min(ta, tb), max(ta, tb)
            if high < -0.6 or low > length + 0.6:  # not touching the stroke
                continue
            reach = max(abs(ta), abs(tb), abs(ta - length), abs(tb - length))
            outside = [start + unit * t for t in (low, high)]
            if (
                any(
                    not (x0 - 1 <= float(p[0]) <= x1 + 1 and y0 - 1 <= float(p[1]) <= y1 + 1)
                    for p in outside
                )
                and reach > length + 1.0
            ):
                runs_on = True
                break
        if runs_on and not _between_glyphs(label, glyph, start, unit, length):
            dropped.add(index)
            axes.append((start, unit))
    if not dropped:
        return label
    for index, glyph in enumerate(label.glyphs):
        if index in dropped or glyph.size > 4.0:
            continue
        centre = np.array(((glyph.box[0] + glyph.box[2]) / 2, (glyph.box[1] + glyph.box[3]) / 2))
        if not any(abs(ink_labels._cross(unit, centre - start)) <= 0.6 for start, unit in axes):
            continue
        ends = ink_labels._ends(glyph)
        if ends is not None:
            length = float(np.linalg.norm(ends[1] - ends[0]))
            if length > 0 and _between_glyphs(
                label, glyph, ends[0], (ends[1] - ends[0]) / length, length
            ):
                continue  # a fraction's bar on the dimension line
        dropped.add(index)
    kept = [g for i, g in enumerate(label.glyphs) if i not in dropped]
    if not kept:
        return None
    return ink_labels.Label(tuple(kept), ink_labels._union([g.box for g in kept]))


@dataclass(frozen=True, slots=True)
class _Part:
    """One dimension to read in a drawn label: the label itself, one row of a label holding several
    dimensions, the value before a note, or millimetres over inches."""

    label: ink_labels.Label
    kind: str = "inches"
    """`inches` (a number with its inch mark) or `mm` (millimetres over inches in brackets)."""
    note: tuple[ink_labels.Token, ...] = ()
    """The note after the value's inch mark, as read (`(FIELD TRIM)`)."""
    turn: ink_labels.Turn | None = None
    """The reading direction the whole label was split in (`None`: the part's own)."""
    also: tuple[ink_labels.Glyph, ...] = ()
    """The note's glyphs: the label's own ink, never a crowding word."""
    spot: Box | None = None
    """Where the dimension line is looked for (`None`: round the part): the value with its note
    (a note in brackets reaches lower than its value, nearer its line)."""


def _row_label(
    label: ink_labels.Label, row: Sequence[ink_labels.Token], turn: ink_labels.Turn
) -> ink_labels.Label:
    """One row of a label as a label of its own: the row's glyphs, and its stacked fractions'
    bars (the label's glyphs no row holds, inside the row's box)."""
    held = {
        id(g) for line in ink_labels.rows(label, turn) for t in line for g in (*t.glyphs, *t.below)
    }
    glyphs = [g for t in row for g in (*t.glyphs, *t.below)]
    frames = [ink_labels._frame(g.box, turn) for g in glyphs]
    low, high = min(f[0] for f in frames), max(f[2] for f in frames)
    top, bottom = min(f[1] for f in frames), max(f[3] for f in frames)
    for g in label.glyphs:
        if id(g) in held:
            continue
        f = ink_labels._frame(g.box, turn)
        middle, centre = (f[0] + f[2]) / 2, (f[1] + f[3]) / 2
        if low <= middle <= high and top <= centre <= bottom:
            glyphs.append(g)
    glyphs.sort(key=lambda g: (g.box[1], g.box[0]))
    return ink_labels.Label(tuple(glyphs), ink_labels._union([g.box for g in glyphs]))


def _merged(row: Sequence[ink_labels.Token], turn: ink_labels.Turn) -> list[ink_labels.Token]:
    """The row with a character drawn in two pieces made one (#1860): a small mark lying four
    fifths or more within a glyph's extent along the row (a `5` whose top bar is its own stroke)
    joins that glyph. Ticks after the last character and a fraction are left as they are."""
    out: list[ink_labels.Token] = []
    for token in row:
        if token.kind == "mark" and out and out[-1].kind == "glyph":
            mark = ink_labels._frame(token.glyphs[0].box, turn)
            glyph = ink_labels._frame(out[-1].glyphs[0].box, turn)
            shared = min(mark[2], glyph[2]) - max(mark[0], glyph[0])
            if shared >= 0.8 * (mark[2] - mark[0]) > 0:
                one = out[-1].glyphs[0]
                paths = (*one.paths, *token.glyphs[0].paths)
                box = ink_labels._union([one.box, token.glyphs[0].box])
                joined = ink_labels.Glyph(paths, box)
                out[-1] = ink_labels.Token("glyph", (joined,), (), out[-1].along, out[-1].middle)
                continue
        out.append(token)
    return out


def _mm_rows(label: ink_labels.Label) -> ink_labels.Turn | None:
    """The reading direction in which the label is millimetres over inches in brackets (`305`
    over `[12]`): exactly two rows, the upper all characters (two to five, no fraction, no inch
    mark; a character drawn in two pieces counts once, `_merged`), the lower starting and ending
    in a tall narrow glyph (the brackets); else `None`."""
    for turn in ink_labels.turns(label):
        rows = [_merged(row, turn) for row in ink_labels.rows(label, turn)]
        if len(rows) != 2 or any(ink_labels.inch_mark_at_end(row, turn) for row in rows):
            continue
        upper, lower = rows
        if (
            any(t.kind != "glyph" for t in upper)
            or not 2 <= len(_characters([g for t in upper for g in t.glyphs], turn)) <= 5
        ):
            continue
        mains = [t for t in lower if t.kind != "mark"]
        if len(mains) < 3 or mains[0].kind != "glyph" or mains[-1].kind != "glyph":
            continue
        height = max(
            ink_labels._frame(g.box, turn)[3] - ink_labels._frame(g.box, turn)[1]
            for t in lower
            for g in t.glyphs
        )
        ends = [ink_labels._frame(mains[i].glyphs[0].box, turn) for i in (0, -1)]
        if all(f[3] - f[1] >= 0.8 * height and f[2] - f[0] <= 0.5 * (f[3] - f[1]) for f in ends):
            return turn
    return None


def _inch_mark_inside(row: Sequence[ink_labels.Token], turn: ink_labels.Turn) -> int:
    """Where a row's value ends when a note follows its inch mark (`48"(FIELD TRIM)`): the index
    after the inch mark's second tick, or 0. At least one character before it, at least two
    after it."""
    for end in range(2, len(row) - 1):
        head = row[:end]
        if not _twin_ticks(head, turn) or row[end].kind == "mark":
            continue
        before = [t for t in head if t.kind != "mark"]
        after = [t for t in row[end:] if t.kind == "glyph"]
        if before and len(after) >= 2:
            return end
    return 0


def _parts(label: ink_labels.Label) -> list[_Part]:
    """The dimensions to read in a drawn label: millimetres over inches; one per row when two or
    more rows each end in their own inch mark (labels a chain stacks closely); the value before
    its note; or the label itself when it ends in an inch mark. Anything else: none."""
    mm = _mm_rows(label)
    if mm is not None:
        return [_Part(label, "mm", turn=mm)]
    for turn in _marked_turns(label):
        rows = ink_labels.rows(label, turn)
        marked = [row for row in rows if _twin_ticks(row, turn)]
        if len(rows) >= 2 and len(marked) >= 2:
            return [_Part(_row_label(label, row, turn), turn=turn) for row in marked]
    if _inch_marked(label):
        return [_Part(label)]
    for turn in ink_labels.turns(label):
        rows = ink_labels.rows(label, turn)
        if len(rows) != 1:
            continue
        end = _inch_mark_inside(rows[0], turn)
        if not end:
            continue
        head, tail = rows[0][:end], rows[0][end:]
        value = _row_label(label, head, turn)
        rest = tuple(g for g in label.glyphs if g not in value.glyphs)
        return [_Part(value, note=tuple(tail), turn=turn, also=rest, spot=label.box)]
    return []


_NOTE_WORDS: Final = re.compile(r"[A-Za-z]{2,}")


def _note_is_words(part: _Part, engines: Sequence[Any]) -> bool:
    """Whether the note after a drawn value reads as words only, by the engine that knows letters
    (RapidOCR): at least one word, no digit and no `+` (`3"+2" FILLER` is a sum, not a note)."""
    for reader in engines:
        if reader.name != "RapidOCR":
            continue
        lines = []
        for height in ink_labels.HEIGHTS_PX:
            picture = ink_labels.row_picture(part.note, part.turn or 0, height)
            if picture is not None:
                lines.append(" ".join(reader(picture, single_line=True)))
        text = " ".join(lines)
        return (
            bool(lines)
            and all(_NOTE_WORDS.search(line) and not re.search(r"[\d+]", line) for line in lines)
            and bool(text)
        )
    return False


_MM_LINE: Final = re.compile(r"\d{2,5}")
_BRACKET_LINE: Final = re.compile(r"\[\s*(\d+(?:\s+\d+/\d+)?|\d+/\d+)\s*\]")


def _mm_value(part: _Part, engines: Sequence[Any]) -> tuple[Decimal, str] | None:
    """Millimetres over inches read on the drawn route, or `None`: the bracketed inches read the
    same by two engines, with no near-miss among their reads, and the millimetres read the same by
    two engines and agreeing with the inches (`units_check`, a free cross-check: a misread inch
    is caught by the millimetres, and the other way round)."""
    turn = part.turn or 0
    rows = ink_labels.rows(part.label, turn)
    if len(rows) != 2:
        return None
    mm_by: dict[str, set[str]] = {}
    inch_by: dict[Decimal, set[str]] = {}
    inch_lines: list[str] = []
    for reader in engines:
        for index, row in enumerate(rows):
            for height in ink_labels.HEIGHTS_PX:
                picture = ink_labels.row_picture(row, turn, height)
                if picture is None:
                    continue
                line = " ".join(reader(picture, single_line=True)).strip()
                if index == 0 and _MM_LINE.fullmatch(line):
                    mm_by.setdefault(line, set()).add(reader.name)
                elif index == 1:
                    inch_lines.append(line)
                    bracket = _BRACKET_LINE.fullmatch(line)
                    value = None if bracket is None else to_inches(bracket.group(1) + '"')
                    if value is not None:
                        inch_by.setdefault(value, set()).add(reader.name)
    inches = [v for v, names in inch_by.items() if len(names) >= 2]
    millimetres = [m for m, names in mm_by.items() if len(names) >= 2]
    if len(inches) != 1 or len(millimetres) != 1:
        return None
    value, mm = inches[0], millimetres[0]
    if not 0 < value <= _BARE_MAX_IN or mm.startswith("0"):
        return None
    if any(vv.near_miss(value, other) for other in vv._ocr_values(inch_lines) | set(inch_by)):
        return None
    if units_check(f"{mm} [{vv._inches_text(value).rstrip(chr(34))}]") != "agrees":
        return None
    return value, mm


def _vector_ones(
    label: ink_labels.Label, value: Decimal, turn: ink_labels.Turn | None = None
) -> bool | None:
    """The `1` shape check on the vectors: for a row whose characters are laid out as the value's
    digits, `True` when every `1` is a narrow stroke, `False` when one is as wide as a `7` (a
    hand-lettered 7), `None` when no row lays out the value's digits (the engines' check then)."""
    exact = Fraction(value)
    whole = exact.numerator // exact.denominator
    rest = exact - whole
    parts = (
        str(whole) if whole else "",
        str(rest.numerator) if rest else "",
        str(rest.denominator) if rest else "",
    )
    for way in (_marked_turns(label) or ink_labels.turns(label)) if turn is None else (turn,):
        for row in ink_labels.rows(label, way):
            found = _row_characters(row, way)
            if found is None or tuple(len(p) for p in found) != tuple(len(p) for p in parts):
                continue
            for digits, characters in zip(parts, found, strict=True):
                for digit, character in zip(digits, characters, strict=True):
                    if digit != "1":
                        continue
                    frame = ink_labels._frame(ink_labels._union([g.box for g in character]), way)
                    wide, tall = frame[2] - frame[0], frame[3] - frame[1]
                    if tall <= 0 or wide / tall >= vv._ONE_MAX_WIDTH:
                        return False
            return True
    return None


def _whole_label(
    page: Any, label: ink_labels.Label, gathered: Box | None = None
) -> ink_labels.Label | None:
    """The label gathered again round its own box until it stops growing, or `None`. A cluster
    gathers the ink a few points round it, so a label at the edge of what it gathered may have
    lost a digit (`47"` cut to `7"`): only a label that gathers to itself is read. A label that
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


def _drawn_value(
    reads: Sequence[Any], ones: Callable[[Decimal], bool | None] | None = None
) -> Decimal | None:
    """The one value a drawn label reads as, or `None`: `verify_value`'s label rule with no value
    to expect. Exactly one value read as a whole row by two engines, no reading of the label a
    near-miss of it (one digit off, or inside a longer number; a pass whose odd digit makes a
    fraction no drawing prints, 6317 for `63 1/2"`, is no rival: `vv.impossible_fraction`), and a
    `1` in it shaped as a 1. A zero or a number too big for inches is no dimension, and neither is
    a sum written out (`2"+3" FILLER`: the line it sits on spans both)."""
    lines = [line for read in reads for line in read.lines]
    if any("+" in line or _LEADING_ZERO.search(line) or _WORD.search(line) for line in lines):
        # A sum written out; a number with a leading zero, which no dimension has (`EQ` read
        # upside down is `03`); or a word read in it (a tag `TG-002`, a note `(3) FIXED
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
    # The `1` shape: on the vectors when a row lays out the value's digits (`ones`), else as the
    # engines saw it.
    shaped = None if ones is None else ones(value)
    if shaped is False:
        return None
    if shaped is None and vv._ones_unsure(list(reads), value) is not None:
        return None
    return value


# ---- one page ------------------------------------------------------------------------------------


_SIDES: Final = {"architect": "architect", "vendor": "vendor", "brand_prototype": "brand"}


def _within(point: tuple[float, float], box: object) -> bool:
    if not isinstance(box, Sequence) or len(box) != 4:
        return False
    x0, y0, x1, y1 = (float(v) for v in box)
    return x0 <= point[0] <= x1 and y0 <= point[1] <= y1


def _side(
    box: Box, halves: Sequence[Mapping[str, Any]], pasted: Sequence[Sequence[float]] = ()
) -> str:
    """Whose drawing a label is on: the nearest drawing's (within 36 pt). A label lying inside
    both an architect's and a vendor's drawing (two pasted drawings overlapping) is on neither
    (#1825): code cannot tell, so it is never paired. A headed pasted picture (`picture: true`
    in `halves`, #1835) is never the nearest drawing here: a label printed on the page is not in
    a picture. `pasted`: the page's pasted pictures (the page map's `pictures`), given for a label
    read inside a picture (`origin: "picture"`): see `_picture_side`."""
    centre = _centre(box)
    if pasted and any(_within(centre, picture) for picture in pasted):
        return _picture_side(centre, halves, pasted)
    cx, cy = centre
    best: tuple[float, str] | None = None
    inside: set[str] = set()
    for half in halves:
        if half.get("picture"):
            continue
        hb = half.get("box")
        if not isinstance(hb, Sequence) or len(hb) != 4:
            continue
        x0, y0, x1, y1 = (float(v) for v in hb)
        distance = math.hypot(max(x0 - cx, 0, cx - x1), max(y0 - cy, 0, cy - y1))
        role = _SIDES.get(str(half.get("role")), "unknown")
        if distance == 0:
            inside.add(role)
        if best is None or distance < best[0]:
            best = (distance, role)
    if best is None or best[0] > 36 or {"architect", "vendor"} <= inside:
        return "unknown"
    return best[1]


def _picture_side(
    centre: tuple[float, float],
    halves: Sequence[Mapping[str, Any]],
    pasted: Sequence[Sequence[float]],
) -> str:
    """Whose drawing a label read inside a pasted picture is on (#1835, decided by Anant
    2026-10-21): the side of the GV heading over that picture (its page-map drawing, `picture:
    true`), by the heading rule pasted drawings have, never a guess from what the picture shows.
    `unknown` (never paired, never red) when a picture holding it has no heading, when the
    pictures holding it disagree, or when it lies inside both an architect's and a vendor's
    region (a picture and a pasted drawing overlapping, #1825)."""
    sides: set[str] = set()
    for picture in pasted:
        if not _within(centre, picture):
            continue
        headed = [
            half for half in halves if half.get("picture") and _same_box(half.get("box"), picture)
        ]
        if not headed:
            return "unknown"
        sides |= {_SIDES.get(str(half.get("role")), "unknown") for half in headed}
    if len(sides) != 1 or "unknown" in sides:
        return "unknown"
    regions = sides | {
        _SIDES.get(str(half.get("role")), "unknown")
        for half in halves
        if not half.get("picture") and _within(centre, half.get("box"))
    }
    if {"architect", "vendor"} <= regions:
        return "unknown"
    return next(iter(sides))


def _same_box(a: object, b: Sequence[float]) -> bool:
    """Whether a page-map drawing's box is this pasted picture's (to the rounding it is kept to)."""
    if not isinstance(a, Sequence) or len(a) != 4 or len(b) != 4:
        return False
    return all(abs(float(x) - float(y)) <= 0.5 for x, y in zip(a, b, strict=True))


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
        **({"mm_ignored": True} if reading.mm_ignored else {}),
        "dim_line": None if line is None else line["dim_line"],
        "extension": None if line is None else line["extension"],
        "span_pt": None if line is None else line["span_pt"],
        **({"flags": list(reading.flags)} if reading.flags else {}),
    }


def _scale_checks(labels: Sequence[dict[str, Any]]) -> None:
    """`scale_ok` on each label with a dimension line: its drawn length per inch within
    `_SCALE_TOLERANCE` of the median of its drawing's labels (at least three), else `False`;
    `None` when there are too few to tell. A line off the drawing's scale may be the wrong line,
    or a dimension not drawn to scale."""
    by_side: dict[str, list[float]] = {}
    for label in labels:
        inches = float(label["inches"])
        if label.get("span_pt") and inches >= 2 and not label.get("derived"):
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
    equal_parts: bool = True,
    picture_budget_s: float = 0.0,
    detector: Any = None,
    pasted: Sequence[Sequence[float]] = (),
) -> PageLabels:
    """The labels of page `page` (from 1) of `pdf`. `halves` are the page map's drawings on the
    page (`{box, role}`), for each label's side. The drawn route runs only while
    `drawn_budget_s` seconds last (0: not at all); `readers` are its OCR engines (default: the
    installed ones). The picture route (#1830) reads the page's pasted pictures within
    `picture_budget_s` seconds (0: not at all), with the same engines and `detector` (default:
    RapidOCR's text detector); `pasted` are the page map's pasted pictures, for the sides of the
    labels read in them (`_picture_side`, #1835). `equal_parts`: the `EQ` parts are added last (`build_dims` adds them
    once, after its drawn pass, so no id is ever given to two labels)."""
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
        metric=_metric(phrases, halves),
    )
    if picture_budget_s > 0:
        _add_pictures(
            result,
            Path(pdf),
            page,
            halves,
            picture_budget_s,
            readers,
            detector,
            clock,
            pasted,
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
    if equal_parts:
        _add_equal_parts(result, parsed, halves, phrases)
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
    ocr_budget_s: float = 0.0,
    phrases: Sequence[Mapping[str, Any]] = (),
    sweep_budget_s: float = 0.0,
    alphabet: bool = False,
    count_ink: bool = False,
) -> PageLabels:
    """Page `page`'s text labels (as `page_labels` gave them) and its picture labels (#1830)
    with its drawn labels added after them, read within `budget_s` seconds: the pack's last
    reading pass, after every page's text and pictures. With `ocr_budget_s` (#1848), the groups
    of ink the drawn route left unlabelled are then read by the OCR route on the same reading of
    the page (`_add_ocr`, at most that many seconds): its labels are kept apart in `ocr_labels`
    (no id), for the pack to number after every other label, and `ocr` says what it did. With
    `sweep_budget_s` (#1860), `read_number` then reads the groups still unlabelled that look like
    a number (`_sweep`); with `alphabet`, the page's confirmed drawn labels are kept as samples of
    the vendor's alphabet (`alphabet_samples`).
    `count_ink`: also count the page's unread pen-stroke numbers (`PageLabels.ink`, #1862) on the
    page this pass already parsed, after every route's labels."""
    started = clock()
    result = PageLabels(
        labels=[
            dict(label)
            for label in text_labels
            if (label["route"] == "text" or label.get("origin") == picture_labels.ORIGIN)
            and not label.get("derived")
        ]
    )
    parsed = _parse(Path(pdf), page - 1)
    missed: list[ink_labels.Label] = []
    _add_drawn(result, parsed, halves, budget_s, readers, clock, missed)
    _number(result.labels)
    if ocr_budget_s > 0 and result.drawn in ("done", "partial"):
        read = PageLabels(labels=list(result.labels), phrases=[dict(p) for p in phrases])
        try:
            _add_ocr(read, parsed, halves, ocr_budget_s, readers, clock, missed)
        except Exception as failed:  # noqa: BLE001 - the page keeps its other labels
            _log.warning("dimension labels: OCR route failed: %s", type(failed).__name__)
            read.labels = list(result.labels)
            read.ocr = {"status": "failed", "spots": 0, "seconds": 0.0, "unconfirmed": []}
        result.ocr_labels = read.labels[len(result.labels) :]
        result.ocr = read.ocr
        left = sweep_budget_s - (float(read.ocr.get("seconds") or 0.0) if read.ocr else 0.0)
        if sweep_budget_s > 0 and left > 0 and read.ocr and read.ocr.get("status") != "failed":
            try:
                found, held, said = _sweep(
                    pdf, page, parsed, halves, read.labels, missed, left, readers, clock
                )
            except Exception as failed:  # noqa: BLE001 - the page keeps its other labels
                _log.warning("dimension labels: number sweep failed: %s", type(failed).__name__)
                found, held, said = [], [], {"status": "failed"}
            result.ocr_labels += found
            result.sweep_held = held
            result.ocr = {**read.ocr, "sweep": said}
    if alphabet:
        result.alphabet = alphabet_samples(parsed, result.labels, page)
    if count_ink:
        try:
            ink = import_module("app.review.assistant.reading_ink")
            result.ink = ink.page_ink(parsed, halves, [*result.labels, *result.ocr_labels])
        except Exception as failed:  # noqa: BLE001 - counting never costs the page its labels
            _log.warning("dimension labels: reading coverage failed: %s", type(failed).__name__)
    result.seconds = round(clock() - started, 2)
    return result


# ---- read_number at pack time (#1860) ---------------------------------------------------------------

#: The engines' pool for `read_number` at pack time (Tesseract runs in its own process).
SWEEP_WORKERS: Final = 4


def _number_shaped(label: ink_labels.Label) -> bool:
    """Whether a group of ink is shaped like a printed dimension by its vectors alone (no
    engine): a row ending in an inch mark (`_mark_layouts`, `_marked_turns`) or a stacked
    fraction. A word, a tag or a symbol is neither (millimetres over inches have their
    cross-check; a bare number with no mark is left out)."""
    if _mark_layouts(label) or _marked_turns(label):
        return True
    for turn in ink_labels.turns(label):
        if any(t.kind == "fraction" for row in ink_labels.rows(label, turn) for t in row):
            return True
    return False


@dataclass(slots=True)
class SweepHeld:
    """A spot `read_number` read with its OCR pair alone: kept for the vendor's alphabet (learned
    from the whole set afterwards) to second it, or not."""

    file: str
    """The PDF's file name."""
    page: int
    box: tuple[float, float, float, float]
    ocr: Decimal
    shape: Decimal | None
    ruler: float | None
    ink: Any
    turns: tuple[int, ...]


def _sweep(
    pdf: Path,
    page: int,
    parsed: _Parsed,
    halves: Sequence[Mapping[str, Any]],
    labels: Sequence[Mapping[str, Any]],
    missed: Sequence[ink_labels.Label],
    budget_s: float,
    readers: Sequence[Any] | None,
    clock: Callable[[], float],
) -> tuple[list[dict[str, Any]], list[SweepHeld], dict[str, Any]]:
    """`read_number` on every vendor group of ink the drawn route and the OCR route left
    unlabelled that its vectors shape like a dimension (`_number_shaped`), within `budget_s` seconds, `SWEEP_WORKERS` at a time. Its rule
    stands: two families agreeing, or one reader the ruler seconds; the ruler blocks a reading
    its line disagrees with. A reading is a label after every other one (`origin: "ocr"`, no
    dimension line, `read_by` its routes). A spot only the OCR pair read is held for the vendor's
    alphabet (`SweepHeld`)."""
    from concurrent.futures import ThreadPoolExecutor

    read_number = import_module("app.review.assistant.read_number")
    view_scales = import_module("app.review.assistant.view_scales")
    started = clock()
    engines = list(drawn_readers() if readers is None else readers)
    if len({str(e.name) for e in engines}) < 2:
        return [], [], {"status": "no_engine", "spots": 0, "read": 0}
    taken = [tuple(label["box"]) for label in labels]
    lines = _line_ends(parsed.segments)
    spots: list[ink_labels.Label] = []
    looked: list[Box] = []
    for raw in sorted(missed, key=lambda p: (-len(p.glyphs), p.box[1], p.box[0])):
        if _side(raw.box, halves) != "vendor":
            continue
        if any(_overlap(raw.box, other) > 0.3 for other in [*taken, *looked]):
            continue
        looked.append(raw.box)
        # A number's size and at least two glyphs (a value and its mark), line work left out.
        piece = _without_line_work(raw, lines)
        if piece is None or ocr_labels.spot_of(piece) is None:
            continue
        if clock() - started > budget_s:
            break
        if _number_shaped(piece):
            spots.append(raw)
    read_number.seed_page(pdf, page, parsed)
    scales, _checks = view_scales.page_scales(page, labels)
    file = Path(pdf).name

    def one(piece: ink_labels.Label) -> Any:
        if clock() - started > budget_s:
            return None
        box = tuple(float(v) for v in piece.box)
        request = read_number.Request(file, page, "vendor", box=box)
        return read_number.read_number(
            request, Path(pdf), labels=labels, halves=halves, scales=scales, engines=engines
        )

    with ThreadPoolExecutor(max_workers=SWEEP_WORKERS) as pool:
        found = list(pool.map(one, spots))
    out: list[dict[str, Any]] = []
    held: list[SweepHeld] = []
    for piece, reading in zip(spots, found, strict=True):
        if reading is None:
            continue
        routes = {route.name: route.value for route in reading.routes}
        if reading.outcome == "read" and reading.value is not None and reading.box:
            value = Decimal(str(reading.value))
            box = tuple(float(v) for v in reading.box)
            turns = read_number._turns(piece)
            sideways = bool(turns) and all(t != 0 for t in turns)
            label = _label(
                _Reading(
                    text=vv._inches_text(value),
                    inches=value,
                    box=box,  # type: ignore[arg-type]
                    along=(0.0, 1.0) if sideways else (1.0, 0.0),
                    font=min(box[2] - box[0], box[3] - box[1]),
                    bare=False,
                    route="drawn",
                ),
                None,
                "vendor",
            )
            label["origin"] = ocr_labels.ORIGIN
            label["read_by"] = list(reading.agreed)
            out.append(label)
        elif reading.code == "one_route" and routes.get("ocr"):
            ruler = routes.get("ruler")
            held.append(
                SweepHeld(
                    file,
                    page,
                    tuple(float(v) for v in piece.box),  # type: ignore[arg-type]
                    Decimal(str(routes["ocr"])),
                    None if routes.get("shape") is None else Decimal(str(routes["shape"])),
                    None if ruler is None else float(ruler),
                    piece,
                    tuple(read_number._turns(piece)),
                )
            )
    said = {
        "status": "done" if clock() - started <= budget_s else "partial",
        "spots": len(spots),
        "read": len(out),
        "seconds": round(clock() - started, 2),
    }
    return out, held, said


def seconded(
    held: Sequence[SweepHeld], alphabet: vendor_alphabet.Alphabet
) -> list[tuple[SweepHeld, Decimal]]:
    """The held spots the vendor's alphabet seconds (`read_number.decide` with the alphabet beside
    the OCR pair), with their value."""
    read_number = import_module("app.review.assistant.read_number")
    out: list[tuple[SweepHeld, Decimal]] = []
    for item in held:
        values = {v for t in item.turns if (v := alphabet.read(item.ink, t)) is not None}
        if len(values) != 1:
            continue
        decided = read_number.decide(
            ocr=item.ocr, shape=item.shape, ruler=item.ruler, alphabet=values.pop()
        )
        if decided.value is not None:
            out.append((item, decided.value))
    return out


# ---- the picture route (#1830) -------------------------------------------------------------------


def _picture_reading(found: picture_labels.PictureLabel) -> _Reading:
    return _Reading(
        text=found.text,
        inches=found.inches,
        box=found.box,
        along=found.along,
        font=found.font,
        bare=found.kind == "bare",
        bracket=found.kind == "bracket" or found.mm is not None,
        mm=found.mm,
        units=None if found.mm is None else units_check(found.text),
        route="drawn",
    )


def _add_pictures(
    result: PageLabels,
    pdf: Path,
    page: int,
    halves: Sequence[Mapping[str, Any]],
    budget_s: float,
    readers: Sequence[Any] | None,
    detector: Any,
    clock: Callable[[], float],
    pasted: Sequence[Sequence[float]] = (),
) -> None:
    """The picture route: the page's pasted pictures read by two OCR engines
    (`picture_labels`). A confirmed reading is a label like any other (the OCR trust class,
    `route: "drawn"`, marked `origin: "picture"`, with no dimension line), added after the labels
    already listed, unless the page's own text already prints a label or phrase there; a
    millimetre value is a label only over its bracketed inches with both agreeing (`_paired`,
    `units_check`). Every other reading is listed in `pictures.unconfirmed`, never a label. A
    reading inside one of the page map's pasted pictures (`pasted`) takes that picture's heading's
    side or none (`_picture_side`, #1835); one in a picture inside a pasted drawing (a scan, a
    photo) takes the drawing's, as any label."""
    engines = list(drawn_readers() if readers is None else readers)
    try:
        read = picture_labels.read_page(
            pdf, page - 1, engines, detector=detector, budget_s=budget_s, clock=clock
        )
    except Exception as failed:  # noqa: BLE001 - the page keeps its other labels
        _log.warning("dimension labels: picture route failed: %s", type(failed).__name__)
        result.pictures = {"status": "failed", "boxes": [], "unconfirmed": []}
        return
    if not read.boxes:
        return
    taken = [tuple(label["box"]) for label in result.labels] + [
        tuple(phrase["box"]) for phrase in result.phrases
    ]
    fresh = [
        found
        for found in read.labels
        if not any(_overlap(found.box, other) > 0.5 for other in taken)
    ]
    added, unconfirmed = _read_labels(
        fresh,
        lambda box: _side(box, halves, pasted),
        picture_labels.ORIGIN,
        picture_labels.UNCONFIRMED,
    )
    result.labels += added[: max(0, MAX_LABELS - len(result.labels))]
    result.pictures = {
        "status": read.status,
        "boxes": [[round(v, 2) for v in box] for box in read.boxes],
        "unconfirmed": unconfirmed,
    }


def _read_labels(
    found: Sequence[picture_labels.PictureLabel],
    side_of: Callable[[Box], str],
    origin: str,
    unconfirmed_why: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Numbers two OCR engines read (a pasted picture's, `picture_labels`, or a vector spot's,
    `ocr_labels`) as labels and the unconfirmed rest. A confirmed reading is a label like any other
    (the OCR trust class, `route: "drawn"`, marked `origin`, with no dimension line); a millimetre
    value is a label only over its bracketed inches with both agreeing (`_paired`,
    `units_check`). Every other reading is listed as unconfirmed (`{text, box, orientation, side,
    why}`), never a label."""
    confirmed = [item for item in found if item.confirmed]
    joined, _any_pair = _paired([_picture_reading(item) for item in confirmed])
    unconfirmed: list[dict[str, Any]] = [
        {
            "text": item.text,
            "box": list(item.box),
            "orientation": item.orientation,
            "side": side_of(item.box),
            "why": f"{unconfirmed_why} ({item.why})",
        }
        for item in found
        if not item.confirmed
    ]
    added: list[dict[str, Any]] = []
    for reading in joined:
        if reading.bare or (reading.bracket and reading.mm is None):
            continue  # a lone millimetre number, or inches in brackets with no millimetres
        if reading.mm is not None and reading.units != "agrees":
            unconfirmed.append(
                {
                    "text": reading.text,
                    "box": [round(v, 2) for v in reading.box],
                    "orientation": _orientation(reading.along),
                    "side": side_of(reading.box),
                    "why": f"{unconfirmed_why} (the millimetres and inches disagree)",
                }
            )
            continue
        # No dimension line: an OCR reading never passes a check on a line.
        label = _label(reading, None, side_of(reading.box))
        label["origin"] = origin
        added.append(label)
    added.sort(key=lambda label: (round(label["box"][1] / 4), label["box"][0]))
    return added, unconfirmed


def picture_pass(
    pdf: Path,
    page: int,
    halves: Sequence[Mapping[str, Any]],
    labels: Sequence[Mapping[str, Any]],
    phrases: Sequence[Mapping[str, Any]],
    *,
    budget_s: float,
    readers: Sequence[Any] | None = None,
    detector: Any = None,
    clock: Callable[[], float] = monotonic,
    pasted: Sequence[Sequence[float]] = (),
) -> PageLabels:
    """Page `page`'s labels (as the text pass gave them) with the labels read in its pasted
    pictures added after them (#1830): the pack's pass between the text and the drawn ink.
    `pasted`: the page map's pasted pictures (`_picture_side`, #1835)."""
    result = PageLabels(
        labels=[dict(label) for label in labels],
        phrases=[dict(phrase) for phrase in phrases],
    )
    _add_pictures(result, Path(pdf), page, halves, budget_s, readers, detector, clock, pasted)
    _number(result.labels)
    return result


# ---- the OCR route (#1848) -----------------------------------------------------------------------


def _ocr_spots(
    parsed: _Parsed,
    halves: Sequence[Mapping[str, Any]],
    taken: Sequence[Box],
    missed: Sequence[ink_labels.Label] | None = None,
    vendor_pieces: list[ink_labels.Label] | None = None,
) -> tuple[list[tuple[Box, list[ocr_labels.Spot]]], list[Box]]:
    """The page's vector ink no label and no phrase covers, as spots grouped by the page map's
    drawing they are on (`(drawing box, spots)`), and every group's box (kept from the line work's
    whitening). A spot is a group of the ink split by `ink_labels` (the dimension line's own
    pieces left out, `_without_line_work`), of a number's size, on one of the drawings, with room
    round it (`_crowded`: a part of a longer number or of a word is never read alone). `missed`:
    the groups the drawn route looked at and left unlabelled (`_add_drawn`), so the page's ink is
    not grouped twice; without it the page's ink is grouped here. `vendor_pieces` (when given) is
    filled with every group on a vendor drawing that nothing covers (for `_mm_spots`)."""
    if vendor_pieces is None:
        vendor_pieces = []
    page = parsed.page
    lines = _line_ends(parsed.segments)
    every: list[Box] = []
    pieces: list[ink_labels.Label] = list(missed or ())
    vendor = [h["box"] for h in halves if h.get("role") == "vendor" and h.get("box")]
    clusters, lone = _clusters(page.paths, taken) if missed is None else ([], [])
    for cluster in [*clusters, *lone]:
        if not any(_within(_centre(cluster), box) for box in vendor):
            continue  # read only on the vendor's drawings (below): no time spent elsewhere
        near = vv._ink_paths_near(page, cluster)
        if len(near) > _MAX_NEAR_PATHS:
            near = [p for p in near if _overlap(p.box, cluster) > 0]
        pieces += ink_labels.labels(near, cluster)
    # A cluster's edge may cut a label (`18"` without its second tick), and a fill's dots may join
    # one: where two pieces share most of their ink, the one whose vectors end it in an inch mark
    # is read, else the one with the most glyphs.
    found: list[tuple[ink_labels.Label, ocr_labels.Spot]] = []
    vendor_pieces.clear()
    for piece in pieces:
        every.append(piece.box)
        if _side(piece.box, halves) == "vendor" and not any(
            _overlap(piece.box, o) > 0.1 for o in taken
        ):
            vendor_pieces.append(piece)
        bare = _without_line_work(piece, lines)
        if bare is None:
            continue
        spot = ocr_labels.spot_of(
            bare, _mark_layouts(bare), lambda turn, label=bare: _one_row_layout(label, turn)
        )
        # Only a row of digits the vectors end in an inch mark (two twin ticks) is read: a word, a
        # tag, a symbol or hatching never is, so the engines read a few spots a page, not dozens.
        if spot is not None and spot.marked and not any(_overlap(spot.box, o) > 0.1 for o in taken):
            found.append((bare, spot))
    found.sort(
        key=lambda item: (not item[1].marked, -len(item[0].glyphs), item[1].box[1], item[1].box[0])
    )
    seen: list[Box] = []
    by_drawing: dict[int, list[ocr_labels.Spot]] = {}
    drawings = [h for h in halves if h.get("box") and len(h["box"]) == 4 and not h.get("picture")]
    for bare, spot in found:
        if any(_overlap(spot.box, other) > 0.5 for other in seen):
            continue
        seen.append(spot.box)
        centre = _centre(spot.box)
        holding = next((i for i, h in enumerate(drawings) if _within(centre, h["box"])), None)
        # The vendor's drawings only: the architect's numbers are the reference (a misread one
        # would set the wrong target) and are text or hand lettering, which have their own routes.
        if holding is None or _side(spot.box, halves) != "vendor":
            continue
        if _crowded(page, bare):
            continue
        by_drawing.setdefault(holding, []).append(spot)
    areas: list[tuple[Box, list[ocr_labels.Spot]]] = []
    count = 0
    for index in sorted(by_drawing):
        spots = by_drawing[index][: max(0, ocr_labels.MAX_SPOTS - count)]
        count += len(spots)
        if spots:
            box = tuple(float(v) for v in drawings[index]["box"])
            areas.append((box, spots))  # type: ignore[arg-type]
    return areas, every


def _mark_layouts(label: ink_labels.Label) -> tuple[tuple[int, _Layout], ...]:
    """`(turn, layout)` for each direction in which the label is one row of digits (and at most
    one stacked fraction) ending in an inch mark drawn as two twin ticks (`_twin_ticks`), its
    digits counted by the vectors (`_layout`). A row with any other mark (a foot mark, a dash:
    `7'-3"`) is none: its digits alone are not its value."""
    out: list[tuple[int, _Layout]] = []
    for turn in _marked_turns(label) or tuple(t for t in (0, 90, 270) if _end_ticks(label, t)):
        rows = ink_labels.rows(label, turn)
        if len(rows) != 1:
            continue
        row = rows[0]
        if _twin_ticks(row, turn):
            end = len(row) - ink_labels.inch_mark_at_end(row, turn)
        elif _end_ticks(label, turn):
            end = len(row) - 2
        else:
            continue
        if any(token.kind == "mark" for token in row[:end]):
            continue
        layout = _layout(row, turn)
        if layout is not None:
            out.append((int(turn), layout))
    return tuple(out)


def _one_row_layout(label: ink_labels.Label, turn: int) -> _Layout | None:
    """The label's layout (`_layout`) read as `turn` when it is one row, else `None`."""
    rows = ink_labels.rows(label, turn)  # type: ignore[arg-type]
    return _layout(rows[0], turn) if len(rows) == 1 else None  # type: ignore[arg-type]


def _fits_vectors(spot: ocr_labels.Spot, turn: int, text: str, parsed: tuple[str, Decimal]) -> bool:
    """Whether a reading of a spot fits its vectors in that direction: the row is one the vectors
    lay out (`_layout`), the reading has as many digits as the vectors draw characters, and a
    reading in inches is the value its digits give laid out as the vectors draw them
    (`_laid_out`: `21 3/8"` with a stacked fraction read `213"` does not fit). No layout, no
    fit: an OCR reading is a label only where the vectors confirm its digits."""
    layout = dict(spot.layouts).get(turn)
    if layout is None:
        return False
    plain = vv._plain(text.translate(picture_labels._PRIMES))
    if sum(ch.isdigit() for ch in plain) != sum(layout):
        return False
    kind, value = parsed
    if kind == "feet":
        return True  # a foot mark and a dash are marks: the digit count is what the vectors give
    return _laid_out(plain, layout) == value


def _end_ticks(label: ink_labels.Label, turn: int) -> bool:
    """Whether the label, read as `turn`, is one row ending in two small marks of the same size,
    side by side with their tops level, in the row's upper half: an inch mark drawn as two outlined
    ticks (each its own closed outline, so `_twin_ticks`, which wants two single strokes, does not
    see it)."""
    rows = ink_labels.rows(label, turn)  # type: ignore[arg-type]
    if len(rows) != 1 or len(rows[0]) < 3:
        return False
    row = rows[0]
    if [t.kind for t in row[-2:]] != ["mark", "mark"] or any(t.kind == "mark" for t in row[:-2]):
        return False
    frames = [ink_labels._frame(t.glyphs[0].box, turn) for t in row]  # type: ignore[arg-type]
    top = min(f[1] for f in frames)
    bottom = max(f[3] for f in frames)
    (a0, a1, a2, a3), (b0, b1, b2, b3) = frames[-2:]
    size = max(a3 - a1, b3 - b1)
    return (
        size > 0
        and a3 - a1 >= a2 - a0
        and b3 - b1 >= b2 - b0
        and abs((a3 - a1) - (b3 - b1)) <= 0.25 * size
        and abs(a1 - b1) <= 0.25 * size
        and b0 >= a2 - 0.1 * size
        and max(a3, b3) <= top + 0.6 * (bottom - top)
    )


_MARKED_LINE: Final = re.compile(r'^[\d /"]+$')


def _marked_value(spot: ocr_labels.Spot, turn: int, text: str) -> Decimal | None:
    """A reading with the drawn inch mark dropped or read as `9`s (`41`, `4199`), as inches: only
    in a direction in which the vectors draw that mark, and only when the line is digits (a space,
    a slash or the mark at most) and their count is the vectors' (`_laid_out`)."""
    layout = dict(spot.marked).get(turn)
    if layout is None:
        return None
    line = _mark_as_nines(vv._plain(text.translate(picture_labels._PRIMES)).strip(), layout)
    if not _MARKED_LINE.fullmatch(line) or '"' in line.rstrip('"'):
        return None
    value = _laid_out(line, layout)
    return value if value is not None and value > 0 else None


def _add_ocr(
    result: PageLabels,
    parsed: _Parsed,
    halves: Sequence[Mapping[str, Any]],
    budget_s: float,
    readers: Sequence[Any] | None,
    clock: Callable[[], float],
    missed: Sequence[ink_labels.Label] | None = None,
) -> None:
    """The OCR route (`ocr_labels`): the numbers the label readers missed on the page's drawings,
    read at their spots by two OCR engines. A confirmed reading is a label (`origin: "ocr"`, no
    dimension line), added after every label already listed; the rest are listed in
    `ocr.unconfirmed`, never labels."""
    started = clock()
    taken = [tuple(label["box"]) for label in result.labels] + [
        tuple(phrase["box"]) for phrase in result.phrases
    ]
    pieces: list[ink_labels.Label] = []
    areas, every = _ocr_spots(parsed, halves, taken, missed, pieces)
    dual = _mm_spots(pieces)
    spots = sum(len(found) for _box, found in areas) + len(dual)
    if not spots:
        seconds = round(clock() - started, 2)
        result.ocr = {"status": "done", "spots": 0, "seconds": seconds, "unconfirmed": []}
        return
    engines = list(drawn_readers() if readers is None else readers)
    page = parsed.page
    crossed = _mm_crossed(dual, engines, started + budget_s, clock)
    read = ocr_labels.read_spots(
        page.vendor_pdf,
        page.size,
        areas,
        [*taken, *every],
        engines,
        marked=_marked_value,
        fits=_fits_vectors,
        deadline=started + budget_s,
        clock=clock,
    )
    added, unconfirmed = _read_labels(
        [*crossed, *read.labels],
        lambda box: _side(box, halves),
        ocr_labels.ORIGIN,
        ocr_labels.UNCONFIRMED,
    )
    result.labels += added[: max(0, MAX_LABELS - len(result.labels))]
    result.ocr = {
        "status": read.status,
        "spots": read.spots + len(dual),
        "seconds": round(clock() - started, 2),
        "unconfirmed": unconfirmed,
    }


# ---- the vendor's alphabet (#1860) -------------------------------------------------------------------


def alphabet_samples(
    parsed: _Parsed, labels: Sequence[Mapping[str, Any]], page: int
) -> list[vendor_alphabet.Sample]:
    """The characters of the page's confirmed drawn vendor labels as learned samples
    (`vendor_alphabet.samples_of`): read by the drawn route's two engines (never an OCR reading
    of pixels, never millimetres, never a derived part)."""
    out: list[vendor_alphabet.Sample] = []
    for label in labels:
        if (
            label.get("route") != "drawn"
            or label.get("origin")
            or label.get("derived")
            or label.get("mm")
            or label.get("side") != "vendor"
        ):
            continue
        try:
            value = Decimal(str(label["inches"]))
            box = tuple(float(v) for v in label["box"])
        except (KeyError, TypeError, ValueError, ArithmeticError):
            continue
        near = vv._ink_paths_near(parsed.page, box)  # type: ignore[arg-type]
        found = [g for g in ink_labels.labels(near, box) if _overlap(g.box, box) > 0.5]  # type: ignore[arg-type]
        if not found:
            continue
        ink = found[0]
        if label.get("orientation") == "h":
            turns: tuple[int, ...] = (0,)
        else:
            turns = tuple(t for t in (_marked_turns(ink) or ink_labels.turns(ink)) if t != 0)
        if len(turns) != 1:
            continue
        out += vendor_alphabet.samples_of(ink, turns[0], value, page)
    return out


# ---- millimetres over inches, cross-checked (#1860) ------------------------------------------------

#: A millimetre or inch reading must come back the same at least this many times (two engines,
#: two sizes, the inch row with and without its brackets) to be one side of the cross-check.
MM_CROSS_READS: Final = 2
_INCH_CORE: Final = re.compile(r"(\d+(?:\s+\d+/\d+)?|\d+/\d+)\s*\"?")


def _mm_spots(pieces: Sequence[ink_labels.Label]) -> list[tuple[ink_labels.Label, ink_labels.Turn]]:
    """The groups of vendor ink that are millimetres over inches in brackets (`_mm_rows`), each
    with its reading direction; where two share most of their ink, the one with more glyphs."""
    out: list[tuple[ink_labels.Label, ink_labels.Turn]] = []
    for piece in sorted(pieces, key=lambda p: (-len(p.glyphs), p.box[1], p.box[0])):
        if any(_overlap(piece.box, other.box) > 0.5 for other, _turn in out):
            continue
        turn = _mm_rows(piece)
        if turn is not None:
            out.append((piece, turn))
    return out


def _mm_cross(
    label: ink_labels.Label, turn: ink_labels.Turn, engines: Sequence[Any]
) -> tuple[Decimal, str] | None:
    """Millimetres over bracketed inches read by the cross-check (#1860), or `None`: each row
    re-typeset and read by each engine at two sizes, the inch row also with its brackets dropped
    (an engine may read nothing with them); a millimetre reading (two to five digits, no leading
    zero) and an inch reading each come back the same at least `MM_CROSS_READS` times, and exactly
    one millimetre value and one inch value agree with each other (`units_check`): the two rows
    are independent witnesses of one value, so a misread on either row is caught by the other."""
    rows = [_merged(row, turn) for row in ink_labels.rows(label, turn)]
    if len(rows) != 2:
        return None
    upper, lower = rows
    mains = [i for i, t in enumerate(lower) if t.kind != "mark"]
    core = lower[mains[0] + 1 : mains[-1]] if len(mains) >= 3 else []
    mm_reads: dict[str, int] = {}
    inch_reads: dict[Decimal, int] = {}
    for reader in engines:
        for height in ink_labels.HEIGHTS_PX:
            for row, kind in ((upper, "mm"), (lower, "bracket"), (core, "core")):
                picture = ink_labels.row_picture(row, turn, height) if row else None
                if picture is None:
                    continue
                line = " ".join(reader(picture, single_line=True)).strip()
                if kind == "mm":
                    if _MM_LINE.fullmatch(line) and not line.startswith("0"):
                        mm_reads[line] = mm_reads.get(line, 0) + 1
                    continue
                found = (
                    _BRACKET_LINE.fullmatch(line)
                    if kind == "bracket"
                    else _INCH_CORE.fullmatch(vv._plain(line).strip())
                )
                value = None if found is None else to_inches(found.group(1) + '"')
                if value is not None and 0 < value <= _BARE_MAX_IN:
                    inch_reads[value] = inch_reads.get(value, 0) + 1
    pairs = {
        (mm, value)
        for mm, seen in mm_reads.items()
        if seen >= MM_CROSS_READS
        for value, times in inch_reads.items()
        if times >= MM_CROSS_READS
        and units_check(f"{mm} [{vv._inches_text(value).rstrip(chr(34))}]") == "agrees"
    }
    if len(pairs) != 1:
        return None
    mm, value = pairs.pop()
    return value, mm


def _mm_crossed(
    dual: Sequence[tuple[ink_labels.Label, ink_labels.Turn]],
    engines: Sequence[Any],
    deadline: float,
    clock: Callable[[], float],
) -> list[picture_labels.PictureLabel]:
    """The millimetre-over-inch spots the cross-check reads, as confirmed readings."""
    out: list[picture_labels.PictureLabel] = []
    if len({str(reader.name) for reader in engines}) < 2:
        return out
    for label, turn in dual:
        if clock() > deadline:
            break
        try:
            found = _mm_cross(label, turn, engines)
        except Exception as failed:  # noqa: BLE001 - an unread spot is left out
            _log.warning("dimension labels: mm cross-check failed: %s", type(failed).__name__)
            continue
        if found is None:
            continue
        value, mm = found
        box = tuple(round(v, 2) for v in label.box)
        inches = vv._inches_text(value).rstrip(chr(34))
        out.append(
            picture_labels.PictureLabel(
                text=f"{mm} [{inches}]",
                inches=value,
                box=box,  # type: ignore[arg-type]
                turn=int(turn),
                font=round(min(box[2] - box[0], box[3] - box[1]), 2),
                kind=f"mm {mm}",
                confirmed=True,
                reads=(),
            )
        )
    return out


def ocr_pass(
    pdf: Path,
    page: int,
    halves: Sequence[Mapping[str, Any]],
    labels: Sequence[Mapping[str, Any]],
    phrases: Sequence[Mapping[str, Any]],
    *,
    budget_s: float,
    readers: Sequence[Any] | None = None,
    clock: Callable[[], float] = monotonic,
) -> PageLabels:
    """Page `page`'s labels (every route's, `EQ` parts included) with the numbers the OCR route
    confirms added after them (#1848): the pack's last reading pass, so no older label's id moves.
    Labels are numbered on from the last one; nothing before them is renumbered."""
    result = PageLabels(
        labels=[dict(label) for label in labels],
        phrases=[dict(phrase) for phrase in phrases],
    )
    before = len(result.labels)
    try:
        _add_ocr(result, _parse(Path(pdf), page - 1), halves, budget_s, readers, clock)
    except Exception as failed:  # noqa: BLE001 - the page keeps its other labels
        _log.warning("dimension labels: OCR route failed: %s", type(failed).__name__)
        result.labels = result.labels[:before]
        result.ocr = {"status": "failed", "spots": 0, "seconds": 0.0, "unconfirmed": []}
        return result
    for number, label in enumerate(result.labels[before:], start=before + 1):
        label["id"] = f"D{number}"
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
        if reading.bare and _in_ring(reading, parsed.rings):
            continue
        kept.append((reading, line))
    kept.sort(key=lambda item: (round(item[0].box[1] / 4), item[0].box[0]))
    return [_label(r, line, _side(r.box, halves)) for r, line in kept[:MAX_LABELS]]


type _Taken = tuple[str, float, float, float]
"""A span another label is centred on: `(orientation, across, start, end)`."""


def _stroke_key(a: Point, b: Point) -> tuple[tuple[float, float], tuple[float, float]]:
    """A straight stroke, either way round, to a tenth of a point."""
    p = (round(a[0], 1), round(a[1], 1))
    q = (round(b[0], 1), round(b[1], 1))
    return (p, q) if p <= q else (q, p)


def _own_strokes(label: ink_labels.Label) -> set[tuple[tuple[float, float], tuple[float, float]]]:
    """The straight strokes of a drawn label's own glyphs (its paths' polylines)."""
    own: set[tuple[tuple[float, float], tuple[float, float]]] = set()
    for glyph in label.glyphs:
        for path in glyph.paths:
            for line in getattr(path, "polylines", ()):
                own.update(_stroke_key(a, b) for a, b in pairwise(line) if a != b)
    return own


def _without_own(
    segments: Sequence[Segment],
    own: set[tuple[tuple[float, float], tuple[float, float]]],
    orientation: str,
) -> list[Segment]:
    """`segments` without the label's own strokes that run along `orientation` (h or v)."""
    out: list[Segment] = []
    for a, b in segments:
        dx, dy = abs(b[0] - a[0]), abs(b[1] - a[1])
        along = dy <= 0.02 * dx if orientation == "h" else dx <= 0.02 * dy
        if along and _stroke_key(a, b) in own:
            continue
        out.append((a, b))
    return out


def _line_of(
    parsed: _Parsed, part: _Part, taken: Sequence[_Taken] = ()
) -> tuple[dict[str, Any], str] | None:
    """A drawn label's dimension line and its orientation (`dimension_line`, `drawn=True`), or
    `None`. `taken`: spans another label is centred on."""
    box = part.label.box
    # A dimension's number reads along its line (upright on a horizontal line, up or down the
    # page on a vertical one): that line is looked for first.
    turns = (part.turn,) if part.turn is not None else _marked_turns(part.label)
    ways: tuple[tuple[str, int], ...] = (("h", 3), ("v", 2))
    if turns and all(t != 0 for t in turns):
        ways = (("v", 2), ("h", 3))
    segments: Sequence[Segment] = parsed.segments
    if part.kind == "mm":
        ways = ways[:1]
        # The line runs between the millimetres and the inches, so the label's own strokes
        # (the brackets, a digit's foot) touch it: they are left out, and the line is looked
        # for along the reading direction only.
        x0, y0, x1, y1 = box
        segments = [
            (a, b)
            for a, b in parsed.segments
            if not all(
                x0 - 0.3 <= q[0] <= x1 + 0.3 and y0 - 0.3 <= q[1] <= y1 + 0.3 for q in (a, b)
            )
        ]
    # A label's own strokes along a line are never that line (#1629: a drawn `4"`'s upright, a
    # few points long, was taken for its chain's line); its ticks and crossings still cut it.
    own = _own_strokes(part.label) if part.kind != "mm" else set()
    spot = part.spot or box
    for orientation, axis in ways:
        font = spot[axis] - spot[axis - 2]
        found = dimension_line(
            spot,
            orientation,
            _without_own(segments, own, orientation) if own else segments,
            font,
            drawn=True,
            taken=[(t[1], t[2], t[3]) for t in taken if t[0] == orientation],
        )
        if found is not None:
            return found, orientation
    return None


def _span(label: Mapping[str, Any]) -> tuple[str, float, float, float, float] | None:
    """A label's dimension line as `(orientation, across, start, end, how far its middle is from
    the line's middle, as a share of half the line)`, or `None`."""
    line, orientation = label.get("dim_line"), label.get("orientation")
    if not line or orientation not in ("h", "v"):
        return None
    (x0, y0), (x1, y1) = line
    box = label["box"]
    if orientation == "h":
        start, end, across, middle = min(x0, x1), max(x0, x1), (y0 + y1) / 2, (box[0] + box[2]) / 2
    else:
        start, end, across, middle = min(y0, y1), max(y0, y1), (x0 + x1) / 2, (box[1] + box[3]) / 2
    half = max((end - start) / 2, 1e-6)
    return orientation, across, start, end, abs(middle - (start + end) / 2) / half


def _settle_lines(
    parsed: _Parsed, found: list[tuple[dict[str, Any], _Part]], others: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """One dimension line, one label: when two labels found the same line, the one centred on it
    keeps it and the other looks again past it (a chain's `3/4"` stacked over its ticks finds the
    overall's line first). A drawn label left with no line of its own is dropped. A label never
    takes back a line it has given up (#1629: a `3/4"` between a chain's line and its overall's
    swung from one to the other each round and kept a neighbour's)."""
    ceded: dict[int, list[_Taken]] = {}
    for _round in range(3):
        spans = [(_span(label), label) for label in [*others, *(f[0] for f in found)]]
        moved = False
        kept: list[tuple[dict[str, Any], _Part]] = []
        for label, part in found:
            mine = _span(label)
            if mine is None:
                continue
            rivals = [
                (span, other)
                for span, other in spans
                if span is not None
                and other is not label
                and span[0] == mine[0]
                and abs(span[1] - mine[1]) <= 0.6
                and abs(span[2] - mine[2]) <= 0.6
                and abs(span[3] - mine[3]) <= 0.6
                and span[4] < mine[4]
            ]
            if not rivals:
                kept.append((label, part))
                continue
            taken = ceded.setdefault(id(label), [])
            taken += [(s[0], s[1], s[2], s[3]) for s, _ in rivals]
            again = _line_of(parsed, part, taken)
            moved = True
            if again is None:
                continue
            line, orientation = again
            label.update(
                dim_line=line["dim_line"],
                extension=line["extension"],
                span_pt=line["span_pt"],
                orientation=orientation,
            )
            kept.append((label, part))
        found = kept
        if not moved:
            break
    return [label for label, _part in found]


def _drawn_label(
    parsed: _Parsed, part: _Part, engines: Sequence[Any], side: str
) -> dict[str, Any] | None:
    """One part of a drawn label read as a label, or `None`: room round it (`_crowded`), a
    dimension line of its own (`_line_of`), and its value read (`_drawn_value`, or `_mm_value`
    for millimetres over inches; a note after the value must read as words only)."""
    label = part.label
    if _crowded(parsed.page, label, part.also, part.turn):
        return None
    line = _line_of(parsed, part)
    if line is None:
        return None
    found, orientation = line
    box = label.box
    width, height = box[2] - box[0], box[3] - box[1]
    mm: str | None = None
    try:
        if part.kind == "mm":
            both = _mm_value(part, engines)
            value = None if both is None else both[0]
            mm = None if both is None else both[1]
        else:
            value = _drawn_value(
                _label_reads(label, engines, part.turn),
                lambda v: _vector_ones(label, v, part.turn),
            )
            if value is not None and part.note and not _note_is_words(part, engines):
                value = None
    except Exception as failed:  # noqa: BLE001 - an unread label is left out
        _log.warning("dimension labels: drawn read failed: %s", type(failed).__name__)
        return None
    if value is None:
        return None
    text = vv._inches_text(value)
    if mm is not None:
        text = f"{mm} [{text.rstrip(chr(34))}]"
    reading = _Reading(
        text=text,
        inches=value,
        box=box,
        along=(1.0, 0.0) if orientation == "h" else (0.0, 1.0),
        font=min(width, height),
        bare=False,
        bracket=mm is not None,
        mm=mm,
        units=None if mm is None else units_check(text),
        route="drawn",
    )
    return _label(reading, found, side)


def _add_drawn(
    result: PageLabels,
    parsed: _Parsed,
    halves: Sequence[Mapping[str, Any]],
    budget_s: float,
    readers: Sequence[Any] | None,
    clock: Callable[[], float],
    missed: list[ink_labels.Label] | None = None,
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

    def side_of_box(box: Box) -> str:
        return _side(box, halves)

    skip = [tuple(label["box"]) for label in labels]
    seen: list[Box] = []
    drawn: list[tuple[dict[str, Any], _Part]] = []
    clusters, lone = _clusters(parsed.page.paths, skip)
    lines = _line_ends(parsed.segments)
    for cluster in [*clusters, *lone]:
        if cluster in lone and any(_overlap(cluster, other) > 0.5 for other in seen):
            continue  # a lone path already read as part of a label
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
                if missed is not None and side_of_box(piece.box) == "vendor":
                    missed.append(piece)  # the OCR route's (#1848): the vectors never closed
                continue
            box = label.box
            # Clusters gather their neighbours' ink too: a label is read once.
            if any(_overlap(box, other) > 0.5 for other in seen):
                continue
            seen.append(box)
            side = _side(box, halves)
            if side != "vendor":
                continue
            bare = _without_line_work(label, lines)
            if bare is None:
                continue
            read = False
            for part in _parts(bare):
                found_label = _drawn_label(parsed, part, engines, side)
                if found_label is not None:
                    drawn.append((found_label, part))
                    read = True
            if not read and missed is not None:
                missed.append(label)  # the OCR route's (#1848): no part of it was read
        if result.drawn == "partial" or len(labels) + len(drawn) >= MAX_LABELS:
            break
    if result.drawn != "partial":
        _add_lettered(result, parsed, halves, engines, budget_s - (clock() - started), clock, drawn)
    settled = _settle_lines(parsed, drawn, labels)
    settled.sort(key=lambda label: (round(label["box"][1] / 4), label["box"][0]))
    labels += settled


def _lettered_value(item: hand_labels.Lettered, engines: Sequence[Any]) -> Decimal | None:
    """The one value two engines read a hand-lettered dimension as, or `None`: each row picture
    (`hand_labels.tokens`, at two sizes) read by each engine, every reading laid out by the vectors
    (`hand_labels.value`; a reading the vectors refuse counts for nothing). Both engines must give
    a value, and every value given must be the same."""
    found: dict[str, set[Decimal]] = {}
    row = hand_labels.tokens(item)
    for reader in engines:
        mark = getattr(reader, "reads_inch_marks", True)
        for height in ink_labels.HEIGHTS_PX:
            picture = ink_labels.row_picture(row, item.turn, height, inch_mark=mark)
            if picture is None:
                continue
            line = " ".join(reader(picture, single_line=True)).strip()
            got = hand_labels.value(item, line)
            if got is not None:
                found.setdefault(reader.name, set()).add(got)
    every = {value for values in found.values() for value in values}
    if len(found) < 2 or len(every) != 1:
        return None
    return every.pop()


def _lettered_label(
    parsed: _Parsed, item: hand_labels.Lettered, engines: Sequence[Any]
) -> tuple[dict[str, Any], _Part] | None:
    """One hand-lettered dimension as a label, or `None`: room round it (`_crowded`), a dimension
    line of its own running the way it reads (`_line_of`), and one value two engines read
    (`_lettered_value`)."""
    part = _Part(item.label, turn=item.turn)
    if _crowded(parsed.page, item.label, (), item.turn):
        return None
    line = _line_of(parsed, part)
    if line is None or line[1] != ("h" if item.turn == 0 else "v"):
        return None
    found, orientation = line
    try:
        value = _lettered_value(item, engines)
    except Exception as failed:  # noqa: BLE001 - an unread label is left out
        _log.warning("dimension labels: lettered read failed: %s", type(failed).__name__)
        return None
    if value is None:
        return None
    box = item.label.box
    reading = _Reading(
        text=hand_labels.text_of(item, value),
        inches=value,
        box=box,
        along=(1.0, 0.0) if orientation == "h" else (0.0, 1.0),
        font=min(box[2] - box[0], box[3] - box[1]),
        bare=False,
        route="drawn",
    )
    return _label(reading, found, "architect"), part


def _add_lettered(
    result: PageLabels,
    parsed: _Parsed,
    halves: Sequence[Mapping[str, Any]],
    engines: Sequence[Any],
    budget_s: float,
    clock: Callable[[], float],
    drawn: list[tuple[dict[str, Any], _Part]],
) -> None:
    """The architect's hand-lettered dimensions (`hand_labels`: feet and inches, inline
    fractions), on the architect's drawings only, within what is left of the time budget."""
    started = clock()
    skip = [tuple(label["box"]) for label in result.labels]
    paths = [
        path
        for path in parsed.page.paths
        if _side(path.box, halves) == "architect"
        and not any(_overlap(path.box, box) > 0.5 for box in skip)
    ]
    if not paths:
        return
    try:
        found_items = hand_labels.lettered(paths)
    except Exception as failed:  # noqa: BLE001 - the page keeps every other label
        _log.warning("dimension labels: lettering search failed: %s", type(failed).__name__)
        return
    for item in found_items:
        if len(result.labels) + len(drawn) >= MAX_LABELS:
            break
        if clock() - started > budget_s:
            result.drawn = "partial"
            break
        if _side(item.label.box, halves) != "architect":
            continue
        found = _lettered_label(parsed, item, engines)
        if found is not None:
            drawn.append(found)


# ---- equal parts (`EQ`) --------------------------------------------------------------------------

#: A mark that the parts of a dimension it stands on are equal: `EQ`, `EQ.`, `(EQ)`, `EQUAL`.
_EQUAL_MARK: Final = re.compile(r"^\(?EQ(?:UAL)?\.?\)?$", re.IGNORECASE)
#: Two parts of a chain meet when their spans are at most this far apart (points): what a centre
#: line marks (a faucet's stem) may be drawn across the line between them.
_CHAIN_GAP_PT: Final = 3.0
#: Equal parts are drawn equal within this share of the longest (a drawing is seldom exact).
_EQUAL_DRAWN: Final = 0.25
#: A derived part is a whole number of these parts of an inch (as a drawing prints).
_PART_DENOMINATOR: Final = 64

type _OnLine = tuple[dict[str, Any], tuple[str, float, float, float, float], bool]
"""A mark or a label on a dimension line: itself, its span (`_span`), and whether it is a mark."""


def _equal_marks(
    parsed: _Parsed, halves: Sequence[Mapping[str, Any]], phrases: Sequence[tuple[str, Box]]
) -> list[_OnLine]:
    """Each `EQ` mark the page prints on a dimension line of its own, with that line's span."""
    out: list[_OnLine] = []
    for text, box in phrases:
        if not _EQUAL_MARK.match(text.strip()):
            continue
        orientation = "h" if box[2] - box[0] >= box[3] - box[1] else "v"
        font = min(box[2] - box[0], box[3] - box[1])
        # An equal-parts line ends at its ticks: a grid or centre line crossing it cuts no part.
        line = dimension_line(box, orientation, parsed.segments, font, drawn=True)
        if line is None:
            continue
        mark: dict[str, Any] = {
            "text": " ".join(text.split()),
            "box": [round(v, 2) for v in box],
            "side": _side(box, halves),
            "orientation": orientation,
            **line,
        }
        span = _span(mark)
        if span is not None:
            out.append((mark, span, True))
    return out


def _chain(items: Sequence[_OnLine], mark: dict[str, Any]) -> list[list[_OnLine]] | None:
    """The run of parts on `mark`'s line that meet end to end and hold it, as spans (each the marks
    or labels on it), or `None` when a span holds a mark and a label both."""
    ordered = sorted(items, key=lambda item: (item[1][2], item[1][3]))
    spans: list[list[_OnLine]] = []
    for item in ordered:
        first = spans[-1][0][1] if spans else None
        if (
            first is not None
            and abs(item[1][2] - first[2]) <= 0.6
            and abs(item[1][3] - first[3]) <= 0.6
        ):
            spans[-1].append(item)
        else:
            spans.append([item])
    runs: list[list[list[_OnLine]]] = []
    for span in spans:
        last = runs[-1][-1][0][1][3] if runs else None
        if last is not None and -0.6 <= span[0][1][2] - last <= _CHAIN_GAP_PT:
            runs[-1].append(span)
        else:
            runs.append([span])
    for run in runs:
        if any(item[0] is mark for span in run for item in span):
            if any(len({item[2] for item in span}) > 1 for span in run):
                return None
            return run
    return None


def _equal_parts(
    parsed: _Parsed,
    labels: Sequence[dict[str, Any]],
    halves: Sequence[Mapping[str, Any]],
    phrases: Sequence[tuple[str, Box]],
) -> list[dict[str, Any]]:
    """The parts `EQ` marks stand for, as derived labels (#1668): `EQ` / `EQ` on a dimension line
    means the parts are equal, so each is the printed overall (less any printed parts of the same
    chain) over the number of `EQ` parts: 36" wall to wall with the sink's centre line `EQ` / `EQ`
    is 18" each side. Code derives one only when all of these hold:

    - the marks sit on dimension lines of their own (`dimension_line`), on one drawing;
    - their spans and the chain's printed parts meet end to end along one line;
    - exactly one value is printed for the whole chain: a label of the same drawing and direction
      whose line runs between the chain's two ends (on any line beside it);
    - every other part of the chain has one printed value;
    - the `EQ` parts are drawn equal (the shortest at least three quarters of the longest);
    - the part is a whole number of 64ths of an inch.

    A derived label keeps what the drawing prints as its `text` (`EQ`), its value in `inches`, and
    `derived`: `{rule: "equal_parts", from: the overall's id, parts: how many, less: the other
    parts' ids}`. It is numbered after every printed label."""
    marks = _equal_marks(parsed, halves, phrases)
    if not marks:
        return []
    printed: list[_OnLine] = []
    for label in labels:
        span = _span(label)
        if span is not None and not label.get("derived"):
            printed.append((label, span, False))
    out: list[dict[str, Any]] = []
    done: set[int] = set()
    for mark, span, _is_mark in marks:
        if id(mark) in done:
            continue
        side, orientation, across = mark["side"], span[0], span[1]
        on_line = [
            item
            for item in (*marks, *printed)
            if item[0]["side"] == side
            and item[1][0] == orientation
            and abs(item[1][1] - across) <= 0.6
        ]
        run = _chain(on_line, mark)
        if run is None:
            continue
        equal = [span_items for span_items in run if span_items[0][2]]
        done |= {id(item[0]) for span_items in equal for item in span_items}
        found = _derived(run, printed, side, orientation)
        if found is None:
            continue
        part, parent, less = found
        lengths = [items[0][1][3] - items[0][1][2] for items in equal]
        if min(lengths) < (1 - _EQUAL_DRAWN) * max(lengths):
            continue
        for items in equal:
            for item, _span_of, _ in items:
                out.append(
                    {
                        "id": "",
                        "text": item["text"],
                        "inches": format(part.normalize(), "f"),
                        "box": item["box"],
                        "side": side,
                        "orientation": orientation,
                        "route": "text",
                        "mm": None,
                        "units": None,
                        "dim_line": item["dim_line"],
                        "extension": item["extension"],
                        "span_pt": item["span_pt"],
                        "derived": {
                            "rule": "equal_parts",
                            "from": parent,
                            "parts": len(equal),
                            "less": less,
                        },
                    }
                )
    out.sort(key=lambda label: (round(label["box"][1] / 4), label["box"][0]))
    return out


def _derived(
    run: Sequence[Sequence[_OnLine]],
    printed: Sequence[_OnLine],
    side: str,
    orientation: str,
) -> tuple[Decimal, str, list[str]] | None:
    """An `EQ` part's value, the overall's id and the other parts' ids, or `None`."""
    start, end = run[0][0][1][2], run[-1][0][1][3]
    reach = max(1.0, 0.01 * (end - start))
    inside = {id(item[0]) for items in run for item in items}
    totals = {
        Decimal(str(label["inches"]))
        for label, span, _ in printed
        if label["side"] == side
        and span[0] == orientation
        and id(label) not in inside
        and abs(span[2] - start) <= reach
        and abs(span[3] - end) <= reach
    }
    parents = [
        str(label["id"])
        for label, span, _ in printed
        if label["side"] == side
        and span[0] == orientation
        and id(label) not in inside
        and abs(span[2] - start) <= reach
        and abs(span[3] - end) <= reach
    ]
    if len(totals) != 1:
        return None
    rest = Fraction(totals.pop())
    less: list[str] = []
    count = 0
    for items in run:
        if items[0][2]:
            count += 1
            continue
        values = {Decimal(str(item[0]["inches"])) for item in items}
        if len(values) != 1:
            return None
        rest -= Fraction(values.pop())
        less.append(str(items[0][0]["id"]))
    if not count:
        return None
    part = rest / count
    if part <= 0 or (part * _PART_DENOMINATOR).denominator != 1:
        return None
    return Decimal(part.numerator) / Decimal(part.denominator), parents[0], less


def _add_equal_parts(
    result: PageLabels,
    parsed: _Parsed,
    halves: Sequence[Mapping[str, Any]],
    phrases: Sequence[tuple[str, Box]] | None = None,
) -> None:
    """The page's `EQ` parts (`_equal_parts`) added after its numbered labels, numbered on."""
    try:
        parts = _equal_parts(
            parsed, result.labels, halves, _phrases(parsed) if phrases is None else phrases
        )
    except Exception as failed:  # noqa: BLE001 - the page keeps its printed labels
        _log.warning("dimension labels: equal parts failed: %s", type(failed).__name__)
        return
    if parts:
        result.labels += parts[: max(0, MAX_LABELS - len(result.labels))]
        _number(result.labels)


@dataclass(slots=True)
class PageRead:
    """One page read whole by `read_page`: its `dims.json` entry, its drawn pass's result (the
    alphabet's samples, the sweep's held spots, the OCR labels still to number) and the seconds it
    took of each of the set's budgets."""

    entry: dict[str, Any]
    found: PageLabels | None = None
    spent: dict[str, float] = field(default_factory=dict)
    ink: Any = None
    health: dict[str, Any] | None = None
    """The page's health check (`extraction/page_health`), when the task asked for it."""


def unreadable_page(task: dims_pool.PageTask, why: str) -> PageRead:
    """A page code could not read at all: no labels, and why (`page N could not be read: ...`)."""
    _log.warning(
        "dimension labels: page %d of %s could not be read: %s", task.number, task.name, why
    )
    return PageRead(
        {
            "file": task.name,
            "page": task.number,
            "drawn": "failed",
            "labels": [],
            "scale_notes": [],
            "phrases": [],
            "error": f"page {task.number} could not be read: {why}",
        }
    )


def read_page(task: dims_pool.PageTask, given: Mapping[str, float]) -> PageRead:
    """One page read whole, as the set pack reads it (`build_dims`): its text route, its pasted
    pictures (`given["pictures"]` seconds, when it shows one), then its drawn ink (`given["drawn"]`
    seconds, the OCR route within `min(given["ocr"], OCR_PAGE_S)`, the sweep within
    `given["sweep"]`)."""
    pdf, name, number = Path(task.pdf), task.name, task.number
    halves = list(task.halves)
    spent: dict[str, float] = {}
    try:
        found = page_labels(pdf, number, halves, equal_parts=False)
    except Exception as failed:  # noqa: BLE001 - a page code cannot read has no labels
        _log.warning("dimension labels: page failed: %s", type(failed).__name__)
        found = PageLabels(drawn="failed")
    entry: dict[str, Any] = {
        "file": name,
        "page": number,
        "drawn": found.drawn,
        "labels": found.labels,
        "scale_notes": found.scale_notes,
        "phrases": found.phrases,
        **({"metric": found.metric} if found.metric else {}),
    }
    # The pasted pictures (#1830), after the page's text and before the drawn ink.
    if task.with_pictures and given.get("pictures", 0.0) > 0:
        started = monotonic()
        seen: PageLabels | None = None
        try:
            seen = picture_pass(
                pdf,
                number,
                halves,
                entry["labels"],
                entry.get("phrases") or [],
                budget_s=float(given["pictures"]),
                pasted=task.pasted,
            )
        except Exception as failed:  # noqa: BLE001 - the page keeps its text labels
            _log.warning("dimension labels: picture pass failed: %s", type(failed).__name__)
        finally:
            spent["pictures"] = monotonic() - started
        if seen is not None and seen.pictures is not None:
            entry["labels"], entry["pictures"] = seen.labels, seen.pictures
    if entry["drawn"] != "not_run" or given.get("drawn", 0.0) <= 0:
        return _finish_page(PageRead(entry, None, spent), pdf, halves)
    started = monotonic()
    try:
        drawn = drawn_labels(
            pdf,
            number,
            halves,
            entry["labels"],
            budget_s=float(given["drawn"]),
            ocr_budget_s=min(float(given.get("ocr", 0.0)), OCR_PAGE_S),
            phrases=entry.get("phrases") or [],
            sweep_budget_s=float(given.get("sweep", 0.0)),
            alphabet=True,
            count_ink=task.count_ink,
        )
    except Exception as failed:  # noqa: BLE001 - the page keeps its text labels
        _log.warning("dimension labels: drawn pass failed: %s", type(failed).__name__)
        entry["drawn"] = "failed"
        spent["drawn"] = monotonic() - started
        return _finish_page(PageRead(entry, None, spent), pdf, halves)
    spent["drawn"] = monotonic() - started
    entry["drawn"], entry["labels"] = drawn.drawn, drawn.labels
    if drawn.ocr is not None:
        spent["ocr"] = float(drawn.ocr.get("seconds") or 0.0)
        spent["sweep"] = float((drawn.ocr.get("sweep") or {}).get("seconds") or 0.0)
    ink, drawn.ink = drawn.ink, None
    return _finish_page(PageRead(entry, drawn, spent, ink), pdf, halves)


def _finish_page(read: PageRead, pdf: Path, halves: Sequence[Mapping[str, Any]]) -> PageRead:
    """The page's `EQ` parts, after every printed label of the page (text and drawn), then the OCR
    route's labels (#1848), numbered after every other label so no id moves. Only what the vendor's
    alphabet seconds (learned from the whole set) is added later, numbered after these."""
    entry = read.entry
    phrases = [(str(p["text"]), tuple(p["box"])) for p in entry.get("phrases") or []]
    if any(_EQUAL_MARK.match(text.strip()) for text, _box in phrases):
        try:
            result = PageLabels(labels=list(entry["labels"]))
            parsed = _parse(pdf, int(entry["page"]) - 1)
            _add_equal_parts(result, parsed, halves, phrases)
        except Exception as failed:  # noqa: BLE001 - the page keeps its printed labels
            _log.warning("dimension labels: equal parts failed: %s", type(failed).__name__)
        else:
            entry["labels"] = result.labels
    found = read.found
    if found is not None and found.ocr is not None:
        _number_ocr(entry, found.ocr_labels)
        entry["ocr"] = found.ocr
    return read


def _number_ocr(entry: dict[str, Any], labels: Sequence[Mapping[str, Any]]) -> None:
    """`labels` (the OCR route's) added to the page's, numbered after them, up to `MAX_LABELS`."""
    page = entry["labels"]
    room = max(0, MAX_LABELS - len(page))
    for number, label in enumerate(labels[:room], start=len(page) + 1):
        page.append({**label, "id": f"D{number}"})


def build_dims(
    pdfs: Mapping[str, Path],
    page_map: Mapping[str, Any] | None,
    *,
    drawn_budget_s: float = 0.0,
    on_page: Callable[[dict[str, Any]], None] | None = None,
    pictures_budget_s: float = PICTURES_BUDGET_S,
    ocr_budget_s: float = OCR_BUDGET_S,
    sweep_budget_s: float = SWEEP_BUDGET_S,
    ink: dict[tuple[str, int], Any] | None = None,
    workers: int = 1,
    keep: Path | None = None,
    reader: Callable[[dims_pool.PageTask, Mapping[str, float]], PageRead] | None = None,
    health: Mapping[str, Mapping[str, Any]] | None = None,
    uploads: Mapping[str, Path] | None = None,
    on_status: Callable[[dict[str, Any]], None] | None = None,
    reads: dict[int, PageRead] | None = None,
    line_work: Path | None = None,
) -> dict[str, Any]:
    """The set pack's `dims.json`: every page of every upload and its labels. Three passes: the
    text route on every page first (the agent has every printed label in minutes), then the pasted
    pictures (#1830) of the pages that show one, within `pictures_budget_s` seconds (a page read
    gets `pictures`), then the drawn route page by page, sharing `drawn_budget_s` seconds across
    the set (a page it does not reach stays `not_run`; one it leaves half read is `partial`). With
    each page's drawn route, the OCR route (#1848) reads the groups of ink it left unlabelled,
    sharing `ocr_budget_s` seconds, at most `OCR_PAGE_S` a page (a page read gets `ocr`, with its
    seconds), then `read_number` reads the groups still unlabelled that look like numbers, sharing
    `sweep_budget_s` seconds (#1860); the vendor's alphabet is learned from every page's confirmed
    drawn labels (`dims.json`'s `alphabet`) and seconds the sweep's OCR-only readings. After the
    `EQ` parts these labels are numbered after every other label, so no id moves. `on_page` sees
    the document after each page of each pass (to write it as it grows). `ink`: filled with each
    page's unread pen-stroke candidates the drawn pass counted (#1862), keyed `(file, page)`; a
    page it did not reach is not in it.

    Since #1872 each page is read whole (text, pictures, drawn ink) by `read_page`, `workers`
    pages at once in worker processes (`dims_pool`; 1: in this process, in order), every budget
    now a limit for ONE page (`drawn_budget_s` too); `keep`: the
    folder the finished pages are kept in, so a stopped run resumes. A page that crashed the PDF
    reader twice has no labels and says so (`error`). `reader`: what reads a page (default
    `read_page`; the set pack's adds the page's health check, given `health`, each upload's listing
    entry, and `uploads`, the uploads themselves); `on_status`: the reading table
    (`dims_pool.Table`) each time a page's state changes; `reads`: filled with each page's
    `PageRead`, by its place in the set."""
    halves: dict[tuple[str, int], list[Mapping[str, Any]]] = {}
    pasted: dict[tuple[str, int], list[Sequence[float]]] = {}
    entries = page_map.get("pages") if isinstance(page_map, Mapping) else None
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, Mapping):
            key = (str(entry.get("file")), int(entry.get("page") or 0))
            halves[key] = [h for h in entry.get("halves") or [] if isinstance(h, Mapping)]
            # The page's pasted pictures (#1835): a label read in one takes its heading's side.
            pasted[key] = [
                b for b in entry.get("pictures") or [] if isinstance(b, Sequence) and len(b) == 4
            ]
            # A page of a separate upload not yet known to be a drawing (`set_pack`, #1772): its
            # candidate whole-page drawing gives a side to every label outside its pasted
            # drawings (listed last, so a pasted drawing keeps its own labels), so its pen
            # strokes are read too; the set pack decides it from these labels afterwards.
            candidate = entry.get("candidate")
            if isinstance(candidate, Mapping):
                halves[key].append(candidate)
    document: dict[str, Any] = {"version": LABELS_VERSION, "files": list(pdfs), "pages": []}
    # Each page read whole (its text, its pasted pictures, its drawn ink), in worker processes
    # (`dims_pool`, #1872), each within its own time limits.
    tasks: list[dims_pool.PageTask] = []
    for name, pdf in pdfs.items():
        count = 0
        if pdf.is_file():
            with pdfplumber.open(str(pdf)) as opened:
                count = len(opened.pages)
        with_pictures: set[int] = set()
        if count and pictures_budget_s > 0:
            try:
                with_pictures = set(picture_labels.pages_with_pictures(pdf))
            except Exception as failed:  # noqa: BLE001 - the pages keep their text labels
                _log.warning("dimension labels: pictures not listed: %s", type(failed).__name__)
        tasks += [
            dims_pool.PageTask(
                name,
                str(pdf),
                number,
                tuple(halves.get((name, number), ())),
                tuple(tuple(b) for b in pasted.get((name, number), ())),
                number in with_pictures,
                ink is not None,
                None if health is None else tuple(sorted(dict(health.get(name) or {}).items())),
                str((uploads or {}).get(name, "")),
                "" if line_work is None else str(line_work),
            )
            for number in range(1, count + 1)
        ]
    limits = {
        "pictures": pictures_budget_s,
        "drawn": drawn_budget_s,
        "ocr": ocr_budget_s,
        "sweep": sweep_budget_s,
    }
    reads = {} if reads is None else reads

    def finished(index: int, read: PageRead) -> None:
        reads[index] = read
        document["pages"] = [reads[i].entry for i in sorted(reads)]
        if on_page is not None:
            on_page(document)

    dims_pool.read_pages(
        tasks,
        limits,
        reader or read_page,
        finished,
        unreadable=unreadable_page,
        count=workers,
        keep=keep,
        version=f"labels {LABELS_VERSION}",
        table=dims_pool.Table(tasks, on_status),
        finished=lambda read: read.entry.get("drawn") not in ("not_run", "partial"),
    )
    pending: dict[tuple[str, int], PageLabels] = {}
    samples: list[vendor_alphabet.Sample] = []
    held: list[SweepHeld] = []
    for index in sorted(reads):
        read = reads[index]
        if ink is not None and read.ink is not None:
            ink[(read.entry["file"], read.entry["page"])] = read.ink
        found = read.found
        if found is None:
            continue
        samples += found.alphabet
        held += found.sweep_held
        if found.ocr is not None:
            pending[(read.entry["file"], read.entry["page"])] = found
    # The `EQ` parts and the OCR route's labels were added with each page (`_finish_page`).
    # The vendor's alphabet (#1860), learned from every page's confirmed drawn labels: kept in
    # the pack (`read_number` reads with it on demand) and seconding the spots the sweep read with
    # its OCR pair alone, numbered after the page's other labels.
    alphabet = vendor_alphabet.Alphabet(samples)
    if samples:
        document["alphabet"] = alphabet.as_json()
    entries = {(str(e["file"]), int(e["page"])): e for e in document["pages"]}
    for item, value in seconded(held, alphabet):
        for (name, number), found in pending.items():
            if number != item.page or Path(pdfs[name]).name != item.file:
                continue
            if not any(_overlap(item.box, tuple(lab["box"])) > 0.3 for lab in found.ocr_labels):
                turns = item.turns
                sideways = bool(turns) and all(t != 0 for t in turns)
                label = _label(
                    _Reading(
                        text=vv._inches_text(value),
                        inches=value,
                        box=item.box,
                        along=(0.0, 1.0) if sideways else (1.0, 0.0),
                        font=min(item.box[2] - item.box[0], item.box[3] - item.box[1]),
                        bare=False,
                        route="drawn",
                    ),
                    None,
                    "vendor",
                )
                label["origin"] = ocr_labels.ORIGIN
                label["read_by"] = ["ocr", "alphabet"]
                found.ocr_labels.append(label)
                _number_ocr(entries[(name, number)], [label])
                if on_page is not None:
                    on_page(document)
            break
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
