"""The vendor consistency check (differentiator #3): the vendor's drawings checked against themselves,
by code, $0, no AI.

claude.ai reads a drawing set one picture at a time; it cannot cross-check every printed dimension
on every page. Code can, from the set pack's numbered dimension labels (`dims.json`, `dim_labels`)
and page map (`page_map.json`). Three checks, each built to report only what code can defend:

1. **Strings that don't add up.** A vendor dimension string is a row of chained dimensions (the
   parts) under or over an overall dimension. Code accepts a row as the overall's string only when
   the drawing proves it: the parts sit on one dimension line, touch end to end, and their outer
   ends land on the overall's own two extension lines (within `END_PT` points; true strings in the
   measured sets meet to 0.02 pt). An overall that is wider than its parts by an overhang or an
   inset (a top over its cabinets) does not meet them, so it is never added up: on Set 3 a plain
   adder raised 15 such false Wrongs (`TOOLS-design.md`, E1). Code then adds the parts exactly
   (`ledger.add_up`, the repo's chain check) and compares the sum with the printed overall
   (`TOLERANCE`, 1/16"). It also measures each dimension at its drawing's scale (the scale shared by
   at least `MIN_SCALE_SUPPORT` printed dimensions of that drawing): when two or more of the
   string's dimensions are not drawn to their own values, the labels may belong to other lines (or
   the view is not to scale), so it is not a finding (`doubt`). One that is off names the likely
   wrong number. A string that misses its overall by as little as 1/8" is a Wrong too, even when
   each value is its drawn length rounded by the CAD program (GV's rule: exact match, flag
   everything; Anant, 2026-10-23, after #1888 had called such a miss rounding). When the drawing
   cannot say which number is wrong, the red is the string as a whole: correct the parts or the
   overall.
2. **The same span printed twice.** Two of the vendor's dimension lines in one view (on the same
   pasted drawing, along the same axis, and proved to be in one projection: at each end one
   extension line runs across both lines, `_one_view`) that end on exactly the same two extension
   lines measure the same span (a part detail drawn beside an elevation may line up with it by
   chance, and is never paired); when they print different values,
   one of them is wrong. Code names which one by the drawing's scale (the one drawn to its value is
   the reference, the "should be"); when the scale cannot tell, it is not a finding. Two drawings
   on different pages share no coordinates, so a contradiction across sheets is left to the reading
   ledger (`record_reading`), where the AI names the item.
3. **Completeness** (the "could not compare" grey list, never a Wrong): an area the architect draws
   with no vendor sheet, a vendor sheet for an area the architect does not have, an area a later
   round leaves out when the same part of the set has that round elsewhere, and sheets the vendor's
   printed sheet index lists that no page shows.

And one please-check (`find_unsized_ends`, the owner's rule DECIDED 2026-10-27, never red): an
overall over a row of its pieces (the next row, touching end to end) that runs past them at BOTH
ends by more than GV's field cut per wall end, the leftover measured at the drawing's scale and
equal to the printed difference, with nothing printed or drawn in either end's gap and no label of
the drawing printing an end's size (a real upload: an overall over three cabinets with a few
inches unsized at each end). Code never adds such ends up (the 15 false Wrongs above), so the item is a Likely problem
("please check the ends", `likely_problems.unsized_ends`), status `check`. Where code cannot
measure the ends, sees something written there, or a settled rule (the field cut, a printed filler,
a field-cut overall) explains them, nothing is reported, as before. When the reading recorded the
same overall over the same pieces as a string that does not add up (never verified, Not compared),
code's please-check replaces it (`judge_unsized`) instead of being hidden as its duplicate.

Every finding is in the chat finding shape (CONTRACT2, `created_by: "consistency"`) with its code
witnesses: a string carries its parts and printed overall (code's `arithmetic` check) and the
overall's label (`dims`, the `printed` check); a contradiction carries both labels (`printed` on
both spots) and `reference_side: vendor`. The "should be" is the parts' sum for a string (the red
round the printed overall), or, when the drawing names a part as the wrong number, that part with
the overall less the other parts (`wrong_part`, #1887: the overall is right); and the reference
value for a contradiction, so the red/green cloud drawing, the screen, the CSV and the trust rule
use them like any other finding.

Labels read from the vendor's drawn ink (`route: drawn`, OCR) are used when the pack has them; a
finding resting on one says so, and the trust rule keeps it below Confirmed until a second read
agrees (#756 D2).

Pure code: JSON in, plain values out; the page texts for the sheet index are read by the caller
(`page_texts`, `set_pack`'s reader, loaded by name). Source: issue #1584 (this module),
`~/gv-local/research/agents/chat-first/DIFFERENTIATORS.md` #3 and `TOOLS-design.md` T1/T6.
Verification: `tests/review/assistant/test_vendor_consistency.py` (synthetic sheets).
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from importlib import import_module
from itertools import pairwise
from pathlib import Path
from time import monotonic
from typing import Any, Final, Literal
from uuid import uuid4

from app.review.assistant import field_cut, likely_problems, reading_routes, scale_fit
from app.review.assistant.finding_checks import WRONG_PART
from app.review.assistant.ledger import add_up, format_inches

__all__ = [
    "CREATED_BY",
    "END_PT",
    "GAP_PT",
    "LINE_WORK_DIR",
    "MIN_SCALE_SUPPORT",
    "ROW_APART_PT",
    "TOLERANCE",
    "UNSIZED_ENDS",
    "ConsistencyResult",
    "Label",
    "StringCheck",
    "UnsizedEnds",
    "check_pack",
    "check_set",
    "end_allowance",
    "find_unsized_ends",
    "judge_string",
    "judge_unsized",
    "label_rows",
    "line_work",
    "proved_chain",
    "proved_run",
    "read_label",
    "summary_line",
    "unsized_words",
    "warm_line_work",
]

_log = logging.getLogger(__name__)

#: Who made the findings (the chat finding's `created_by`).
CREATED_BY: Final = "consistency"
#: The printed values may differ by this much and still agree (the drawing's precision).
TOLERANCE: Final = Fraction(1, 16)
#: A string's outer ends land on the overall's extension lines within this many points (true
#: strings measured 0.00-0.02 pt; the narrowest overhang measured 1.4 pt).
END_PT: Final = 0.25
#: Two parts of a string meet within this many points (a tick mark's width is about 0.3 pt).
GAP_PT: Final = 0.6
#: Two parts also meet across a wider joint, up to this many line weights, when one tick marks
#: both ends (#1629: a joint drawn as a double extension line under one slash, 0.9-1.1 pt apart
#: on lines 0.72 pt wide). The usual rule of vector clean-up: snap ends within a small multiple of
#: the stroke width, never more than the drawing itself shows to be one point.
JOINT_WEIGHTS: Final = 2.5
#: Labels on one dimension line: their lines are this close across (points).
ROW_PT: Final = 0.6
#: Two dimension lines are different rows when at least this far apart (points).
ROW_APART_PT: Final = 2.0
#: A drawing's scale counts only when at least this many of its printed dimensions share it.
MIN_SCALE_SUPPORT: Final = 3
#: A dimension shares a scale when drawn within this of its value: max(1/8", 0.5 %).
_SUPPORT_IN: Final = Fraction(1, 8)
_SUPPORT_SHARE: Final = Fraction(1, 200)
#: A dimension is off its drawing's scale when drawn further than this from its value:
#: max(3/16", 1 %).
_OFF_IN: Final = Fraction(3, 16)
_OFF_SHARE: Final = Fraction(1, 100)
#: Dimensions shorter than this tell nothing about a drawing's scale (a 3/4" reveal is a point).
_SCALE_MIN_IN: Final = Fraction(2)
#: A slip, not a label read off another line: a dimension printed at most this far from what its
#: line measures (or from the other value of its span): max(3", 10 %).
_SLIP_IN: Final = Fraction(3)
_SLIP_SHARE: Final = Fraction(1, 10)
#: Two values printed for one span: each label sits over the middle of its line (this share of the
#: line kept clear at each end), so it is that line's own label.
_MIDDLE_SHARE: Final = 0.2
#: A cleanly read value: inches (`88 1/2"`), feet and inches (`3'-6 1/2"`), or millimetres over
#: inches (`915 [36]`). Anything else (`25 "`, a fraction's lost part) is not used for a finding.
_CLEAN: Final = re.compile(
    r"^(?:\d{1,4}(?: \d{1,2}/\d{1,2})?\"|\d{1,2}/\d{1,2}\"|"
    r"\d{1,3}' ?-? ?\d{1,2}(?: \d{1,2}/\d{1,2})?\"|\d{1,3}'|"
    r"\d{2,5} \[\d{1,4}(?: \d{1,2}/\d{1,2})?\])$"
)
#: A CAD program prints each dimension rounded to the drawing's step (1/8", or 1/16" where the
#: drawing prints sixteenths). A string that misses its overall is a Wrong even when that rounding
#: explains the miss (GV's rule: exact match, flag everything); the step is only used to name the
#: one wrong number of a string (`_odd_one`): the one value that is not the rounding of its own
#: drawn length when every other value is.
_ROUNDING_STEP_IN: Final = Fraction(1, 8)
_ROUNDING_FINE_IN: Final = Fraction(1, 16)
#: How far the true scale may sit from the drawing's (its printed dimensions share it within 1 %).
_ROUNDING_SCALE_SHARE: Final = 0.01
#: Where a line's end is, give or take (points): a joint's own gap is measured from its middle.
_ROUNDING_END_PT: Final = 0.05
#: An extension line reaches a dimension line (or its end) when within this many points.
_EXTENSION_PT: Final = 1.0
#: A label belongs to a drawing on the page when within this of its box (points), as `dim_labels`.
_HALF_PT: Final = 36.0
#: The most findings and grey items one set gives (a 300-page set stays readable).
MAX_FINDINGS: Final = 60
MAX_UNKNOWNS: Final = 60
#: The page texts' cache in the set pack (the sheet index reads them once per set).
TEXTS_FILE: Final = "page_texts.json"

type Verdict = Literal["adds_up", "does_not_add_up", "doubt"]
type Box = tuple[float, float, float, float]
type Point = tuple[float, float]
type Segment = tuple[Point, Point]
#: The page's line work (`dim_labels`' segments, points of the page as shown), by (file, page).
type Segments = Callable[[str, int], Sequence[Segment] | None]


# ---- labels --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Label:
    """One printed dimension with its dimension line, in the axis frame of its orientation."""

    id: str
    file: str
    page: int
    text: str
    inches: Fraction
    box: Box
    side: str
    orientation: str
    route: str
    units: str | None
    start: float
    """Where its dimension line starts along its axis (points)."""
    end: float
    across: float
    """Where its dimension line is across its axis (points)."""
    extensions: tuple[bool, bool]
    half: int | None
    raw: Mapping[str, Any] = field(repr=False, compare=False)

    @property
    def span(self) -> float:
        return self.end - self.start

    @property
    def bare(self) -> bool:
        """No inch mark, foot mark, fraction or bracket: a lone number (maybe a fragment)."""
        return not any(mark in self.text for mark in ('"', "'", "/", "[", "″"))

    @property
    def clean(self) -> bool:
        """Read cleanly as one value (see `_CLEAN`)."""
        return bool(_CLEAN.match(self.text))

    def centred(self, margin: float = _MIDDLE_SHARE) -> bool:
        """Its text sits over its own line, away from the line's ends by `margin` of the line."""
        x0, y0, x1, y1 = self.box
        middle = (x0 + x1) / 2 if self.orientation == "h" else (y0 + y1) / 2
        clear = margin * self.span
        return self.start + clear <= middle <= self.end - clear

    @property
    def ref(self) -> str:
        return f"p{self.page}.{self.id}"


def _fraction(value: object) -> Fraction | None:
    try:
        return Fraction(Decimal(str(value)))
    except (InvalidOperation, ValueError, ArithmeticError):
        return None


def _box(raw: object) -> Box | None:
    if not isinstance(raw, Sequence) or len(raw) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in raw)
    except (TypeError, ValueError):
        return None
    return x0, y0, x1, y1


def _half_of(box: Box, halves: Sequence[Mapping[str, Any]]) -> int | None:
    """Which of the page's pasted drawings the label is on (its index in the page map), or None."""
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    best: tuple[float, int] | None = None
    for index, half in enumerate(halves):
        hb = _box(half.get("box"))
        if hb is None:
            continue
        dx = max(hb[0] - cx, 0.0, cx - hb[2])
        dy = max(hb[1] - cy, 0.0, cy - hb[3])
        distance = (dx * dx + dy * dy) ** 0.5
        if best is None or distance < best[0]:
            best = (distance, index)
    if best is None or best[0] > _HALF_PT:
        return None
    return best[1]


def _label(
    raw: Mapping[str, Any], file: str, page: int, halves: Sequence[Mapping[str, Any]]
) -> _Label | None:
    """A dims.json label with a horizontal or vertical dimension line, else None."""
    inches = _fraction(raw.get("inches"))
    box = _box(raw.get("box"))
    line = raw.get("dim_line")
    orientation = raw.get("orientation")
    if inches is None or inches <= 0 or box is None or orientation not in ("h", "v"):
        return None
    if not isinstance(line, Sequence) or len(line) != 2:
        return None
    try:
        (x0, y0), (x1, y1) = ((float(p[0]), float(p[1])) for p in line)
    except (TypeError, ValueError, IndexError):
        return None
    if orientation == "h":
        start, end, across = min(x0, x1), max(x0, x1), (y0 + y1) / 2
    else:
        start, end, across = min(y0, y1), max(y0, y1), (x0 + x1) / 2
    if end - start <= 0:
        return None
    ext = raw.get("extension")
    extensions = (
        (bool(ext[0]), bool(ext[1]))
        if isinstance(ext, Sequence) and len(ext) == 2
        else (False,) * 2
    )
    return _Label(
        id=str(raw.get("id") or ""),
        file=file,
        page=page,
        text=" ".join(str(raw.get("text") or "").split()),
        inches=inches,
        box=box,
        side=str(raw.get("side") or "unknown"),
        orientation=str(orientation),
        route=str(raw.get("route") or "text"),
        units=raw.get("units") if isinstance(raw.get("units"), str) else None,
        start=start,
        end=end,
        across=across,
        extensions=extensions,
        half=_half_of(box, halves),
        raw=raw,
    )


# ---- the page map --------------------------------------------------------------------------------


@dataclass(slots=True)
class _Area:
    key: tuple[str, int]
    """(file, the area's first page)."""
    name: str
    part: int
    pages: list[int] = field(default_factory=list)
    architect: list[int] = field(default_factory=list)
    prototype: list[int] = field(default_factory=list)
    vendor_rounds: dict[int, list[int]] = field(default_factory=dict)
    """Round -> the pages that show the vendor's drawing of that round (round 1 when unknown)."""


_PART_TITLE: Final = re.compile(r"^\s*\d{1,3}\s*$")
_AREA_NUMBER: Final = re.compile(r"^\s*\d{1,3}\s*[.)]\s*")


def area_words(name: str) -> str:
    """`5.Reception desk` -> `Reception desk`."""
    return _AREA_NUMBER.sub("", name).strip() or name.strip()


def _entries(page_map: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    pages = page_map.get("pages") if isinstance(page_map, Mapping) else None
    return [
        e
        for e in pages or []
        if isinstance(e, Mapping) and isinstance(e.get("page"), int) and not e.get("skip")
    ]


def _areas(page_map: Mapping[str, Any] | None) -> dict[tuple[str, int], _Area]:
    """The set's areas, from GV's typed headings: an area starts on a page that names it and runs on
    the pages carried from it (`area_from_page`). A divider page whose section is a bare number
    ("1", "2") starts a new part of the set (a set can hold several jobs' drawings)."""
    areas: dict[tuple[str, int], _Area] = {}
    part = 0
    for entry in sorted(_entries(page_map), key=lambda e: (str(e.get("file")), int(e["page"]))):
        file, page = str(entry.get("file")), int(entry["page"])
        if entry.get("role") == "general" and _PART_TITLE.match(str(entry.get("section") or "")):
            part += 1
        name = entry.get("area")
        if not isinstance(name, str) or not name.strip():
            continue
        start = entry.get("area_from_page")
        key = (file, int(start) if isinstance(start, int) else page)
        area = areas.setdefault(key, _Area(key, name, part))
        area.pages.append(page)
        for half in entry.get("halves") or []:
            if not isinstance(half, Mapping):
                continue
            role = half.get("role")
            if role == "architect":
                area.architect.append(page)
            elif role == "brand_prototype":
                area.prototype.append(page)
            elif role == "vendor":
                number = half.get("round")
                area.vendor_rounds.setdefault(
                    int(number) if isinstance(number, int) and number > 0 else 1, []
                ).append(page)
    return areas


def _page_area(areas: Mapping[tuple[str, int], _Area]) -> dict[tuple[str, int], _Area]:
    return {(area.key[0], page): area for area in areas.values() for page in area.pages}


# ---- scale ---------------------------------------------------------------------------------------


def _drawn(label: _Label, pt_per_inch: float) -> Fraction:
    return Fraction(label.span / pt_per_inch).limit_denominator(1024)


def _close(label: _Label, pt_per_inch: float, absolute: Fraction, share: Fraction) -> bool:
    return abs(_drawn(label, pt_per_inch) - label.inches) <= max(absolute, share * label.inches)


def _scale(members: Sequence[_Label], drawing: Sequence[_Label]) -> tuple[float, int] | None:
    """The drawing's scale (points per inch) the members are drawn at: of the scales the members'
    own lines give, the one most of the drawing's printed dimensions share (at least
    `MIN_SCALE_SUPPORT`), and how many share it. None when no scale is shared enough."""
    best: tuple[int, float] | None = None
    for member in members:
        if member.inches < _SCALE_MIN_IN:
            continue
        candidate = member.span / float(member.inches)
        support = sum(
            1
            for other in drawing
            if other.inches >= _SCALE_MIN_IN
            and _close(other, candidate, _SUPPORT_IN, _SUPPORT_SHARE)
        )
        if best is None or support > best[0]:
            best = (support, candidate)
    if best is None or best[0] < MIN_SCALE_SUPPORT:
        return None
    return best[1], best[0]


def _off_scale(members: Sequence[_Label], pt_per_inch: float) -> list[_Label]:
    return [m for m in members if not _close(m, pt_per_inch, _OFF_IN, _OFF_SHARE)]


# ---- end marks -----------------------------------------------------------------------------------

#: An end mark (a tick, an arrowhead's edge, a dot's edge) touches the line's end within this.
_MARK_PT: Final = 0.8
_MARK_MIN_PT: Final = 0.8
_MARK_MAX_PT: Final = 8.0


def _tick_strokes(segments: Sequence[Segment], label: _Label, at: float) -> set[int]:
    """The end marks (as `_end_marked` finds them) touching the label's line at `at`, by index."""
    flip = label.orientation == "v"
    px, py = (label.across, at) if flip else (at, label.across)
    found: set[int] = set()
    for index, (a, b) in enumerate(segments):
        (ax, ay), (bx, by) = a, b
        dx, dy = bx - ax, by - ay
        length = (dx * dx + dy * dy) ** 0.5
        if not _MARK_MIN_PT <= length <= _MARK_MAX_PT:
            continue
        along, square = (abs(dy), abs(dx)) if flip else (abs(dx), abs(dy))
        if square < 0.17 * length or along < 0.17 * length:
            continue
        t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (length * length)))
        cx, cy = ax + t * dx - px, ay + t * dy - py
        if (cx * cx + cy * cy) ** 0.5 <= _MARK_PT:
            found.add(index)
    return found


def _end_marked(segments: Sequence[Segment], label: _Label, at: float) -> bool:
    """A tick or an arrowhead on the label's own dimension line at `at` (along its axis): a short
    piece of line work at a slant to the dimension line that touches the end. An extension line
    crossing there is not one (it is square to the line): a line cut at another dimension's
    extension line has no mark of its own, so it is never taken for that span."""
    return bool(_tick_strokes(segments, label, at))


def ends_marked(segments: Sequence[Segment] | None, label: _Label) -> bool | None:
    """Both ends of the label's line carry their own end marks (None: the line work is not known)."""
    if segments is None:
        return None
    return _end_marked(segments, label, label.start) and _end_marked(segments, label, label.end)


# ---- strings -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StringCheck:
    """One vendor dimension string code found, and what it says."""

    overall: _Label
    parts: tuple[_Label, ...]
    verdict: Verdict
    parts_sum: Fraction
    reason: str
    """Why a string is a doubt, or the drawing's hint for one that does not add up."""
    culprit: _Label | None = None
    culprit_drawn: Fraction | None = None

    @property
    def off_by(self) -> Fraction:
        return self.overall.inches - self.parts_sum

    @property
    def wrong_part(self) -> tuple[_Label, Fraction] | None:
        """The part the drawing names as the wrong number and what it should be: the overall,
        drawn to its own value, less the other parts (`20" - 2 5/8" - 3"` = `14 3/8"` for a 15"
        part drawn 14 3/8"). None when the likely wrong number is the overall (its should-be is
        the parts' sum) or the drawing names none."""
        culprit = self.culprit
        if culprit is None or culprit is self.overall or culprit not in self.parts:
            return None
        rest = sum((p.inches for p in self.parts if p is not culprit), Fraction(0))
        should = self.overall.inches - rest
        return (culprit, should) if should > 0 else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "page": self.overall.page,
            "file": self.overall.file,
            "overall": self.overall.id,
            "printed": self.overall.text,
            "parts": [p.id for p in self.parts],
            "parts_text": [p.text for p in self.parts],
            "sum": format_inches(self.parts_sum),
            "verdict": self.verdict,
            "reason": self.reason,
        }


def _rows(labels: Sequence[_Label]) -> list[list[_Label]]:
    """Labels on one dimension line (the same place across, within `ROW_PT`)."""
    rows: list[list[_Label]] = []
    for label in sorted(labels, key=lambda la: la.across):
        if rows and abs(label.across - rows[-1][-1].across) <= ROW_PT:
            rows[-1].append(label)
        else:
            rows.append([label])
    return rows


type Joint = Callable[[_Label, _Label], bool]


def _chain(
    overall: _Label, row: Sequence[_Label], joint: Joint | None = None
) -> list[_Label] | None:
    """The row's labels that tile the overall's span exactly (end to end, from one of its extension
    lines to the other), or None when the row is not its string. `joint` says whether two parts
    further apart than `GAP_PT` still meet (`_joint`)."""
    lo, hi = overall.start - GAP_PT, overall.end + GAP_PT
    inside = sorted((la for la in row if la.start >= lo and la.end <= hi), key=lambda la: la.start)
    # A dimension of the row that crosses one of the overall's ends: the row runs past it.
    if any(la not in inside and (la.start < lo < la.end or la.start < hi < la.end) for la in row):
        return None
    if len(inside) < 2:
        return None
    # The same value printed twice on one line (a text read two ways) is one dimension.
    if any(abs(a.start - b.start) <= GAP_PT for a, b in pairwise(inside)):
        return None
    if abs(inside[0].start - overall.start) > END_PT or abs(inside[-1].end - overall.end) > END_PT:
        return None
    for a, b in pairwise(inside):
        if abs(a.end - b.start) > GAP_PT and (joint is None or not joint(a, b)):
            return None
    return inside


def _joint(segments: Sequence[Segment], weight: float) -> Joint:
    """Two consecutive parts meet across a gap wider than `GAP_PT` when the gap is at most
    `JOINT_WEIGHTS` line weights and ONE tick marks both ends (the drafter's single joint over a
    doubled extension line). Two ticks, or none, are two places: never bridged."""

    def meets(a: _Label, b: _Label) -> bool:
        gap = b.start - a.end
        if gap < 0 or gap > max(GAP_PT, JOINT_WEIGHTS * weight):
            return False
        if a.orientation != b.orientation or abs(a.across - b.across) > ROW_PT:
            return False
        return bool(_tick_strokes(segments, a, a.end) & _tick_strokes(segments, b, b.start))

    return meets


def _judge(
    overall: _Label,
    parts: Sequence[_Label],
    drawing: Sequence[_Label],
    segments: Segments | None,
) -> StringCheck:
    total = sum((p.inches for p in parts), Fraction(0))
    result, _summed, _off = add_up([p.text for p in parts], overall.text)
    if abs(total - overall.inches) <= TOLERANCE or result == "adds_up":
        return StringCheck(overall, tuple(parts), "adds_up", total, "")
    members = [overall, *parts]

    def doubt(reason: str) -> StringCheck:
        return StringCheck(overall, tuple(parts), "doubt", total, reason)

    if result == "not_a_number":
        return doubt("a value is not one code can add")
    if any(m.bare or not m.clean for m in members):
        return doubt("a value is not read cleanly (it may be part of another number)")
    if any(m.units == "disagrees" for m in members):
        return doubt("a value's millimetres and inches disagree")
    if not all(overall.extensions) or not parts[0].extensions[0] or not parts[-1].extensions[1]:
        return doubt("code did not find both extension lines the string ends on")
    lines = segments(overall.file, overall.page) if segments is not None else None
    marked = [ends_marked(lines, m) for m in members]
    if None in marked:
        return doubt("the drawing's line work was not read, so its ends were not checked")
    if not all(marked):
        return doubt("a dimension line has no end mark of its own (it may be cut at another line)")
    scale = _scale(members, drawing)
    if scale is None:
        return doubt("the drawing's scale could not be told from its dimensions")
    # The parts drawn short of (or past) the overall's ends by the difference itself (#1866): two
    # 21 7/8" doors drawn 1/8" inside each end of a 44" cabinet print 43 3/4" because they are
    # drawn so (a reveal or an inset), not by a slip. `END_PT` lets such ends pass on a small
    # drawing (1/8" is 0.2 pt at 1/2" = 1'-0"), so the ends are measured against the difference.
    short = abs(parts[0].start - overall.start) + abs(parts[-1].end - overall.end)
    if short > 0 and Fraction(short / scale[0]).limit_denominator(1024) * 2 >= abs(
        total - overall.inches
    ):
        return doubt(
            "its parts are drawn short of the overall's ends by about the difference (a reveal "
            "or an inset), so they do not measure the same span"
        )
    off = _off_scale(members, scale[0])
    if len(off) >= 2:
        return doubt(
            "more than one of its dimensions is not drawn to its value (the labels may belong to "
            "other lines, or the view is not to scale)"
        )
    culprit = off[0] if off else _odd_one(overall, parts, drawing, scale[0])
    drawn = _drawn(culprit, scale[0]) if culprit is not None else None
    if (
        culprit is not None
        and drawn is not None
        and (not culprit.centred(0.0) or not _slip(culprit.inches, drawn))
    ):
        return doubt(
            f"its {culprit.text} label may belong to another line (its line measures "
            f"{format_inches(_sixteenths(drawn))})"
        )
    reason = (
        f"at the drawing's scale ({scale[1]} of its dimensions agree) the {culprit.text} "
        f"dimension is drawn as {format_inches(_sixteenths(drawn))}"
        if culprit is not None and drawn is not None
        else ""
    )
    return StringCheck(overall, tuple(parts), "does_not_add_up", total, reason, culprit, drawn)


def _rounding_step(drawing: Sequence[_Label]) -> Fraction:
    """The step the drawing's CAD program rounds its printed values to: 1/16" when it prints a
    sixteenth, else 1/8" (never coarser: a drawing of whole inches is not taken to round to 1")."""
    if any(label.inches.denominator % 16 == 0 for label in drawing):
        return _ROUNDING_FINE_IN
    return _ROUNDING_STEP_IN


type _Drawn = tuple[_Label, float, float]
"""A member of a string as drawn: its label, its length (points) and how far off that may be."""


def _drawn_members(overall: _Label, parts: Sequence[_Label]) -> list[_Drawn]:
    """The overall and each part as drawn: each part measured between the middles of its joints
    (`_ROUNDING_END_PT` give or take, plus half the joint's own gap)."""
    bounds = [overall.start]
    slack = [_ROUNDING_END_PT]
    for a, b in pairwise(parts):
        bounds.append((a.end + b.start) / 2)
        slack.append(_ROUNDING_END_PT + abs(b.start - a.end) / 2)
    bounds.append(overall.end)
    slack.append(_ROUNDING_END_PT)
    return [(overall, overall.span, 2 * _ROUNDING_END_PT)] + [
        (part, bounds[i + 1] - bounds[i], slack[i] + slack[i + 1]) for i, part in enumerate(parts)
    ]


def _one_scale(members: Sequence[_Drawn], step: Fraction, pt_per_inch: float) -> bool:
    """One true scale, within `_ROUNDING_SCALE_SHARE` of the drawing's, makes every member's
    printed value the rounding (to `step`) of its drawn length."""
    half = float(step) / 2
    low = pt_per_inch * (1 - _ROUNDING_SCALE_SHARE)
    high = pt_per_inch * (1 + _ROUNDING_SCALE_SHARE)
    for label, span, give in members:
        printed = float(label.inches)
        if printed - half <= 0 or span - give <= 0:
            return False
        low = max(low, (span - give) / (printed + half))
        high = min(high, (span + give) / (printed - half))
    return low <= high


def _odd_one(
    overall: _Label, parts: Sequence[_Label], drawing: Sequence[_Label], pt_per_inch: float
) -> _Label | None:
    """The one member of a string that does not add up whose printed value is not the rounding
    of its drawn length, when every other member's is (one true scale makes them all so,
    `_one_scale`) and no other member could be left out instead. A slip too small for the
    drawing's scale to show alone (`_OFF_IN`: an 8 1/8" part drawn 8 1/4"), named by the
    string's own lines. None when it is not just one."""
    if any(m.units is not None for m in (overall, *parts)):
        return None
    step = _rounding_step(drawing)
    members = _drawn_members(overall, parts)
    odd = [
        member[0]
        for member in members
        if _one_scale([m for m in members if m is not member], step, pt_per_inch)
    ]
    return odd[0] if len(odd) == 1 else None


def _slip(printed: Fraction, other: Fraction) -> bool:
    """Close enough to be a misprint of the same thing, not another dimension's label."""
    return abs(printed - other) <= max(_SLIP_IN, _SLIP_SHARE * max(printed, other))


def _sixteenths(value: Fraction) -> Fraction:
    return Fraction(round(value * 16), 16)


def find_strings(labels: Sequence[_Label], segments: Segments | None = None) -> list[StringCheck]:
    """Every vendor dimension string code can prove from the drawing (see the module).
    `segments` gives a page's line work, read only for a string that does not add up."""
    out: list[StringCheck] = []
    seen: set[tuple[Any, ...]] = set()
    joints: dict[tuple[str, int], Joint | None] = {}

    def joint_of(file: str, page: int) -> Joint | None:
        if (file, page) not in joints:
            lines = segments(file, page) if segments is not None else None
            weight = segments.weight(file, page) if isinstance(segments, LineWork) else None
            joints[(file, page)] = (
                None if lines is None or weight is None else _joint(lines, weight)
            )
        return joints[(file, page)]

    groups: dict[tuple[str, int, int | None], list[_Label]] = {}
    for label in labels:
        if label.side == "vendor" and label.half is not None:
            groups.setdefault((label.file, label.page, label.half), []).append(label)
    for members in groups.values():
        for orientation in ("h", "v"):
            axis = [la for la in members if la.orientation == orientation]
            rows = _rows(axis)
            for overall in axis:
                for row in rows:
                    if abs(row[0].across - overall.across) < ROW_APART_PT:
                        continue
                    parts = _chain(overall, row)
                    if parts is None:
                        # A wider joint is looked at only for a row that is otherwise its string.
                        parts = _chain(overall, row, joint_of(overall.file, overall.page))
                    if parts is None:
                        continue
                    key = (
                        overall.file,
                        overall.page,
                        overall.inches,
                        tuple(p.id for p in parts),
                    )
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append(_judge(overall, parts, members, segments))
    out.sort(key=lambda s: (s.overall.file, s.overall.page, s.overall.id))
    return out


# ---- an overall past its sized pieces (unsized ends) ---------------------------------------------

#: Rows of one stack of dimensions are at most this far apart across (points), as
#: `field_cut.STACK_PT`: an overall farther from its pieces' row is another view's.
_STACK_PT: Final = 60.0
#: An end mark (a tick, an arrowhead, a dot) lies within this of its station (points).
_MARK_REACH_PT: Final = _MARK_MAX_PT / 2
#: Something is written in an end's gap when at least this many short pen strokes lie there that
#: are not a dimension line, an extension line or an end mark (a drawn character takes several;
#: text drawn as strokes is not read, so code cannot tell what it says).
_TEXT_STROKES: Final = 4
#: A segment this close to square with an axis (share of its length) is a line, not a letter's
#: curve: dimension and extension lines are left out of the strokes counted.
_SQUARE_SHARE: Final = 0.17


@dataclass(frozen=True, slots=True)
class UnsizedEnds:
    """A vendor overall that runs past its sized pieces at both ends, the ends sized nowhere (the
    owner's rule, DECIDED 2026-10-27: a please-check, never red and never added up)."""

    overall: _Label
    parts: tuple[_Label, ...]
    parts_sum: Fraction
    ends: tuple[Fraction, Fraction]
    """Each end's leftover (inches) at the drawing's scale, to the nearest 1/16"."""
    support: int
    """How many of the drawing's printed dimensions share the scale it was measured at."""

    def as_dict(self) -> dict[str, Any]:
        return {
            "page": self.overall.page,
            "file": self.overall.file,
            "overall": self.overall.id,
            "printed": self.overall.text,
            "parts": [p.id for p in self.parts],
            "parts_text": [p.text for p in self.parts],
            "sum": format_inches(self.parts_sum),
            "ends": [format_inches(e) for e in self.ends],
        }


def _short_chain(
    overall: _Label, row: Sequence[_Label], joint: Joint | None = None
) -> list[_Label] | None:
    """The row's labels that touch end to end inside the overall's span and stop short of both
    its extension lines (by more than `END_PT`), or None. As `_chain`, but for a row that does not
    reach the overall's ends."""
    lo, hi = overall.start - GAP_PT, overall.end + GAP_PT
    inside = sorted((la for la in row if la.start >= lo and la.end <= hi), key=lambda la: la.start)
    if any(la not in inside and (la.start < lo < la.end or la.start < hi < la.end) for la in row):
        return None
    if len(inside) < 2:
        return None
    if any(abs(a.start - b.start) <= GAP_PT for a, b in pairwise(inside)):
        return None
    if inside[0].start - overall.start <= END_PT or overall.end - inside[-1].end <= END_PT:
        return None
    for a, b in pairwise(inside):
        if abs(a.end - b.start) > GAP_PT and (joint is None or not joint(a, b)):
            return None
    return inside


def _page_box(label: _Label, along: tuple[float, float], across: tuple[float, float]) -> Box:
    """A box of the page from a span along the label's axis and one across it."""
    if label.orientation == "h":
        return along[0], across[0], along[1], across[1]
    return across[0], along[0], across[1], along[1]


def _meets(a: Box, b: Box) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _written_in(
    gap: Box,
    stations: Sequence[Point],
    lines: Sequence[Segment],
) -> bool:
    """Whether pen strokes are drawn in the gap that are not a line or an end mark: text drawn as
    strokes, which code does not read (`_TEXT_STROKES`)."""
    count = 0
    for a, b in lines:
        if not all(gap[0] <= p[0] <= gap[2] and gap[1] <= p[1] <= gap[3] for p in (a, b)):
            continue
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = (dx * dx + dy * dy) ** 0.5
        if min(abs(dx), abs(dy)) <= _SQUARE_SHARE * length:
            continue  # a dimension line, an extension line, a straight part of the drawing
        if any(
            all(
                abs(p[0] - s[0]) <= _MARK_REACH_PT and abs(p[1] - s[1]) <= _MARK_REACH_PT
                for p in (a, b)
            )
            for s in stations
        ):
            continue  # an end mark
        count += 1
        if count >= _TEXT_STROKES:
            return True
    return False


#: Sides whose text never sizes a vendor's end.
_NOT_VENDOR: Final = frozenset({"architect", "brand_prototype"})


type _Marks = Callable[[str, int], Sequence[tuple[Box, Fraction | None, str | None, str]]]
"""A page's printed text code read: (box, inches when it is a size, orientation, label id)."""


def _page_marks(dims: Mapping[str, Any] | None) -> _Marks:
    """Every text of a page that may size an end: the vendor's labels (with or without a dimension
    line), its phrases that print a number, and the numbers an OCR engine read alone."""
    by_page: dict[tuple[str, int], list[tuple[Box, Fraction | None, str | None, str]]] = {}
    for entry in (dims or {}).get("pages") or []:
        if not isinstance(entry, Mapping):
            continue
        key = (str(entry.get("file")), int(entry.get("page") or 0))
        out = by_page.setdefault(key, [])
        for raw in entry.get("labels") or []:
            if not isinstance(raw, Mapping) or str(raw.get("side") or "") in _NOT_VENDOR:
                continue
            box = _box(raw.get("box"))
            if box is not None:
                orientation = raw.get("orientation")
                out.append(
                    (
                        box,
                        _fraction(raw.get("inches")),
                        orientation if isinstance(orientation, str) else None,
                        str(raw.get("id") or ""),
                    )
                )
        for phrase in entry.get("phrases") or []:
            if not isinstance(phrase, Mapping) or str(phrase.get("side") or "") in _NOT_VENDOR:
                continue
            box = _box(phrase.get("box"))
            if box is not None and any(ch.isdigit() for ch in str(phrase.get("text") or "")):
                out.append((box, None, None, ""))
        for name in ("ocr", "pictures"):
            found = entry.get(name)
            spots = found.get("unconfirmed") if isinstance(found, Mapping) else None
            for spot in spots or []:
                box = _box(spot.get("box")) if isinstance(spot, Mapping) else None
                if box is not None:
                    out.append((box, None, None, ""))

    def marks(file: str, page: int) -> Sequence[tuple[Box, Fraction | None, str | None, str]]:
        return by_page.get((file, page)) or ()

    return marks


def _end_sized(
    overall: _Label,
    parts: Sequence[_Label],
    gap: Box,
    measured: Fraction,
    marks: Sequence[tuple[Box, Fraction | None, str | None, str]],
    halves: Sequence[Mapping[str, Any]],
) -> bool:
    """Whether something printed may size this end: a text in its gap, or a vendor label of the
    same drawing and axis printing the end's measured size (a filler printed on another row)."""
    members = {overall.id, *(p.id for p in parts)}
    for box, inches, orientation, label_id in marks:
        if label_id and label_id in members:
            continue
        if _meets(box, gap):
            return True
        if (
            inches is not None
            and orientation in (None, overall.orientation)
            and _half_of(box, halves) == overall.half
            and abs(inches - measured) <= max(_OFF_IN, _OFF_SHARE * measured)
        ):
            return True
    return False


def find_unsized_ends(
    labels: Sequence[_Label],
    segments: Segments | None,
    *,
    strings: Sequence[StringCheck] = (),
    marks: _Marks | None = None,
    halves: Callable[[str, int], Sequence[Mapping[str, Any]]] | None = None,
    field_cuts: Callable[[str, int], Any] | None = None,
    allowance: Fraction = Fraction(1),
) -> list[UnsizedEnds]:
    """Every vendor overall that runs past its sized pieces at both ends, the ends sized nowhere,
    which code can measure from the drawing (DECIDED 2026-10-27, a real upload):

    - the pieces are one row of the same drawing next to the overall's own row (no row between,
      within `_STACK_PT`), touching end to end, inside the overall's span and short of both its
      extension lines; every one read cleanly, with its end marks, drawn to its value at the
      drawing's scale; the overall prints more than their sum, and the leftover drawn at the two
      ends is that difference (within the drawing's precision, `_OFF_IN` / `_OFF_SHARE`);
    - nothing sizes either end: no other row is the overall's string (`strings`), no text is
      printed or drawn in the end's gap, no label of the drawing prints the end's size, and the
      overall is not one that includes GV's field cut (`field_cuts`);
    - each end is more than GV's settled rules explain (`allowance`: the field cut per wall end,
      1" unless the project sets another; an end within it is a field cut or an overhang).

    Anything code cannot measure or see is left as before: no item."""
    if segments is None:
        return []
    proved = {(s.overall.file, s.overall.page, s.overall.id) for s in strings}
    out: list[UnsizedEnds] = []
    groups: dict[tuple[str, int, int | None], list[_Label]] = {}
    for label in labels:
        if label.side == "vendor" and label.half is not None:
            groups.setdefault((label.file, label.page, label.half), []).append(label)
    for (file, page, _half), members in groups.items():
        lines: Sequence[Segment] | None = None
        cuts = field_cuts(file, page) if field_cuts is not None else None
        cut_overalls = set(getattr(cuts, "overalls", None) or {})
        for orientation in ("h", "v"):
            axis = [la for la in members if la.orientation == orientation]
            rows = _rows(axis)
            for overall in axis:
                if (file, page, overall.id) in proved or overall.id in cut_overalls:
                    continue
                if not (overall.clean and not overall.bare and all(overall.extensions)):
                    continue
                near = sorted(
                    (
                        r
                        for r in rows
                        if ROW_APART_PT <= abs(r[0].across - overall.across) <= _STACK_PT
                    ),
                    key=lambda r: abs(r[0].across - overall.across),
                )
                for row in near:
                    low, high = sorted((row[0].across, overall.across))
                    if any(
                        low < other[0].across < high
                        and any(la.start < overall.end and la.end > overall.start for la in other)
                        for other in rows
                    ):
                        continue  # another row lies between them: not the overall's own pieces
                    if lines is None:
                        lines = segments(file, page) or ()
                    joint = (
                        _joint(lines, segments.weight(file, page) or 0.0)
                        if isinstance(segments, LineWork) and segments.weight(file, page)
                        else None
                    )
                    parts = _short_chain(overall, row, joint)
                    if parts is None:
                        continue
                    found = _unsized(
                        overall,
                        parts,
                        members,
                        lines,
                        marks(file, page) if marks is not None else (),
                        halves(file, page) if halves is not None else (),
                        allowance,
                    )
                    if found is not None:
                        out.append(found)
                    break  # the nearest row of pieces is the overall's; never a farther one
    out.sort(key=lambda u: (u.overall.file, u.overall.page, u.overall.id))
    return out


def _unsized(
    overall: _Label,
    parts: Sequence[_Label],
    drawing: Sequence[_Label],
    lines: Sequence[Segment],
    marks: Sequence[tuple[Box, Fraction | None, str | None, str]],
    halves: Sequence[Mapping[str, Any]],
    allowance: Fraction,
) -> UnsizedEnds | None:
    """The overall's unsized ends over these pieces (see `find_unsized_ends`), else None."""
    total = sum((p.inches for p in parts), Fraction(0))
    difference = overall.inches - total
    if difference <= 2 * allowance:
        return None  # within a field cut (or an overhang) at each end
    members = [overall, *parts]
    if any(m.bare or not m.clean or m.units == "disagrees" for m in members):
        return None
    if not parts[0].extensions[0] or not parts[-1].extensions[1]:
        return None
    if not all(ends_marked(lines, m) for m in members):
        return None
    scale = _scale(members, drawing)
    if scale is None or _off_scale(members, scale[0]):
        return None
    ppi = scale[0]
    left = Fraction((parts[0].start - overall.start) / ppi).limit_denominator(1024)
    right = Fraction((overall.end - parts[-1].end) / ppi).limit_denominator(1024)
    if abs(left + right - difference) > max(_OFF_IN, _OFF_SHARE * overall.inches):
        return None  # the drawing does not show the difference at the ends
    if min(left, right) <= allowance + _OFF_IN:
        return None  # one end is within a field cut (or an overhang): not unsized at each end
    pitch = abs(parts[0].across - overall.across)
    across = (
        min(parts[0].across, overall.across) - pitch,
        max(parts[0].across, overall.across) + pitch,
    )
    stations: list[Point] = []
    for at, row in (
        (overall.start, overall.across),
        (overall.end, overall.across),
        (parts[0].start, parts[0].across),
        (parts[-1].end, parts[-1].across),
    ):
        stations.append((at, row) if overall.orientation == "h" else (row, at))
    for (a, b), measured in (
        ((overall.start, parts[0].start), left),
        ((parts[-1].end, overall.end), right),
    ):
        gap = _page_box(overall, (a, b), across)
        if _written_in(gap, stations, lines):
            return None
        if _end_sized(overall, parts, gap, measured, marks, halves):
            return None
    return UnsizedEnds(
        overall, tuple(parts), total, (_sixteenths(left), _sixteenths(right)), scale[1]
    )


# ---- the same span printed twice -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Contradiction:
    reference: _Label
    odd: _Label
    support: int
    odd_drawn: Fraction


def _one_view(a: _Label, b: _Label) -> bool:
    """Whether two parallel dimension lines are proved to be in one view (one projection): at each
    end, one of their extension lines runs from one line to the other (a line drawn across both),
    within `_EXTENSION_PT`. A part detail beside an elevation, whose own extension lines stop at
    its own outline, lines up with it at most by chance, and is never proved."""
    if a.orientation != b.orientation:
        return False
    low, high = sorted((a.across, b.across))
    for index, at in ((0, a.start), (1, a.end)):
        found = False
        for member in (a, b):
            ends = member.raw.get("extension")
            if not isinstance(ends, Sequence) or len(ends) != 2:
                continue
            line = ends[index]
            if not isinstance(line, Sequence) or len(line) != 2:
                continue
            try:
                (fx, fy), (nx, ny) = ((float(p[0]), float(p[1])) for p in line)
            except (TypeError, ValueError, IndexError):
                continue
            along = fx if member.orientation == "h" else fy
            span = sorted((fy, ny) if member.orientation == "h" else (fx, nx))
            if (
                abs(along - at) <= _EXTENSION_PT
                and span[0] - _EXTENSION_PT <= low
                and high <= span[1] + _EXTENSION_PT
            ):
                found = True
                break
        if not found:
            return False
    return True


def find_contradictions(
    labels: Sequence[_Label], segments: Segments | None = None
) -> tuple[list[Contradiction], int]:
    """Pairs of the vendor's dimension lines in one drawing that end on the same two extension lines
    and print different values, with the one the drawing's scale backs as the reference. Also how
    many such pairs code could not settle (no scale, or both off it)."""
    out: list[Contradiction] = []
    unsettled = 0
    groups: dict[tuple[str, int, int | None, str], list[_Label]] = {}
    drawings: dict[tuple[str, int, int | None], list[_Label]] = {}
    for label in labels:
        if label.side != "vendor" or label.half is None:
            continue
        drawings.setdefault((label.file, label.page, label.half), []).append(label)
        if (
            label.bare
            or not label.clean
            or label.units == "disagrees"
            or label.inches < 1
            or not label.centred()
        ):
            continue
        groups.setdefault((label.file, label.page, label.half, label.orientation), []).append(label)
    done: set[tuple[str, str]] = set()
    for (file, page, half, _orientation), members in groups.items():
        drawing = drawings[(file, page, half)]
        for index, a in enumerate(members):
            same = [
                b
                for b in members[index + 1 :]
                if abs(b.start - a.start) <= END_PT
                and abs(b.end - a.end) <= END_PT
                and abs(b.across - a.across) >= ROW_APART_PT
                and abs(b.inches - a.inches) > TOLERANCE
                and _slip(a.inches, b.inches)
            ]
            for b in same:
                pair = tuple(sorted((a.ref, b.ref)))
                if pair in done:
                    continue
                done.add(pair)  # type: ignore[arg-type]
                if not _one_view(a, b):
                    # Not proved to be one projection: a part detail drawn beside an elevation
                    # can line up with it by chance (no false Wrong from that).
                    unsettled += 1
                    continue
                scale = _scale([a, b], drawing)
                if scale is None:
                    unsettled += 1
                    continue
                on = [m for m in (a, b) if _close(m, scale[0], _OFF_IN, _OFF_SHARE)]
                if len(on) != 1:
                    unsettled += 1
                    continue
                # Last (it reads the page's line work): each line ends in its own marks.
                lines = segments(file, page) if segments is not None else None
                if not (ends_marked(lines, a) and ends_marked(lines, b)):
                    unsettled += 1
                    continue
                reference = on[0]
                odd = b if reference is a else a
                out.append(Contradiction(reference, odd, scale[1], _drawn(odd, scale[0])))
    out.sort(key=lambda c: (c.odd.file, c.odd.page, c.odd.id))
    return out, unsettled


# ---- findings ------------------------------------------------------------------------------------


def _provenance(label: _Label) -> dict[str, Any]:
    """As `dim_findings._provenance`: which label a side was built from (the `printed` check)."""
    return {
        "id": label.id,
        "page": label.page,
        "file": label.file,
        "text": label.text,
        "inches": label.raw.get("inches"),
        "route": label.route,
        "side": label.side,
        "box": list(label.box),
    }


def _box_dict(label: _Label) -> dict[str, Any]:
    x0, y0, x1, y1 = label.box
    return {"page": label.page, "x0": x0, "y0": y0, "x1": x1, "y1": y1, "file": label.file}


def _direction(label: _Label) -> str:
    return "across" if label.orientation == "h" else "up and down"


def _round_words(label: _Label, page_map: Mapping[tuple[str, int], Mapping[str, Any]]) -> str:
    entry = page_map.get((label.file, label.page)) or {}
    halves = entry.get("halves") or []
    if label.half is not None and label.half < len(halves):
        number = (
            halves[label.half].get("round") if isinstance(halves[label.half], Mapping) else None
        )
        if isinstance(number, int) and number > 0:
            return f", round {number}"
    return ""


def _drawn_note(labels: Iterable[_Label]) -> str:
    drawn = [la for la in labels if la.route == "drawn"]
    if not drawn:
        return ""
    names = ", ".join(la.text for la in drawn)
    return f" {names} read from the vendor's drawn ink (OCR): a second read must agree."


def _string_finding(
    check: StringCheck, area: str | None, entries: Mapping[tuple[str, int], Mapping[str, Any]]
) -> dict[str, Any]:
    overall, parts = check.overall, check.parts
    pieces = " + ".join(p.text for p in parts)
    total = format_inches(check.parts_sum)
    sum_text = (
        f"{pieces} = {total} ≠ {overall.text} printed (off {format_inches(abs(check.off_by))})"
    )
    place = f"{area}: " if area else ""
    why = (
        f"The vendor's parts don't add up: {sum_text}. Code checked the drawing: the parts touch "
        "end to end and end on the overall's own extension lines, so they are one string."
    )
    wrong = check.wrong_part
    if wrong is not None:
        part, should = wrong
        rest = " - ".join(p.text for p in parts if p is not part)
        why += (
            f" The drawing names the wrong number: {check.reason}, and the {overall.text} overall "
            f"is drawn to its value, so the {part.text} part should be {format_inches(should)} "
            f"({overall.text} - {rest})."
        )
    elif check.reason:
        why += f" A hint: {check.reason}."
    if check.culprit is None:
        # GV's rule (exact match, flag everything): even a 1/8" miss is a Wrong. Code never
        # guesses which number the vendor must change when the drawing does not show it.
        why += (
            f" The parts add up to {total} but the overall says {overall.text} "
            f"({format_inches(abs(check.off_by))} off), and the drawing does not show which "
            "number is wrong: correct the parts or the overall."
        )
    why += _drawn_note([overall, *parts])
    # The red cloud goes round the wrong number and the green says what it should be: the part
    # the drawing names, else the printed overall against the parts' sum.
    marked = overall if wrong is None else wrong[0]
    finding: dict[str, Any] = {
        "page": overall.page,
        "item": f"{place}dimension string ({_direction(overall)}{_round_words(overall, entries)})",
        "category": "other",
        "architect": total if wrong is None else format_inches(wrong[1]),
        "vendor": marked.text,
        "status": "different",
        "why": why,
        "verified": False,
        "source": "vendor_arithmetic",
        "parts": [p.text for p in parts],
        "printed_total": overall.text,
        "vendor_box": _box_dict(marked),
        "dims": {"vendor": _provenance(marked)},
        "sum_text": sum_text,
        "created_by": CREATED_BY,
        "ledger_ids": [p.ref for p in (overall, *parts)],
    }
    if wrong is not None:
        finding[WRONG_PART] = {
            "text": wrong[0].text,
            "should_be": str(Decimal(wrong[1].numerator) / Decimal(wrong[1].denominator)),
            "label": wrong[0].ref,
            # How long the wrong part's line is drawn, at the drawing's scale (the screen's proof).
            "drawn": (
                format_inches(_sixteenths(check.culprit_drawn))
                if check.culprit_drawn is not None
                else None
            ),
            "overall": overall.text,
        }
    return finding


#: The finding's record of an overall past its sized pieces (`likely_problems.UNSIZED_ENDS`).
UNSIZED_ENDS: Final = likely_problems.UNSIZED_ENDS


def _ends_words(ends: tuple[Fraction, Fraction]) -> tuple[str, str]:
    """("about 3"", "at each end"), or ("about 3" and 4 1/2"", "at its two ends") (to 1/4")."""
    left, right = (Fraction(round(e * 4), 4) for e in ends)
    if left == right:
        return f"about {format_inches(left)}", "at each end"
    return f"about {format_inches(left)} and {format_inches(right)}", "at its two ends"


def unsized_words(overall: str, ends: tuple[Fraction, Fraction], pieces: str, total: str) -> str:
    """The please-check line of an overall past its sized pieces (owner's wording, 2026-10-27):
    "Please check and confirm: the vendor's 49" overall runs about 2 1/2" past the sized pieces (20"
    + 12" + 12" = 44") at each end; nothing sizes those ends (unsized fillers or ends). Please check
    the ends." (made-up values)"""
    by, where = _ends_words(ends)
    return (
        f"{likely_problems.LETTER_LEAD}: the vendor's {overall} overall runs {by} past the sized "
        f"pieces ({pieces} = {total}) {where}; nothing sizes those ends (unsized fillers or "
        "ends). Please check the ends."
    )


def _unsized_finding(
    found: UnsizedEnds, area: str | None, entries: Mapping[tuple[str, int], Mapping[str, Any]]
) -> dict[str, Any]:
    """A please-check item (status `check`, never red): the vendor's overall, its sized pieces and
    the leftover code measured at each end."""
    overall, parts = found.overall, found.parts
    pieces = " + ".join(p.text for p in parts)
    total = format_inches(found.parts_sum)
    place = f"{area}: " if area else ""
    words = unsized_words(overall.text, found.ends, pieces, total)
    said = words[len(likely_problems.LETTER_LEAD) + 2 :]
    why = (
        f"{said[:1].upper()}{said[1:]} Code checked the drawing: the pieces touch end to end on "
        "the row next to the overall, all are drawn to scale, and the "
        f"{format_inches(found.overall.inches - found.parts_sum)} difference is drawn at the two "
        "ends, where no size is printed. GV's field cut per wall end does not explain it. Code "
        "never adds unsized ends up (an overhang or a filler can be there), so this is never red."
        + _drawn_note([overall, *parts])
    )
    return {
        "page": overall.page,
        "item": (
            f"{place}overall past its sized pieces ({_direction(overall)}"
            f"{_round_words(overall, entries)})"
        ),
        "category": "other",
        "architect": f"sized pieces {pieces} = {total}",
        "vendor": overall.text,
        "status": "check",
        "why": why,
        "verified": False,
        "source": "vendor_arithmetic",
        "parts": [p.text for p in parts],
        "printed_total": overall.text,
        "vendor_box": _box_dict(overall),
        "dims": {"vendor": _provenance(overall)},
        "reference_side": "vendor",
        "created_by": CREATED_BY,
        "ledger_ids": [p.ref for p in (overall, *parts)],
        UNSIZED_ENDS: {
            "words": words,
            "overall": overall.text,
            "parts": [p.text for p in parts],
            "sum": total,
            "ends": [format_inches(e) for e in found.ends],
        },
    }


def end_allowance(rules_md: str | None) -> Fraction:
    """How far past its sized pieces an overall may run at each end by GV's settled rules: the
    field cut per wall end (`field_cut.per_end_from`: 1", or the project's value), or the job's
    countertop overhang when GV entered one and it is more."""
    from app.review.assistant.sink_offset import setting_from

    allowance = field_cut.per_end_from(rules_md)
    overhang = setting_from(rules_md, "countertop_overhang", "countertop overhang")
    return max(allowance, overhang.value) if overhang is not None else allowance


def _contradiction_finding(
    found: Contradiction,
    area: str | None,
    entries: Mapping[tuple[str, int], Mapping[str, Any]],
) -> dict[str, Any]:
    ref, odd = found.reference, found.odd
    place = f"{area}: " if area else ""
    why = (
        f"The vendor prints {ref.text} and {odd.text} for the same span on this drawing: both "
        "dimension lines end on the same two extension lines (code measured them). At the "
        f"drawing's scale ({found.support} of its dimensions agree) the span is {ref.text}; "
        f"{odd.text} is drawn as {format_inches(_sixteenths(found.odd_drawn))}."
    )
    why += _drawn_note([ref, odd])
    return {
        "page": odd.page,
        "item": f"{place}same span printed twice ({_direction(odd)}{_round_words(odd, entries)})",
        "category": "other",
        "architect": ref.text,
        "vendor": odd.text,
        "status": "different",
        "why": why,
        "verified": False,
        "source": "cross_view",
        "reference_side": "vendor",
        "arch_box": _box_dict(ref),
        "vendor_box": _box_dict(odd),
        "dims": {"architect": _provenance(ref), "vendor": _provenance(odd)},
        "created_by": CREATED_BY,
        "ledger_ids": [ref.ref, odd.ref],
    }


# ---- completeness --------------------------------------------------------------------------------

_NEEDS: Final = {
    "no_counterpart_vendor": "the vendor's shop drawing for this area (ask the vendor, or check it "
    "was uploaded)",
    "no_counterpart_architect": "the architect's drawing for this area (or confirm it is the "
    "vendor's addition)",
    "missing_round": "the vendor's drawing of this area for that round, or confirm it did not "
    "change",
    "not_in_set": "the sheet the vendor's index lists (ask the vendor, or check it was uploaded)",
}


def _unknown(page: int | None, what: str, why: str, needs: str) -> dict[str, Any]:
    return {"page": page, "what": what, "why": why, "needs": needs, "scope": "set"}


#: A vendor page whose numbers are drawn as lines code could not read (`set_pack.NUMBERS_UNREAD`).
UNREADABLE_WORDS: Final = "the numbers on this page can't be read (drawn as lines, not text)"
UNREADABLE_NEEDS: Final = (
    "a person reads the numbers on the drawing; ask the vendor to save its PDFs with real text "
    "(Save as PDF or Export from the drawing program, not Print to PDF)"
)


def unreadable_pages(page_map: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """A grey item for each vendor page whose numbers are only pen strokes code could not read
    (owner, 2026-10-23: "flag everything"): said on the page, never silently a general page."""
    out: list[dict[str, Any]] = []
    for entry in _entries(page_map):
        if entry.get("numbers_unread") != "strokes":
            continue
        page = int(entry["page"])
        out.append(
            {
                **_unknown(
                    page, f"Vendor page {page}: {UNREADABLE_WORDS}", "unreadable", UNREADABLE_NEEDS
                ),
                "scope": "page",
            }
        )
    return out


def completeness(page_map: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """The "could not compare" grey items the page map shows (areas on one side only, rounds left
    out). Never a Wrong: the drawings decide, and a reviewer may know why a sheet is missing."""
    areas = _areas(page_map)
    out: list[dict[str, Any]] = []
    if not areas:
        return out
    # Areas are compared side to side only where the set is organised by area on both sides.
    paired = [a for a in areas.values() if a.architect and a.vendor_rounds]
    architect_by_area = bool(paired) and all(
        bool(e.get("area"))
        for e in _entries(page_map)
        for h in e.get("halves") or []
        if isinstance(h, Mapping) and h.get("role") == "architect"
    )
    latest: dict[int, int] = {}
    for area in areas.values():
        if area.vendor_rounds:
            latest[area.part] = max(latest.get(area.part, 0), *area.vendor_rounds)
    for area in sorted(areas.values(), key=lambda a: a.key):
        name = area_words(area.name)
        first = min(area.pages)
        if paired and area.architect and not area.vendor_rounds:
            out.append(
                _unknown(
                    first,
                    f"{name}: architect drawing, no vendor sheet",
                    "no_counterpart",
                    _NEEDS["no_counterpart_vendor"],
                )
            )
        if architect_by_area and area.vendor_rounds and not area.architect:
            out.append(
                _unknown(
                    first,
                    f"{name}: vendor sheet, no architect drawing"
                    + (" (only the brand prototype)" if area.prototype else ""),
                    "no_counterpart",
                    _NEEDS["no_counterpart_architect"],
                )
            )
        if area.vendor_rounds:
            have = max(area.vendor_rounds)
            want = latest.get(area.part, have)
            if have < want:
                pages = area.vendor_rounds[have]
                out.append(
                    _unknown(
                        min(pages),
                        f"{name}: round {want} does not include this area (round {have} did, "
                        f"p.{', '.join(str(p) for p in sorted(set(pages)))})",
                        "missing_round",
                        _NEEDS["missing_round"],
                    )
                )
    return out


#: A printed sheet index: its heading, then the sheet numbers it lists.
_INDEX_HEADING: Final = re.compile(
    r"\b(?:SHEET|DRAWING)S?\s+(?:INDEX|LIST)\b|\bINDEX\s+OF\s+(?:SHEETS|DRAWINGS)\b|"
    r"\bLIST\s+OF\s+(?:SHEETS|DRAWINGS)\b",
    re.IGNORECASE,
)
_SHEET_CODE: Final = re.compile(r"\b((?:SD|SH|MW|A|ID|M|D|S)-?\d{1,3}(?:\.\d{1,2})?[A-Z]?)\b")
#: An index lists at least this many sheets (fewer is a note, not an index).
_INDEX_MIN: Final = 3


def _norm_code(code: str) -> str:
    return code.upper().replace("-", "").replace(" ", "")


def sheet_index(
    texts: Mapping[tuple[str, int], str], page_map: Mapping[str, Any] | None
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The vendor's printed sheet index against the pages present: each sheet the index lists that
    no other page prints (a grey item), and what was found (`{index_pages, listed, missing}`)."""
    found: dict[str, Any] = {"index_pages": [], "listed": 0, "missing": []}
    index: dict[str, tuple[str, int]] = {}
    for (file, page), text in sorted(texts.items()):
        heading = _INDEX_HEADING.search(text or "")
        if heading is None:
            continue
        codes = [_norm_code(c) for c in _SHEET_CODE.findall(text[heading.end() :])]
        if len(set(codes)) < _INDEX_MIN:
            continue
        found["index_pages"].append(page)
        for code in codes:
            index.setdefault(code, (file, page))
    if not index:
        return [], found
    present: set[str] = set()
    for (file, page), text in texts.items():
        if found["index_pages"] and page in found["index_pages"]:
            continue
        present |= {_norm_code(c) for c in _SHEET_CODE.findall(text or "")}
    for entry in _entries(page_map):
        present |= {_norm_code(str(c)) for c in entry.get("sheet_numbers") or []}
    # The index's own sheet (a cover lists itself first) is the page the index is printed on.
    present.add(next(iter(index)))
    found["listed"] = len(index)
    out: list[dict[str, Any]] = []
    for code, (_file, page) in sorted(index.items()):
        if code in present:
            continue
        found["missing"].append(code)
        out.append(
            _unknown(
                page,
                f"Sheet {code}: in the vendor's sheet index, on no page of the set",
                "not_in_set",
                _NEEDS["not_in_set"],
            )
        )
    return out, found


# ---- the whole check -----------------------------------------------------------------------------


@dataclass(slots=True)
class ConsistencyResult:
    """What the check found in one set."""

    findings: list[dict[str, Any]] = field(default_factory=list)
    """Chat findings (CONTRACT2 shape, without ids): Wrong items code proved."""
    unknowns: list[dict[str, Any]] = field(default_factory=list)
    """Grey "could not compare" items (`chat_unknowns` shape, without ids, `scope: set`)."""
    strings: dict[str, int] = field(default_factory=dict)
    """Counts: `found`, `adds_up`, `does_not_add_up`, `doubt`."""
    contradictions: int = 0
    unsettled: int = 0
    vendor_labels: int = 0
    vendor_pages_without_labels: list[int] = field(default_factory=list)
    sheet_index: dict[str, Any] = field(default_factory=dict)
    seconds: float = 0.0
    checks: list[StringCheck] = field(default_factory=list, repr=False)
    unsized: list[UnsizedEnds] = field(default_factory=list, repr=False)
    """Overalls past their sized pieces at both ends (`find_unsized_ends`): please-check items."""

    def as_dict(self) -> dict[str, Any]:
        return {
            "strings": dict(self.strings),
            "contradictions": self.contradictions,
            "unsettled": self.unsettled,
            "vendor_labels": self.vendor_labels,
            "vendor_pages_without_labels": list(self.vendor_pages_without_labels),
            "sheet_index": dict(self.sheet_index),
            "findings": len(self.findings),
            "unknowns": len(self.unknowns),
            "seconds": self.seconds,
            "string_checks": [s.as_dict() for s in self.checks if s.verdict != "adds_up"],
            "unsized_ends": [u.as_dict() for u in self.unsized],
        }


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def summary_line(result: ConsistencyResult) -> str:
    """One plain line for the full check's summary."""
    strings = result.strings
    found = strings.get("found", 0)
    wrong = strings.get("does_not_add_up", 0)
    bits = [
        (
            f"{_plural(found, 'dimension string', 'dimension strings')} added up"
            + (f", {wrong} {'does' if wrong == 1 else 'do'} not" if wrong else ", all add up")
            if found
            else "no dimension string code could prove from the drawing"
        )
    ]
    if result.contradictions:
        bits.append(
            _plural(
                result.contradictions,
                "span printed with two values",
                "spans printed with two values",
            )
        )
    if result.unsized:
        bits.append(
            _plural(
                len(result.unsized),
                "overall runs past its sized pieces",
                "overalls run past their sized pieces",
            )
            + " at both ends (please check)"
        )
    if result.unknowns:
        bits.append(_plural(len(result.unknowns), "sheet gap", "sheet gaps") + " (see Not checked)")
    if result.vendor_pages_without_labels:
        pages = result.vendor_pages_without_labels
        shown = ", ".join(str(p) for p in pages[:8]) + (" and more" if len(pages) > 8 else "")
        bits.append(f"pages {shown} have no readable vendor numbers yet")
    return "Vendor consistency (by code, $0): " + "; ".join(bits) + "."


def check_set(
    dims: Mapping[str, Any] | None,
    page_map: Mapping[str, Any] | None,
    *,
    texts: Mapping[tuple[str, int], str] | None = None,
    segments: Segments | None = None,
    clock: Callable[[], float] = monotonic,
    index: Any = None,
    typed: reading_routes.TypedOf | None = None,
    numbers: reading_routes.NumberOf | None = None,
    rules_md: str | None = None,
) -> ConsistencyResult:
    """The vendor consistency check over one set's labels and page map (see the module).
    `segments` reads a page's line work (for the end marks); without it nothing is reported.
    `index`: the set index (`set_index.json`), for telling an older copy of a sheet from the
    same sheet number reused in another part of the set (`sheet_revisions`). `typed`: both OCR
    engines on a typed number's spot (`typed_ocr`), its second reading before a red; `numbers`: the
    number reader on a label (`read_number.label_routes`), for a number still short of two.
    `rules_md`: the chat's `rules.md` (the field cut per wall end in force; GV's 1" without it)."""
    started = clock()
    entries = {(str(e.get("file")), int(e["page"])): e for e in _entries(page_map)}
    labels: list[_Label] = []
    vendor_pages: dict[tuple[str, int], int] = {}
    older = _older_pages(dims, page_map, texts, index)
    for entry in (dims or {}).get("pages") or []:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("labels"), list):
            continue
        file, page = str(entry.get("file")), int(entry.get("page") or 0)
        if (file, page) in older:
            continue  # an older copy of a sheet printed again later: only the newest is checked
        halves = [
            h
            for h in (entries.get((file, page)) or {}).get("halves") or []
            if isinstance(h, Mapping)
        ]
        for raw in entry["labels"]:
            if not isinstance(raw, Mapping):
                continue
            if raw.get("side") == "vendor":
                vendor_pages[(file, page)] = vendor_pages.get((file, page), 0) + 1
            made = _label(raw, file, page, halves)
            if made is not None:
                labels.append(made)
    result = ConsistencyResult()
    result.vendor_labels = sum(vendor_pages.values())
    result.vendor_pages_without_labels = sorted(
        page
        for (file, page), entry in entries.items()
        if any(
            isinstance(h, Mapping) and h.get("role") == "vendor" for h in entry.get("halves") or []
        )
        and not vendor_pages.get((file, page))
    )
    areas = _page_area(_areas(page_map))

    def area_of(label: _Label) -> str | None:
        area = areas.get((label.file, label.page))
        return area_words(area.name) if area is not None else None

    checks = find_strings(labels, segments)
    result.checks = checks
    result.strings = {
        "found": len(checks),
        "adds_up": sum(1 for c in checks if c.verdict == "adds_up"),
        "does_not_add_up": sum(1 for c in checks if c.verdict == "does_not_add_up"),
        "doubt": sum(1 for c in checks if c.verdict == "doubt"),
    }
    reported: set[tuple[str, int, Fraction]] = set()
    for check in checks:
        if check.verdict != "does_not_add_up":
            continue
        # One finding per printed overall: two strings that both miss it are the same slip.
        key = (check.overall.file, check.overall.page, check.overall.inches)
        if key in reported:
            continue
        reported.add(key)
        result.findings.append(_string_finding(check, area_of(check.overall), entries))
    contradictions, result.unsettled = find_contradictions(labels, segments)
    result.contradictions = len(contradictions)
    for found in contradictions:
        result.findings.append(_contradiction_finding(found, area_of(found.odd), entries))
    raw_pages = {
        (str(e.get("file")), int(e.get("page") or 0)): e
        for e in (dims or {}).get("pages") or []
        if isinstance(e, Mapping)
    }
    per_end = field_cut.per_end_from(rules_md)

    def cuts_of(file: str, page: int) -> field_cut.PageFieldCuts:
        entry = raw_pages.get((file, page)) or {}
        return field_cut.page_field_cuts(
            [la for la in entry.get("labels") or [] if isinstance(la, Mapping)],
            [ph for ph in entry.get("phrases") or [] if isinstance(ph, Mapping)],
            per_end,
        )

    def halves_of(file: str, page: int) -> Sequence[Mapping[str, Any]]:
        halves = (entries.get((file, page)) or {}).get("halves") or []
        return [h for h in halves if isinstance(h, Mapping)]

    result.unsized = find_unsized_ends(
        labels,
        segments,
        strings=checks,
        marks=_page_marks(dims),
        halves=halves_of,
        field_cuts=cuts_of,
        allowance=end_allowance(rules_md),
    )
    for unsized in result.unsized:
        result.findings.append(_unsized_finding(unsized, area_of(unsized.overall), entries))
    result.findings = result.findings[:MAX_FINDINGS]
    _mark_reading(result.findings, dims, segments, typed, numbers)
    unknowns = completeness(page_map) + unreadable_pages(page_map)
    if texts:
        missing, result.sheet_index = sheet_index(texts, page_map)
        unknowns += missing
    unknowns.sort(key=lambda u: (u["page"] is None, u["page"] or 0))
    result.unknowns = unknowns[:MAX_UNKNOWNS]
    result.seconds = round(clock() - started, 3)
    return result


def _mark_reading(
    findings: Sequence[dict[str, Any]],
    dims: Mapping[str, Any] | None,
    segments: Segments | None,
    typed: reading_routes.TypedOf | None = None,
    numbers: reading_routes.NumberOf | None = None,
) -> None:
    """Each finding's record of how its numbers were read (`reading_routes.mark`, final plan #2):
    every part and the overall of a string, both prints of a span, each with its reading route,
    millimetres and drawn length at its view's fit (`scale_fit.page_fit`). Never raises."""
    if not findings:
        return
    by_page: dict[tuple[str, int], list[Mapping[str, Any]]] = {}
    for entry in (dims or {}).get("pages") or []:
        if isinstance(entry, Mapping) and isinstance(entry.get("labels"), list):
            key = (str(entry.get("file")), int(entry.get("page") or 0))
            by_page[key] = [la for la in entry["labels"] if isinstance(la, Mapping)]
    fits: dict[tuple[str, int], Any] = {}

    def labels_of(file: str, page: int) -> Sequence[Mapping[str, Any]]:
        return by_page.get((file, page)) or ()

    def fit_of(file: str, page: int) -> Any:
        key = (file, page)
        if key not in fits:
            lines = segments(file, page) if segments is not None else None
            fits[key] = scale_fit.page_fit(by_page.get(key) or (), lines)
        return fits[key]

    for finding in findings:
        try:
            reading_routes.mark(finding, labels_of, fit_of, typed, numbers)
        except Exception:  # noqa: BLE001 - no record: the item is judged on its label alone
            _log.exception("vendor consistency: reading record failed")


def _older_pages(
    dims: Mapping[str, Any] | None,
    page_map: Mapping[str, Any] | None,
    texts: Mapping[tuple[str, int], str] | None,
    index: Any = None,
) -> frozenset[tuple[str, int]]:
    """The vendor pages that are older copies of a sheet printed again later in their file (an
    earlier revision, `sheet_revisions`): their sizes are the superseded ones, never compared
    with the newest copy's. Empty without the pages' printed text."""
    if not texts or not page_map:
        return frozenset()
    revisions = import_module("app.review.assistant.sheet_revisions")
    out: set[tuple[str, int]] = set()
    for file in [str(f) for f in page_map.get("files") or [] if f]:
        exact = revisions.exact_copies(file, dims, texts)
        older = revisions.older_copies(page_map, texts, file, exact=exact, index=index)
        out |= {(file, page) for page in older}
    return frozenset(out)


# ---- the set pack --------------------------------------------------------------------------------


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def page_texts(folder: Path, files: Sequence[str]) -> dict[tuple[str, int], str]:
    """Each page's printed text (the pasted drawings' own text, GV's notes left out: the drawings-
    only copies), read once per set and kept in the pack (`TEXTS_FILE`). Empty when it cannot be
    read."""
    cached = _read_json(folder / TEXTS_FILE)
    if isinstance(cached, Mapping) and cached.get("files") == list(files):
        return {
            (str(e.get("file")), int(e.get("page") or 0)): str(e.get("text") or "")
            for e in cached.get("pages") or []
            if isinstance(e, Mapping)
        }
    out: dict[tuple[str, int], str] = {}
    try:
        pack = import_module("app.review.assistant.set_pack")
        for name in files:
            copy = folder / f"{pack.stem(name)}_drawings_only.pdf"
            if not copy.is_file():
                continue
            for number, text in enumerate(pack._page_texts(copy), start=1):
                out[(name, number)] = text
    except Exception as failed:  # noqa: BLE001 - the sheet index is skipped, the rest still runs
        _log.warning("vendor consistency: page texts not read: %s", type(failed).__name__)
        return {}
    try:
        temporary = folder / f".{TEXTS_FILE}.{uuid4().hex}"
        temporary.write_text(
            json.dumps(
                {
                    "files": list(files),
                    "pages": [{"file": f, "page": p, "text": t} for (f, p), t in out.items()],
                }
            ),
            encoding="utf-8",
        )
        os.replace(temporary, folder / TEXTS_FILE)
    except OSError:  # a read-only pack: the texts are read again next time
        pass
    return out


#: The set pack's folder of each page's line work, read once per set (`LineWork`'s cache): reading
#: it from the PDF took about a second a page, half a minute per full check of a 60-page set.
LINE_WORK_DIR: Final = "line_work"


class LineWork:
    """A page's line work from the upload (`dim_labels`' own reading, loaded by name), each page
    read at most once: its straight pieces (calling it, a `Segments`) and its line weight. With a
    set pack (`cache`), each page read is kept there (`LINE_WORK_DIR`, exact floats), so the next
    check of the set reads it in a moment; a pack is per set of uploads, so it never goes stale."""

    def __init__(self, uploads: Path, cache: Path | None = None) -> None:
        self.uploads = uploads
        self.cache = cache
        self._read: dict[tuple[str, int], tuple[Sequence[Segment], float] | None] = {}

    def _kept(self, file: str, page: int) -> Path | None:
        if self.cache is None:
            return None
        pack = import_module("app.review.assistant.set_pack")
        return self.cache / LINE_WORK_DIR / f"{pack.stem(file)}-p{page:04d}.npz"

    def _load(self, path: Path | None) -> tuple[Sequence[Segment], float] | None:
        if path is None or not path.is_file():
            return None
        import numpy as np

        try:
            with np.load(path) as kept:
                rows = kept["segments"].tolist()
                weight = float(kept["weight"])
        except (OSError, ValueError, KeyError):
            return None
        return [((a, b), (c, d)) for a, b, c, d in rows], weight

    def _keep(self, path: Path | None, found: tuple[Sequence[Segment], float]) -> None:
        if path is None:
            return
        import numpy as np

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            held = path.with_name(f".{path.stem}.{uuid4().hex}.npz")
            rows = np.array(
                [[a[0], a[1], b[0], b[1]] for a, b in found[0]], dtype=np.float64
            ).reshape(-1, 4)
            np.savez(held, segments=rows, weight=np.float64(found[1]))
            held.replace(path)
        except OSError as failed:
            _log.info("vendor consistency: line work not kept: %s", type(failed).__name__)

    def _page(self, file: str, page: int) -> tuple[Sequence[Segment], float] | None:
        key = (file, page)
        if key not in self._read:
            kept = self._kept(file, page)
            found = self._load(kept)
            if found is None:
                try:
                    labels = import_module("app.review.assistant.dim_labels")
                    parsed = labels._parse(self.uploads / file, page - 1)
                    found = (parsed.segments, float(parsed.weight))
                    self._keep(kept, found)
                except Exception as failed:  # noqa: BLE001 - its ends stay unchecked: no finding
                    _log.warning(
                        "vendor consistency: line work not read: %s", type(failed).__name__
                    )
            self._read[key] = found
        return self._read[key]

    def __call__(self, file: str, page: int) -> Sequence[Segment] | None:
        found = self._page(file, page)
        return None if found is None else found[0]

    def weight(self, file: str, page: int) -> float | None:
        found = self._page(file, page)
        return None if found is None else found[1]


def line_work(uploads: Path, cache: Path | None = None) -> LineWork:
    """A page's line work from the upload, each page read at most once (`LineWork`), kept in the
    set pack `cache` when one is given."""
    return LineWork(uploads, cache)


def warm_line_work(folder: Path | None, uploads: Path | None) -> int:
    """Reads and keeps in the set pack the line work of every page with labels (the pages the
    consistency check reads), ahead of it (the simple full check starts this beside its compare
    calls). Returns how many pages were read. Never raises."""
    if folder is None or uploads is None:
        return 0
    try:
        dims = _read_json(folder / "dims.json")
        reader = LineWork(uploads, folder)
        done = 0
        for entry in (dims or {}).get("pages") or []:
            if isinstance(entry, Mapping) and entry.get("labels") and entry.get("file"):
                reader(str(entry["file"]), int(entry["page"]))
                done += 1
        return done
    except Exception as failed:  # noqa: BLE001 - the check reads what it needs itself
        _log.info("vendor consistency: line work not warmed: %s", type(failed).__name__)
        return 0


def check_pack(
    folder: Path | None,
    uploads: Path | None,
    *,
    read_texts: bool = True,
    rules_md: str | None = None,
) -> ConsistencyResult | None:
    """The check over a set pack's `dims.json` and page map (with the reviewer's plan-check
    answers laid over it), the line work read from the uploads in `uploads`. None when the pack
    has no labels yet. `rules_md`: the chat's `rules.md` (GV's settings in force)."""
    if folder is None:
        return None
    dims = _read_json(folder / "dims.json")
    if not isinstance(dims, Mapping) or not isinstance(dims.get("pages"), list):
        return None
    plan = import_module("app.review.assistant.full_check_plan")
    page_map = plan.read_page_map(folder)
    files = [str(f) for f in (page_map or {}).get("files") or dims.get("files") or []]
    texts = page_texts(folder, files) if read_texts and files else None
    return check_set(
        dims,
        page_map,
        texts=texts,
        segments=line_work(uploads, folder) if uploads is not None else None,
        index=_read_json(folder / "set_index.json"),
        typed=import_module("app.review.assistant.typed_ocr").reader_for_pack(folder, files),
        numbers=_numbers(folder, uploads),
        rules_md=rules_md,
    )


def _numbers(folder: Path, uploads: Path | None) -> reading_routes.NumberOf:
    """The number reader on a label (`read_number.label_routes`) over the set pack, made when first
    asked (it loads the OCR engines)."""
    made: list[Any] = []

    def read(label: Mapping[str, Any]) -> Any:
        numbers = import_module("app.review.assistant.read_number")
        if not made:
            made.append(numbers.reader_for_pack(folder, uploads))
        return numbers.label_routes(made[0], label)

    return read


# ---- for other code checks (`run_fit`) -----------------------------------------------------------

#: One printed dimension with its dimension line (the check's own reading of a `dims.json` label).
Label = _Label


def read_label(raw: Mapping[str, Any], file: str, page: int) -> _Label | None:
    """A `dims.json` label with a horizontal or vertical dimension line, else None."""
    return _label(raw, file, page, ())


def label_rows(labels: Sequence[_Label]) -> list[list[_Label]]:
    """Labels on one dimension line (the same place across)."""
    return _rows(labels)


def proved_chain(overall: _Label, row: Sequence[_Label]) -> list[_Label] | None:
    """The row's labels that tile the overall's span exactly, end to end, else None."""
    return _chain(overall, row)


def proved_run(parts: Sequence[_Label], drawing: Sequence[_Label] = ()) -> bool:
    """Whether `parts` form one chain end to end along one run, by the rules this check proves the
    vendor's strings with: the same dimension line (across within `ROW_PT`), each part touching
    the next (within `GAP_PT`), every part with its extension lines (so the outer ends land on the
    run's own extension lines), at least two parts, and no label of `drawing` on a parallel line
    printing the run's overall (a printed overall is compared itself, never the sum). Used for the
    architect's chains with no printed overall (client meeting, 2026-10-23)."""
    if len(parts) < 2 or len({label.orientation for label in parts}) != 1:
        return False
    if max(la.across for la in parts) - min(la.across for la in parts) > ROW_PT:
        return False
    ordered = sorted(parts, key=lambda la: la.start)
    if any(abs(a.end - b.start) > GAP_PT for a, b in pairwise(ordered)):
        return False
    if not all(all(label.extensions) for label in ordered):
        return False
    start, end = ordered[0].start, ordered[-1].end
    members = {id(label) for label in parts}
    return not any(
        id(other) not in members
        and other.orientation == ordered[0].orientation
        and abs(other.start - start) <= END_PT
        and abs(other.end - end) <= END_PT
        for other in drawing
    )


def judge_string(
    overall: _Label,
    parts: Sequence[_Label],
    drawing: Sequence[_Label],
    segments: Segments | None,
    area: str | None = None,
) -> dict[str, Any] | None:
    """A string another check proved (its parts tile the overall): the Wrong item when its parts
    do not add up and every drawing check this module makes passes (`_judge`), else None."""
    check = _judge(overall, parts, drawing, segments)
    if check.verdict != "does_not_add_up":
        return None
    return _string_finding(check, area, {})


# ---- the full check's hook -----------------------------------------------------------------------


def _already(finding: Mapping[str, Any], live: Sequence[Mapping[str, Any]]) -> bool:
    """The answer already has this item: a vendor string on the same page with the same printed
    overall, or a finding on the same page that names both values."""
    to_inches = import_module("app.review.assistant.finding_checks").to_inches
    page = finding.get("page")
    wanted = {to_inches(str(finding.get(side) or "")) for side in ("architect", "vendor")}
    for other in live:
        if other.get("page") != page:
            continue
        strings = finding.get("source") == other.get("source") == "vendor_arithmetic"
        printed = to_inches(str(other.get("printed_total") or other.get("vendor") or ""))
        if strings and printed == to_inches(str(finding.get("printed_total") or "")):
            return True
        values = {to_inches(str(other.get(side) or "")) for side in ("architect", "vendor")}
        if None not in wanted and wanted == values:
            return True
    return False


@dataclass(frozen=True, slots=True)
class StringJudgement:
    """What code's geometry says of the strings a reading grouped (#1639)."""

    verified: tuple[str, ...] = ()
    """Findings whose parts are code's own chain that does not add up: their geometry is proved."""
    dropped: Mapping[str, str] = field(default_factory=dict)
    """Findings code's own reading of the same span replaces, with the reason: it adds up, or code
    found other parts for it (its own finding is then added instead)."""


def _overlaps(box: Mapping[str, Any] | None, label: _Label, margin: float = 6.0) -> bool:
    if not isinstance(box, Mapping):
        return False
    try:
        x0, y0, x1, y1 = (float(box[k]) for k in ("x0", "y0", "x1", "y1"))
    except (KeyError, TypeError, ValueError):
        return False
    lx0, ly0, lx1, ly1 = label.box
    return not (lx1 < x0 - margin or x1 + margin < lx0 or ly1 < y0 - margin or y1 + margin < ly0)


def judge_strings(
    findings: Sequence[Mapping[str, Any]], checks: Sequence[StringCheck]
) -> StringJudgement:
    """Each vendor string a reading grouped (`source: vendor_arithmetic`, not the consistency
    check's own), against code's strings of the same span: the same page, the same printed overall
    (at the finding's spot when it has one, else the only one with that value on the page).

    - code's string does not add up and has the same parts: verified (the geometry is code's),
      unless the drawing names one part as the wrong number: dropped (code's own finding, red at
      that part with what it should be, replaces it);
    - code's string adds up: dropped (the reading grouped dimensions of another chain);
    - code's string does not add up with other parts: dropped (code's own finding replaces it);
    - no string of code's, or a doubt: left as it is (never red: Not compared)."""
    to_inches = import_module("app.review.assistant.finding_checks").to_inches
    verified: list[str] = []
    dropped: dict[str, str] = {}
    for finding in findings:
        if finding.get("source") != "vendor_arithmetic" or finding.get("created_by") == CREATED_BY:
            continue
        if finding.get("status") != "different" or not finding.get("id"):
            continue
        printed = to_inches(str(finding.get("printed_total") or finding.get("vendor") or ""))
        if printed is None:
            continue
        same = [
            check
            for check in checks
            if check.overall.page == finding.get("page")
            and Fraction(printed) == check.overall.inches
            and check.verdict in ("adds_up", "does_not_add_up")
        ]
        placed = [c for c in same if _overlaps(finding.get("vendor_box"), c.overall)]
        if placed:
            same = placed
        elif len(same) != 1:
            continue  # two strings with that overall and no spot to tell them apart
        check = same[0]
        read = [to_inches(str(p)) for p in finding.get("parts") or []]
        parts = sorted(Fraction(v) for v in read if v is not None)
        ours = sorted(p.inches for p in check.parts)
        overall = check.overall.text
        listed = " + ".join(p.text for p in check.parts)
        if check.verdict == "adds_up":
            dropped[str(finding["id"])] = (
                f"Code measured this string on the drawing: {listed} = {overall} printed, it adds "
                "up; the reading grouped dimensions of another chain."
            )
        elif None not in read and parts == ours and check.wrong_part is None:
            verified.append(str(finding["id"]))
        else:
            dropped[str(finding["id"])] = (
                f"Code measured this string on the drawing as {listed} (printed {overall}); its "
                "own finding replaces the reading's."
            )
    return StringJudgement(tuple(verified), dropped)


def judge_unsized(
    findings: Sequence[Mapping[str, Any]],
    unsized: Sequence[UnsizedEnds],
    verified: Sequence[str] = (),
) -> dict[str, str]:
    """The strings a reading grouped (`source: vendor_arithmetic`, not the consistency check's own)
    that code's please-check of an overall past its sized pieces (`find_unsized_ends`) replaces,
    with the reason (2026-10-27, a real upload): the same page, the same printed overall (at the
    finding's spot when it has one, else the only such overall code measured on the page), and
    the very parts code measured as the overall's sized pieces. Such a string says "the parts do
    not add up to the overall", but code measured that the parts stop short of the overall's ends
    and nothing sizes those ends, so the reading's claim was never verified (Not compared);
    without this, it hid code's own please-check as a duplicate (`_already`). A string whose
    geometry code verified, or with other parts, is left as it is. Never a red: the please-check
    is a Likely problem (`likely_problems.unsized_ends`), and the string it replaces was never red.
    """
    return {
        finding_id: reason
        for finding_id, (_found, reason) in _unsized_replaces(findings, unsized, verified).items()
    }


def _unsized_replaces(
    findings: Sequence[Mapping[str, Any]],
    unsized: Sequence[UnsizedEnds],
    verified: Sequence[str] = (),
) -> dict[str, tuple[UnsizedEnds, str]]:
    """`judge_unsized`, with the overall each replaced string belongs to."""
    to_inches = import_module("app.review.assistant.finding_checks").to_inches
    replaced: dict[str, tuple[UnsizedEnds, str]] = {}
    for finding in findings:
        if finding.get("source") != "vendor_arithmetic" or finding.get("created_by") == CREATED_BY:
            continue
        if finding.get("status") != "different" or not finding.get("id"):
            continue
        if str(finding["id"]) in verified or finding.get("string_geometry") == "verified":
            continue
        printed = to_inches(str(finding.get("printed_total") or finding.get("vendor") or ""))
        read = [to_inches(str(p)) for p in finding.get("parts") or []]
        if printed is None or not read or None in read:
            continue
        same = [
            found
            for found in unsized
            if found.overall.page == finding.get("page")
            and found.overall.inches == Fraction(printed)
        ]
        box = finding.get("vendor_box")
        if isinstance(box, Mapping):
            # At code's overall: the same file (a set may hold two vendor PDFs) and the same spot.
            same = [
                found
                for found in same
                if box.get("file") in (None, found.overall.file) and _overlaps(box, found.overall)
            ]
        if len(same) != 1:
            continue  # not at code's overall, or two such overalls and no spot to tell them apart
        found = same[0]
        if sorted(Fraction(v) for v in read) != sorted(p.inches for p in found.parts):
            continue
        pieces = " + ".join(p.text for p in found.parts)
        reason = (
            f"Code measured this overall on the drawing: the vendor's {found.overall.text} runs "
            f"past its sized pieces ({pieces} = {format_inches(found.parts_sum)}) at both ends, "
            "where nothing is sized; its own please-check replaces the reading's."
        )
        replaced[str(finding["id"])] = (found, reason)
    return replaced


def _of_overall(finding: Mapping[str, Any], found: UnsizedEnds) -> bool:
    """Code's please-check `finding` is the one for this overall (`_unsized_finding`)."""
    dims = finding.get("dims")
    vendor = dims.get("vendor") if isinstance(dims, Mapping) else None
    return (
        isinstance(finding.get(UNSIZED_ENDS), Mapping)
        and isinstance(vendor, Mapping)
        and vendor.get("id") == found.overall.id
        and vendor.get("page") == found.overall.page
        and vendor.get("file") == found.overall.file
    )


def add_to_answer(engine: Any) -> ConsistencyResult | None:
    """The engine's hook (a full check, at its finish, $0): the check over the set pack; its Wrong
    items added through the finding book like the ledger's (the same checks and trust rule), unless
    the answer already has them; its grey items kept for the "not checked" list
    (`engine.consistency_gaps`); one line for the summary (`engine.consistency_line`)."""
    if getattr(engine.settings, "mode", None) != "full_check":
        return None
    workspace = engine.tools.workspace
    try:
        rules_md: str | None = (workspace.skills / "rules.md").read_text(encoding="utf-8")
    except (OSError, AttributeError, TypeError, ValueError):
        rules_md = None  # GV's company standard (the field cut per wall end) applies
    result = check_pack(workspace.set_pack, workspace.uploads, rules_md=rules_md)
    if result is None:
        return None
    book = engine.book
    with engine.finding_lock:
        # The strings a reading grouped, against code's geometry (#1639): verified, or dropped
        # for code's own reading of the same span; the rest are never red (Not compared).
        judged = judge_strings(book.live(), result.checks)
        for finding in book.live():
            if str(finding.get("id")) in judged.verified:
                proved = {**finding, "string_geometry": "verified"}
                if book.replace(proved):
                    engine._show_finding(proved)
        for finding_id, reason in judged.dropped.items():
            if book.absorb(finding_id):
                engine._show_dropped(finding_id, reason)
        # An overall past its sized pieces: code's please-check replaces the reading's unverified
        # string of the same overall, which would otherwise hide it as a duplicate (`_already`).
        # The reading's string goes only once code's own item is in the answer.
        replaces = _unsized_replaces(book.live(), result.unsized, judged.verified)
        live = book.live()
        for finding in result.findings:
            # Only the please-check of that very overall looks past the reading's string; for
            # every other code finding the string still counts as already there.
            hidden = {
                finding_id
                for finding_id, (found, _reason) in replaces.items()
                if _of_overall(finding, found)
            }
            if _already(finding, [f for f in live if str(f.get("id")) not in hidden]):
                continue
            added = book.add_code_finding(finding, created_by=CREATED_BY)
            if added is not None:
                engine._show_finding(added)
                live.append(added)
                for finding_id in sorted(hidden):
                    if book.absorb(finding_id):
                        engine._show_dropped(finding_id, replaces[finding_id][1])
    engine.consistency_gaps = list(result.unknowns)
    # The summary's line: code's own summary carries it, else it follows the AI's (the engine).
    engine.consistency_line = summary_line(result)
    return result
