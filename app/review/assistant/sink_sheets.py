"""The sink cut-out on two sheets of the same top: code, $0, no AI (Fix D, 2026-11-04).

A vanity's top is drawn on more than one sheet of a set: the cabinet maker's vanity sheet draws
the top over its cabinet (often a "top view" of the stone beside it), and the stone fabricator's
countertop sheet draws the same top again with its sink cut-out, its offsets and its faucet hole.
Nothing compared the two: on a real set (2026-11-04) the vanity sheet and the countertop sheet of
the same top printed the sink cut-out in two sizes, at two distances from the front edge, and
nobody was told.

**What code reads on every vendor drawing (`read_drawing`)**, from its own printed labels
(`dims.json`, inches only: GV's settled rule, millimetres are ignored) and its line work:

- **a sink cut-out in plan**: a closed rectangle (square or rounded corners) with exactly one
  faucet hole behind it (a small round closed outline between the cut-out and the top's back
  edge, within the cut-out's width): the faucet side is the back, the other side the front, and
  left and right are the viewer's, standing at the front (a turn of the page, never a mirror);
- **the top around it**, when code can prove it: the nearest whole line on each of the four
  sides, meeting as one closed outline, its width and its depth each printed by a dimension that
  spans exactly that outline, and nothing but dimension lines drawn within `OUTSIDE_IN` outside
  it (a cabinet face under the top, or a top's edge in front of the cabinets, leaves which outline
  is the top's open: then code reads only the cut-out's own sizes);
- **each dimension between two of these places**: a dimension line (one line, never a row of
  chained dimensions) ending at both places within `END_PT`, a tick or arrowhead at each end, with
  exactly one printed label beside it, read from the drawing's text (never drawn ink), a plain
  size (`36"`, `12 3/32" [307mm]`, `305 [12]`) whose millimetres, when printed, agree.

The places are the top's back, front, left and right edges, the cut-out's four edges and its
centre line, and the faucet hole's centre (a centre line through the faucet hole is the same
place). Each dimension is named by its two ends (`FEATURES`): the cut-out's width and depth, its
front and back offsets, its left and right offsets to its sides or its centre line, the faucet
hole to the cut-out and to the back edge, and the top's width and depth.

**Drawn to its value.** A view's scale is proved by two of its dimensions that agree on it (within
`SCALE_SHARE`); each dimension of the view is then drawn to its printed value or not. One drawn far
from its value is another line's label and is dropped; one a little off is read but not proved
(the owner's rule of 2026-10-26: a number that disagrees with its drawn length is never red).

**The same top on two sheets (`same_top`).** Two pages are sheets of the same item when they print
the same item code (`pairing.item_tags_in`, numbered as an item, exactly one on each page) or the
work list pairs both with the same architect pages; and when both print the top's width and depth,
those agree. Two drawings of different rounds of one drawing are a revision, never compared.

**The finding** (`findings`). When the sheets print the same dimension (the same two ends) with
different inches, one finding on both pages with both labels' boxes: "page 12 prints 445 [17 1/2],
page 17 prints 17 7/16" [443mm]". It is a Wrong (the shape of `same_item_pages`: which sheet is
right is not known, "correct one so both pages agree") only when code proved both: each label
beside its own dimension line between the two places, each drawn to its value at its view's proved
scale, and the pages the same item by its printed code or by the architect pages and the top's
size. Anything less is a please-check (a Likely problem, never red). Matching values make nothing.
A left or right offset that the other sheet prints on its other side (a mirrored drawing) is never
compared.

**GV's front offset rule on a top's plan (`edge_findings`).** GV's rule CT-SINK-OFFSET-FRONT-001
is the cut-out's distance from the CABINET's front (GV's countertop deck: top depth = overhang +
front offset + cut-out + back offset + splash), so on a plan of the top alone the printed offset
from the top's front edge is the rule's value plus the job's front overhang
(`countertop_overhang`). Code applies the rule there through `sink_offset` (its items, its
outputs): with the overhang entered, the offset must be the rule's value plus it (a Wrong or a
Right where no architect drawing is paired, a please-check otherwise); with no overhang entered, an
offset under the rule's own value is under it whatever the overhang (a please-check), and any other
offset is "a job value is missing: the front overhang" (`job_value_rules`), with the overhang that
would make it right.

**The overhang the vendor's own drawing prints (`printed_overhangs`, Fix K, 2026-11-04).** With no
job overhang entered (an entered one always wins), code may take the front overhang the vendor's
own drawing of the same top prints, never a default. It reads it only on a vendor plan where
`sink_offset` proved a sink cut-out and its front (the faucet hole behind it), from three of the
drawing's own plain printed labels on one chain at right angles to the front: the whole (from
behind the cut-out to in front of it) printing exactly the top's depth that the top's own plan
proved, split into a back part (12" or more) and a front part (over 0 and at most
`OVERHANG_MAX_IN`) that add up to it exactly, each drawn to its value. The split must stand on a
solid line across the whole cut-out (the cabinets' front) in front of the cut-out, and the front
part must end on a line across it (the top's front edge, solid or dashed). The drawing must print
exactly the top's own item code (same round), and every such drawing of the item must agree on the
value, else none is used. Then the rule's item says which page and which printed label it used;
an offset that the rule plus that overhang does not give is a please-check (never red), one it
gives is Right only where no architect drawing is paired.

Pure code over the set pack (`dims.json`, the page map, the work list, the line work); no AI call.
Findings in the chat finding shape (`created_by: "sink_sheets"`; the rule's items are
`sink_offset`'s). Verification: `tests/review/assistant/test_sink_sheets.py` (made-up plans).
"""

from __future__ import annotations

import logging
import math
import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from importlib import import_module
from itertools import pairwise
from pathlib import Path
from typing import Any, Final, Literal

from app.review.assistant import likely_problems
from app.review.assistant import sink_offset as so
from app.review.assistant.ledger import format_inches

__all__ = [
    "CREATED_BY",
    "END_PT",
    "FEATURES",
    "Measure",
    "PrintedOverhang",
    "SheetTop",
    "SinkView",
    "add_to_answer",
    "check_pack",
    "edge_findings",
    "findings",
    "printed_overhangs",
    "read_drawing",
    "read_pack",
    "same_top",
]

_log = logging.getLogger(__name__)

#: Who made the findings (the chat finding's `created_by`).
CREATED_BY: Final = "sink_sheets"
#: A dimension's end lands on a place within this many points (as `sink_offset`).
END_PT: Final = 0.8
#: A cut-out's side, in points, at least and at most (its scale is not known yet): a 6" cut-out at
#: 1:48 is 9 pt; a 48" one at 1:5 is 690 pt.
CUT_MIN_PT: Final = 9.0
CUT_MAX_PT: Final = 700.0
#: A faucet hole across, as a share of the cut-out's longer side (a 1 3/8" hole by a 17" cut-out:
#: 8%; by a 36" double-bowl cut-out: 3.8%; a 1 1/2" hole by a 12" bar sink: 13%). The clips' small
#: holes beside a cut-out (11/32" by a 17" one: 2%) are never one.
HOLE_SHARE: Final = (0.03, 0.25)
#: Behind the cut-out, the faucet hole is looked for within this many of the cut-out's depths.
REACH_SHARE: Final = 1.5
#: The top's edges are looked for within this many of the cut-out's larger side around it (a sink
#: near one end of a long top).
TOP_REACH_SHARE: Final = 4.0
#: A whole line across a side: this share of it drawn (as `sink_offset.COVER`).
COVER: Final = 0.9
#: The top's outline closes when each side covers this share of the next ones' span.
CLOSED: Final = 0.98
#: Only dimension lines may be drawn this far outside the top's outline (inches at the view's
#: scale): a cabinet face or a second edge there leaves which outline is the top's open.
OUTSIDE_IN: Final = 3.0
#: A dimension line stands within this share of the view's size outside it (and at least
#: `NEAR_PT`): a view's dimensions, never another view's.
NEAR_SHARE: Final = 0.75
NEAR_PT: Final = 60.0
#: A dimension line covers this share of its length (a gap only for its text); down to `GAPPED`
#: when its one label stands across the gap.
COVERED: Final = 0.6
GAPPED: Final = 0.3
#: A short dimension drawn as two ticked stubs at its ends, its label between them: the stubs
#: cover at least this share, and the label fills at least `FILLS` of the gap.
STUBS: Final = 0.1
FILLS: Final = 0.8
#: A label is beside its dimension line within this many points across it.
LABEL_GAP_PT: Final = 8.0
#: A view's scale: two of its dimensions agree on it within this share.
SCALE_SHARE: Final = 0.015
#: Drawn to its value within max(3/16", 1.5 %); further than max(3/4", 20 %): another line's label.
DRAWN_IN: Final = Fraction(3, 16)
DRAWN_SHARE: Final = 0.015
OTHER_IN: Final = 0.75
OTHER_SHARE: Final = 0.2
#: Views of one page are one top's when their cut-outs are drawn this close in size (inches).
SAME_CUT_IN: Final = 0.5

Place = Literal[
    "top_back",
    "faucet",
    "cut_back",
    "cut_front",
    "top_front",
    "top_left",
    "cut_left",
    "centre",
    "cut_right",
    "top_right",
]
Front = Literal["bottom", "top", "left", "right"]
Point = tuple[float, float]
Segment = tuple[Point, Point]

#: Each dimension code reads: its name, its two ends (back to front, or left to right as the
#: viewer stands at the front), the words a finding says, and whether it runs front to back.
FEATURES: Final[tuple[tuple[str, Place, Place, str, bool], ...]] = (
    ("cutout_depth", "cut_back", "cut_front", "sink cut-out depth", True),
    (
        "front_offset",
        "cut_front",
        "top_front",
        "sink cut-out front offset (to the top's front edge)",
        True,
    ),
    (
        "back_offset",
        "top_back",
        "cut_back",
        "sink cut-out back offset (to the top's back edge)",
        True,
    ),
    ("faucet_to_cutout", "faucet", "cut_back", "faucet hole to the sink cut-out", True),
    ("faucet_from_back", "top_back", "faucet", "faucet hole from the top's back edge", True),
    ("top_depth", "top_back", "top_front", "top depth", True),
    ("cutout_width", "cut_left", "cut_right", "sink cut-out width", False),
    ("top_width", "top_left", "top_right", "top width", False),
    ("left_to_cutout", "top_left", "cut_left", "top's left edge to the sink cut-out", False),
    ("right_to_cutout", "cut_right", "top_right", "sink cut-out to the top's right edge", False),
    ("left_to_centre", "top_left", "centre", "top's left edge to the sink's centre line", False),
    ("right_to_centre", "centre", "top_right", "sink's centre line to the top's right edge", False),
)
_WORDS: Final = {name: words for name, _a, _b, words, _v in FEATURES}
_ENDS: Final = {name: (a, b) for name, a, b, _w, _v in FEATURES}
#: The features that need the top's outline (an edge of the top at one end).
_ON_TOP: Final = frozenset(
    name for name, a, b, _w, _v in FEATURES if a.startswith("top_") or b.startswith("top_")
)
#: A left offset and its right one (a mirrored sheet prints one where the other prints the other).
_MIRROR: Final = {
    "left_to_cutout": "right_to_cutout",
    "right_to_cutout": "left_to_cutout",
    "left_to_centre": "right_to_centre",
    "right_to_centre": "left_to_centre",
}
#: A plain printed size: inches (`36"`, `12 3/32"`, `3'-6 1/2"`), millimetres over inches
#: (`305 [12]`) or inches over millimetres (`12 3/32" [307mm]`); nothing else beside it.
_SIZE: Final = r"\d{1,4}(?:'\s?-?\s?\d{1,2})?(?:[ -]\d{1,2}/\d{1,3})?|\d{1,2}/\d{1,3}"
_PLAIN: Final = re.compile(
    rf"^(?:{_SIZE})\s?(?:\"|''|″)?\s?(?:\[\s?(?:{_SIZE})\s?(?:mm|\"|''|″)?\s?\])?$", re.IGNORECASE
)


# ---- one drawing ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Measure:
    """One dimension code read between two places of a sink plan."""

    feature: str
    label: Mapping[str, Any]
    value: Fraction
    span_pt: float
    unsure: str | None = None
    """Why code is not sure of it (never red); `None` when proved."""

    @property
    def id(self) -> str:
        return str(self.label.get("id"))

    @property
    def text(self) -> str:
        return " ".join(str(self.label.get("text") or "").split())


@dataclass(frozen=True, slots=True)
class SinkView:
    """One sink plan code read on a drawing: its cut-out, its faucet hole, the top around it (when
    proved), the dimensions between them and the view's scale (points per inch, when proved)."""

    file: str
    page: int
    front: Front
    cut: tuple[float, float, float, float]
    """The cut-out on the page (PDF points)."""
    top: tuple[float, float, float, float] | None
    """The top's outline on the page, when proved."""
    measures: tuple[Measure, ...]
    scale: float | None
    cut_in: tuple[float, float] | None = None
    """The cut-out as drawn, in inches at the view's scale (width, depth)."""
    why_no_top: str | None = None
    hole_pt: float = 0.0
    """The faucet hole across, in points (the largest behind the cut-out)."""


def _rotate(point: Point, front: Front) -> Point:
    """A page point in the plan's own frame: `u` grows to the viewer's right as they stand at the
    front, `v` grows toward the front. A turn of the page, never a mirror (left stays left)."""
    x, y = point
    if front == "bottom":
        return (x, y)
    if front == "top":
        return (-x, -y)
    if front == "left":
        return (y, -x)
    return (-y, x)


def _unrotate_box(box: Sequence[float], front: Front) -> tuple[float, float, float, float]:
    """A box of the plan's frame back on the page."""
    u0, v0, u1, v1 = (float(v) for v in box)
    corners = []
    for u, v in ((u0, v0), (u1, v1)):
        if front == "bottom":
            corners.append((u, v))
        elif front == "top":
            corners.append((-u, -v))
        elif front == "left":
            corners.append((-v, u))
        else:
            corners.append((v, -u))
    xs, ys = [c[0] for c in corners], [c[1] for c in corners]
    return (min(xs), min(ys), max(xs), max(ys))


def _turn_box(box: Sequence[float], front: Front) -> tuple[float, float, float, float]:
    a = _rotate((float(box[0]), float(box[1])), front)
    b = _rotate((float(box[2]), float(box[3])), front)
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]))


def _frame(segments: Iterable[Segment], front: Front, swap: bool = False) -> so._Frame:
    """The drawing's line work turned so the front is down; `swap`: its two axes swapped, so a
    dimension along the front is read as one at right angles to it."""
    turned: list[Segment] = []
    for a, b in segments:
        p, q = _rotate(a, front), _rotate(b, front)
        if swap:
            p, q = (p[1], p[0]), (q[1], q[0])
        turned.append((p, q))
    return so._Frame.of(turned, "bottom")


def _value(label: Mapping[str, Any]) -> Fraction | None:
    try:
        found = Fraction(Decimal(str(label.get("inches"))))
    except (InvalidOperation, ValueError, ArithmeticError, TypeError):
        return None
    return found if found > 0 else None


def _label_doubt(label: Mapping[str, Any]) -> str | None:
    """Why a label's value is not sure: drawn ink, words beside the number, its millimetres and
    inches disagreeing, a scan's own OCR text."""
    if label.get("route") not in (None, "text") or label.get("origin") not in (None, "text"):
        return "the value is read from drawn ink or a scan, not the drawing's text"
    text = " ".join(str(label.get("text") or "").split())
    if not _PLAIN.match(text):
        return "the label holds words beside the number"
    if label.get("units") == "disagrees":
        return "its millimetres and inches disagree"
    return None


def _lines_across(frame: so._Frame, u0: float, u1: float, lo: float, hi: float) -> list[float]:
    """Whole lines across u0..u1 strictly between lo and hi (nearest lo first)."""
    if hi <= lo:
        return []
    return frame.lines_across(u0, u1, lo, hi, COVER)


def _ticked_line(frame: so._Frame, v: float, u0: float, u1: float) -> bool:
    """The line across u0..u1 at `v` is a dimension line: a tick or an arrowhead at each end."""
    pieces = [(a, b) for a, b, _v in frame._at(v) if b > u0 and a < u1]
    if not pieces:
        return False
    first, last = min(a for a, _b in pieces), max(b for _a, b in pieces)
    return so._ticked(frame, (first, v)) and so._ticked(frame, (last, v))


def _edges(frame: so._Frame, u0: float, u1: float, lo: float, hi: float) -> list[float]:
    """`_lines_across` without the dimension lines (a top's edge is never ticked at both ends)."""
    return [v for v in _lines_across(frame, u0, u1, lo, hi) if not _ticked_line(frame, v, u0, u1)]


def _dimension_lines(
    frame: so._Frame, a: float, b: float, window: tuple[float, float], away: Sequence[float]
) -> list[tuple[float, tuple[float, float] | None]]:
    """Where (u, within `window`) one dimension line runs from `a` to `b` at right angles to the
    front, with the gap left in it for its text when it covers less than `COVERED` of the way
    (its label must then stand in that gap): its pieces end at both places, cover at least
    `GAPPED` of it, a tick or an arrowhead at each end, and no tick where two of its pieces meet (a
    row of chained dimensions is each part's, never one line). `away`: places (u) of the outlines'
    own sides."""
    places: dict[float, list[tuple[float, float]]] = {}
    for v0, v1, u in frame.along:
        if not window[0] <= u <= window[1]:
            continue
        if any(abs(u - other) <= END_PT for other in away):
            continue
        if v0 >= a - END_PT and v1 <= b + END_PT:
            places.setdefault(round(u, 1), []).append((v0, v1))
    out: list[tuple[float, tuple[float, float] | None]] = []
    for u, pieces in places.items():
        pieces.sort()
        if abs(pieces[0][0] - a) > END_PT or abs(max(v1 for _v0, v1 in pieces) - b) > END_PT:
            continue
        covered, place = 0.0, a
        gap: tuple[float, float] = (a, a)
        for v0, v1 in pieces:
            if v0 - place > gap[1] - gap[0]:
                gap = (place, v0)
            if v1 > place:
                covered += v1 - max(v0, place)
                place = v1
        if covered < GAPPED * (b - a) and (len(pieces) < 2 or covered < STUBS * (b - a)):
            continue  # too little of a line (two stubs at its ends need their label between)
        if not (so._ticked(frame, (u, a)) and so._ticked(frame, (u, b))):
            continue
        # Where two pieces meet end to end (never across a gap left for the text).
        joints = [
            first[1]
            for first, second in pairwise(pieces)
            if abs(second[0] - first[1]) <= END_PT and a + END_PT < first[1] < b - END_PT
        ]
        if any(so._crossed(frame, u, v, so._SAME_PT) for v in joints):
            continue  # chained dimensions meet there: each part has its own label
        out.append((u, None if covered >= COVERED * (b - a) else gap))
    return sorted(out)


def _beside(
    labels: Sequence[tuple[Mapping[str, Any], tuple[float, float, float, float]]],
    u: float,
    a: float,
    b: float,
) -> list[Mapping[str, Any]]:
    """The labels beside a dimension line at `u` from `a` to `b` (each box in the same frame)."""
    out = []
    for label, (bu0, bv0, bu1, bv1) in labels:
        gap = 0.0 if bu0 <= u <= bu1 else min(abs(bu0 - u), abs(bu1 - u))
        overlap = min(bv1, b + 2) - max(bv0, a - 2)
        if gap <= LABEL_GAP_PT and bv1 > bv0 and overlap >= 0.5 * (bv1 - bv0):
            out.append(label)
    return out


def _measures_between(
    frame: so._Frame,
    labels: Sequence[tuple[Mapping[str, Any], tuple[float, float, float, float]]],
    feature: str,
    a: float,
    b: float,
    window: tuple[float, float],
    away: Sequence[float],
) -> list[Measure]:
    if b - a < 1.0:
        return []
    out: list[Measure] = []
    for u, gap in _dimension_lines(frame, a, b, window, away):
        found = _beside(labels, u, a, b)
        if len(found) != 1:
            continue  # none, or a row's labels: not this line's own
        label = found[0]
        if gap is not None:
            box = next(b for la, b in labels if la is label)
            middle = (box[1] + box[3]) / 2
            if not (box[0] <= u <= box[2] and gap[0] - END_PT <= middle <= gap[1] + END_PT):
                continue  # a short line left with a gap: its label must stand in the gap
            fills = min(box[3], gap[1]) - max(box[1], gap[0])
            if (b - a) - (gap[1] - gap[0]) < GAPPED * (b - a) and fills < FILLS * (gap[1] - gap[0]):
                continue  # only stubs at its ends: its label must fill the gap between them
        value = _value(label)
        if value is None:
            continue
        out.append(Measure(feature, label, value, b - a, _label_doubt(label)))
    return out


def _closed(frame: so._Frame, side: so._Frame, top: tuple[float, float, float, float]) -> bool:
    """The four lines meet as one outline: the back and front lines run the whole width, the two
    sides the whole depth (`side`: the frame with its axes swapped)."""
    u0, v0, u1, v1 = top
    across = all(frame.coverage(v, u0, u1) >= CLOSED for v in (v0, v1))
    along = all(side.coverage(u, v0, v1) >= CLOSED for u in (u0, u1))
    return across and along


def _only_dimensions_outside(
    frame: so._Frame,
    side: so._Frame,
    top: tuple[float, float, float, float],
    reach: float,
) -> bool:
    """Nothing but dimension lines (a tick at each end) is drawn within `reach` outside the top's
    outline along its whole side: no cabinet face, no second edge."""
    u0, v0, u1, v1 = top

    def plain(fr: so._Frame, lo: float, hi: float, s0: float, s1: float) -> bool:
        return not _edges(fr, s0, s1, lo, hi)

    return (
        plain(frame, v1 + so._SAME_PT, v1 + reach, u0, u1)
        and plain(frame, v0 - reach, v0 - so._SAME_PT, u0, u1)
        and plain(side, u1 + so._SAME_PT, u1 + reach, v0, v1)
        and plain(side, u0 - reach, u0 - so._SAME_PT, v0, v1)
    )


def _scale(measures: Sequence[Measure]) -> float | None:
    """The view's points per inch: the median of its sure dimensions' ratios, when at least two
    dimensions of different names agree with it within `SCALE_SHARE`."""
    ratios = sorted(
        (m.span_pt / float(m.value), m.feature)
        for m in measures
        if m.unsure is None and m.value > 0 and m.span_pt > 0
    )
    if len(ratios) < 2:
        return None
    middle = ratios[len(ratios) // 2][0]
    agree = {name for ratio, name in ratios if abs(ratio - middle) <= SCALE_SHARE * middle}
    return middle if len(agree) >= 2 else None


def _drawn(measure: Measure, scale: float | None) -> Measure | None:
    """The measure judged by its drawn length: dropped when drawn far from its value (another
    line's label), unsure when a little off, unsure when the view's scale is not proved."""
    if measure.unsure is not None:
        return measure
    if scale is None:
        return Measure(
            measure.feature,
            measure.label,
            measure.value,
            measure.span_pt,
            "code could not prove the view's scale, so not that it is drawn to its value",
        )
    drawn = measure.span_pt / scale
    off = abs(drawn - float(measure.value))
    if off > max(OTHER_IN, OTHER_SHARE * float(measure.value)):
        return None
    if off > max(float(DRAWN_IN), DRAWN_SHARE * float(measure.value)):
        sixteenths = Fraction(round(drawn * 16), 16)
        return Measure(
            measure.feature,
            measure.label,
            measure.value,
            measure.span_pt,
            f"it is drawn {format_inches(sixteenths)} long, not to its value",
        )
    return measure


#: A round outline is read as a faucet hole up to this many points across (a 3" hole at 1:2).
_ROUND_MAX_PT: Final = 110.0


class _Grid:
    """A drawing's pieces by where they are (cells of `CELL_PT`), so a question about one small area
    reads only the pieces near it (speed: a page of 14,000 pieces)."""

    CELL_PT: Final = 24.0

    def __init__(self, segments: Sequence[Segment]) -> None:
        self.segments = list(segments)
        self.cells: dict[tuple[int, int], list[int]] = defaultdict(list)
        size = self.CELL_PT
        for index, (a, b) in enumerate(self.segments):
            if not all(math.isfinite(v) for v in (*a, *b)):
                continue
            for gx in range(
                math.floor(min(a[0], b[0]) / size), math.floor(max(a[0], b[0]) / size) + 1
            ):
                for gy in range(
                    math.floor(min(a[1], b[1]) / size), math.floor(max(a[1], b[1]) / size) + 1
                ):
                    self.cells[(gx, gy)].append(index)

    def near(self, x0: float, y0: float, x1: float, y1: float) -> list[Segment]:
        """The pieces that may cross the box (a few more never hurt: each is measured exactly)."""
        size = self.CELL_PT
        found: set[int] = set()
        for gx in range(math.floor(x0 / size), math.floor(x1 / size) + 1):
            for gy in range(math.floor(y0 / size), math.floor(y1 / size) + 1):
                found.update(self.cells.get((gx, gy), ()))
        return [self.segments[i] for i in sorted(found)]


def _slashed(centre: Point, size: float, segments: Sequence[Segment]) -> bool:
    """A diameter sign (Ø), not a hole: a straight slanted stroke through the outline's middle,
    about as long as it is across or longer (a centre line through a hole runs square to the page,
    never slanted)."""
    radius = size / 2
    for a, b in segments:
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dy)
        if length < 0.9 * size or abs(dx) < 0.1 * length or abs(dy) < 0.1 * length:
            continue
        along = ((centre[0] - a[0]) * dx + (centre[1] - a[1]) * dy) / (length * length)
        across = abs(dx * (a[1] - centre[1]) - dy * (a[0] - centre[0])) / length
        if 0 < along < 1 and across <= 0.25 * radius:
            return True
    return False


#: A round outline is read as a faucet hole up to this many points across (a 3" hole at 1:2).
_ROUND_MAX_PT: Final = 110.0


def _round_outlines(segments: Sequence[Segment], grid: _Grid) -> list[tuple[Point, float]]:
    """Every small round closed outline of the drawing, read once (`sink_offset.round_outlines`):
    its centre and its size across, in page points; a diameter sign (`_slashed`) is none."""
    if not segments:
        return []
    xs = [v for a, b in segments for v in (a[0], b[0])]
    ys = [v for a, b in segments for v in (a[1], b[1])]
    region = (min(xs) - 1, min(ys) - 1, max(xs) + 1, max(ys) + 1)
    frame = so._Frame.of(segments, "bottom")
    found = so.round_outlines(frame, region, 1.0, _ROUND_MAX_PT, places=2)
    out: list[tuple[Point, float]] = []
    for (x, y), size in found:
        reach = size
        if not _slashed((x, y), size, grid.near(x - reach, y - reach, x + reach, y + reach)):
            out.append(((x, y), size))
    return out


def _empty_inside(
    rect: tuple[float, float, float, float],
    grid: _Grid,
    frame: so._Frame,
    outlines: Sequence[tuple[Point, float]],
) -> bool:
    """A cut-out is drawn empty: no round outline of a drain's size inside it, and no line inside
    it longer than a quarter of its shorter side but a dimension line (a tick at each end) or a
    dashed centre line's dashes. A sink drawn in plan (its bowl, its drain), a cabinet's face or a
    table's cell has more."""
    x0, y0, x1, y1 = rect
    shorter, longer = min(x1 - x0, y1 - y0), max(x1 - x0, y1 - y0)

    def inside(p: Point) -> bool:
        return x0 + 1 <= p[0] <= x1 - 1 and y0 + 1 <= p[1] <= y1 - 1

    for centre, size in outlines:
        if inside(centre) and size >= HOLE_SHARE[0] * longer:
            return False
    for a, b in grid.near(x0, y0, x1, y1):
        if not (inside(a) and inside(b)) or math.dist(a, b) <= 0.25 * shorter:
            continue
        if not (so._ticked(frame, a) and so._ticked(frame, b)):
            return False
    return True


def _hole_near(box: Sequence[float], outlines: Sequence[tuple[Point, float]]) -> bool:
    """A round outline of a faucet hole's size for a cut-out about `box` (its sides' ends; a
    rounded corner may add up to a third each way), within reach of it on some side: the only
    boxes worth reading as a cut-out (`_faucet` decides)."""
    x0, top, x1, bottom = (float(v) for v in box)
    grow = so._ROUND_SHARE * max(x1 - x0, bottom - top)
    longer = max(x1 - x0, bottom - top + 2 * grow)
    reach = REACH_SHARE * longer + grow
    for (x, y), size in outlines:
        if not HOLE_SHARE[0] * longer * 0.5 <= size <= HOLE_SHARE[1] * longer:
            continue
        across = x0 <= x <= x1 and (top - reach <= y <= top or bottom <= y <= bottom + reach)
        beside = top - grow <= y <= bottom + grow and (
            x0 - reach <= x <= x0 or x1 <= x <= x1 + reach
        )
        if across or beside:
            return True
    return False


def _faucet(
    frame_of: Callable[[], so._Frame],
    cut: tuple[float, float, float, float],
    outlines: Sequence[tuple[Point, float]],
    front: Front,
) -> list[tuple[float, float, float]]:
    """The faucet holes behind the cut-out (in its frame, front down): round outlines of a faucet
    hole's size for this cut-out (`HOLE_SHARE` of its width), wholly within its width, between it
    and `REACH_SHARE` of its depth behind it, no whole line across between them: (u, v, size)."""
    u0, v0, u1, v1 = cut
    depth = v1 - v0
    longer = max(u1 - u0, depth)
    low, high = max(1.0, HOLE_SHARE[0] * longer), HOLE_SHARE[1] * longer
    holes: list[tuple[float, float, float]] = []
    for centre, size in outlines:
        if not low <= size <= high:
            continue
        u, v = _rotate(centre, front)
        if (
            u0 + size / 2 <= u <= u1 - size / 2
            and v0 - REACH_SHARE * depth <= v - size / 2
            and (v + size / 2 <= v0 - so._SAME_PT)
        ):
            holes.append((u, v, size))
    if not holes:
        return []
    nearest = max(v for _u, v, _s in holes)
    if frame_of().lines_across(u0, u1, nearest, v0 - so._SAME_PT):
        return []  # a line across the cut-out between it and the hole: the hole is elsewhere
    return holes


def _labels_in(
    labels: Sequence[Mapping[str, Any]], front: Front, swap: bool = False
) -> list[tuple[Mapping[str, Any], tuple[float, float, float, float]]]:
    out = []
    for label in labels:
        box = label.get("box")
        if not isinstance(box, Sequence) or len(box) != 4:
            continue
        u0, v0, u1, v1 = _turn_box(box, front)
        out.append((label, (v0, u0, v1, u1) if swap else (u0, v0, u1, v1)))
    return out


#: The words a page must print for code to read a sink plan on it (a cut-out or a sink).
_NAMED: Final = re.compile(
    r"\bcut\s*-?\s*outs?\b|\bsinks?\b|\blav(?:atory|atories|s)?\b|\bbasins?\b", re.IGNORECASE
)
#: A cut-out at its view's proved scale (inches each way), and its faucet hole across.
CUT_IN: Final = (6.0, 48.0)
HOLE_IN: Final = (0.5, 3.0)


def read_drawing(
    file: str,
    page: int,
    box: Sequence[float],
    labels: Sequence[Mapping[str, Any]],
    segments: Sequence[Segment],
    words: str = "",
) -> list[SinkView]:
    """Every sink plan code reads on one drawing (`box` on its page, its labels, the page's line
    work, the page's printed `words`, which must name a cut-out or a sink), with the dimensions
    between its places (see the module). A plan is kept only when its view's scale is proved and
    its cut-out and faucet hole are of a sink's size at that scale."""
    if not _NAMED.search(words or ""):
        return []
    x0, y0, x1, y1 = (float(v) for v in box)

    def inside(p: Point) -> bool:
        return x0 - 2 <= p[0] <= x1 + 2 and y0 - 2 <= p[1] <= y1 + 2

    mine = [(a, b) for a, b in segments if inside(a) and inside(b)]
    if not mine:
        return []
    labels = [la for la in labels if la.get("side") in (None, "vendor")]
    frames: dict[tuple[Front, bool], so._Frame] = {}

    def frame_of(front: Front, swap: bool = False) -> so._Frame:
        if (front, swap) not in frames:
            frames[(front, swap)] = _frame(mine, front, swap)
        return frames[(front, swap)]

    grid = _Grid(mine)
    outlines = _round_outlines(mine, grid)
    if not outlines:
        return []  # no faucet hole anywhere: no sink plan
    out: list[SinkView] = []
    seen: set[tuple[float, float, float, float]] = set()
    found = so._rectangles(
        frame_of("bottom"),
        1.0,
        size_pt=(CUT_MIN_PT, CUT_MAX_PT),
        tick_margin=so._SAME_PT,
        keep=lambda box: _hole_near(box, outlines),
    )
    for rect in found:
        if not _empty_inside(rect, grid, frame_of("bottom"), outlines):
            continue
        view = _view(file, page, rect, labels, frame_of, outlines)
        if view is None or view.scale is None or view.cut_in is None:
            continue
        if not all(CUT_IN[0] <= side <= CUT_IN[1] for side in view.cut_in):
            continue
        if not HOLE_IN[0] <= view.hole_pt / view.scale <= HOLE_IN[1]:
            continue
        key = tuple(round(v, 1) for v in view.cut)
        if key in seen:
            continue
        seen.add(key)  # type: ignore[arg-type]
        out.append(view)
    return out


def _view(
    file: str,
    page: int,
    rect: tuple[float, float, float, float],
    labels: Sequence[Mapping[str, Any]],
    frame_of: Callable[[Front, bool], so._Frame],
    outlines: Sequence[tuple[Point, float]],
) -> SinkView | None:
    """The sink plan around one closed rectangle, or `None` when it is no cut-out with one faucet
    hole behind it."""
    fronts: list[
        tuple[Front, tuple[float, float, float, float], list[tuple[float, float, float]]]
    ] = []
    for front in ("bottom", "top", "left", "right"):
        cut = _turn_box(rect, front)
        holes = _faucet(lambda front=front: frame_of(front, False), cut, outlines, front)
        if holes:
            fronts.append((front, cut, holes))
    if len(fronts) != 1:
        return None  # no faucet hole, or holes on two sides: the front is not known
    front, cut, holes = fronts[0]
    frame, side = frame_of(front, False), frame_of(front, True)
    u0, v0, u1, v1 = cut
    width, depth = u1 - u0, v1 - v0
    reach = TOP_REACH_SHARE * max(width, depth)
    places: dict[str, float] = {
        "cut_back": v0,
        "cut_front": v1,
        "cut_left": u0,
        "cut_right": u1,
        "centre": (u0 + u1) / 2,
    }
    faucet: Point | None = (holes[0][0], holes[0][1]) if len(holes) == 1 else None
    if faucet is not None:
        places["faucet"] = faucet[1]
        if abs(faucet[0] - places["centre"]) > END_PT:
            # A faucet off the cut-out's centre line: a dimension to it is not to the centre.
            places["faucet_u"] = faucet[0]
    # The top's outline: the nearest whole line on each side.
    behind_from = (faucet[1] if faucet is not None else min(v for _u, v, _s in holes)) - so._SAME_PT
    behind = _edges(frame, u0, u1, v0 - reach - depth, behind_from)
    ahead = _edges(frame, u0, u1, v1 + so._SAME_PT, v1 + reach)
    left = _edges(side, v0, v1, u0 - reach, u0 - so._SAME_PT)
    right = _edges(side, v0, v1, u1 + so._SAME_PT, u1 + reach)
    top: tuple[float, float, float, float] | None = None
    why_no_top = None
    if behind and ahead and left and right:
        top = (left[-1], behind[-1], right[0], ahead[0])
        if not _closed(frame, side, top):
            top, why_no_top = None, "the lines around the cut-out do not close as one outline"
    else:
        why_no_top = "code found no whole line on every side of the cut-out"
    if top is not None:
        places.update(
            {"top_back": top[1], "top_front": top[3], "top_left": top[0], "top_right": top[2]}
        )
    extent = top or cut
    near = max(NEAR_PT, NEAR_SHARE * max(extent[2] - extent[0], extent[3] - extent[1]))
    away_u = [u0, u1] + ([top[0], top[2]] if top else [])
    away_v = [v0, v1] + ([top[1], top[3]] if top else [])
    straight = _labels_in(labels, front)
    swapped = _labels_in(labels, front, swap=True)
    measures: list[Measure] = []
    for name, a, b, _words, front_to_back in FEATURES:
        if a not in places or b not in places:
            continue
        if "faucet_u" in places and "centre" in (a, b):
            continue  # the centre line and the faucet are two places: not read
        lo, hi = places[a], places[b]
        if front_to_back:
            window = (extent[0] - near, extent[2] + near)
            measures += _measures_between(frame, straight, name, lo, hi, window, away_u)
        else:
            window = (extent[1] - near, extent[3] + near)
            measures += _measures_between(side, swapped, name, lo, hi, window, away_v)
    if top is not None:
        names = {m.feature for m in measures if m.unsure is None}
        if not {"top_width", "top_depth"} <= names:
            top, why_no_top = None, "the top's width and depth are not both printed on its outline"
        else:
            scale = _scale([m for m in measures if m.feature in ("top_width", "top_depth")])
            if scale is None or not _only_dimensions_outside(frame, side, top, OUTSIDE_IN * scale):
                top, why_no_top = (
                    None,
                    (
                        "another line is drawn just outside the outline (a cabinet face or a second "
                        "edge?): code cannot tell which outline is the top's"
                    ),
                )
        if top is None:
            measures = [m for m in measures if m.feature not in _ON_TOP]
    scale = _scale(measures)
    judged = [d for m in measures if (d := _drawn(m, scale)) is not None]
    cut_in = (width / scale, depth / scale) if scale else None
    return SinkView(
        file,
        page,
        front,
        _unrotate_box(cut, front),
        None if top is None else _unrotate_box(top, front),
        tuple(judged),
        scale,
        cut_in,
        why_no_top,
        max(size for _u, _v, size in holes),
    )


# ---- one sheet's tops ------------------------------------------------------------------------


@dataclass
class SheetTop:
    """One top as one page draws it: its sink plan views, one value per dimension (a dimension the
    page prints twice with two values is left out: `conflicts`), its printed size, and what makes
    two pages the same item (their item codes, the architect pages the work list pairs, a round)."""

    file: str
    page: int
    views: list[SinkView]
    tags: frozenset[str] = frozenset()
    architect: frozenset[tuple[str, int]] = frozenset()
    round: int | None = None
    alone: bool = False
    """No architect drawing is paired with the page (`WorkList.alone`)."""
    area: str | None = None
    values: dict[str, Measure] = field(default_factory=dict)
    conflicts: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        found: dict[str, list[Measure]] = defaultdict(list)
        for view in self.views:
            for measure in view.measures:
                found[measure.feature].append(measure)
        for name, measures in found.items():
            if len({m.value for m in measures}) > 1:
                self.conflicts.add(name)
                continue
            proved = [m for m in measures if m.unsure is None]
            self.values[name] = (proved or measures)[0]

    def size(self) -> tuple[Fraction, Fraction] | None:
        """The top's printed width and depth, when both are proved on its outline."""
        width, depth = self.values.get("top_width"), self.values.get("top_depth")
        if width is None or depth is None or width.unsure or depth.unsure:
            return None
        return (width.value, depth.value)

    @property
    def tag(self) -> str | None:
        """The page's one item code, as printed (`XX-213`), when it prints exactly one."""
        return next(iter(self.tags)) if len(self.tags) == 1 else None


def _tops_of(views: Sequence[SinkView], **facts: Any) -> list[SheetTop]:
    """The page's tops: each view with a proved outline is one (two of the same printed size and
    cut-out are one top drawn twice); a view of the cut-out alone (a cut-out detail) joins the one
    top whose cut-out is drawn the same size, else it is left out."""
    framed = [v for v in views if v.top is not None]
    details = [v for v in views if v.top is None]
    groups: list[list[SinkView]] = []
    for view in framed:
        size = _view_size(view)
        home = next((g for g in groups if _view_size(g[0]) == size and _same_cut(g[0], view)), None)
        if home is None:
            groups.append([view])
        else:
            home.append(view)
    if not groups and len(details) >= 1:
        # A page that draws only the cut-out (a detail sheet): one top, its cut-out's sizes only.
        firsts = [details[0]] + [d for d in details[1:] if _same_cut(details[0], d)]
        return [SheetTop(views=firsts, **facts)] if len(firsts) == len(details) else []
    for detail in details:
        homes = [g for g in groups if _same_cut(g[0], detail)]
        if len(homes) == 1:
            homes[0].append(detail)
    return [SheetTop(views=group, **facts) for group in groups]


def _view_size(view: SinkView) -> tuple[Fraction, Fraction] | None:
    values = {m.feature: m.value for m in view.measures if m.unsure is None}
    if "top_width" in values and "top_depth" in values:
        return (values["top_width"], values["top_depth"])
    return None


def _same_cut(a: SinkView, b: SinkView) -> bool:
    if a.cut_in is None or b.cut_in is None:
        return False
    return all(abs(x - y) <= SAME_CUT_IN for x, y in zip(a.cut_in, b.cut_in, strict=True))


# ---- the set pack -----------------------------------------------------------------------------


def _read_json(path: Path) -> Any:
    import json

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _texts(entry: Mapping[str, Any] | None) -> list[str]:
    return [
        str(p.get("text") or "")
        for p in (entry or {}).get("phrases") or []
        if isinstance(p, Mapping)
    ]


def _set_items(file: str, pages: Mapping[tuple[str, int], Mapping[str, Any]]) -> frozenset[str]:
    """The vendor file's item codes, as the work list tells them from finishes and products
    (`worklist.ITEM_TAG_SHARE`, #2066): numbered as an item (100 or more), and printed on few of
    its pages or named by its own title blocks as their sheet's item. A sink's model number printed
    on many vanities' sheets (a real set: one model on five different vanities) is never one."""
    pairing = import_module("app.review.assistant.simple_check.pairing")
    worklist = import_module("app.review.assistant.simple_check.worklist")
    mine = sorted(key for key in pages if key[0] == file)
    counts: dict[str, int] = defaultdict(int)
    for key in mine:
        for tag in pairing.item_tags_in(_texts(pages[key])):
            if pairing.numbered_as_item(tag):
                counts[tag] += 1
    limit = max(worklist.ITEM_TAG_MIN, worklist.ITEM_TAG_SHARE * len(mine))
    try:
        named = worklist._title_block_items(mine, pages)
    except Exception:  # noqa: BLE001 - only the page count then
        named = frozenset()
    return frozenset(t for t, n in counts.items() if n <= limit or t in named)


def _item_tags(texts: Sequence[str], items: frozenset[str]) -> frozenset[str]:
    """The item codes (`_set_items`) a page prints, as printed (`XX-213`)."""
    pairing = import_module("app.review.assistant.simple_check.pairing")
    out: set[str] = set()
    for tag in pairing.item_tags_in(texts):
        if tag not in items:
            continue
        printed = pairing.printed_item_tag(texts, tag)
        if printed is not None:
            out.add(printed)
    return frozenset(out)


#: Each set pack's tops, read once per pack (the sink rules and the cross-sheet check both read
#: them): (folder, dims.json's time) -> tops.
_READ: dict[tuple[str, float], list[SheetTop]] = {}
_READ_LOCK: Final = __import__("threading").Lock()


def read_pack(
    folder: Path | None,
    uploads: Path | None,
    main_file: str | None = None,
    segments: Callable[[str, int], Sequence[Segment] | None] | None = None,
    stop: Callable[[], bool] | None = None,
    work: Any = None,
) -> list[SheetTop]:
    """Every top the vendor's drawings draw (the work list's vendor drawings, paired or not, the
    newest round of an unpaired one), read once per set pack. `work`: the set's work list when the
    caller has built it already."""
    if folder is None:
        return []
    folder = Path(folder)
    try:
        stamp = (folder / "dims.json").stat().st_mtime
    except OSError:
        return []
    key = (str(folder.resolve()), stamp)
    with _READ_LOCK:
        if key in _READ:
            return _READ[key]
    tops = _read_pack(folder, uploads, main_file, segments, stop, work)
    if stop is None or not stop():
        with _READ_LOCK:
            _READ.clear()  # one pack at a time is enough: a check reads its own pack
            _READ[key] = tops
    return tops


def _read_pack(
    folder: Path,
    uploads: Path | None,
    main_file: str | None,
    segments: Callable[[str, int], Sequence[Segment] | None] | None,
    stop: Callable[[], bool] | None,
    work: Any,
) -> list[SheetTop]:
    consistency = import_module("app.review.assistant.vendor_consistency")
    if work is None:
        worklist = import_module("app.review.assistant.simple_check.worklist")
        try:
            work = worklist.build_worklist(folder, main_file)
        except (FileNotFoundError, ValueError):
            return []
    dims = _read_json(folder / "dims.json")
    if not isinstance(dims, Mapping):
        return []
    pages: dict[tuple[str, int], Mapping[str, Any]] = {}
    for entry in dims.get("pages") or []:
        if isinstance(entry, Mapping) and isinstance(entry.get("page"), int):
            pages[(str(entry.get("file")), int(entry["page"]))] = entry
    if segments is None:
        segments = consistency.line_work(uploads or folder, folder)
    alone = [item for item in getattr(work, "alone", ()) if item.newest_round]
    items = _set_items(str(getattr(work, "file", "") or ""), pages)
    tops: list[SheetTop] = []
    for item, by_itself in [*((i, False) for i in work.items), *((i, True) for i in alone)]:
        if stop is not None and stop():
            return []
        drawing = item.vendor
        entry = pages.get((str(drawing.file), int(drawing.own_page)))
        if entry is None:
            continue
        wanted = {label.id for label in drawing.labels}
        labels = [
            raw
            for raw in entry.get("labels") or []
            if isinstance(raw, Mapping) and str(raw.get("id")) in wanted
        ]
        if not labels:
            continue
        phrases = [p for p in entry.get("phrases") or [] if isinstance(p, Mapping)]
        words = " ".join(str(p.get("text") or "") for p in phrases)
        try:
            lines = segments(str(drawing.file), int(drawing.own_page))
            if not lines:
                continue
            views = read_drawing(
                str(drawing.file), int(drawing.own_page), drawing.box, labels, lines, words
            )
        except Exception:  # noqa: BLE001 - a drawing code cannot read gives nothing
            _log.exception("sink sheets: p%s could not be read", drawing.own_page)
            continue
        if not views:
            continue
        area = re.sub(r"^\s*\d{1,3}\s*[.)]\s*", "", item.area or "").strip() or None
        tops += _tops_of(
            views,
            file=str(drawing.file),
            page=int(drawing.own_page),
            tags=_item_tags(_texts(entry), items),
            architect=frozenset((str(d.file), int(d.own_page)) for d in item.architect),
            round=drawing.round,
            alone=by_itself,
            area=area,
        )
    return tops


# ---- two sheets of the same top -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Same:
    """Why two pages draw the same top, and whether that is proved (a printed item code, or the
    same architect pages and the same printed size)."""

    words: str
    proved: bool


def _size_words(size: tuple[Fraction, Fraction]) -> str:
    return f"{format_inches(size[0])} x {format_inches(size[1])}"


def same_top(a: SheetTop, b: SheetTop, shared: frozenset[str] = frozenset()) -> Same | None:
    """Whether two pages' tops are the same top (see the module), else `None`. `shared`: item
    codes the set prints on tops of different printed sizes (never an item's own code)."""
    if a.file == b.file and a.page == b.page:
        return None
    if a.round is not None and b.round is not None and a.round != b.round:
        return None  # two rounds of one drawing: a revision, not a mistake
    first, second = a.size(), b.size()
    if first is not None and second is not None and first != second:
        return None
    sized = first is not None and first == second
    size = f", a {_size_words(first)} top" if sized and first is not None else ""
    if a.tag is not None and a.tag == b.tag and a.tag not in shared:
        return Same(f"both sheets print item {a.tag}{size}", True)
    if a.architect and a.architect == b.architect:
        pages = ", ".join(str(page) for _file, page in sorted(a.architect))
        many = "s" if len(a.architect) > 1 else ""
        return Same(f"both sheets are paired with the architect's page{many} {pages}{size}", sized)
    return None


def _pairs(tops: Sequence[SheetTop]) -> list[tuple[SheetTop, SheetTop, Same]]:
    """Every two tops of different pages that are the same top; a page drawing two tops is paired
    only by their printed size, each with exactly one. An item code printed on tops of two printed
    sizes names no one top (a product's model): it pairs nothing."""
    per_page: dict[tuple[str, int], list[SheetTop]] = defaultdict(list)
    sizes: dict[str, set[tuple[Fraction, Fraction]]] = defaultdict(set)
    for top in tops:
        per_page[(top.file, top.page)].append(top)
        size = top.size()
        if size is not None:
            for tag in top.tags:
                sizes[tag].add(size)
    shared = frozenset(tag for tag, found in sizes.items() if len(found) > 1)
    out: list[tuple[SheetTop, SheetTop, Same]] = []
    keys = sorted(per_page)
    for i, first in enumerate(keys):
        for second in keys[i + 1 :]:
            mine, theirs = per_page[first], per_page[second]
            if len(mine) == 1 and len(theirs) == 1:
                same = same_top(mine[0], theirs[0], shared)
                if same is not None:
                    out.append((mine[0], theirs[0], same))
                continue
            for top in mine:
                if top.size() is None:
                    continue
                matches = [t for t in theirs if t.size() == top.size()]
                back = [t for t in mine if t.size() == top.size()]
                if len(matches) == 1 and len(back) == 1:
                    same = same_top(top, matches[0], shared)
                    if same is not None:
                        out.append((top, matches[0], same))
    return out


_PLACE_WORDS: Final[Mapping[str, str]] = {
    "top_back": "the top's back edge",
    "faucet": "the faucet hole's centre",
    "cut_back": "the cut-out's back edge",
    "cut_front": "the cut-out's front edge",
    "top_front": "the top's front edge",
    "top_left": "the top's left end",
    "cut_left": "the cut-out's left edge",
    "centre": "the sink's centre line",
    "cut_right": "the cut-out's right edge",
    "top_right": "the top's right end",
}


def _ends_words(feature: str) -> str:
    a, b = _ENDS[feature]
    return f"from {_PLACE_WORDS[a]} to {_PLACE_WORDS[b]}"


def _box(measure: Measure, top: SheetTop) -> dict[str, Any]:
    x0, y0, x1, y1 = (float(v) for v in measure.label["box"])
    return {"page": top.page, "file": top.file, "x0": x0, "y0": y0, "x1": x1, "y1": y1}


def _provenance(measure: Measure, top: SheetTop) -> dict[str, Any]:
    label = measure.label
    return {
        "id": measure.id,
        "page": top.page,
        "file": top.file,
        "text": measure.text,
        "inches": label.get("inches"),
        "route": label.get("route") or "text",
        "side": label.get("side") or "vendor",
        "box": [float(v) for v in label["box"]],
    }


@dataclass
class _Difference:
    feature: str
    first: SheetTop
    second: SheetTop
    a: Measure
    b: Measure
    same: Same
    also: list[tuple[SheetTop, Measure]] = field(default_factory=list)

    @property
    def proved(self) -> bool:
        return self.same.proved and self.a.unsure is None and self.b.unsure is None


def _differences(tops: Sequence[SheetTop]) -> list[_Difference]:
    """Every dimension two sheets of the same top print with different inches, one per label of
    the later page and value of the earlier (the earlier pages printing the same value listed under
    `also`)."""
    found: dict[tuple[str, str, int, str, Fraction], _Difference] = {}
    for first, second, same in _pairs(tops):
        if (first.file, first.page) > (second.file, second.page):
            first, second = second, first
        for name in sorted(set(first.values) & set(second.values)):
            a, b = first.values[name], second.values[name]
            if a.value == b.value:
                continue
            mirror = _MIRROR.get(name)
            if mirror is not None:
                theirs = second.values.get(mirror)
                mine = first.values.get(mirror)
                if (theirs is not None and theirs.value == a.value) or (
                    mine is not None and mine.value == b.value
                ):
                    continue  # one sheet may be drawn mirrored: its left is the other's right
            key = (name, second.file, second.page, b.id, a.value)
            held = found.get(key)
            if held is None:
                found[key] = _Difference(name, first, second, a, b, same)
                continue
            better = _Difference(name, first, second, a, b, same)
            if better.proved and not held.proved:
                better.also = [(held.first, held.a), *held.also]
                found[key] = better
            else:
                held.also.append((first, a))
    return sorted(found.values(), key=lambda d: (d.second.page, d.first.page, d.feature))


def _item(diff: _Difference) -> str:
    tag = diff.second.tag or diff.first.tag
    place = f"{tag}: " if tag else (f"{diff.second.area}: " if diff.second.area else "")
    return f"{place}{_WORDS[diff.feature]} on two sheets"[:160]


def findings(tops: Sequence[SheetTop], category: str = "countertop") -> list[dict[str, Any]]:
    """The cross-sheet findings (see the module), in the chat finding shape: a Wrong in the shape
    of `same_item_pages` when code proved both labels measure the same thing on the same top, else
    a please-check (a Likely problem, never red), each with both labels' boxes."""
    out: list[dict[str, Any]] = []
    for diff in _differences(tops):
        a, b, first, second = diff.a, diff.b, diff.first, diff.second
        also_pages = sorted({t.page for t, _m in diff.also})
        also_words = (
            f" (page{'s' if len(also_pages) > 1 else ''} "
            f"{', '.join(str(p) for p in also_pages)} print{'' if len(also_pages) > 1 else 's'} "
            f"{a.text} too)"
            if also_pages
            else ""
        )
        printed = (
            f"page {first.page} prints {a.text}{also_words}, page {second.page} prints {b.text}"
        )
        what = _WORDS[diff.feature]
        also_on = [
            {"page": top.page, "vendor": m.text, "vendor_box": _box(m, top)} for top, m in diff.also
        ]
        finding: dict[str, Any] = {
            "page": second.page,
            "vs_page": first.page,
            "item": _item(diff),
            "category": category,
            "architect": a.text,
            "vendor": b.text,
            "verified": False,
            "source": "cross_view",
            "reference_side": "vendor",
            "arch_box": _box(a, first),
            "vendor_box": _box(b, second),
            "dims": {"architect": _provenance(a, first), "vendor": _provenance(b, second)},
            "ledger_ids": [f"p{first.page}.{a.id}", f"p{second.page}.{b.id}"],
            "also_on": also_on,
        }
        if diff.proved:
            finding.update(
                {
                    "status": "different",
                    "paired_by": "code",
                    "why": (
                        f"Two sheets of the same top print the {what} differently: {printed} "
                        f"(inches count). Code proved both dimensions run {_ends_words(diff.feature)} "
                        f"on a plan of the top ({diff.same.words}), each drawn to its value, and read "
                        "both values from the drawings' text. One of them is wrong; which one is "
                        "not known: correct one so both pages agree."
                    ),
                    "same_item_pages": {
                        "pages": [
                            {"page": first.page, "value": a.text, "file": first.file},
                            {"page": second.page, "value": b.text, "file": second.file},
                        ]
                    },
                }
            )
        else:
            reasons = [
                f"page {top.page}'s {m.text}: {m.unsure}"
                for top, m in ((first, a), (second, b))
                if m.unsure
            ]
            if not diff.same.proved:
                reasons.append(
                    f"code could not prove both sheets draw the same top ({diff.same.words}, "
                    "but no item code or printed size ties them)"
                )
            why = "; ".join(reasons)
            words = (
                f"{likely_problems.LETTER_LEAD}: the {what}: {printed}; both measure "
                f"{_ends_words(diff.feature)} on the same top ({diff.same.words})."
            )
            finding.update(
                {
                    "status": "check",
                    "check_reason": "pairing_not_certain",
                    "why": f"{words[len(likely_problems.LETTER_LEAD) + 2 :]} Not proved: {why}."[
                        :600
                    ],
                    likely_problems.FIELD: {
                        "by": "set_check",
                        "proof": "likely_only",
                        "kind": "size",
                        "claim": "please_check",
                        "sure": "likely",
                        "why": why[:400],
                        "words": words[:600],
                    },
                }
            )
        out.append(finding)
    return out


def check_pack(
    folder: Path | None,
    uploads: Path | None,
    main_file: str | None = None,
    category: str = "countertop",
    stop: Callable[[], bool] | None = None,
) -> list[dict[str, Any]]:
    """The set pack's cross-sheet findings (`read_pack`, `findings`)."""
    return findings(read_pack(folder, uploads, main_file, stop=stop), category)


def _already(finding: Mapping[str, Any], live: Sequence[Mapping[str, Any]]) -> bool:
    """The answer already holds this difference: an item on the same two labels (the reading
    ledger's same item on two pages, an earlier pass of this check)."""
    mine = {
        (str(d.get("file") or ""), int(d.get("page") or 0), str(d.get("id")))
        for d in (finding.get("dims") or {}).values()
        if isinstance(d, Mapping)
    }
    for other in live:
        dims = other.get("dims")
        if not isinstance(dims, Mapping):
            continue
        theirs = {
            (str(d.get("file") or ""), int(d.get("page") or 0), str(d.get("id")))
            for d in dims.values()
            if isinstance(d, Mapping) and d.get("id")
        }
        if len(theirs) == 2 and theirs == mine:
            return True
    return False


def add_to_answer(engine: Any) -> int:
    """The engine's hook (a full check, at its finish, $0): the cross-sheet findings added through
    the finding book (its checks and trust rule), unless the answer already holds the same two
    labels' item. Returns how many were added."""
    if getattr(engine.settings, "mode", None) != "full_check":
        return 0
    workspace = engine.tools.workspace
    scope_module = import_module("app.review.assistant.product_scope")
    category = scope_module.sink_cutout_category(getattr(engine, "product_scope", None))
    if category is None:
        return 0
    book = engine.book
    found = check_pack(
        workspace.set_pack,
        getattr(workspace, "uploads", None),
        getattr(book, "main_file", None),
        category,
        stop=getattr(getattr(engine, "cancel", None), "is_set", None),
    )
    if not found:
        return 0
    trust = import_module("app.review.assistant.chat_trust")
    added = 0
    with engine.finding_lock:
        live = book.live()
        for finding in found:
            if _already(finding, live):
                continue
            extra = {
                k: finding.pop(k)
                for k in ("same_item_pages", likely_problems.FIELD, "also_on")
                if k in finding
            }
            stored = book.add_code_finding(finding, created_by=CREATED_BY)
            if stored is None:
                continue
            also = [{**entry, "finding_id": stored["id"]} for entry in extra.pop("also_on", [])]
            marked = trust.judged({**stored, **extra, "also_on": also})
            if book.replace(marked):
                stored = marked
            engine._show_finding(stored)
            live.append(stored)
            added += 1
    return added


# ---- GV's front offset rule on a plan of the top ------------------------------------------------

#: The job value a top's plan adds to GV's front offset (`rules.md`'s key; `sink_offset.TOP`).
OVERHANG: Final = "countertop_overhang"
#: What the rule's items on a top's plan say the dimension is.
EDGE_PROVED: Final = (
    "code proved the dimension runs from the sink cut-out's front edge to the top's front edge, "
    "on a plan of the top alone (its outline closed and printed; the faucet hole marks the back)"
)


#: The largest front overhang code reads from a vendor's chain (a top's lip, not a seat).
OVERHANG_MAX_IN: Final = Fraction(2)
#: The smallest back part of that chain (a cabinet's depth, not a splash or a reveal).
CABINET_MIN_IN: Final = Fraction(12)
#: How far from the cut-out (inches at the drawing's scale) the chain may stand.
CHAIN_REACH_IN: Final = 30.0


@dataclass(frozen=True, slots=True)
class PrintedOverhang:
    """A front overhang the vendor's own drawing prints for a top (`printed_overhangs`)."""

    file: str
    page: int
    tags: frozenset[str]
    round: int | None
    value: Fraction
    depth: Fraction
    """The top's depth the chain's whole prints (= the top's own plan's)."""
    cabinet: Fraction
    label: Mapping[str, Any]
    """The overhang's own printed label."""

    @property
    def text(self) -> str:
        return " ".join(str(self.label.get("text") or "").split())

    def words(self) -> str:
        """Where it comes from, for the rule's item."""
        return (
            f"the vendor's own drawing on page {self.page} prints the top's front overhang "
            f"{self.text} (its {format_inches(self.depth)} depth split into "
            f"{format_inches(self.cabinet)} to the cabinets' front + {format_inches(self.value)})"
        )


def _chain_dims(
    drawing: so.Drawing, front: so.Side
) -> list[tuple[Mapping[str, Any], Fraction, float, float, float]]:
    """The drawing's plain printed labels whose dimension line runs at right angles to the front:
    (label, value, u, nearer v, farther v) in the cut-out's frame, each drawn to its value."""
    out: list[tuple[Mapping[str, Any], Fraction, float, float, float]] = []
    for label in drawing.labels:
        line = label.get("dim_line")
        value = _value(label)
        if value is None or _label_doubt(label) is not None:
            continue
        if not (isinstance(line, Sequence) and len(line) == 2):
            continue
        try:
            a = so._turn((float(line[0][0]), float(line[0][1])), front)
            b = so._turn((float(line[1][0]), float(line[1][1])), front)
        except (TypeError, ValueError, IndexError):
            continue
        if abs(a[0] - b[0]) > END_PT:
            continue
        span = abs(a[1] - b[1])
        drawn = span / drawing.pt_per_in
        if abs(drawn - float(value)) > max(OTHER_SHARE * float(value), float(DRAWN_IN)):
            continue  # not drawn to its value: another line's label
        out.append((label, value, (a[0] + b[0]) / 2, min(a[1], b[1]), max(a[1], b[1])))
    return out


def _overhangs_on(
    drawing: so.Drawing, plan: so.SinkPlan, segments: Sequence[Segment], depth: Fraction
) -> set[tuple[Fraction, Fraction, str]]:
    """The front overhangs one proved sink plan's drawing prints for a top `depth` deep (see the
    module): (overhang, cabinet part, the overhang label's id)."""
    ppi = drawing.pt_per_in
    if ppi <= 0:
        return set()
    x0, y0, x1, y1 = drawing.box
    mine = [
        (a, b)
        for a, b in segments
        if x0 - 2 <= a[0] <= x1 + 2
        and y0 - 2 <= a[1] <= y1 + 2
        and x0 - 2 <= b[0] <= x1 + 2
        and y0 - 2 <= b[1] <= y1 + 2
    ]
    frame = so._Frame.of(mine, plan.front)
    cu0, cv0, cu1, cv1 = so._turn_box(plan.box, plan.front)
    reach = CHAIN_REACH_IN * ppi
    dims = [d for d in _chain_dims(drawing, plan.front) if cu0 - reach <= d[2] <= cu1 + reach]
    found: set[tuple[Fraction, Fraction, str]] = set()
    for _whole, total, _uw, w0, w1 in dims:
        if total != depth or not (w0 <= cv0 and w1 >= cv1):
            continue
        for front_label, lip, uf, f0, f1 in dims:
            if not (0 < lip <= OVERHANG_MAX_IN) or abs(f1 - w1) > END_PT or f0 <= cv1 + END_PT:
                continue
            for _back, cabinet, ub, b0, b1 in dims:
                if (
                    cabinet < CABINET_MIN_IN
                    or cabinet + lip != total
                    or abs(ub - uf) > END_PT
                    or abs(b0 - w0) > END_PT
                    or abs(b1 - f0) > END_PT
                ):
                    continue
                # The split stands on a solid line across the whole cut-out (the cabinets'
                # front) and the front part ends on the top's edge (solid or dashed); no other
                # solid line across the cut-out between them.
                if not frame.lines_across(cu0, cu1, f0 - END_PT, f0 + END_PT):
                    continue
                if not frame.lines_across(cu0, cu1, f1 - END_PT, f1 + END_PT, GAPPED):
                    continue
                if frame.lines_across(cu0, cu1, f0 + END_PT, f1 - END_PT):
                    continue
                found.add((lip, cabinet, str(front_label.get("id"))))
    return found


def printed_overhangs(
    drawings: Sequence[tuple[so.Drawing, Sequence[so.SinkPlan], int | None]],
    segments: Callable[[str, int], Sequence[Segment] | None] | None,
    pages: Mapping[tuple[str, int], Mapping[str, Any]],
    tops: Sequence[SheetTop],
) -> list[PrintedOverhang]:
    """The front overhangs the vendor's drawings (`sink_offset`'s proved sink plans, with the
    drawing's round) print for the tops' depths (see the module). Never raises."""
    if segments is None:
        return []
    depths = {
        m.value
        for top in tops
        if (m := top.values.get("top_depth")) is not None
        and m.unsure is None
        and "top_depth" not in top.conflicts
    }
    if not depths:
        return []
    items: dict[str, frozenset[str]] = {}
    out: list[PrintedOverhang] = []
    for drawing, plans, round_ in drawings:
        try:
            lines = segments(drawing.file, drawing.page)
            if not lines or not plans:
                continue
            if drawing.file not in items:
                items[drawing.file] = _set_items(drawing.file, pages)
            tags = _item_tags(_texts(pages.get((drawing.file, drawing.page))), items[drawing.file])
            if len(tags) != 1:
                continue
            by_id = {str(label.get("id")): label for label in drawing.labels}
            for depth in sorted(depths):
                found: set[tuple[Fraction, Fraction, str]] = set()
                for plan in plans:
                    found |= _overhangs_on(drawing, plan, lines, depth)
                if len({lip for lip, _c, _id in found}) != 1:
                    continue  # none, or two values on one drawing: none is used
                lip, cabinet, label_id = min(found, key=lambda f: f[2])
                out.append(
                    PrintedOverhang(
                        drawing.file,
                        drawing.page,
                        tags,
                        round_,
                        lip,
                        depth,
                        cabinet,
                        by_id.get(label_id, {}),
                    )
                )
        except Exception:  # noqa: BLE001 - a drawing code cannot read gives nothing
            _log.exception("sink sheets: the overhang on p%s could not be read", drawing.page)
    return out


def _printed_for(top: SheetTop, printed: Sequence[PrintedOverhang]) -> PrintedOverhang | None:
    """The one front overhang the vendor's own drawings of this top print: same file and round,
    exactly the top's own item code, the top's proved depth; all of them agreeing."""
    depth = top.values.get("top_depth")
    if top.tag is None or depth is None or depth.unsure is not None or "top_depth" in top.conflicts:
        return None
    mine = [
        p
        for p in printed
        if p.file == top.file
        and p.tags == top.tags
        and p.depth == depth.value
        and (p.round is None or top.round is None or p.round == top.round)
    ]
    if len({p.value for p in mine}) != 1:
        return None
    # The top's own page first, else the nearest page.
    return min(mine, key=lambda p: (abs(p.page - top.page), p.page))


def edge_findings(
    tops: Sequence[SheetTop],
    setting: so.Setting,
    overhang: so.Setting | None = None,
    category: str = "countertop",
    printed: Sequence[PrintedOverhang] = (),
) -> list[dict[str, Any]]:
    """GV's front offset rule on each top's plan whose front offset code proved from the top's
    front edge (see the module), as `sink_offset` items (`created_by: "sink_offset"`, the
    `gv_standard` block, `edge: true`). `printed`: the overhangs the vendor's own drawings print
    (`printed_overhangs`), used only when no job overhang is entered."""
    out: list[dict[str, Any]] = []
    for top in tops:
        measure = top.values.get("front_offset")
        if measure is None or measure.unsure is not None or "front_offset" in top.conflicts:
            continue
        drawn = None if overhang is not None else _printed_for(top, printed)
        made = _edge_item(top, measure, setting, overhang, category, drawn)
        if made is not None:
            out.append(made)
    return out


def _edge_item(
    top: SheetTop,
    measure: Measure,
    setting: so.Setting,
    overhang: so.Setting | None,
    category: str,
    drawn: PrintedOverhang | None = None,
) -> dict[str, Any] | None:
    printed, rule = measure.value, setting.value
    mine = format_inches(printed)
    shown = f"{mine} (printed {measure.text})" if measure.text != mine else mine
    place = f"{top.area}: " if top.area else (f"{top.tag}: " if top.tag else "")
    cabinets = (
        f"GV's sink front offset is {setting.text} from the cabinets' front ({setting.origin}, "
        f"{setting.where})"
    )
    required = None if overhang is None else rule + overhang.value
    standard: dict[str, Any] = {
        "rule": so.RULE_ID,
        "setting": so.SETTING,
        "name": so.NAME,
        "value": setting.text if required is None else format_inches(required),
        "origin": setting.origin,
        "from": setting.where,
        "architect": None,
        "proved": True,
        "follows_architect": False,
        "edge": True,
        "overhang": None if overhang is None else overhang.text,
    }
    finding: dict[str, Any] = {
        "page": top.page,
        "item": f"{place}sink cut-out, front offset (from the top's front edge)"[:160],
        "category": category,
        "vendor": measure.text,
        "architect": standard["value"],
        "source": "rules",
        "verified": False,
        "vendor_box": _box(measure, top),
        "dims": {"vendor": _provenance(measure, top)},
        "ledger_ids": [measure.id],
        "created_by": so.CREATED_BY,
        "gv_standard": standard,
    }
    alone = f"; {so.NO_ARCHITECT}" if top.alone else ""
    if required is not None and overhang is not None:
        should = (
            f"{format_inches(required)} from the top's front edge: {cabinets}, plus the job's "
            f"{overhang.text} front overhang ({overhang.where})"
        )
        if printed == required:
            if not top.alone:
                return None  # it may be right: the architect comparison speaks for the label
            finding["status"] = "same"
            finding["why"] = f"Matches {should}{alone}. The vendor prints {shown}; {EDGE_PROVED}."
            return finding
        if top.alone:
            how = f"{should}; the vendor prints {shown}; {EDGE_PROVED}{alone}"
            finding["status"] = "different"
            finding["why"] = (
                f"Vendor: {measure.text} · Should be: {format_inches(required)} "
                f"({setting.governs}, plus the overhang). {how}."
            )
            finding["settled"] = {"outcome": "wrong", "method": so.METHOD, "how": how + "."}
            finding["checks"] = {"rule": "agrees"}
            return finding
        why = (
            f"the sink cut-out is {shown} from the top's front edge; GV's rule gives {should}. "
            "The architect's drawing of this item may give another value, so code does not "
            "decide it"
        )
        return _edge_please_check(finding, why)
    if drawn is not None and printed >= rule:
        return _edge_drawn(finding, top, measure, setting, drawn, shown, cabinets, alone)
    if printed < rule:
        why = (
            f"the sink cut-out is {shown} from the top's front edge, so from the cabinets' front "
            f"it is {mine} less the overhang: under {setting.text} whatever the overhang. {cabinets}"
        )
        return _edge_please_check(finding, why)
    gap = printed - rule
    only = (
        "only if the top does not overhang the cabinets at all"
        if gap == 0
        else f"only if the top overhangs the cabinets by exactly {format_inches(gap)}"
    )
    why = (
        f"{cabinets}. This plan of the top prints the cut-out {shown} from the top's front edge, "
        f"which is right {only}. The job's front overhang is not entered, so code could not "
        "decide it"
    )
    finding["status"] = "check"
    finding["gv_note"] = f"Not compared: {why}"
    finding["why"] = f"Not compared: {why}. {EDGE_PROVED[0].upper()}{EDGE_PROVED[1:]}."
    finding["job_value_rule"] = {
        "rule": so.RULE_ID,
        "name": "sink front offset",
        "formula": (
            f"the cut-out's offset from the top's front edge = GV's front offset {setting.text} + "
            "the front overhang"
        ),
        "missing": ["front overhang"],
        "why": why,
    }
    return finding


def _edge_drawn(
    finding: dict[str, Any],
    top: SheetTop,
    measure: Measure,
    setting: so.Setting,
    drawn: PrintedOverhang,
    shown: str,
    cabinets: str,
    alone: str,
) -> dict[str, Any] | None:
    """The rule with the overhang the vendor's own drawing prints (no job overhang entered): the
    offset from the top's front edge must be the rule's value plus it. A match is Right only where
    no architect drawing is paired; a difference is a please-check, never red (the overhang is
    read from the vendor's drawing, not entered for the job)."""
    required = setting.value + drawn.value
    standard = finding["gv_standard"]
    standard["overhang"] = format_inches(drawn.value)
    standard["overhang_from"] = {"page": drawn.page, "label": drawn.text}
    source = drawn.words()
    should = (
        f"{format_inches(required)} from the top's front edge: {cabinets}, plus the "
        f"{format_inches(drawn.value)} front overhang ({source}; no job overhang is entered)"
    )
    if measure.value == required:
        if not top.alone:
            return None  # it may be right: the architect comparison speaks for the label
        standard["value"] = format_inches(required)
        finding["architect"] = standard["value"]
        finding["status"] = "same"
        finding["why"] = f"Matches {should}{alone}. The vendor prints {shown}; {EDGE_PROVED}."
        return finding
    from_cabinets = format_inches(measure.value - drawn.value)
    why = (
        f"the sink cut-out is {shown} from the top's front edge; {source}, so the cut-out is "
        f"{from_cabinets} from the cabinets' front. {cabinets}, so with that overhang it should "
        f"be {format_inches(required)} from the top's front edge. The overhang is read from the "
        "vendor's drawing, not entered for the job, so code does not decide it"
    )
    return _edge_please_check(finding, why)


def _edge_please_check(finding: dict[str, Any], why: str) -> dict[str, Any]:
    """A rule item code cannot decide but that breaks the rule as printed: a please-check (a
    Likely problem, never red), the rule and the printed value side by side."""
    words = f"{likely_problems.LETTER_LEAD}: {finding['item']}: {why}."
    finding["status"] = "check"
    finding["gv_note"] = f"Please check: {why}"
    finding["why"] = f"{why[0].upper()}{why[1:]}. {EDGE_PROVED[0].upper()}{EDGE_PROVED[1:]}."
    finding[likely_problems.FIELD] = {
        "by": "whole_drawing_look",
        "proof": "likely_only",
        "kind": "size",
        "claim": "please_check",
        "sure": "likely",
        "why": why[:400],
        "words": words[:600],
    }
    return finding


def edge_item(finding: Mapping[str, Any]) -> bool:
    """One of this module's rule items on a top's plan (`edge_findings`)."""
    standard = finding.get("gv_standard")
    return isinstance(standard, Mapping) and bool(standard.get("edge"))
