"""Fillers named only by a leader note: which printed dimension the note points at (code, $0).

A vendor's elevation often prints a filler's width as a plain dimension in the run's string (`4"`)
and names it only by a note beside the drawing, `Filler Left` / `Filler Right`, with a leader line
to the filler. The dimension's own words never say "filler", so the filler range rule
(`filler_range`) could not know it is one. This module links the two by the drawing's geometry,
never by a value:

1. **The note**: a printed phrase that says only "filler" (`Filler Left`, `FILLER (TYP.)`), with no
   size of its own (a note with its size is a filler label already).
2. **The leader**: the one drawn line that starts at the note (an end within `REACH_PT` of its box)
   and runs away from it, followed through pieces that meet end to end (`JOIN_PT`) while exactly
   one piece continues. Arrowheads and ticks (pieces shorter than `MIN_PIECE_PT`, and closed
   shapes such as a filled CAD arrowhead) are never
   followed. Two lines starting at the note, or two ways on at a joint, make it ambiguous:
   nothing is linked.
3. **The object** it points at: the smallest drawn rectangle the leader's end lies in (within
   `TOUCH_PT`). A filler is a strip, so its width is the rectangle's narrower side; a rectangle
   that is not a strip (`STRIP_RATIO`: a cabinet, a door) links nothing.
4. **The dimension**: the one vendor label whose dimension line runs along that side and whose two
   ends land on that side's two edges (within `END_PT`): the line the label reader found, or, when
   it found none or the whole string (a CAD string drawn as one line), the line the drawing shows
   beside the label, cut by ticks or extension lines exactly at the two edges and nowhere between.
   A sheet that draws the run several times (one elevation under another) dimensions each
   filler at the same place along: the label whose line is clearly nearest the strip
   (`_NEARER`) is its own. None, or two as near, links nothing.

Every step must be unique; anything unsure links nothing (a filler that is not linked is never
judged, so it is never a false red).

Boxes and points are in points of the page as shown, origin top-left: the frame of `dims.json`
(`verify_value._placement`, as `dim_labels` reads the page). Draws nothing and reads no picture,
but reads the PDF with pdfplumber, so it is loaded by name (`filler_range`), never imported by
`app/api/`. Verification: `tests/review/assistant/test_filler_leaders.py` (made-up geometry and a
made-up PDF).
"""

from __future__ import annotations

import io
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib import import_module
from itertools import pairwise
from pathlib import Path
from typing import Any, Final

__all__ = [
    "END_PT",
    "JOIN_PT",
    "MIN_PIECE_PT",
    "REACH_PT",
    "STRIP_RATIO",
    "TOUCH_PT",
    "Geometry",
    "filler_notes",
    "leader_end",
    "link",
    "linked_on_page",
    "object_at",
    "page_geometry",
]

type Point = tuple[float, float]
type Box = tuple[float, float, float, float]

#: A leader starts this close (points) to its note's box.
REACH_PT: Final = 8.0
#: Two pieces of one leader meet when their ends are this close (points).
JOIN_PT: Final = 1.0
#: Pieces shorter than this (arrowheads, ticks, dots) are never followed.
MIN_PIECE_PT: Final = 6.0
#: The leader's end may stop this far outside the object it points at (an arrow tip on its edge).
TOUCH_PT: Final = 2.0
#: A dimension's ends land on the object's edges within this many points.
END_PT: Final = 1.5
#: At most this many pieces make one leader.
_MAX_PIECES: Final = 6
#: A filler is a strip: its long side is at least this many times its narrow side (a box less
#: elongated is a cabinet, a door or a panel, whose width is no filler's).
STRIP_RATIO: Final = 3.0
#: Two rectangles this close on every side are one (a filled shape and its outline).
_SAME_BOX_PT: Final = 0.5

_FILLER_WORD: Final = re.compile(r"^fillers?$", re.IGNORECASE)
#: Words that may stand beside FILLER in a leader note.
_BESIDE: Final = frozenset(
    {"l", "r", "lt", "rt", "left", "right", "typ", "typical", "ea", "each", "end", "scribe"}
)
_PUNCTUATION: Final = re.compile(r"[().,:;]+")


@dataclass(frozen=True, slots=True)
class Geometry:
    """A page's drawn lines (polylines) and closed axis-aligned rectangles, in shown points."""

    paths: tuple[tuple[Point, ...], ...]
    rects: tuple[Box, ...]


def _note_words(text: object) -> bool:
    words = _PUNCTUATION.sub(" ", " ".join(str(text or "").split())).split()
    if not words or any(ch.isdigit() for ch in "".join(words)):
        return False
    named = [w for w in words if _FILLER_WORD.match(w)]
    return len(named) == 1 and all(w in named or w.lower() in _BESIDE for w in words)


def _box(value: object) -> Box | None:
    if not isinstance(value, Sequence) or isinstance(value, str) or len(value) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in value)
    except (TypeError, ValueError):
        return None
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def filler_notes(phrases: Sequence[Mapping[str, Any]]) -> list[tuple[str, Box]]:
    """The page's phrases that say only "filler" (with a side or "typical" word), with no size."""
    out: list[tuple[str, Box]] = []
    for phrase in phrases:
        box = _box(phrase.get("box")) if isinstance(phrase, Mapping) else None
        text = " ".join(str(phrase.get("text") or "").split()) if box is not None else ""
        if box is not None and _note_words(text):
            out.append((text, box))
    return out


def _to_box(point: Point, box: Box) -> float:
    x, y = point
    return math.hypot(max(box[0] - x, 0.0, x - box[2]), max(box[1] - y, 0.0, y - box[3]))


def _length(path: Sequence[Point]) -> float:
    return sum(math.dist(a, b) for a, b in pairwise(path))


def leader_end(note: Box, paths: Sequence[Sequence[Point]]) -> Point | None:
    """Where the one leader that starts at `note` ends, or None (no leader, or not one way)."""
    # A closed path (a CAD arrowhead, a box) is a shape, never a piece of a leader: a filled
    # arrowhead's base sits on the leader's end, within `JOIN_PT` of its corner.
    long = [
        tuple(p) for p in paths if len(p) >= 2 and _length(p) >= MIN_PIECE_PT and not _closed(p)
    ]
    starts: list[tuple[int, tuple[Point, ...]]] = []
    for index, path in enumerate(long):
        near_first, near_last = _to_box(path[0], note), _to_box(path[-1], note)
        if near_first <= REACH_PT and near_first < near_last:
            starts.append((index, path))
        elif near_last <= REACH_PT and near_last < near_first:
            starts.append((index, tuple(reversed(path))))
    if len(starts) != 1:
        return None
    used = {starts[0][0]}
    end = starts[0][1][-1]
    for _ in range(_MAX_PIECES - 1):
        nexts: list[tuple[int, tuple[Point, ...]]] = []
        for index, path in enumerate(long):
            if index in used:
                continue
            if math.dist(path[0], end) <= JOIN_PT:
                nexts.append((index, path))
            elif math.dist(path[-1], end) <= JOIN_PT:
                nexts.append((index, tuple(reversed(path))))
        if not nexts:
            return end
        if len(nexts) > 1:
            return None
        used.add(nexts[0][0])
        end = nexts[0][1][-1]
    return None  # longer than a leader: some other line work


def object_at(point: Point, rects: Sequence[Box]) -> Box | None:
    """The smallest rectangle `point` lies in (within `TOUCH_PT`), or None (none, or two of the
    same size that are not one)."""
    holding = sorted(
        (r for r in rects if _to_box(point, r) <= TOUCH_PT),
        key=lambda r: (r[2] - r[0]) * (r[3] - r[1]),
    )
    if not holding:
        return None
    first = holding[0]
    for other in holding[1:]:
        if (other[2] - other[0]) * (other[3] - other[1]) > (first[2] - first[0]) * (
            first[3] - first[1]
        ) * 1.01:
            break
        if any(abs(a - b) > _SAME_BOX_PT for a, b in zip(first, other, strict=True)):
            return None
    return first


def _span(label: Mapping[str, Any]) -> tuple[str, float, float] | None:
    line = label.get("dim_line")
    orientation = label.get("orientation")
    if orientation not in ("h", "v") or not isinstance(line, Sequence) or len(line) != 2:
        return None
    try:
        (x0, y0), (x1, y1) = ((float(p[0]), float(p[1])) for p in line)
    except (TypeError, ValueError, IndexError):
        return None
    if orientation == "h":
        return "h", min(x0, x1), max(x0, x1)
    return "v", min(y0, y1), max(y0, y1)


def _strip(box: Box) -> bool:
    """A long, narrow rectangle (`STRIP_RATIO`): the shape of a filler."""
    narrow, long = sorted((box[2] - box[0], box[3] - box[1]))
    return narrow > 0 and long >= STRIP_RATIO * narrow


def _flip(point: Point) -> Point:
    return point[1], point[0]


def _drawn_row(
    label: Mapping[str, Any], orientation: str, low: float, high: float, geometry: Geometry
) -> float | None:
    """Where the drawing shows a label's own dimension line along `low`..`high` when the label
    reader did not find it (a string drawn as one line, cut only by its ticks): a straight line
    beside the label (within 0.7 of its height, as `dim_labels` looks) running over the whole
    side, cut by a tick or an extension line within `END_PT` of each of its two ends and nowhere
    between, with the label's middle between them. Its place across (y of a horizontal line), or
    `None`. In the frame of a horizontal line (`v` is flipped)."""
    box = _box(label.get("box"))
    if box is None or label.get("orientation") != orientation:
        return None
    flip = orientation == "v"
    if flip:
        box = (box[1], box[0], box[3], box[2])
    middle, size = (box[0] + box[2]) / 2, box[3] - box[1]
    if not low < middle < high:
        return None
    segments = [
        (_flip(a), _flip(b)) if flip else (a, b)
        for path in geometry.paths
        for a, b in pairwise(path)
        if math.dist(a, b) > 0
    ]
    rows: list[float] = []
    for a, b in segments:
        dx, dy = b[0] - a[0], b[1] - a[1]
        y = (a[1] + b[1]) / 2
        if (
            abs(dy) > 0.02 * math.hypot(dx, dy)
            or not box[1] - 0.7 * size <= y <= box[3] + 0.7 * size
        ):
            continue
        if min(a[0], b[0]) <= low + END_PT and max(a[0], b[0]) >= high - END_PT:
            rows.append(y)
    for y in sorted(set(rows), key=lambda r: abs(r - (box[1] + box[3]) / 2)):
        cuts: list[float] = []
        for a, b in segments:
            dx, dy = b[0] - a[0], b[1] - a[1]
            if abs(dy) < 1.0 or not min(a[1], b[1]) <= y <= max(a[1], b[1]):
                continue
            cuts.append(a[0] + dx * (y - a[1]) / dy)
        at_low = any(abs(c - low) <= END_PT for c in cuts)
        at_high = any(abs(c - high) <= END_PT for c in cuts)
        between = any(low + END_PT < c < high - END_PT for c in cuts)
        if at_low and at_high and not between:
            return y
    return None


#: The nearest dimension to a strip must be at least this many times nearer than the next one
#: (and `END_PT` nearer) to be the strip's own: one sheet often draws the same run several times.
_NEARER: Final = 2.0


def _measured_by(
    obj: Box, labels: Sequence[Mapping[str, Any]], geometry: Geometry | None = None
) -> Mapping[str, Any] | None:
    """The one label whose dimension runs along the object's narrower side, end to end: its line
    as the label reader found it, or else as the drawing shows it (`_drawn_row`). Several such
    labels (a sheet drawing the same run more than once, one elevation under another): the one
    whose line is clearly nearest the object, else none."""
    width, height = obj[2] - obj[0], obj[3] - obj[1]
    orientation, low, high = ("h", obj[0], obj[2]) if width <= height else ("v", obj[1], obj[3])
    near, far = (obj[1], obj[3]) if orientation == "h" else (obj[0], obj[2])
    found: list[tuple[float, Mapping[str, Any]]] = []
    for label in labels:
        across: float | None = None
        span = _span(label)
        if (
            span is not None
            and span[0] == orientation
            and abs(span[1] - low) <= END_PT
            and abs(span[2] - high) <= END_PT
        ):
            line = label["dim_line"]
            index = 1 if orientation == "h" else 0
            across = (float(line[0][index]) + float(line[1][index])) / 2
        elif geometry is not None:
            across = _drawn_row(label, orientation, low, high, geometry)
        if across is not None:
            found.append((max(near - across, 0.0, across - far), label))
    if not found:
        return None
    found.sort(key=lambda one: one[0])
    if len(found) > 1 and found[1][0] < max(_NEARER * found[0][0], found[0][0] + END_PT):
        return None
    return found[0][1]


def link(
    notes: Sequence[tuple[str, Box]],
    geometry: Geometry,
    labels: Sequence[Mapping[str, Any]],
) -> dict[str, str]:
    """Each label a filler note's leader points at: label id -> the note's words. A label two
    notes point at is linked to neither."""
    out: dict[str, str] = {}
    clashes: set[str] = set()
    for text, box in notes:
        end = leader_end(box, geometry.paths)
        obj = object_at(end, geometry.rects) if end is not None else None
        if obj is not None and not _strip(obj):
            continue  # a cabinet or a panel, never a filler's width
        label = _measured_by(obj, labels, geometry) if obj is not None else None
        if label is None or not label.get("id"):
            continue
        ident = str(label["id"])
        if ident in out:
            clashes.add(ident)
        out[ident] = text
    return {k: v for k, v in out.items() if k not in clashes}


# ---- the page's geometry -------------------------------------------------------------------------


def _closed(path: Sequence[Point]) -> bool:
    """A path that ends where it starts (at least three points: a triangle, a box)."""
    return len(path) >= 4 and math.dist(path[0], path[-1]) <= JOIN_PT


def _closed_box(points: Sequence[Point]) -> Box | None:
    """A closed polyline of four axis-aligned sides as a rectangle, else None. Its four corners
    must be the rectangle's four corners, one each: a thin CAD arrowhead (a triangle whose slanted
    sides lean less than `JOIN_PT`) is not a rectangle."""
    if len(points) not in (4, 5):
        return None
    corners = list(points[:4])
    if len(points) == 5 and math.dist(points[0], points[-1]) > JOIN_PT:
        return None
    for a, b in zip(corners, [*corners[1:], corners[0]], strict=True):
        if abs(a[0] - b[0]) > JOIN_PT and abs(a[1] - b[1]) > JOIN_PT:
            return None
    xs, ys = [p[0] for p in corners], [p[1] for p in corners]
    box = (min(xs), min(ys), max(xs), max(ys))
    if box[2] - box[0] <= JOIN_PT or box[3] - box[1] <= JOIN_PT:
        return None
    wanted = [(box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0], box[3])]
    for corner in corners:
        near = [w for w in wanted if math.dist(corner, w) <= JOIN_PT]
        if not near:
            return None
        wanted.remove(near[0])
    return box


def page_geometry(pdf: Path, page: int) -> Geometry:
    """Page `page` (from 1) of `pdf`: its lines and rectangles, in the frame `dims.json` uses
    (the page's own content and its pasted drawings, any colour)."""
    pdfplumber = import_module("pdfplumber")
    vv = import_module("app.review.assistant.verify_value")
    vendor = vv.vendor_page_at(Path(pdf), page - 1)  # the file held open (#1874)
    paths: list[tuple[Point, ...]] = []
    rects: list[Box] = []
    with pdfplumber.open(io.BytesIO(vendor)) as document:
        shown_page = document.pages[0]
        shown = vv._placement(shown_page)
        for rect in shown_page.rects:
            x0, y0, x1, y1 = vv._shown_box(
                shown, *(float(rect[k]) for k in ("x0", "top", "x1", "bottom"))
            )
            rects.append((x0, y0, x1, y1))
        for item in (*shown_page.lines, *shown_page.curves):
            for line in vv._polylines(item):
                points = tuple(shown(float(x), float(y)) for x, y in line)
                if len(points) < 2:
                    continue
                paths.append(points)
                closed = _closed_box(points)
                if closed is not None:
                    rects.append(closed)
    return Geometry(tuple(paths), tuple(rects))


def linked_on_page(
    pdf: Path, page: int, labels: Sequence[Mapping[str, Any]], phrases: Sequence[Mapping[str, Any]]
) -> dict[str, str]:
    """The page's labels its filler notes point at (label id -> the note's words); the PDF is read
    only when the page prints a filler note."""
    notes = filler_notes(phrases)
    if not notes or not labels:
        return {}
    return link(notes, page_geometry(pdf, page), labels)
