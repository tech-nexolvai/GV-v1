"""GV's sink front offset on the vendor's plan views, $0, no AI: code decides, never the AI.

GV's rule CT-SINK-OFFSET-FRONT-001 (the setting `front_offset_required`, 4" by default; the client
lead: "keep 4" as standard", adjustable in special cases) is the strip of top between the sink
cut-out's front edge and the front of the run, read on the plan view. Until now only GV's countertop
records used it; the full check compared the vendor's offset with the architect's only, so a wrong
vendor offset on a set where the architect prints none was never caught.

**What code must prove** (zero false reds; anything unsure is never red):

- **A sink cut-out in plan.** A closed rectangle (its corners square or rounded) 6"-48" each way
  at the drawing's own scale, on a page whose printed words name a sink, with a **faucet hole**
  behind it (a small round closed outline, 1/2"-3" across, between the cut-out and the run's back
  line, within the cut-out's width): the faucet side is the back, so the opposite side is the front.
  No hole, or holes on two sides: code cannot tell the front, and nothing is claimed.
- **The run around it.** The nearest line across the whole cut-out behind the faucet hole (the
  back line) and in front of the cut-out (the front line), 12"-42" apart (a run's depth, not a
  room's height). The front line is the first line across the whole cut-out in front of it.
- **The dimension.** A line at right angles to the front, from the cut-out's front edge to the
  front line (both ends within 0.8 pt, a tick or arrow at each end), with exactly one printed label
  beside it: a plain number read from the PDF's text (never drawn ink), its value within 35% of the
  drawn length (a sanity check of which dimension it is, never of its value).
- **Unsure, so Not compared, never red:** a second line across the cut-out drawn within 4" in front
  of the front line (a top's edge in front of the cabinet front: code cannot tell which edge the
  offset is measured from), or a dashed or partial line parallel to the front between the cut-out
  and the front line or just in front of it (GV's rule measures from the cabinet's front, not the
  top's edge), a value read from drawn ink, two labels by one dimension.

**The setting in force** comes from the chat's `rules.md` (the rulebook's default, GV's company
standard, the project's value or its sink card, a value typed for the drawing set: the highest one
present), with where it comes from: "GV standard", "project value" or "drawing set value".

**The architect** is looked at the same way, on the work item's architect drawings (one sink
cut-out each side, paired by order along the run): the architect **prints** that span (a proved
label), is **silent** (one vendor sink; every architect drawing of the item read; one proved sink
plan with no label anywhere in the strip between its cut-out and front line; and the vendor's value
printed nowhere on the architect's drawings) or **unknown** (anything else: no sink plan code could
read, two values, an unreadable drawing, the vendor's value printed elsewhere).

**The cabinets' front.** GV's rule measures from the cabinet's front, never the top's edge (the
overhang is its own value). The front line counts only when a cabinet side meets it on each side
of the cut-out (a cabinet plan); a top's plan with only its outline is unsure.

| Vendor | Architect | Item |
|---|---|---|
| = setting | silent | Right: "matches GV standard 4"" |
| = setting | prints the setting | Right, the architect governs, the GV standard agrees |
| differs | silent | Wrong: should be the setting ("GV standard: sink front offset") |
| differs | prints the setting | Wrong, should be the architect's value, the GV standard agrees |
| any | prints another value | Not compared: "architect 5" vs GV standard 4": reviewer decides" |
|  |  | (it decides the label only when the vendor prints the architect's value) |
| differs | unknown, or the vendor's side unsure | Not compared with the reason |
| = setting | unknown | nothing (left to the architect comparison) |

The first four, and the fifth when the vendor prints the architect's value, **decide** the vendor's
label: the answer's other items on it (the compare call's, a helper's) are replaced, so there is
one item and no blind look is paid for it (a vendor value that differs from the architect's other
value stays the architect comparison's: never red from GV's standard, never hidden from it). An
item on the label that **contradicts** this check (a Right where it says Wrong or the other way
round, an architect value other than this item's) is never replaced: it speaks, and this check
stands aside (`agrees`; in the simple check, code's item goes). A Not-compared
note of the last kind is only added where the answer has no other item on the label (the
architect comparison then speaks for it).

Findings are in the chat finding shape (`created_by: "sink_offset"`), filed under countertops, or
under cabinets when countertops are not in the check's scope (`product_scope.sink_cutout_category`:
a sink cut-out sits on the sink cabinet's plan). Each carries a `gv_standard` block (the rule, the
setting, its value and where it comes from), which the proof's "governs" words name in every output
(cloud drawing, proof page, letter, register, Not-compared list, summary).

Verification: `tests/review/assistant/test_sink_offset.py` (made-up plans, no client data).
"""

from __future__ import annotations

import json
import logging
import math
import re
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from importlib import import_module
from pathlib import Path
from typing import Any, Final, Literal

from app.review.assistant.ledger import format_inches

__all__ = [
    "CREATED_BY",
    "RULE_ID",
    "SETTING",
    "Offset",
    "Setting",
    "SinkPlan",
    "add_to_answer",
    "agrees",
    "agrees_with_compare",
    "check_pack",
    "decided_items",
    "decided_labels",
    "findings_for",
    "noted_labels",
    "own_item",
    "plans_on",
    "setting_from",
]

_log = logging.getLogger(__name__)

CREATED_BY: Final = "sink_offset"
RULE_ID: Final = "CT-SINK-OFFSET-FRONT-001"
SETTING: Final = "front_offset_required"
#: The standard's name in the outputs ("GV standard: sink front offset").
NAME: Final = "sink front offset"
#: Two values closer than this are the same (a sixteenth: the drawings' own step).
TOLERANCE: Final = Fraction(1, 16)
#: The rule's method word in a Wrong item's `settled` (`proof.proof_line` says its `how`).
METHOD: Final = "gv_standard"
#: The architect gives another value than GV's standard: the GV note's keep reason
#: (`classification`: never red, the reviewer decides).
CONFLICT: Final = "architect_outside_gv_rule"

#: Sizes, in inches at the drawing's own scale.
CUTOUT_MIN_IN: Final = 6.0
CUTOUT_MAX_IN: Final = 48.0
HOLE_MIN_IN: Final = 0.5
HOLE_MAX_IN: Final = 3.0
#: How far behind the cut-out a faucet hole may stand, and how far the run's lines may be.
HOLE_REACH_IN: Final = 8.0
LINE_REACH_IN: Final = 12.0
DEPTH_MIN_IN: Final = 12.0
DEPTH_MAX_IN: Final = 42.0
#: A cabinet side runs back at least this far from the front line, within this reach of the
#: cut-out, and at least this far inside the front line's ends (`_cabinet_face`).
CABINET_SIDE_IN: Final = 6.0
CABINET_REACH_IN: Final = 30.0
CABINET_END_IN: Final = 1.0
#: A second front edge this close in front of the front line makes the front unsure.
SECOND_EDGE_IN: Final = 4.0
#: The printed value within this share of the drawn length (or `DRAWN_SLACK_IN`).
DRAWN_SHARE: Final = 0.2
DRAWN_SLACK_IN: Final = 0.75
#: A line "across the cut-out" covers this share of its width.
COVER: Final = 0.9
#: Points: one place, a dimension's end on its line, a label beside its dimension line.
_SAME_PT: Final = 0.3
_END_PT: Final = 0.8
_TICK_PT: Final = 3.0
_LABEL_GAP_PT: Final = 8.0
#: A line covering this share of the cut-out's width counts as a possible second front edge (a
#: dashed cabinet face under the top).
_DASHED_SHARE: Final = 0.3
#: A corner's rounding at most this share of the cut-out's shorter side.
_ROUND_SHARE: Final = 0.3
#: A label that is a plain dimension: a number with inches or feet marks, nothing else.
_PLAIN: Final = re.compile(r"^[\d\s/'\"\-.′″]+$")
#: The words of a sink on the page.
_SINK_WORDS: Final = re.compile(r"\b(?:sinks?|lav|lavs|lavatory|lavatories|basin)\b", re.IGNORECASE)

type Point = tuple[float, float]
type Segment = tuple[Point, Point]
type Box = tuple[float, float, float, float]
type Segments = Callable[[str, int], Sequence[Segment] | None]
type Side = Literal["top", "bottom", "left", "right"]
type Outcome = Literal["right", "wrong", "conflict", "unsure"]


# ---- the setting in force -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Setting:
    """The front offset GV requires on this set, and where it comes from."""

    value: Fraction
    text: str
    """As the outputs print it (`4"`)."""
    origin: str
    """"GV standard", "project value" or "drawing set value"."""
    where: str
    """`rules.md`'s own words for where it comes from ("GV's company standard")."""

    @property
    def governs(self) -> str:
        """ "GV standard: sink front offset"."""
        return f"{self.origin}: {NAME}"


def _origin(where: str) -> str:
    """`rules.md`'s "Where it comes from" words (`skills_export._ORIGINS`, then the details) in
    short: the project's value or sink card, a value typed for the drawing set, else GV's own."""
    words = where.lower().strip()
    if words.startswith("this project's"):
        return "project value"
    if words.startswith("typed for this drawing set"):
        return "drawing set value"
    return "GV standard"


def _fraction(value: object) -> Fraction | None:
    try:
        found = Fraction(Decimal(str(value)))
    except (InvalidOperation, ValueError, ArithmeticError, TypeError):
        return None
    return found if found > 0 else None


def setting_from(rules_md: str | None) -> Setting | None:
    """The setting in force from the chat's `rules.md` table (`| ... (`front_offset_required`) |
    4" | GV's company standard | ... |`); `None` when it is not listed or not set."""
    to_inches = import_module("app.review.assistant.finding_checks").to_inches
    for line in (rules_md or "").splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 3 or f"(`{SETTING}`)" not in cells[0]:
            continue
        if cells[1].lower().startswith("not set"):
            return None
        value = to_inches(cells[1])
        if value is None or value <= 0:
            return None
        exact = Fraction(value)
        return Setting(exact, format_inches(exact), _origin(cells[2]), cells[2])
    return None


# ---- the line work, turned so the front is down ----------------------------------------------------

_SIDES: Final[tuple[Side, ...]] = ("bottom", "top", "right", "left")


def _turn(point: Point, side: Side) -> Point:
    """A page point in the cut-out's own frame: `u` along the front, `v` growing toward it."""
    x, y = point
    if side == "bottom":
        return (x, y)
    if side == "top":
        return (x, -y)
    if side == "right":
        return (y, x)
    return (y, -x)


def _turn_box(box: Sequence[float], side: Side) -> Box:
    a = _turn((float(box[0]), float(box[1])), side)
    b = _turn((float(box[2]), float(box[3])), side)
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]))


@dataclass(frozen=True, slots=True)
class _Frame:
    """One drawing's line work in one frame: pieces along `u` (at a `v`, sorted by `v`), along `v`
    (at a `u`), and the slanted strokes."""

    across: tuple[tuple[float, float, float], ...]
    """(u0, u1, v) of each piece parallel to the front, sorted by `v`."""
    places: tuple[float, ...]
    """Each `across` piece's `v`, in the same order (for a binary search)."""
    along: tuple[tuple[float, float, float], ...]
    """(v0, v1, u) of each piece at right angles to it."""
    slanted: tuple[Segment, ...]
    all: tuple[Segment, ...]

    @classmethod
    def of(cls, segments: Iterable[Segment], side: Side) -> _Frame:
        across: list[tuple[float, float, float]] = []
        along: list[tuple[float, float, float]] = []
        slanted: list[Segment] = []
        kept: list[Segment] = []
        for a, b in segments:
            p, q = _turn(a, side), _turn(b, side)
            kept.append((p, q))
            du, dv = q[0] - p[0], q[1] - p[1]
            if abs(dv) < 0.05 and abs(du) >= 0.3:
                across.append((min(p[0], q[0]), max(p[0], q[0]), (p[1] + q[1]) / 2))
            elif abs(du) < 0.05 and abs(dv) >= 0.3:
                along.append((min(p[1], q[1]), max(p[1], q[1]), (p[0] + q[0]) / 2))
            elif math.hypot(du, dv) >= 0.3:
                slanted.append((p, q))
        across.sort(key=lambda piece: piece[2])
        places = tuple(piece[2] for piece in across)
        return cls(tuple(across), places, tuple(along), tuple(slanted), tuple(kept))

    def _at(self, v: float) -> tuple[tuple[float, float, float], ...]:
        """The pieces parallel to the front within `_SAME_PT` of `v`."""
        lo = bisect_left(self.places, v - _SAME_PT)
        hi = bisect_right(self.places, v + _SAME_PT)
        return self.across[lo:hi]

    def coverage(self, v: float, u0: float, u1: float) -> float:
        """The share of u0..u1 covered by pieces parallel to the front at `v`."""
        pieces = sorted((max(a, u0), min(b, u1)) for a, b, _at in self._at(v) if b > u0 and a < u1)
        covered, place = 0.0, u0
        for a, b in pieces:
            if b > place:
                covered += b - max(a, place)
                place = b
        return covered / (u1 - u0) if u1 > u0 else 0.0

    def lines_across(
        self, u0: float, u1: float, lo: float, hi: float, share: float = COVER
    ) -> list[float]:
        """The places (v, lo < v < hi) of lines across `share` of u0..u1 (the whole of it by
        default; less finds a dashed line), nearest `lo` first."""
        start, end = bisect_right(self.places, lo), bisect_left(self.places, hi)
        places = sorted({round(v, 2) for v in self.places[start:end]})
        merged: list[float] = []
        for v in places:
            if merged and v - merged[-1] <= _SAME_PT:
                continue
            merged.append(v)
        return [v for v in merged if self.coverage(v, u0, u1) >= share]


# ---- the sink cut-out --------------------------------------------------------------------------


def _rectangles(frame: _Frame, pt_per_in: float) -> list[Box]:
    """Closed rectangles (square or rounded corners) of a cut-out's size, as (u0, v0, u1, v1)."""
    low, high = CUTOUT_MIN_IN * pt_per_in, CUTOUT_MAX_IN * pt_per_in
    sides = sorted(frame.along, key=lambda piece: piece[2])
    found: set[Box] = set()
    for i, (a0, a1, left) in enumerate(sides):
        for b0, b1, right in sides[i + 1 :]:
            width = right - left
            if width > high:
                break
            if width < low or abs(a0 - b0) > _SAME_PT * 3 or abs(a1 - b1) > _SAME_PT * 3:
                continue
            top_side, bottom_side = (a0 + b0) / 2, (a1 + b1) / 2
            reach = _ROUND_SHARE * min(width, bottom_side - top_side)
            top = _edge(frame, left, right, top_side, -1, reach)
            bottom = _edge(frame, left, right, bottom_side, 1, reach)
            if top is None or bottom is None:
                continue
            height = bottom - top
            if low <= height <= high:
                found.add((round(left, 2), round(top, 2), round(right, 2), round(bottom, 2)))
    return sorted(found)


def _edge(
    frame: _Frame, left: float, right: float, end: float, toward: int, reach: float
) -> float | None:
    """The cut-out's edge beyond a side's `end` (toward -1 up, +1 down): a line across between the
    sides, less the corner's rounding (the same distance as the edge from the side's end)."""
    best: float | None = None
    lo, hi = (end - reach, end + _SAME_PT) if toward < 0 else (end - _SAME_PT, end + reach)
    start, stop = bisect_left(frame.places, lo), bisect_right(frame.places, hi)
    for v in sorted(set(frame.places[start:stop])):
        rounding = (v - end) * toward
        if not -_SAME_PT <= rounding <= reach:
            continue
        inset = max(rounding, 0.0)
        if frame.coverage(v, left + inset + _SAME_PT, right - inset - _SAME_PT) < COVER:
            continue
        if best is None or abs(v - end) < abs(best - end):
            best = v
    return best


def _hull(points: Sequence[Point]) -> list[Point]:
    pts = sorted(set(points))
    if len(pts) <= 2:
        return pts

    def cross(o: Point, a: Point, b: Point) -> float:
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list[Point] = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: list[Point] = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _area(points: Sequence[Point]) -> float:
    return (
        abs(
            sum(
                points[i][0] * points[(i + 1) % len(points)][1]
                - points[(i + 1) % len(points)][0] * points[i][1]
                for i in range(len(points))
            )
        )
        / 2
    )


def _holes(frame: _Frame, region: Box, pt_per_in: float) -> list[Point]:
    """The centres of small round closed outlines wholly inside `region` (a faucet hole): each
    piece's ends joined to the next, every corner as far from the centre as the others (within a
    fifth), and filling at least half of the circle through them (a tick's thin outline does not).
    """
    u0, v0, u1, v1 = region
    longest = HOLE_MAX_IN * pt_per_in

    def inside(p: Point) -> bool:
        return u0 <= p[0] <= u1 and v0 <= p[1] <= v1

    pieces = {
        tuple(sorted(((round(a[0], 1), round(a[1], 1)), (round(b[0], 1), round(b[1], 1)))))
        for a, b in frame.all
        if inside(a) and inside(b) and math.dist(a, b) <= longest
    }
    parent: dict[Point, Point] = {}

    def root(p: Point) -> Point:
        while parent.setdefault(p, p) != p:
            parent[p] = parent[parent[p]]
            p = parent[p]
        return p

    for a, b in pieces:
        parent[root(a)] = root(b)
    groups: dict[Point, list[tuple[Point, Point]]] = {}
    for a, b in pieces:
        groups.setdefault(root(a), []).append((a, b))
    out: list[Point] = []
    for edges in groups.values():
        degree: dict[Point, int] = {}
        for a, b in edges:
            degree[a] = degree.get(a, 0) + 1
            degree[b] = degree.get(b, 0) + 1
        if len(edges) < 4 or any(count != 2 for count in degree.values()):
            continue  # not one closed outline
        corners = list(degree)
        xs, ys = [p[0] for p in corners], [p[1] for p in corners]
        width, height = max(xs) - min(xs), max(ys) - min(ys)
        size = max(width, height)
        if not HOLE_MIN_IN * pt_per_in <= size <= longest or min(width, height) < 0.75 * size:
            continue
        centre = ((max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2)
        reach = [math.dist(centre, p) for p in corners]
        mean = sum(reach) / len(reach)
        if mean <= 0 or any(abs(r - mean) > 0.2 * mean for r in reach):
            continue
        if _area(_hull(corners)) < 0.5 * math.pi * mean * mean:
            continue
        out.append(centre)
    return out


@dataclass(frozen=True, slots=True)
class Offset:
    """One dimension from the cut-out's front edge to the run's front line, with its label."""

    label: Mapping[str, Any]
    value: Fraction | None
    span_pt: float
    unsure: str | None = None
    """Why code is not sure of it (Not compared, never red); `None` when proved."""

    @property
    def id(self) -> str:
        return str(self.label.get("id"))


@dataclass(frozen=True, slots=True)
class SinkPlan:
    """One sink cut-out code proved in plan, with its front and the offsets printed there."""

    box: Box
    """The cut-out on the page (PDF points)."""
    front: Side
    edge: float
    """The cut-out's front edge, in its own frame (`v`)."""
    front_line: float
    along: tuple[float, float]
    """The cut-out's extent along the front, in its own frame (`u0`, `u1`)."""
    offsets: tuple[Offset, ...]
    labels_in_band: int
    """The drawing's labels standing in the strip in front of the cut-out (silent: none)."""
    unsure: str | None = None
    """Why the front of this cut-out is not sure (every offset then unsure too)."""
    order: float = 0.0
    """Its place along the run (page points), to pair the vendor's with the architect's."""


def _ticked(frame: _Frame, at: Point) -> bool:
    """A tick or an arrowhead at a dimension's end: a slanted stroke crossing it or ending there."""
    for a, b in frame.slanted:
        length = math.dist(a, b)
        if length > 12.0:
            continue
        middle = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        if math.dist(middle, at) <= _TICK_PT or min(math.dist(a, at), math.dist(b, at)) <= _END_PT:
            return True
    return False


def _dimension_lines(frame: _Frame, u0: float, u1: float, edge: float, front: float) -> list[float]:
    """Where (u) a dimension line runs from the cut-out's front edge to the front line, ticked at
    both ends: its pieces at right angles to the front, joined (a gap only for its text)."""
    places: dict[float, list[tuple[float, float]]] = {}
    for a, b, u in frame.along:
        if a >= edge - _END_PT and b <= front + _END_PT and u0 - _SAME_PT <= u <= u1 + _SAME_PT:
            places.setdefault(round(u, 1), []).append((a, b))
    out: list[float] = []
    for u, pieces in places.items():
        pieces.sort()
        if abs(pieces[0][0] - edge) > _END_PT or abs(max(b for _a, b in pieces) - front) > _END_PT:
            continue
        covered, place = 0.0, edge
        for a, b in pieces:
            if b > place:
                covered += b - max(a, place)
                place = b
        if covered < 0.6 * (front - edge):
            continue  # pieces of other lines, not one dimension line (a gap only for its text)
        if not (_ticked(frame, (u, edge)) and _ticked(frame, (u, front))):
            continue
        out.append(u)
    return sorted(out)


def _value_text(label: Mapping[str, Any]) -> str:
    return " ".join(str(label.get("text") or "").split())


def _offsets(
    frame: _Frame,
    labels: Sequence[Mapping[str, Any]],
    side: Side,
    u0: float,
    u1: float,
    edge: float,
    front: float,
    pt_per_in: float,
) -> tuple[list[Offset], int]:
    """The offsets printed in front of the cut-out, and how many labels stand in that strip."""
    boxes = [(label, _turn_box(label["box"], side)) for label in labels if _box_ok(label)]
    # Any label in the strip between the cut-out's front edge and the front line, anywhere along
    # the drawing (a dimension placed off to the side still counts): an architect is silent only
    # when there is none.
    in_band = sum(
        1
        for _label, (_bu0, bv0, _bu1, bv1) in boxes
        if edge - _END_PT <= (bv0 + bv1) / 2 <= front + _END_PT
    )
    found: list[Offset] = []
    span = front - edge
    for u in _dimension_lines(frame, u0, u1, edge, front):
        beside = []
        for label, (bu0, bv0, bu1, bv1) in boxes:
            gap = 0.0 if bu0 <= u <= bu1 else min(abs(bu0 - u), abs(bu1 - u))
            overlap = min(bv1, front + 2) - max(bv0, edge - 2)
            if gap <= _LABEL_GAP_PT and overlap >= 0.5 * (bv1 - bv0):
                beside.append(label)
        if not beside:
            continue
        if len(beside) > 1:
            for label in beside:
                found.append(Offset(label, _fraction(label.get("inches")), span, "two labels"))
            continue
        label = beside[0]
        value = _fraction(label.get("inches"))
        found.append(Offset(label, value, span, _label_doubt(label, value, span, pt_per_in)))
    return found, in_band


def _box_ok(label: Mapping[str, Any]) -> bool:
    box = label.get("box")
    return isinstance(box, Sequence) and len(box) == 4


def _label_doubt(
    label: Mapping[str, Any], value: Fraction | None, span_pt: float, pt_per_in: float
) -> str | None:
    """Why the label's value is not sure: words beside the number, drawn ink, no value, or a value
    far from the drawn length (another dimension's label)."""
    if value is None:
        return "code could not read the printed value"
    if label.get("route") not in (None, "text"):
        return "the value is read from drawn ink, not the drawing's text"
    if not _PLAIN.match(_value_text(label)):
        return "the label holds words beside the number"
    drawn = span_pt / pt_per_in
    if abs(drawn - float(value)) > max(DRAWN_SHARE * float(value), DRAWN_SLACK_IN):
        return "the printed value is far from the drawn length (another dimension's label?)"
    return None


def plans_on(
    labels: Sequence[Mapping[str, Any]],
    segments: Sequence[Segment],
    box: Sequence[float],
    pt_per_in: float,
) -> list[SinkPlan]:
    """Every sink cut-out code proves in plan on one drawing (`box` on its page, its labels and the
    page's line work), with the front offsets printed in front of it."""
    if pt_per_in <= 0:
        return []
    x0, y0, x1, y1 = (float(v) for v in box)

    def on_drawing(p: Point) -> bool:
        return x0 - 2 <= p[0] <= x1 + 2 and y0 - 2 <= p[1] <= y1 + 2

    mine = [(a, b) for a, b in segments if on_drawing(a) and on_drawing(b)]
    upright = _Frame.of(mine, "bottom")
    out: list[SinkPlan] = []
    for rect in _rectangles(upright, pt_per_in):
        plan = _plan(mine, labels, rect, pt_per_in)
        if plan is not None:
            out.append(plan)
    return sorted(out, key=lambda plan: plan.order)


def _plan(
    segments: Sequence[Segment],
    labels: Sequence[Mapping[str, Any]],
    rect: Box,
    pt_per_in: float,
) -> SinkPlan | None:
    """The cut-out's plan: its back by the faucet hole, its run's lines and its front offsets."""
    fronts: list[tuple[Side, _Frame, Box, float]] = []
    for side in _SIDES:
        # Turned so that `side` is the front (down): a faucet hole behind the cut-out (up).
        frame = _Frame.of(segments, side)
        u0, v0, u1, v1 = _turn_box(rect, side)
        region = (u0, v0 - HOLE_REACH_IN * pt_per_in, u1, v0 - _SAME_PT)
        holes = _holes(frame, region, pt_per_in)
        if not holes:
            continue
        nearest = max(v for _u, v in holes)
        if frame.lines_across(u0, u1, nearest, v0 - _SAME_PT):
            continue  # a line across the cut-out between it and the hole: the hole is elsewhere
        fronts.append((side, frame, (u0, v0, u1, v1), min(v for _u, v in holes)))
    if len(fronts) != 1:
        return None  # no faucet hole, or holes on two sides: the front is not known
    side, frame, (u0, v0, u1, v1), farthest = fronts[0]
    behind = frame.lines_across(u0, u1, farthest - LINE_REACH_IN * pt_per_in, farthest)
    ahead = frame.lines_across(u0, u1, v1 + _SAME_PT, v1 + LINE_REACH_IN * pt_per_in)
    if not behind or not ahead:
        return None
    back_line, front_line = behind[-1], ahead[0]
    depth = (front_line - back_line) / pt_per_in
    if not DEPTH_MIN_IN <= depth <= DEPTH_MAX_IN:
        return None
    unsure = None
    second = [v for v in ahead[1:] if v - front_line <= SECOND_EDGE_IN * pt_per_in]
    # A dashed or partial line parallel to the front, between the cut-out and the front line or
    # just in front of it (a cabinet face under the top, an overhang's edge): GV's rule measures
    # from the cabinet's front, never the top's edge, so with two candidate edges code cannot
    # tell which one the printed dimension stops at.
    dashed = [
        v
        for v in frame.lines_across(
            u0, u1, v1 + _SAME_PT, front_line + SECOND_EDGE_IN * pt_per_in, _DASHED_SHARE
        )
        if abs(v - front_line) > _SAME_PT and v not in second
    ]
    if second:
        gap = format_inches(_sixteenths(second[0] - front_line, pt_per_in))
        unsure = (
            f"a second front edge is drawn {gap} in front of the run's front line (a top's edge "
            "in front of the cabinets?): code cannot tell which edge the offset is measured from"
        )
    elif dashed:
        unsure = (
            "another line parallel to the front is drawn near the run's front line (a cabinet "
            "face under the top?): code cannot tell which edge the offset is measured from"
        )
    elif not _cabinet_face(frame, u0, u1, front_line, pt_per_in):
        unsure = (
            "code could not prove the front line is the cabinets' front (no cabinet side meets it "
            "on each side of the sink; a top's edge would add the overhang): the offset is "
            "measured from the cabinet's front"
        )
    offsets, in_band = _offsets(frame, labels, side, u0, u1, v1, front_line, pt_per_in)
    for later in second:
        more, extra = _offsets(frame, labels, side, u0, u1, v1, later, pt_per_in)
        offsets += [Offset(o.label, o.value, o.span_pt, unsure) for o in more]
        in_band += extra
    if unsure is not None:
        offsets = [Offset(o.label, o.value, o.span_pt, o.unsure or unsure) for o in offsets]
    centre = ((rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2)
    order = centre[0] if side in ("top", "bottom") else centre[1]
    return SinkPlan(rect, side, v1, front_line, (u0, u1), tuple(offsets), in_band, unsure, order)


def _cabinet_face(frame: _Frame, u0: float, u1: float, front: float, pt_per_in: float) -> bool:
    """The front line is the cabinets' front, not a top's edge (GV's rule measures from the
    cabinet's front): a cabinet side, a line at right angles to it running back at least
    `CABINET_SIDE_IN` from it, meets it on each side of the cut-out, within `CABINET_REACH_IN` of
    the cut-out and not at the front line's own ends (a top's outline has only its two ends)."""
    pieces = [(a, b) for a, b, _v in frame._at(front)]
    if not pieces:
        return False
    first, last = min(a for a, _b in pieces), max(b for _a, b in pieces)
    inside = CABINET_END_IN * pt_per_in
    reach = CABINET_REACH_IN * pt_per_in
    sides = [
        u
        for v0, v1, u in frame.along
        if abs(v1 - front) <= _END_PT
        and front - v0 >= CABINET_SIDE_IN * pt_per_in
        and first + inside <= u <= last - inside
    ]
    left = any(u0 - reach <= u < u0 - _SAME_PT for u in sides)
    right = any(u1 + _SAME_PT < u <= u1 + reach for u in sides)
    return left and right


def _sixteenths(points: float, pt_per_in: float) -> Fraction:
    return Fraction(round(points / pt_per_in * 16), 16)


# ---- one vendor drawing against its architect drawings ------------------------------------------


@dataclass(frozen=True, slots=True)
class Drawing:
    """One drawing as the check reads it."""

    file: str
    page: int
    """Its page in its own file."""
    box: Box
    labels: tuple[Mapping[str, Any], ...]
    pt_per_in: float
    words: str
    """Its page's printed words (a sink must be named there)."""


def _sink_named(drawing: Drawing) -> bool:
    if _SINK_WORDS.search(drawing.words):
        return True
    return any(_SINK_WORDS.search(_value_text(label)) for label in drawing.labels)


def drawing_plans(drawing: Drawing, segments: Segments | None) -> list[SinkPlan] | None:
    """The drawing's sink plans; `None` when its line work cannot be read."""
    if segments is None:
        return None
    lines = segments(drawing.file, drawing.page)
    if lines is None:
        return None
    if not _sink_named(drawing):
        return []
    return plans_on(drawing.labels, lines, drawing.box, drawing.pt_per_in)


@dataclass(frozen=True, slots=True)
class _Architect:
    kind: Literal["prints", "silent", "unknown"]
    value: Fraction | None = None
    label: Mapping[str, Any] | None = None
    drawing: Drawing | None = None
    why: str = ""


def _architect_for(
    index: int,
    count: int,
    architects: Sequence[tuple[Drawing, list[SinkPlan] | None]],
    vendor_value: Fraction | None = None,
) -> _Architect:
    """What the architect gives for the vendor's `index`-th of `count` sink cut-outs. Silent only
    when everything says so: one vendor sink, every architect drawing of the item read, one
    proved sink plan with no label in front of its cut-out, and the vendor's value printed nowhere
    on the architect's drawings (an enlarged detail or a note may give it)."""
    if count != 1:
        return _Architect(
            "unknown", why="the vendor draws more than one sink: code does not pair them"
        )
    if any(plans is None for _drawing, plans in architects):
        return _Architect(
            "unknown", why="code could not read the line work of an architect drawing"
        )
    shown = [(drawing, plans) for drawing, plans in architects if plans]
    if not shown:
        return _Architect("unknown", why="code found no sink plan on the architect's drawings")
    if len(shown) > 1:
        return _Architect("unknown", why="the architect draws the sink plan more than once")
    drawing, plans = shown[0]
    assert plans is not None
    if len(plans) != count:
        return _Architect("unknown", why="the architect's drawing has another number of sinks")
    plan = plans[index]
    if plan.unsure is not None:
        return _Architect(
            "unknown", drawing=drawing, why=f"on the architect's drawing, {plan.unsure}"
        )
    proved = [o for o in plan.offsets if o.unsure is None and o.value is not None]
    if any(o.unsure is not None for o in plan.offsets):
        return _Architect("unknown", drawing=drawing, why="the architect's offset is not sure")
    if proved:
        values = {o.value for o in proved}
        if len(values) > 1:
            return _Architect("unknown", drawing=drawing, why="the architect prints two values")
        return _Architect("prints", proved[0].value, proved[0].label, drawing)
    if plan.labels_in_band:
        return _Architect(
            "unknown", drawing=drawing, why="a label in front of the architect's cut-out"
        )
    if vendor_value is not None:
        for other, _plans in architects:
            for label in other.labels:
                value = _fraction(label.get("inches"))
                if value is not None and _same(value, vendor_value):
                    return _Architect(
                        "unknown",
                        drawing=drawing,
                        why=(
                            f"the architect prints {_inches(vendor_value)} elsewhere on the "
                            "drawing: code cannot tell it is not this offset"
                        ),
                    )
    return _Architect("silent", drawing=drawing)


def _same(a: Fraction, b: Fraction) -> bool:
    return abs(a - b) < TOLERANCE


def _inches(value: Fraction) -> str:
    return format_inches(value)


def _box(label: Mapping[str, Any], drawing: Drawing) -> dict[str, Any]:
    x0, y0, x1, y1 = (float(v) for v in label["box"])
    return {"page": drawing.page, "x0": x0, "y0": y0, "x1": x1, "y1": y1, "file": drawing.file}


def _provenance(label: Mapping[str, Any], drawing: Drawing) -> dict[str, Any]:
    return {
        "id": str(label.get("id")),
        "page": drawing.page,
        "file": drawing.file,
        "text": _value_text(label),
        "inches": label.get("inches"),
        "route": label.get("route"),
        "side": label.get("side"),
        "box": [float(v) for v in label["box"]],
    }


_PROVED: Final = (
    "code proved the dimension runs from the sink cut-out's front edge to the run's front edge "
    "(plan view: the faucet hole marks the back)"
)


def findings_for(
    vendor: Drawing,
    plans: Sequence[SinkPlan],
    architects: Sequence[tuple[Drawing, list[SinkPlan] | None]],
    setting: Setting,
    area: str | None = None,
    category: str = "countertop",
) -> list[dict[str, Any]]:
    """The vendor drawing's front offset items (see the module's table)."""
    out: list[dict[str, Any]] = []
    place = f"{area}: " if area else ""
    for index, plan in enumerate(plans):
        for offset in plan.offsets:
            architect = _architect_for(index, len(plans), architects, offset.value)
            made = _judge(vendor, offset, architect, setting)
            if made is None:
                continue
            outcome, why = made
            out.append(_finding(vendor, offset, architect, setting, outcome, why, place, category))
    return out


def _judge(
    vendor: Drawing, offset: Offset, architect: _Architect, setting: Setting
) -> tuple[Outcome, str] | None:
    standard = f"{setting.governs} {setting.text}"
    if offset.unsure is not None or offset.value is None:
        if offset.value is not None and _same(offset.value, setting.value):
            return None  # nothing to say: it may be right, the architect comparison speaks
        return "unsure", f"Not compared: {offset.unsure}; {standard}"
    value = offset.value
    if architect.kind == "prints" and architect.value is not None:
        if not _same(architect.value, setting.value):
            conflict = (
                f"architect {_inches(architect.value)} vs {setting.origin} {setting.text} "
                f"({NAME}): reviewer decides"
            )
            return "conflict", conflict
        return ("right" if _same(value, setting.value) else "wrong"), ""
    if architect.kind == "silent":
        return ("right" if _same(value, setting.value) else "wrong"), ""
    if _same(value, setting.value):
        return None  # the architect may give another value code cannot see: left to it
    unseen = architect.why or "code could not see what the architect gives"
    return "unsure", f"Not compared: {unseen}; {standard}"


def _finding(
    vendor: Drawing,
    offset: Offset,
    architect: _Architect,
    setting: Setting,
    outcome: Outcome,
    why: str,
    place: str,
    category: str,
) -> dict[str, Any]:
    label = offset.label
    mine = _value_text(label)
    standard = {
        "rule": RULE_ID,
        "setting": SETTING,
        "name": NAME,
        "value": setting.text,
        "origin": setting.origin,
        "from": setting.where,
        "architect": None if architect.value is None else _inches(architect.value),
        "proved": offset.unsure is None,
        # The vendor prints the architect's own value (a conflict note then governs the label: the
        # architect comparison has nothing to add); else the comparison speaks for it.
        "follows_architect": (
            architect.value is not None
            and offset.value is not None
            and _same(offset.value, architect.value)
        ),
    }
    finding: dict[str, Any] = {
        "page": vendor.page,
        "item": f"{place}sink cut-out, front offset"[:160],
        "category": category,
        "vendor": mine,
        "verified": False,
        "vendor_box": _box(label, vendor),
        "dims": {"vendor": _provenance(label, vendor)},
        "created_by": CREATED_BY,
        "gv_standard": standard,
    }
    # An unsure reading is never paired with the architect's label: code did not prove it.
    printed = architect.kind == "prints" and architect.label is not None and outcome != "unsure"
    if printed and architect.drawing is not None and architect.label is not None:
        theirs = architect.label
        finding.update(
            {
                "architect": _value_text(theirs),
                "source": "architect",
                "arch_box": _box(theirs, architect.drawing),
                "paired_by": "code",
                "vs_page": architect.drawing.page,
            }
        )
        finding["dims"]["architect"] = _provenance(theirs, architect.drawing)
        finding["ledger_ids"] = [str(label.get("id")), str(theirs.get("id"))]
    else:
        finding.update({"architect": setting.text, "source": "rules"})
        finding["ledger_ids"] = [str(label.get("id"))]
    if outcome == "right":
        finding["status"] = "same"
        finding["why"] = (
            f"Matches {setting.governs} {setting.text} ({setting.where})"
            + (f"; the architect prints {finding['architect']} too" if printed else "")
            + f". The vendor prints {mine}; {_PROVED}."
        )
    elif outcome == "wrong":
        finding["status"] = "different"
        how = (
            f"{setting.governs} {setting.text} ({setting.where}); the vendor prints {mine}; "
            f"{_PROVED}"
            + (f"; the architect prints {finding['architect']}, the same" if printed else "")
        )
        finding["why"] = f"Vendor: {mine} · Should be: {setting.text} ({setting.governs}). {how}."
        finding["settled"] = {"outcome": "wrong", "method": METHOD, "how": how + "."}
        finding["checks"] = {"rule": "agrees"}
    elif outcome == "conflict":
        finding["status"] = "check"
        finding["keep_reason"] = CONFLICT
        finding["gv_note"] = why
        finding["why"] = f"{why}. The vendor prints {mine}; {_PROVED}."
    else:
        finding["status"] = "check"
        finding["gv_note"] = why
        finding["why"] = why
    return finding


# ---- the set pack -------------------------------------------------------------------------------


def _read(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _scale(drawing: Any, labels: Sequence[Mapping[str, Any]]) -> float:
    """Points per inch: the work list's measured scale, else the median of the drawing's own
    printed dimensions code trusts (`scale_ok`)."""
    given = getattr(drawing, "scale_in_per_pt", None)
    if isinstance(given, float | int) and given > 0:
        return 1.0 / float(given)
    ratios = sorted(
        float(label["span_pt"]) / float(value)
        for label in labels
        if label.get("scale_ok")
        and label.get("span_pt")
        and (value := _fraction(label.get("inches"))) is not None
    )
    return ratios[len(ratios) // 2] if ratios else 0.0


def _drawing(
    drawing: Any, pages: Mapping[tuple[str, int], Mapping[str, Any]], words: str
) -> Drawing | None:
    page = int(drawing.own_page)
    entry = pages.get((str(drawing.file), page))
    if entry is None:
        return None
    wanted = {label.id for label in drawing.labels}
    labels = tuple(
        raw
        for raw in entry.get("labels") or []
        if isinstance(raw, Mapping) and str(raw.get("id")) in wanted
    )
    box = tuple(float(v) for v in drawing.box)
    return Drawing(str(drawing.file), page, box, labels, _scale(drawing, labels), words)  # type: ignore[arg-type]


def check_pack(
    folder: Path | None,
    uploads: Path | None,
    setting: Setting | None,
    main_file: str | None = None,
    category: str = "countertop",
    segments: Segments | None = None,
) -> list[dict[str, Any]]:
    """Every front offset item of the set pack's vendor drawings (`dims.json`, the work list's
    pairing, the uploads' line work), against the setting in force."""
    if folder is None or setting is None:
        return []
    worklist = import_module("app.review.assistant.simple_check.worklist")
    consistency = import_module("app.review.assistant.vendor_consistency")
    try:
        work = worklist.build_worklist(Path(folder), main_file)
    except (FileNotFoundError, ValueError):
        return []
    dims = _read(Path(folder) / "dims.json")
    if not isinstance(dims, Mapping):
        return []
    pages: dict[tuple[str, int], Mapping[str, Any]] = {}
    for entry in dims.get("pages") or []:
        if isinstance(entry, Mapping) and isinstance(entry.get("page"), int):
            pages[(str(entry.get("file")), int(entry["page"]))] = entry
    if segments is None and uploads is not None:
        segments = consistency.line_work(uploads, Path(folder))
    files = sorted({name for name, _page in pages})
    try:
        texts = consistency.page_texts(Path(folder), files)
    except Exception:  # noqa: BLE001 - no page words: the labels' own words only
        texts = {}
    out: list[dict[str, Any]] = []
    for item in work.items:
        try:
            vendor = _drawing(
                item.vendor, pages, texts.get((item.vendor.file, item.vendor.own_page), "")
            )
            if vendor is None:
                continue
            plans = drawing_plans(vendor, segments)
            if not plans:
                continue
            architects: list[tuple[Drawing, list[SinkPlan] | None]] = []
            for other in item.architect:
                found = _drawing(other, pages, texts.get((other.file, other.own_page), ""))
                if found is not None:
                    architects.append((found, drawing_plans(found, segments)))
            area = re.sub(r"^\s*\d{1,3}\s*[.)]\s*", "", item.area or "").strip() or None
            out.extend(findings_for(vendor, plans, architects, setting, area, category))
        except Exception:  # noqa: BLE001 - a drawing code cannot read gives no finding
            _log.exception("sink offset: work item %s could not be checked", item.id)
    order = {"different": 0, "check": 1, "same": 2}
    return sorted(out, key=lambda f: (order.get(str(f["status"]), 3), f["page"]))


# ---- the answer's own items ----------------------------------------------------------------------


def own_item(finding: Mapping[str, Any]) -> bool:
    return finding.get("created_by") == CREATED_BY


def _label_key(finding: Mapping[str, Any], main_file: str | None) -> tuple[str, int, str] | None:
    dims = finding.get("dims") if isinstance(finding.get("dims"), Mapping) else {}
    vendor = (dims or {}).get("vendor")
    if not isinstance(vendor, Mapping) or not vendor.get("id"):
        return None
    try:
        page = int(vendor.get("page") or finding.get("page") or 0)
    except (TypeError, ValueError):
        return None
    return (str(vendor.get("file") or main_file or ""), page, str(vendor["id"]))


def _decides(finding: Mapping[str, Any]) -> bool:
    """A Right, a Wrong, or the architect's other value the vendor follows: the item governs its
    label. A Not-compared note of an unsure reading does not, nor a conflict where the vendor
    differs from the architect too (the architect comparison speaks for the label: never red from
    GV's standard, but a difference from the architect stays the comparison's)."""
    if finding.get("status") in ("same", "different"):
        return True
    standard = finding.get("gv_standard")
    return (
        finding.get("keep_reason") == CONFLICT
        and isinstance(standard, Mapping)
        and bool(standard.get("follows_architect"))
    )


def decided_labels(
    live: Sequence[Mapping[str, Any]], main_file: str | None = None
) -> set[tuple[str, int, str]]:
    """The vendor labels this check decided: `(file, own page, label id)`."""
    return {
        key
        for finding in live
        if own_item(finding) and _decides(finding)
        if (key := _label_key(finding, main_file)) is not None
    }


def decided_items(
    live: Sequence[Mapping[str, Any]], main_file: str | None = None
) -> dict[tuple[str, int, str], Mapping[str, Any]]:
    """The items this check decided, by their vendor label `(file, own page, label id)`."""
    return {
        key: finding
        for finding in live
        if own_item(finding) and _decides(finding) and finding.get("id")
        if (key := _label_key(finding, main_file)) is not None
    }


def noted_labels(
    live: Sequence[Mapping[str, Any]], main_file: str | None = None
) -> dict[tuple[str, int, str], str]:
    """The vendor labels this check only noted (unsure: Not compared), with the note's id."""
    return {
        key: str(finding["id"])
        for finding in live
        if own_item(finding) and not _decides(finding) and finding.get("id")
        if (key := _label_key(finding, main_file)) is not None
    }


#: A simple check compare outcome as the finding status it stands for (`agrees`).
_OUTCOME_STATUS: Final[Mapping[str, str]] = {
    "red": "different",
    "candidate": "different",
    "right": "same",
}


def agrees(
    finding: Mapping[str, Any], status: object, architect: object = None, source: object = None
) -> bool:
    """Whether another item on the label (its status, its architect-side value and source) agrees
    with this check's decided item, so it may be replaced: one item per label, never a red or a
    Right hidden by a contradicting one. It disagrees when it says Right where this says Wrong (or
    the other way round), or when it names an architect value other than this item's own (the
    setting, or the architect's printed value): the architect then gives another value code did not
    see, so this check stands aside and the other item speaks."""
    mine = str(finding.get("status") or "")
    theirs = str(status or "")
    if theirs in ("same", "different"):
        if mine in ("same", "different") and theirs != mine:
            return False
        if mine == "check" and theirs == "different":
            return False  # the vendor follows the architect here; the other found a difference
    if str(source or "architect") != "architect":
        return True
    to_inches = import_module("app.review.assistant.finding_checks").to_inches
    given = to_inches(str(architect)) if architect not in (None, "") else None
    if given is None:
        return True
    expected = to_inches(str(finding.get("architect") or ""))
    return expected is not None and _same(Fraction(given), Fraction(expected))


def agrees_with_compare(finding: Mapping[str, Any], one: Any) -> bool:
    """`agrees` for a simple check compare item (`Verified`): its outcome and its finding's
    architect value and source."""
    found = getattr(one, "finding", None) or {}
    status = _OUTCOME_STATUS.get(str(getattr(one, "outcome", "")), found.get("status"))
    return agrees(finding, status, found.get("architect"), found.get("source"))


def _decided_by_reviewer(finding: Mapping[str, Any]) -> bool:
    decision = finding.get("decision")
    return isinstance(decision, Mapping) and bool(decision.get("decision"))


#: The fields of this check's items laid over what the finding book recorded.
_EXTRA: Final = ("settled", "gv_standard", "keep_reason", "gv_note")


def add_to_answer(engine: Any) -> dict[str, int] | None:
    """The engine's hook (a full check, at its finish, $0): each front offset item added through
    the finding book (its checks, trust rule and product scope). An item that decides its label
    replaces the answer's other items on it (one item per label); a Not-compared note is added only
    where the answer has none. Returns how many were added and replaced."""
    if getattr(engine.settings, "mode", None) != "full_check":
        return None
    workspace = engine.tools.workspace
    try:
        rules = (workspace.skills / "rules.md").read_text(encoding="utf-8")
    except (OSError, AttributeError, TypeError):
        rules = None
    setting = setting_from(rules)
    if setting is None:
        return None
    scope_module = import_module("app.review.assistant.product_scope")
    category = scope_module.sink_cutout_category(getattr(engine, "product_scope", None))
    if category is None:
        return None
    book = engine.book
    main_file = getattr(book, "main_file", None)
    found = check_pack(
        workspace.set_pack, getattr(workspace, "uploads", None), setting, main_file, category
    )
    if not found:
        return None
    trust = import_module("app.review.assistant.chat_trust")
    counts = {"added": 0, "replaced": 0}
    with engine.finding_lock:
        for finding in found:
            key = _label_key(finding, main_file)
            others = [
                other
                for other in book.live()
                if not own_item(other) and key is not None and _label_key(other, main_file) == key
            ]
            if not _decides(finding):
                if others:
                    continue  # the answer's own item on the label speaks for it
            else:
                if any(_decided_by_reviewer(other) for other in others):
                    continue  # the reviewer decided that label: left as it is
                if not all(
                    agrees(finding, o.get("status"), o.get("architect"), o.get("source"))
                    for o in others
                ):
                    continue  # another item contradicts it: that one speaks, never hidden
            added = book.add_code_finding(finding, created_by=CREATED_BY)
            if added is None:
                continue
            if _decides(finding):
                reason = (
                    "Settled by GV Review: the sink front offset, "
                    f"{(finding.get('gv_standard') or {}).get('origin', 'GV standard')} "
                    f"{(finding.get('gv_standard') or {}).get('value', '')}."
                )
                for other in others:
                    if book.absorb(str(other["id"])):
                        engine._show_dropped(str(other["id"]), reason)
                        counts["replaced"] += 1
            extra: dict[str, Any] = {k: finding[k] for k in _EXTRA if k in finding}
            if finding.get("checks"):
                extra["checks"] = {**(added.get("checks") or {}), **finding["checks"]}
            updated = trust.judged({**added, **extra})
            if book.replace(updated):
                added = updated
            engine._show_finding(added)
            counts["added"] += 1
    return counts
