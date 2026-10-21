"""Architect against vendor dimensions paired by what their ends land on: walls, the floor and the
outlets drawn on both drawings. Code, $0, no AI, no single scale, never the printed values (#1618).

Positional pairing (`dim_pairs`, wrapping `extraction/architect/pairing.py`) slides one drawing
over the other at each drawing's measured scale and pairs ends that coincide. On real sets it pairs
nothing where an architect's elevation is not drawn to one scale, or where the vendor moved a
fixture (the very difference a review is for). This module pairs by **anchors** instead: a
dimension pairs with the other drawing's dimension when both its ends land on the *same thing* in
both drawings, whatever the scale and wherever the thing was moved to.

**1. Each label's span, from its own ticks.** A dimension's ends are its slash ticks (short strokes
at 45 degrees across its dimension line), never any line that happens to cross it (a fridge
outline crossing a dimension line is not where it ends). The line is the one beside the label on
the drafting side (below a horizontal label's text, right of a vertical one) with ticks on both
sides of the label's centre. A span is kept only when it is drawn to its value within `FIT` of the
drawing's own median (a local scale, used only to tell a label's span from a neighbour's, never to
read a value).

**2. Anchors.**
- *Walls*: the two ends of the drawing's longest run of joined horizontal spans, each with a solid
  vertical line within 1" of it spanning at least 85% of the floor-to-top height, on a drawing at
  least `ROOM_IN` tall (a room elevation, not a counter: a cabinet end never stands in for a wall).
- *Floor*: the lowest tick shared by at least two vertical spans, on a solid line across the run.
- *Outlets*: closed rectangles 4-20 pt long, 1.4-1.9 times as long as wide, holding at least 30
  strokes (a receptacle's two plugs). Black and grey ink only: GV's coloured markup is never an
  anchor (the line work comes from the vendor layer, as `dim_labels` reads it).

**3. The same outlet on both drawings.** Outlets are grouped in rows by height above the floor (the
height in each drawing's own local scale; rows more than `ROW_GAP_IN` apart are different rows). A
row pairs with the other drawing's row within `ROW_MATCH_IN` when no other row of either drawing is
that close; then the rows must hold the **same number** of outlets, and the k-th from the left is
the k-th from the left, each moved at most `MAX_SHIFT_IN` along the wall. A row with a missing or
added outlet pairs nothing.

**4. Where a tick lands on an outlet.** A tick reaches an outlet when the outlet's box sits on the
tick's line and the extension line runs from the dimension line to it (or it is within 3"). Where
on the outlet counts: its middle half (`c`), or exactly its low or high edge (`lo`, `hi`). A
dimension to an outlet's centre never pairs with one to its edge (rule R8: "underside vs top" is
not the same thing). An edge reading is refused where a long solid line (a counter top, a toe kick)
lies on the same tick: the dimension may be measuring to that edge instead.

**5. Pairs.** A vendor dimension pairs with the architect dimension whose two ends carry the same
anchors (`one`), or with the architect's chain of joined spans on one line between those two
anchors (`sum`: the architect's value is the chain's sum, kept on the page only). Any doubt
(anchors on both ends of one side, two architect candidates that disagree, a band that does not
match) means no pair. Each pair is compared exactly (`same` within 1/32").

The result is in `pairs.json`'s shape (`dim_pairs.flat_pairs`, `full_check_advocate.code_pairs`)
with `paired_by: code`, `method: anchors` and the anchors as evidence. A pair code made is proof by
itself for a red cloud, so this module pairs only what it is sure of.

Measured on Set 3 ($0, local, every pair checked by eye): 18 pairs on the two kitchenettes, 13 one
to one and 5 chain sums, 0 wrong (`~/gv-local/research/agents/chat-first/PAIRING2.md`).
Verification: `tests/review/assistant/test_dim_anchors.py` (hand-made line work).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from importlib import import_module
from pathlib import Path
from typing import Any, Final

__all__ = [
    "FIT",
    "MAX_SHIFT_IN",
    "ROOM_IN",
    "ROW_GAP_IN",
    "ROW_MATCH_IN",
    "Drawing",
    "drawing",
    "page_segments",
    "pair_drawings",
    "set_anchor_pairs",
]

type Point = tuple[float, float]
type Segment = tuple[Point, Point]
type Box = tuple[float, float, float, float]
type Label = Mapping[str, Any]

#: A span is drawn to its value within this share of the drawing's median points per inch.
FIT: Final = 0.08
#: A drawing at least this tall (floor to its highest tick, inches) is a room elevation with walls.
ROOM_IN: Final = 72.0
#: Outlets more than this apart in height (inches) are in different rows.
ROW_GAP_IN: Final = 4.0
#: Two drawings' outlet rows are the same row within this (inches).
ROW_MATCH_IN: Final = 3.0
#: The k-th outlet of a row may be moved along the wall at most this much (inches).
MAX_SHIFT_IN: Final = 12.0
#: Two printed values are the same within this (inches).
SAME_IN: Final = Fraction(1, 32)
#: How far a label's dimension line may be from its text (points).
_ROW_PT: Final = 6.0
#: Points added to a line on the wrong side of its text (above a horizontal label, left of a
#: vertical one): drafting puts the line below or right, so a neighbour's line loses a tie.
_WRONG_SIDE_PT: Final = 3.0
#: Two ticks closer than this (points) are one tick; two stations closer than this are one place.
_SAME_PT: Final = 0.8


# ---- line work -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Lines:
    """One drawing's line work, split once: horizontal, vertical, and short slanted strokes."""

    horizontal: tuple[tuple[float, float, float], ...]
    """(x0, x1, y) of each horizontal piece."""
    vertical: tuple[tuple[float, float, float], ...]
    """(y0, y1, x) of each vertical piece."""
    slanted: tuple[tuple[float, float], ...]
    """Midpoints of the short strokes at 25-70 degrees: dimension ticks."""
    all: tuple[Segment, ...]


def _inside(point: Point, box: Box, pad: float = 0.0) -> bool:
    return box[0] - pad <= point[0] <= box[2] + pad and box[1] - pad <= point[1] <= box[3] + pad


def _lines(segments: Iterable[Segment], box: Box) -> _Lines:
    horizontal: list[tuple[float, float, float]] = []
    vertical: list[tuple[float, float, float]] = []
    slanted: list[tuple[float, float]] = []
    kept: list[Segment] = []
    for a, b in segments:
        if not (_inside(a, box, 2) and _inside(b, box, 2)):
            continue
        kept.append((a, b))
        dx, dy = b[0] - a[0], b[1] - a[1]
        if abs(dy) < 0.05 and abs(dx) >= 0.5:
            horizontal.append((min(a[0], b[0]), max(a[0], b[0]), (a[1] + b[1]) / 2))
        elif abs(dx) < 0.05 and abs(dy) >= 0.5:
            vertical.append((min(a[1], b[1]), max(a[1], b[1]), (a[0] + b[0]) / 2))
        elif dx and 1.5 <= math.hypot(dx, dy) <= 12 and 0.4 <= abs(dy / dx) <= 2.5:
            slanted.append(((a[0] + b[0]) / 2, (a[1] + b[1]) / 2))
    return _Lines(tuple(horizontal), tuple(vertical), tuple(slanted), tuple(kept))


def _ticks(lines: _Lines, axis: str, row: float, lo: float, hi: float) -> list[float]:
    """Slash ticks crossing the dimension line at `row` (y for a horizontal line, x for a vertical
    one): their places along it, from `lo` to `hi`, merged within `_SAME_PT`."""
    found = sorted(
        along
        for mx, my in lines.slanted
        for along, across in ([(mx, my)] if axis == "h" else [(my, mx)])
        if abs(across - row) <= 0.9 and lo <= along <= hi
    )
    merged: list[list[float]] = []
    for tick in found:
        if merged and tick - merged[-1][-1] <= _SAME_PT:
            merged[-1].append(tick)
        else:
            merged.append([tick])
    return [sum(group) / len(group) for group in merged]


def _pieces(lines: _Lines, axis: str, at: float, tol: float) -> list[tuple[float, float]]:
    """The pieces of line lying on `at` (x for `v`, y for `h`), as (from, to) along it."""
    if axis == "v":
        return [(y0, y1) for y0, y1, x in lines.vertical if abs(x - at) <= tol]
    return [(x0, x1) for x0, x1, y in lines.horizontal if abs(y - at) <= tol]


def _longest(lines: _Lines, axis: str, at: float, lo: float, hi: float) -> float:
    """The longest solid run of line on `at` inside lo..hi (gaps up to 2 pt bridged)."""
    best = 0.0
    start = end = None
    for p0, p1 in sorted(p for p in _pieces(lines, axis, at, 0.05) if p[1] >= lo and p[0] <= hi):
        p0, p1 = max(p0, lo), min(p1, hi)
        if start is not None and end is not None and p0 <= end + 2.0:
            end = max(end, p1)
        else:
            start, end = p0, p1
        best = max(best, end - start)
    return best


def _coverage(lines: _Lines, axis: str, at: float, lo: float, hi: float) -> tuple[float, float]:
    """The share of lo..hi covered by line work on `at`, and the largest gap left."""
    pieces = sorted(
        (max(p0, lo), min(p1, hi))
        for p0, p1 in _pieces(lines, axis, at, 0.8)
        if p1 > lo and p0 < hi
    )
    covered, gap, place = 0.0, 0.0, lo
    for p0, p1 in pieces:
        gap = max(gap, p0 - place)
        if p1 > place:
            covered += p1 - max(p0, place)
            place = p1
    gap = max(gap, hi - place)
    return (covered / (hi - lo) if hi > lo else 1.0), gap


# ---- spans -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Span:
    """One label's dimension: its line (`row`, across) and its two ends (`a` < `b`, along)."""

    id: str
    axis: str
    row: float
    a: float
    b: float
    inches: Fraction
    label: Label


def _inches(label: Label) -> Fraction | None:
    try:
        value = Fraction(Decimal(str(label.get("inches"))))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return value if value > 0 else None


def _candidates(lines: _Lines, label: Label, box: Box) -> list[tuple[float, float, float, float]]:
    """(distance, row, a, b): the dimension lines near the label with a tick each side of it."""
    x0, y0, x1, y1 = (float(v) for v in label["box"])
    axis = label["orientation"]
    centre = (x0 + x1) / 2 if axis == "h" else (y0 + y1) / 2
    near, far = (y0, y1) if axis == "h" else (x0, x1)
    rows = {
        round((my if axis == "h" else mx) * 2) / 2
        for mx, my in lines.slanted
        if near - _ROW_PT - 2 <= (my if axis == "h" else mx) <= far + _ROW_PT + 2
    }
    lo, hi = (box[0], box[2]) if axis == "h" else (box[1], box[3])
    out: list[tuple[float, float, float, float]] = []
    for row in sorted(rows):
        distance = 0.0 if near <= row <= far else min(abs(row - near), abs(row - far))
        if distance > _ROW_PT:
            continue
        if row < near:
            distance += _WRONG_SIDE_PT
        ticks = _ticks(lines, axis, row, lo - 5, hi + 5)
        left = [t for t in ticks if t < centre]
        right = [t for t in ticks if t > centre]
        if left and right:
            out.append((distance, row, max(left), min(right)))
    return sorted(out)


def _spans(lines: _Lines, labels: Sequence[Label], box: Box) -> tuple[dict[str, Span], float]:
    """The drawing's labels with a span code trusts, and its median points per inch. A label read
    inside a pasted picture never has one (#1835): the ticks found beside it are vector line work
    of whatever lies under or near the picture, never the picture's own (its line work is pixels),
    so it never pairs on its ends."""
    found: dict[str, tuple[str, list[tuple[float, float, float, float]], Fraction, Label]] = {}
    for label in labels:
        axis = label.get("orientation")
        value = _inches(label)
        raw = label.get("box")
        if axis not in ("h", "v") or value is None or not raw or len(raw) != 4:
            continue
        if label.get("origin") == "picture":
            continue
        centre = ((float(raw[0]) + float(raw[2])) / 2, (float(raw[1]) + float(raw[3])) / 2)
        if not _inside(centre, box):
            continue
        rows = _candidates(lines, label, box)
        if rows:
            found[str(label.get("id"))] = (str(axis), rows, value, label)
    ratios = sorted(
        (rows[0][3] - rows[0][2]) / float(value)
        for _axis, rows, value, _label in found.values()
        if value >= 6 and len({(round(r[2]), round(r[3])) for r in rows if r[0] == rows[0][0]}) == 1
    )
    if not ratios:
        return {}, 0.0
    median = ratios[len(ratios) // 2]
    spans: dict[str, Span] = {}
    for key, (axis, rows, value, label) in found.items():
        fits = [r for r in rows if abs((r[3] - r[2]) / float(value) / median - 1) <= FIT]
        if not fits:
            continue
        best = fits[0]
        rivals = {(round(r[2], 1), round(r[3], 1)) for r in fits if r[0] <= best[0] + 2.0}
        if len(rivals) == 1:
            spans[key] = Span(key, axis, best[1], best[2], best[3], value, label)
    return spans, median


# ---- one drawing --------------------------------------------------------------------------------


@dataclass(slots=True)
class Outlet:
    """A receptacle drawn on the elevation: its box, height above the floor and place along the
    run (inches, at the drawing's own local scale), and its id once matched (`b46#1`)."""

    box: Box
    height_in: float
    along_in: float
    id: str | None = None


@dataclass(slots=True)
class Drawing:
    """One drawing's spans and anchors."""

    role: str
    pt_per_in: float
    spans: dict[str, Span]
    lines: _Lines
    floor: float | None = None
    floor_ok: bool = False
    run: tuple[float, float] | None = None
    walls: dict[str, float] = field(default_factory=dict)
    outlets: list[Outlet] = field(default_factory=list)


def _receptacles(lines: _Lines) -> list[Box]:
    """Closed rectangles shaped like a receptacle, with at least 30 strokes inside."""
    by_ends: dict[tuple[float, float], list[float]] = {}
    for x0, x1, y in lines.horizontal:
        if 2.5 <= x1 - x0 <= 25:
            by_ends.setdefault((round(x0, 1), round(x1, 1)), []).append(y)
    found: set[Box] = set()
    for (x0, x1), heights in by_ends.items():
        for top in heights:
            for bottom in heights:
                height, width = bottom - top, x1 - x0
                if (
                    not 2.5 <= height <= 25
                    or not 1.4 <= max(width, height) / min(width, height) <= 1.9
                ):
                    continue
                if not 4 <= max(width, height) <= 20:
                    continue
                sides = [
                    any(
                        abs(x - at) <= 0.2 and y0 <= top + 0.2 and y1 >= bottom - 0.2
                        for y0, y1, x in lines.vertical
                    )
                    for at in (x0, x1)
                ]
                if all(sides):
                    found.add((x0, top, x1, bottom))
    out: list[Box] = []
    for box in sorted(found):
        inner = (box[0] + 0.3, box[1] + 0.3, box[2] - 0.3, box[3] - 0.3)
        strokes = sum(1 for a, b in lines.all if _inside(a, inner) and _inside(b, inner))
        if strokes >= 30:
            out.append(box)
    return out


def drawing(
    role: str, labels: Sequence[Label], segments: Iterable[Segment], box: Box
) -> Drawing | None:
    """One drawing (`box` on its page, `role` architect or vendor) with its spans and anchors;
    `None` when code trusts no span on it."""
    lines = _lines(segments, box)
    mine = [label for label in labels if label.get("side") == role]
    spans, scale = _spans(lines, mine, box)
    if not spans:
        return None
    found = Drawing(role, scale, spans, lines)
    vertical = [s for s in spans.values() if s.axis == "v"]
    bottoms = sorted(s.b for s in vertical)
    if bottoms and sum(1 for b in bottoms if abs(b - bottoms[-1]) <= _SAME_PT) >= 2:
        found.floor = bottoms[-1]
    rows: dict[int, list[tuple[float, float]]] = {}
    for span in spans.values():
        if span.axis == "h":
            rows.setdefault(round(span.row), []).append((span.a, span.b))
    for pieces in rows.values():
        pieces.sort()
        start, end = pieces[0]
        for a, b in [*pieces[1:], (math.inf, math.inf)]:
            if abs(a - end) <= _SAME_PT:
                end = b
                continue
            if found.run is None or end - start > found.run[1] - found.run[0]:
                found.run = (start, end)
            start, end = a, b
    if found.floor is None or found.run is None:
        return found
    left, right = found.run
    found.floor_ok = _longest(lines, "h", found.floor, left - 5, right + 5) >= 0.7 * (right - left)
    top = min(s.a for s in vertical)
    if (found.floor - top) / scale >= ROOM_IN:
        xs = sorted({round(x, 2) for _y0, _y1, x in lines.vertical})
        for name, at in (("wall_L", left), ("wall_R", right)):
            for x in xs:
                if abs(x - at) > max(1.0, scale):
                    continue
                if _longest(lines, "v", x, top - 5, found.floor + 5) >= 0.85 * (found.floor - top):
                    found.walls[name] = at
                    break
    for outlet in _receptacles(lines):
        cx, cy = (outlet[0] + outlet[2]) / 2, (outlet[1] + outlet[3]) / 2
        if left < cx < right and cy < found.floor:
            found.outlets.append(Outlet(outlet, (found.floor - cy) / scale, (cx - left) / scale))
    return found


def _rows(outlets: Sequence[Outlet]) -> list[list[Outlet]]:
    rows: list[list[Outlet]] = []
    for outlet in sorted(outlets, key=lambda o: o.height_in):
        if rows and outlet.height_in - rows[-1][-1].height_in <= ROW_GAP_IN:
            rows[-1].append(outlet)
        else:
            rows.append([outlet])
    return [sorted(row, key=lambda o: o.box[0]) for row in rows]


def _match_outlets(architect: Drawing, vendor: Drawing, notes: list[str]) -> None:
    """Give the same id to the same outlet on both drawings (row by height, then by order)."""
    for outlet in (*architect.outlets, *vendor.outlets):
        outlet.id = None
    arch_rows, vendor_rows = _rows(architect.outlets), _rows(vendor.outlets)

    def height(row: Sequence[Outlet]) -> float:
        return sum(o.height_in for o in row) / len(row)

    for row in arch_rows:
        near = [v for v in vendor_rows if abs(height(v) - height(row)) <= ROW_MATCH_IN]
        back = [a for a in arch_rows if near and abs(height(a) - height(near[0])) <= ROW_MATCH_IN]
        where = f'outlets at {height(row):.0f}"'
        if len(near) != 1 or len(back) != 1:
            notes.append(f"{where}: no one vendor row at that height")
            continue
        other = near[0]
        if len(other) != len(row):
            notes.append(f"{where}: architect draws {len(row)}, vendor {len(other)}")
            continue
        if any(
            abs(v.along_in - a.along_in) > MAX_SHIFT_IN for a, v in zip(row, other, strict=True)
        ):
            notes.append(f'{where}: an outlet moved more than {MAX_SHIFT_IN:.0f}"')
            continue
        for number, (a, v) in enumerate(zip(row, other, strict=True), start=1):
            a.id = v.id = f"b{round(height(row))}#{number}"


def _long_line_on(lines: _Lines, axis: str, at: float, row: float, length: float) -> bool:
    """A solid line at least `length` long lying on the tick's place (y=at for a vertical
    dimension, x=at for a horizontal one) that does not reach the dimension line: an edge."""
    pieces = _pieces(lines, "h" if axis == "v" else "v", at, 0.5)
    return any(p1 - p0 >= length and not p0 - 3 <= row <= p1 + 3 for p0, p1 in pieces)


def _anchors(found: Drawing, axis: str, at: float, row: float) -> frozenset[str]:
    """The anchors one end of a span lands on."""
    out: set[str] = set()
    if axis == "h":
        out.update(name for name, x in found.walls.items() if abs(at - x) <= _SAME_PT)
    elif found.floor is not None and found.floor_ok and abs(at - found.floor) <= _SAME_PT:
        out.add("floor")
    scale = found.pt_per_in
    for outlet in found.outlets:
        if outlet.id is None:
            continue
        x0, y0, x1, y1 = outlet.box
        if axis == "h":
            low, high, near = x0, x1, (y1 if row > y1 else y0)
        else:
            low, high, near = y0, y1, (x1 if row > x1 else x0)
        if abs(at - (low + high) / 2) <= 0.25 * (high - low):
            where = "c"
        elif abs(at - low) <= 0.6:
            where = "lo"
        elif abs(at - high) <= 0.6:
            where = "hi"
        else:
            continue
        lo, hi = sorted((near, row))
        if hi - lo > 3 * scale:
            share, gap = _coverage(found.lines, "v" if axis == "h" else "h", at, lo, hi)
            if share < 0.35 or gap > 16:
                continue
        if where != "c" and _long_line_on(found.lines, axis, at, row, 3 * scale):
            continue
        out.add(f"{outlet.id}:{where}")
    return frozenset(out)


# ---- pairs ---------------------------------------------------------------------------------------


_WORDS: Final = {"wall_L": "the left wall", "wall_R": "the right wall", "floor": "the floor"}
_WHERE: Final = {"c": "centre", "lo": "low edge", "hi": "high edge"}


def _say(anchors: Iterable[str]) -> str:
    said: list[str] = []
    for anchor in sorted(anchors):
        if anchor in _WORDS:
            said.append(_WORDS[anchor])
            continue
        outlet, _, where = anchor.partition(":")
        row, _, number = outlet.removeprefix("b").partition("#")
        said.append(f'outlet {number} of the row at {row}" ({_WHERE.get(where, where)})')
    return " / ".join(said)


def _text(value: Fraction) -> str:
    whole, rest = divmod(value, 1)
    if not rest:
        return f'{whole}"'
    return f'{whole} {rest.numerator}/{rest.denominator}"' if whole else f'{rest}"'


def pair_drawings(
    architect: Drawing, vendor: Drawing, *, architect_page: int, vendor_page: int
) -> tuple[list[dict[str, Any]], list[str]]:
    """The vendor's dimensions paired with the architect's by anchors, and why rows refused."""
    notes: list[str] = []
    _match_outlets(architect, vendor, notes)

    def ends(found: Drawing) -> dict[str, tuple[Span, frozenset[str], frozenset[str]]]:
        return {
            key: (
                span,
                _anchors(found, span.axis, span.a, span.row),
                _anchors(found, span.axis, span.b, span.row),
            )
            for key, span in found.spans.items()
        }

    arch_ends, vendor_ends = ends(architect), ends(vendor)
    pairs: list[dict[str, Any]] = []
    for key, (span, first, second) in sorted(vendor_ends.items()):
        if not first or not second or first & second:
            continue
        matched = [
            arch
            for arch, a_first, a_second in arch_ends.values()
            if arch.axis == span.axis
            and (
                (
                    a_first & first
                    and a_second & second
                    and not a_first & second
                    and not a_second & first
                )
                or (
                    a_first & second
                    and a_second & first
                    and not a_first & first
                    and not a_second & second
                )
            )
        ]
        chain: list[Span] = []
        if matched:
            places = {(round(m.a, 1), round(m.b, 1)) for m in matched}
            spread = max(abs(p[0] - q[0]) + abs(p[1] - q[1]) for p in places for q in places)
            if len({m.inches for m in matched}) != 1 or spread > 2 * _SAME_PT:
                notes.append(f"vendor {key}: architect {', '.join(m.id for m in matched)} disagree")
                continue
            chain = sorted(matched, key=lambda m: m.id)
            architect_in, kind = chain[0].inches, "one"
        else:
            chain = _chain(arch_ends, span.axis, first, second)
            if len(chain) < 2:
                continue
            architect_in, kind = sum((m.inches for m in chain), Fraction(0)), "sum"
        same = abs(architect_in - span.inches) <= SAME_IN
        routes = {str(label.get("route")) for label in (span.label, *(m.label for m in chain))}
        texts = [str(m.label.get("text")) for m in chain]
        pairs.append(
            {
                "page": vendor_page,
                "kind": kind,
                "architect": {
                    "id": "+".join(m.id for m in chain) if kind == "sum" else chain[0].id,
                    "ids": [m.id for m in chain],
                    "text": " + ".join(texts) if kind == "sum" else texts[0],
                    "page": architect_page,
                },
                "vendor": [{"id": key, "text": span.label.get("text")}],
                "architect_in": _text(architect_in),
                "vendor_in": _text(span.inches),
                "status": "same" if same else "different",
                "printed_both": routes == {"text"},
                "paired_by": "code",
                "method": "anchors",
                "evidence": {"from": sorted(first), "to": sorted(second)},
                "how": (
                    f"paired by what both ends land on: {_say(first)} to {_say(second)}, on both "
                    "drawings (no scale used)"
                    + (
                        " (the architect's chain of joined dimensions, summed)"
                        if kind == "sum"
                        else ""
                    )
                ),
            }
        )
    return pairs, notes


def _chain(
    arch_ends: Mapping[str, tuple[Span, frozenset[str], frozenset[str]]],
    axis: str,
    first: frozenset[str],
    second: frozenset[str],
) -> list[Span]:
    """The architect's joined spans on one line from the place `first` lands to `second`'s."""

    def place(anchors: frozenset[str]) -> float | None:
        found = sorted(
            {
                round(end, 1)
                for span, a, b in arch_ends.values()
                if span.axis == axis
                for end, at in ((span.a, a), (span.b, b))
                if at & anchors
            }
        )
        return found[0] if found and found[-1] - found[0] <= _SAME_PT else None

    start, stop = place(first), place(second)
    if start is None or stop is None or abs(start - stop) < 1:
        return []
    low, high = min(start, stop), max(start, stop)
    by_row: dict[int, list[Span]] = {}
    for span, _a, _b in arch_ends.values():
        if span.axis == axis:
            by_row.setdefault(round(span.row), []).append(span)
    found: list[Span] = []
    for spans in by_row.values():
        here = [s for s in spans if abs(s.a - low) <= _SAME_PT]
        if len(here) != 1:
            continue
        got = [here[0]]
        while abs(got[-1].b - high) > _SAME_PT:
            nxt = [s for s in spans if abs(s.a - got[-1].b) <= _SAME_PT]
            if len(nxt) != 1 or nxt[0].b > high + _SAME_PT:
                got = []
                break
            got.append(nxt[0])
        if got:
            if found:
                return []  # two lines both run between the anchors: which one is meant is unclear
            found = got
    return found


# ---- the whole set -------------------------------------------------------------------------------


def page_segments(pdf: Path, page_index: int) -> tuple[Segment, ...]:
    """The page's black and grey line work as `dim_labels` reads it (the vendor layer: GV's
    markup annotations never reach it)."""
    labels = import_module("app.review.assistant.dim_labels")
    parsed = labels._parse(pdf, page_index)  # the one shared parse of a page
    return tuple(parsed.segments)


def set_anchor_pairs(
    dims: Mapping[str, Any],
    page_map: Mapping[str, Any],
    segments_of: Callable[[str, int], Sequence[Segment]],
    *,
    cache: dict[Any, Any] | None = None,
) -> dict[tuple[str, int], dict[str, Any]]:
    """Every architect drawing against every vendor drawing of the same area and file: {(file,
    vendor page): {"pairs": [...], "notes": [...]}}. `segments_of(file, page)` gives a page's line
    work; `cache` keeps results between calls while `dims.json` grows."""
    labels = {
        (str(entry.get("file")), int(entry.get("page") or 0)): [
            label for label in entry.get("labels") or [] if isinstance(label, Mapping)
        ]
        for entry in dims.get("pages") or []
        if isinstance(entry, Mapping)
    }
    groups: dict[tuple[str, str], list[tuple[int, str, Box]]] = {}
    for entry in page_map.get("pages") or []:
        if not isinstance(entry, Mapping) or not entry.get("area"):
            continue
        file, page = str(entry.get("file")), int(entry.get("page") or 0)
        for half in entry.get("halves") or []:
            role = half.get("role") if isinstance(half, Mapping) else None
            raw = half.get("box") if isinstance(half, Mapping) else None
            if isinstance(half, Mapping) and half.get("picture"):
                continue  # a headed pasted picture (#1835): no vector line work to anchor on
            if role in ("architect", "vendor") and raw and len(raw) == 4:
                box = (float(raw[0]), float(raw[1]), float(raw[2]), float(raw[3]))
                groups.setdefault((file, str(entry["area"])), []).append((page, str(role), box))
    memo = cache if cache is not None else {}
    out: dict[tuple[str, int], dict[str, Any]] = {}
    for (file, _area), halves in groups.items():
        for arch_page, arch_role, arch_box in halves:
            if arch_role != "architect":
                continue
            for vendor_page, vendor_role, vendor_box in halves:
                if vendor_role != "vendor":
                    continue
                arch_labels = labels.get((file, arch_page)) or []
                vendor_labels = labels.get((file, vendor_page)) or []
                if not any(l.get("side") == "architect" for l in arch_labels) or not any(
                    l.get("side") == "vendor" for l in vendor_labels
                ):
                    continue
                key = (
                    file,
                    arch_page,
                    arch_box,
                    len(arch_labels),
                    vendor_page,
                    vendor_box,
                    len(vendor_labels),
                )
                if key not in memo:
                    memo[key] = _pair_halves(
                        file,
                        (arch_page, arch_box, arch_labels),
                        (vendor_page, vendor_box, vendor_labels),
                        segments_of,
                    )
                pairs, notes = memo[key]
                entry = out.setdefault((file, vendor_page), {"pairs": [], "notes": []})
                entry["pairs"].extend(pairs)
                entry["notes"].extend(notes)
    return out


def _pair_halves(
    file: str,
    arch: tuple[int, Box, Sequence[Label]],
    vendor: tuple[int, Box, Sequence[Label]],
    segments_of: Callable[[str, int], Sequence[Segment]],
) -> tuple[list[dict[str, Any]], list[str]]:
    architect = drawing("architect", arch[2], segments_of(file, arch[0]), arch[1])
    vendors = drawing("vendor", vendor[2], segments_of(file, vendor[0]), vendor[1])
    if architect is None or vendors is None:
        return [], []
    pairs, notes = pair_drawings(architect, vendors, architect_page=arch[0], vendor_page=vendor[0])
    where = f"anchors, architect p{arch[0]}"
    return pairs, [f"{where}: {note}" for note in notes]
