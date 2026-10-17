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
   wrong number.
2. **The same span printed twice.** Two of the vendor's dimension lines in one drawing (on the same
   pasted drawing, e.g. a plan above its front view, which line up in projection) that end on
   exactly the same two extension lines measure the same span; when they print different values,
   one of them is wrong. Code names which one by the drawing's scale (the one drawn to its value is
   the reference, the "should be"); when the scale cannot tell, it is not a finding. Two drawings
   on different pages share no coordinates, so a contradiction across sheets is left to the reading
   ledger (`record_reading`), where the AI names the item.
3. **Completeness** (the "could not compare" grey list, never a Wrong): an area the architect draws
   with no vendor sheet, a vendor sheet for an area the architect does not have, an area a later
   round leaves out when the same part of the set has that round elsewhere, and sheets the vendor's
   printed sheet index lists that no page shows.

Every finding is in the chat finding shape (CONTRACT2, `created_by: "consistency"`) with its code
witnesses: a string carries its parts and printed overall (code's `arithmetic` check) and the
overall's label (`dims`, the `printed` check); a contradiction carries both labels (`printed` on
both spots) and `reference_side: vendor`. The "should be" is the parts' sum for a string and the
reference value for a contradiction, so the red/green cloud drawing, the screen, the CSV and the
trust rule use them like any other finding.

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

from app.review.assistant.ledger import add_up, format_inches

__all__ = [
    "CREATED_BY",
    "END_PT",
    "MIN_SCALE_SUPPORT",
    "TOLERANCE",
    "ConsistencyResult",
    "StringCheck",
    "check_pack",
    "check_set",
    "summary_line",
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


def _end_marked(segments: Sequence[Segment], label: _Label, at: float) -> bool:
    """A tick or an arrowhead on the label's own dimension line at `at` (along its axis): a short
    piece of line work at a slant to the dimension line that touches the end. An extension line
    crossing there is not one (it is square to the line): a line cut at another dimension's
    extension line has no mark of its own, so it is never taken for that span."""
    flip = label.orientation == "v"
    px, py = (label.across, at) if flip else (at, label.across)
    for a, b in segments:
        (ax, ay), (bx, by) = a, b
        dx, dy = bx - ax, by - ay
        length = (dx * dx + dy * dy) ** 0.5
        if not _MARK_MIN_PT <= length <= _MARK_MAX_PT:
            continue
        along, square = (abs(dy), abs(dx)) if flip else (abs(dx), abs(dy))
        # Slanted: between about 10 and 80 degrees to the dimension line.
        if square < 0.17 * length or along < 0.17 * length:
            continue
        t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (length * length)))
        cx, cy = ax + t * dx - px, ay + t * dy - py
        if (cx * cx + cy * cy) ** 0.5 <= _MARK_PT:
            return True
    return False


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


def _chain(overall: _Label, row: Sequence[_Label]) -> list[_Label] | None:
    """The row's labels that tile the overall's span exactly (end to end, from one of its extension
    lines to the other), or None when the row is not its string."""
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
    if any(abs(a.end - b.start) > GAP_PT for a, b in pairwise(inside)):
        return None
    return inside


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
    off = _off_scale(members, scale[0])
    if len(off) >= 2:
        return doubt(
            "more than one of its dimensions is not drawn to its value (the labels may belong to "
            "other lines, or the view is not to scale)"
        )
    culprit = off[0] if off else None
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


# ---- the same span printed twice -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Contradiction:
    reference: _Label
    odd: _Label
    support: int
    odd_drawn: Fraction


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
                if not (any(a.extensions) or any(b.extensions)):
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
        f"{pieces} = {total} ≠ {overall.text} printed " f"(off {format_inches(abs(check.off_by))})"
    )
    place = f"{area}: " if area else ""
    why = (
        f"The vendor's parts don't add up: {sum_text}. Code checked the drawing: the parts touch "
        "end to end and end on the overall's own extension lines, so they are one string."
    )
    if check.reason:
        why += f" A hint: {check.reason}."
    why += _drawn_note([overall, *parts])
    return {
        "page": overall.page,
        "item": f"{place}dimension string ({_direction(overall)}{_round_words(overall, entries)})",
        "category": "other",
        "architect": total,
        "vendor": overall.text,
        "status": "different",
        "why": why,
        "verified": False,
        "source": "vendor_arithmetic",
        "parts": [p.text for p in parts],
        "printed_total": overall.text,
        "vendor_box": _box_dict(overall),
        "dims": {"vendor": _provenance(overall)},
        "sum_text": sum_text,
        "created_by": CREATED_BY,
        "ledger_ids": [p.ref for p in (overall, *parts)],
    }


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
) -> ConsistencyResult:
    """The vendor consistency check over one set's labels and page map (see the module).
    `segments` reads a page's line work (for the end marks); without it nothing is reported."""
    started = clock()
    entries = {(str(e.get("file")), int(e["page"])): e for e in _entries(page_map)}
    labels: list[_Label] = []
    vendor_pages: dict[tuple[str, int], int] = {}
    for entry in (dims or {}).get("pages") or []:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("labels"), list):
            continue
        file, page = str(entry.get("file")), int(entry.get("page") or 0)
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
    result.findings = result.findings[:MAX_FINDINGS]
    unknowns = completeness(page_map)
    if texts:
        missing, result.sheet_index = sheet_index(texts, page_map)
        unknowns += missing
    unknowns.sort(key=lambda u: (u["page"] is None, u["page"] or 0))
    result.unknowns = unknowns[:MAX_UNKNOWNS]
    result.seconds = round(clock() - started, 3)
    return result


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


def line_work(uploads: Path) -> Segments:
    """A page's line work from the upload (`dim_labels`' own reading, loaded by name), each page
    read at most once."""
    read: dict[tuple[str, int], Sequence[Segment] | None] = {}

    def segments(file: str, page: int) -> Sequence[Segment] | None:
        key = (file, page)
        if key not in read:
            try:
                labels = import_module("app.review.assistant.dim_labels")
                read[key] = labels._parse(uploads / file, page - 1).segments
            except Exception as failed:  # noqa: BLE001 - its ends stay unchecked: no finding
                _log.warning("vendor consistency: line work not read: %s", type(failed).__name__)
                read[key] = None
        return read[key]

    return segments


def check_pack(
    folder: Path | None, uploads: Path | None, *, read_texts: bool = True
) -> ConsistencyResult | None:
    """The check over a set pack's `dims.json` and page map (with the reviewer's plan-check
    answers laid over it), the line work read from the uploads in `uploads`. None when the pack
    has no labels yet."""
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
        dims, page_map, texts=texts, segments=line_work(uploads) if uploads is not None else None
    )


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


def add_to_answer(engine: Any) -> ConsistencyResult | None:
    """The engine's hook (a full check, at its finish, $0): the check over the set pack; its Wrong
    items added through the finding book like the ledger's (the same checks and trust rule), unless
    the answer already has them; its grey items kept for the "not checked" list
    (`engine.consistency_gaps`); one line for the summary (`engine.consistency_line`)."""
    if getattr(engine.settings, "mode", None) != "full_check":
        return None
    workspace = engine.tools.workspace
    result = check_pack(workspace.set_pack, workspace.uploads)
    if result is None:
        return None
    book = engine.book
    with engine.finding_lock:
        live = book.live()
        for finding in result.findings:
            if _already(finding, live):
                continue
            added = book.add_code_finding(finding, created_by=CREATED_BY)
            if added is not None:
                engine._show_finding(added)
                live.append(added)
    engine.consistency_gaps = list(result.unknowns)
    # The summary's line: code's own summary carries it, else it follows the AI's (the engine).
    engine.consistency_line = summary_line(result)
    return result
