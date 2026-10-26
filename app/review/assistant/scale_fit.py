"""Each view's scale, fitted across and up from its own dimensions, and each value checked against
its drawn length: code, $0, no AI (final plan change #3, 2026-10-25).

A drawing's printed values and its drawn lines are two readings of one size. Within one view the
ratio of printed inches to drawn length is one number (Riegelnegg, TU Wien 2024: "the ratio must be
constant in a valid object view", 0.731% error on 81 real drawings), so the ratio fitted over a
view's dimensions tells which line is a label's own and which value does not agree with its line
(Werk24 lowers a value's confidence when it "doesn't match the drawn length at the drawing's
scale"). `view_scales` already measures each view's scale from the lines the label reader found
(`dims.json`'s `dim_line`); on real sets the reader finds a line for few labels (a GV countertop
page: 3 of 41), because it looks for a line along the text with slash ticks either side. This
module finds the rest and fits the scale across and up separately.

**1. A label's own line** (`spans`). Beside each printed value, along each axis, code looks for a
dimension line: a straight run of line work on one row near the text (the text on it, beside it,
or splitting it), stopped at each end by a mark: an extension line across the row, a slash tick or
an arrowhead. The run's ends are the nearest marks either side of the text (a chain of dimensions
on one line has a mark at every joint). The text may run across the dimension (a `23"` printed
level beside a line drawn up the page). The reader's own line (`dim_line`, on its drawing's scale)
is kept as it is. A label with more than one such line near it keeps none until the scale picks
one (step 3).

**2. The fit** (`fits`), per drawing (side) and per axis (across, up): the ratio inches per drawn
point of every label of at least `MIN_INCHES` with one line, grouped as `view_scales` does (the
largest set within `AGREE` of its own median first). A group of at least `MIN_LABELS` is a scale,
with the box round its labels and lines (a view, or views at the same scale). Where a view has no
fit along one axis, its fit along the other axis is used, said as such (`other_axis`): views are
drawn to one scale both ways, and a pasted picture stretched one way shows as two fits that
disagree, which then give nothing (`at`).

**3. The scale picks a line.** A label with several lines near it keeps the one drawn to its value
at the fit, when only one is.

**4. Does the value agree with its drawn length?** (`Check`) Its line's length at the fit, against
its printed value: it agrees within `AGREE_IN` or `AGREE_SHARE` of the value, whichever is larger.
A value that does not is "drawn N long": typed over, misread, a break line, or a size that is not
to scale. It is never a red's witness (`full_check_advocate.decide`): a person checks it. With no
fit there is no check (unknown).

Pure: no PDF, no model. Verification: `tests/review/assistant/test_scale_fit.py` (made-up line
work).
"""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from statistics import median
from typing import Any, Final

from app.review.assistant.label_origin import ocr_read

__all__ = [
    "AGREE",
    "AGREE_IN",
    "AGREE_SHARE",
    "MIN_INCHES",
    "MIN_LABELS",
    "AxisFit",
    "Check",
    "Drawn",
    "PageFit",
    "page_fit",
]

type Point = tuple[float, float]
type Segment = tuple[Point, Point]
type Box = tuple[float, float, float, float]

#: Ratios within this share of their group's median are one scale (as `view_scales`).
AGREE: Final = 0.01
#: A scale needs this many labels that agree.
MIN_LABELS: Final = 3
#: The shortest value (inches) that gives a scale: a short one's drawn length is mostly line width.
MIN_INCHES: Final = Fraction(2)
#: A value agrees with its drawn length within this (inches) or this share of it, the larger.
AGREE_IN: Final = 0.5
AGREE_SHARE: Final = 0.03
#: ... or this much drawn (points): a line's own width and its ticks at a small scale.
AGREE_PT: Final = 1.5
#: A line found here drawn more than this share off its value at the fit is not the label's own
#: (a detail at another scale, a legend's swatch): it is dropped, never a check.
REJECT_SHARE: Final = 0.25
#: How far across from the text a label's dimension line may lie (points): along the text's own
#: direction, and when the text runs across the dimension (a level `23"` beside an upright line).
ROW_REACH_PT: Final = 8.0
SIDE_REACH_PT: Final = 14.0
#: A run of line is one line over gaps up to this (points); the gap the text leaves in it may be
#: this long at least (`_text_gap`).
JOIN_PT: Final = 1.5
TEXT_GAP_MAX_PT: Final = 50.0
TEXT_GAP_PAD_PT: Final = 6.0
#: An extension line marks an end when it reaches the dimension line within this (points) and is
#: at least `EXTENSION_MIN_PT` long; it stands within `MARK_PT` of the end along the line.
REACH_ROW_PT: Final = 1.5
EXTENSION_MIN_PT: Final = 4.0
MARK_PT: Final = 1.0
#: Rows of pieces are kept in bands this tall (points) for `_Lines.crossing`.
_BAND_PT: Final = 16.0
#: The shortest drawn line (points) a label is given here: shorter is a symbol or the text itself.
MIN_RUN_PT: Final = 6.0
#: Two lines nearer the text than the nearest one plus this (points) are both candidates.
RIVAL_PT: Final = 2.0
#: The most longer runs of one line (`Drawn.wider`) kept for the fit to try.
WIDER_MAX: Final = 12
#: A fit's box reaches this far (points) round its labels and lines.
BOX_MARGIN_PT: Final = 24.0
#: Line work closer than this (points) is one drawing (a view); a blank gap wider parts views.
VIEW_GAP_PT: Final = 6.0
#: A dimension's line lies in one view along at least this share of it.
VIEW_SHARE: Final = 0.6


@dataclass(frozen=True, slots=True)
class Drawn:
    """A label's dimension line: across at `row`, along from `a` to `b` (`a` < `b`)."""

    id: str
    axis: str
    row: float
    a: float
    b: float
    inches: Fraction
    how: str
    """`reader` (the label reader's own line), `marks` (found here), `scale` (one of several
    found here, picked by the fit)."""
    wider: tuple[tuple[float, float], ...] = ()
    """Longer runs of the same line round the text, each from one mark to another past the
    nearest ones (a line drawn across another drawn edge is marked there too): tried when the
    nearest marks' run does not agree with the fit."""

    @property
    def length(self) -> float:
        return self.b - self.a


@dataclass(frozen=True, slots=True)
class AxisFit:
    """One scale along one axis of one drawing."""

    side: str
    view: int
    """The drawing on the page the fit belongs to (`_Views`): line work joined by no more than a
    blank gap of `VIEW_GAP_PT`."""
    axis: str
    in_per_pt: float
    labels: tuple[str, ...]
    spread_percent: float
    box: Box
    borrowed: bool = False
    """Fitted on another drawing of the same side, and confirmed here (`_borrow`)."""

    def as_dict(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "view": self.view,
            "borrowed": self.borrowed,
            "axis": self.axis,
            "in_per_pt": round(self.in_per_pt, 6),
            "about": f"1:{round(self.in_per_pt * 72)}",
            "from_labels": list(self.labels),
            "spread_percent": round(self.spread_percent, 2),
            "box": [round(v, 1) for v in self.box],
        }


@dataclass(frozen=True, slots=True)
class Check:
    """A printed value against its drawn length at its view's fit."""

    id: str
    axis: str
    printed: Fraction
    drawn_in: float
    agrees: bool
    other_axis: bool = False
    """The fit is the view's fit along the other axis (none along this one)."""

    @property
    def off_by(self) -> float:
        return self.drawn_in - float(self.printed)

    def words(self, whose: str = "") -> str:
        """`the vendor's 23" is drawn 24" long at its view's own scale`."""
        from app.review.assistant.ledger import format_inches

        drawn = Fraction(round(self.drawn_in * 16), 16)
        own = f"{whose} " if whose else ""
        return (
            f"{own}{format_inches(self.printed)} is drawn {format_inches(drawn)} long at its "
            "view's own scale"
        ).strip()

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "axis": self.axis,
            "drawn_in": round(self.drawn_in, 2),
            "off_by": round(self.off_by, 2),
            "agrees": self.agrees,
            "other_axis": self.other_axis,
        }


@dataclass(slots=True)
class PageFit:
    """One page: its labels' lines, its fits, and each label's check."""

    spans: dict[str, Drawn] = field(default_factory=dict)
    fits: list[AxisFit] = field(default_factory=list)
    checks: dict[str, Check] = field(default_factory=dict)
    sides: dict[str, str] = field(default_factory=dict)
    """Each label's side (`dims.json`), for `fit_at`."""
    views: dict[str, int] = field(default_factory=dict)
    """Each label's drawing on the page (`_Views`), from its line."""

    def check(self, label_id: object) -> Check | None:
        return self.checks.get(str(label_id))

    def disagrees(self, label_id: object) -> Check | None:
        """The label's check when its value does not agree with its drawn length."""
        found = self.checks.get(str(label_id))
        return found if found is not None and not found.agrees else None

    def fit_at(self, side: str, view: int, axis: str) -> tuple[AxisFit, bool] | None:
        """The fit of `side`'s drawing `view` along `axis` (or, with none along it, along the other
        axis, then `True`); `None` when there is none, or two that disagree."""
        for wanted, other in ((axis, False), (_across(axis), True)):
            found = [
                fit
                for fit in self.fits
                if fit.side == side and fit.view == view and fit.axis == wanted
            ]
            if not found:
                continue
            if any(abs(f.in_per_pt / found[0].in_per_pt - 1) > AGREE for f in found[1:]):
                return None
            return found[0], other
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "fits": [fit.as_dict() for fit in self.fits],
            "labels": [check.as_dict() for check in self.checks.values()],
            "lines": {
                key: {
                    "axis": s.axis,
                    "row": round(s.row, 2),
                    "a": round(s.a, 2),
                    "b": round(s.b, 2),
                    "how": s.how,
                }
                for key, s in self.spans.items()
            },
        }


# ---- line work -----------------------------------------------------------------------------------


class _Lines:
    """A page's line work split once: pieces along each axis kept by their place (rounded to half a
    point) for lookup, and the short slanted strokes (ticks, arrowheads)."""

    def __init__(self, segments: Iterable[Segment]) -> None:
        rows: dict[str, dict[float, list[tuple[float, float, float]]]] = {"h": {}, "v": {}}
        slants: list[tuple[float, float]] = []
        for (x0, y0), (x1, y1) in segments:
            dx, dy = x1 - x0, y1 - y0
            if not all(math.isfinite(v) for v in (x0, y0, x1, y1)):
                continue
            if abs(dy) < 0.05 and abs(dx) >= 0.3:
                at = (y0 + y1) / 2
                rows["h"].setdefault(round(at * 2) / 2, []).append((min(x0, x1), max(x0, x1), at))
            elif abs(dx) < 0.05 and abs(dy) >= 0.3:
                at = (x0 + x1) / 2
                rows["v"].setdefault(round(at * 2) / 2, []).append((min(y0, y1), max(y0, y1), at))
            elif 1.0 <= math.hypot(dx, dy) <= 12 and dx and 0.25 <= abs(dy / dx) <= 4:
                slants.append(((x0 + x1) / 2, (y0 + y1) / 2))
        self.rows = rows
        self.keys = {axis: sorted(found) for axis, found in rows.items()}
        # Long pieces by the bands of rows they cross, for `crossing` (a dimension line's marks).
        self.bands: dict[str, dict[int, list[tuple[float, float, float]]]] = {"h": {}, "v": {}}
        for axis, found in rows.items():
            for pieces in found.values():
                for p0, p1, place in pieces:
                    if p1 - p0 < EXTENSION_MIN_PT:
                        continue
                    for band in range(
                        int((p0 - REACH_ROW_PT) // _BAND_PT),
                        int((p1 + REACH_ROW_PT) // _BAND_PT) + 1,
                    ):
                        self.bands[axis].setdefault(band, []).append((p0, p1, place))
        self.by_x = sorted(slants)
        self._xs = [s[0] for s in self.by_x]
        self.by_y = sorted(slants, key=lambda s: s[1])
        self._ys = [s[1] for s in self.by_y]

    def crossing(self, axis: str, row: float) -> list[tuple[float, float, float]]:
        """Pieces along `axis`, at least `EXTENSION_MIN_PT` long, that reach the place `row`
        across them (within `REACH_ROW_PT`): (from, to, place)."""
        return [
            piece
            for piece in self.bands[axis].get(int(row // _BAND_PT), ())
            if piece[0] - REACH_ROW_PT <= row <= piece[1] + REACH_ROW_PT
        ]

    def places(self, axis: str, low: float, high: float) -> list[float]:
        keys = self.keys[axis]
        return keys[bisect_left(keys, low - 0.5) : bisect_right(keys, high + 0.5)]

    def on(self, axis: str, at: float, tol: float = 0.3) -> list[tuple[float, float]]:
        """Pieces of line along `axis` lying on `at` (y for `h`, x for `v`): (from, to) along."""
        out: list[tuple[float, float]] = []
        for key in self.places(axis, at - tol, at + tol):
            out += [(p0, p1) for p0, p1, place in self.rows[axis][key] if abs(place - at) <= tol]
        return sorted(out)

    def slanted_on(
        self, axis: str, row: float, low: float, high: float
    ) -> list[tuple[float, float]]:
        """The short slanted strokes on the line at `row` (within 1 pt), from `low` to `high`
        along it: their midpoints (x, y)."""
        if axis == "h":
            found = self.by_y[bisect_left(self._ys, row - 1.0) : bisect_right(self._ys, row + 1.0)]
            return [(x, y) for x, y in found if low <= x <= high]
        found = self.by_x[bisect_left(self._xs, row - 1.0) : bisect_right(self._xs, row + 1.0)]
        return [(x, y) for x, y in found if low <= y <= high]


class _Views:
    """The page's drawings: its line work drawn on a grid of `VIEW_GAP_PT` cells, cells next to
    each other joined (a dimension's extension lines stop a hair short of what they measure, so a
    view's dimensions join its drawing; a blank margin parts two views). A detail at another scale
    beside a plan is its own view, so the plan's fit never judges the detail's values."""

    def __init__(self, segments: Iterable[Segment]) -> None:
        import numpy as np

        cell = VIEW_GAP_PT
        rows = np.array([(x0, y0, x1, y1) for (x0, y0), (x1, y1) in segments], dtype=float).reshape(
            -1, 4
        )
        rows = rows[np.isfinite(rows).all(axis=1)]
        steps = np.maximum(
            1, np.ceil(np.hypot(rows[:, 2] - rows[:, 0], rows[:, 3] - rows[:, 1]) / (cell / 2))
        ).astype(int)
        owner = np.repeat(np.arange(len(rows)), steps + 1)
        start = np.repeat(np.cumsum(steps + 1) - (steps + 1), steps + 1)
        t = (np.arange(len(owner)) - start) / np.repeat(steps, steps + 1)
        xs = rows[owner, 0] + (rows[owner, 2] - rows[owner, 0]) * t
        ys = rows[owner, 1] + (rows[owner, 3] - rows[owner, 1]) * t
        # One number per cell (a page is far less than a million cells wide), made unique once.
        keys = np.unique((xs // cell).astype(np.int64) * 1_000_000 + (ys // cell).astype(np.int64))
        across, down = np.divmod(keys, 1_000_000)
        filled: set[tuple[int, int]] = set(zip(across.tolist(), down.tolist(), strict=True))
        self.cell = cell
        self.of: dict[tuple[int, int], int] = {}
        label = 0
        for seed in filled:
            if seed in self.of:
                continue
            label += 1
            self.of[seed] = label
            todo = [seed]
            while todo:
                cx, cy = todo.pop()
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        nxt = (cx + dx, cy + dy)
                        if nxt in filled and nxt not in self.of:
                            self.of[nxt] = label
                            todo.append(nxt)

    def near(self, x: float, y: float) -> set[int]:
        """The views of the line work within one cell of (x, y)."""
        cx, cy = int(x // self.cell), int(y // self.cell)
        return {
            self.of[(cx + dx, cy + dy)]
            for dx in (-1, 0, 1)
            for dy in (-1, 0, 1)
            if (cx + dx, cy + dy) in self.of
        }

    def of_span(self, span: Drawn) -> int | None:
        """The view of a dimension's line: the one its line work lies in along most of its length
        (at least `VIEW_SHARE` of the places looked at that touch any view)."""
        counts: dict[int, int] = {}
        steps = max(2, math.ceil(span.length / self.cell))
        touched = 0
        for i in range(steps + 1):
            along = span.a + span.length * i / steps
            x, y = (along, span.row) if span.axis == "h" else (span.row, along)
            found = self.near(x, y)
            if found:
                touched += 1
            for view in found:
                counts[view] = counts.get(view, 0) + 1
        if not counts:
            return None
        view, count = max(counts.items(), key=lambda item: item[1])
        return view if count >= VIEW_SHARE * touched else None


def _across(axis: str) -> str:
    return "v" if axis == "h" else "h"


def _box(raw: object) -> Box | None:
    if not isinstance(raw, Sequence) or isinstance(raw, str) or len(raw) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in raw)
    except (TypeError, ValueError):
        return None
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def _inches(label: Mapping[str, Any]) -> Fraction | None:
    try:
        value = Fraction(Decimal(str(label.get("inches"))))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return value if value > 0 else None


# ---- a label's own line --------------------------------------------------------------------------


def _marks(lines: _Lines, axis: str, row: float, low: float, high: float, text: Box) -> list[float]:
    """Places along the line at `row` (from `low` to `high`) where something marks a dimension's
    end: an extension line across the row reaching it, or a slash tick or arrowhead on it. Strokes
    inside the text's box are the text's own (pen-stroke numbers), never a mark."""
    other = _across(axis)
    t_lo, t_hi = (text[0], text[2]) if axis == "h" else (text[1], text[3])
    t_near, t_far = (text[1], text[3]) if axis == "h" else (text[0], text[2])
    found: list[float] = []
    for q0, q1, place in lines.crossing(other, row):
        if not low <= place <= high:
            continue
        if t_lo - 1 <= place <= t_hi + 1 and q0 >= t_near - 1 and q1 <= t_far + 1:
            continue  # a stroke of the text itself
        found.append(place)
    for x, y in lines.slanted_on(axis, row, low, high):
        if text[0] - 0.5 <= x <= text[2] + 0.5 and text[1] - 0.5 <= y <= text[3] + 0.5:
            continue  # a stroke of the text itself
        found.append(x if axis == "h" else y)
    found.sort()
    merged: list[float] = []
    for place in found:
        if merged and place - merged[-1] <= MARK_PT:
            continue
        merged.append(place)
    return merged


def _covered(
    pieces: Sequence[tuple[float, float]],
    low: float,
    high: float,
    text: tuple[float, float],
) -> bool:
    """The pieces cover low..high, but for gaps up to `JOIN_PT` and one gap the text leaves: a gap
    holding the text's middle, at most `_text_gap` long (the text and the line of millimetres a
    dual label prints under it)."""
    middle = (text[0] + text[1]) / 2
    longest = _text_gap(text[1] - text[0])
    place = low
    allowed = True
    for p0, p1 in pieces:
        if p1 <= place:
            continue
        if p0 > place + JOIN_PT:
            if allowed and place - JOIN_PT <= middle <= p0 + JOIN_PT and p0 - place <= longest:
                allowed = False  # the one gap the text leaves
            else:
                return False
        place = max(place, p1)
        if place >= high - JOIN_PT:
            return True
    return place >= high - JOIN_PT


def _text_gap(length: float) -> float:
    """The longest gap (points) a text `length` long may leave in its dimension line."""
    return max(length + 2 * max(length, 12.0), TEXT_GAP_MAX_PT)


def _candidates(lines: _Lines, label: Mapping[str, Any], box: Box) -> list[tuple[float, Drawn]]:
    """(distance across from the text, line) for each dimension line near the label, either
    axis, nearest first."""
    key = str(label.get("id"))
    value = _inches(label)
    if value is None:
        return []
    out: list[tuple[float, Drawn]] = []
    for axis in ("h", "v"):
        lo, hi = (box[0], box[2]) if axis == "h" else (box[1], box[3])
        near, far = (box[1], box[3]) if axis == "h" else (box[0], box[2])
        centre = (lo + hi) / 2
        text_along = (lo, hi)
        # The text runs along the line (on it, beside it, splitting it) or across it (beside it).
        reach = ROW_REACH_PT if (hi - lo) >= (far - near) else SIDE_REACH_PT
        for row in lines.places(axis, near - reach, far + reach):
            distance = 0.0 if near <= row <= far else min(abs(row - near), abs(row - far))
            if distance > reach:
                continue
            pieces = lines.on(axis, row)
            if not pieces:
                continue
            start = min(p0 for p0, _ in pieces)
            stop = max(p1 for _, p1 in pieces)
            if start > hi + TEXT_GAP_PAD_PT or stop < lo - TEXT_GAP_PAD_PT:
                continue
            marks = _marks(lines, axis, row, start - 1, stop + 1, box)
            left = [m for m in marks if m < centre - 0.5 and m < lo + 0.5]
            right = [m for m in marks if m > centre + 0.5 and m > hi - 0.5]
            if not left or not right:
                continue
            a, b = max(left), min(right)
            if b - a < MIN_RUN_PT:
                continue
            if not _covered(pieces, a + 0.5, b - 0.5, text_along):
                continue
            wider = tuple(
                (low, high)
                for low in sorted(left, reverse=True)
                for high in right
                if (low, high) != (a, b) and _covered(pieces, low + 0.5, high - 0.5, text_along)
            )[:WIDER_MAX]
            out.append((distance, Drawn(key, axis, row, a, b, value, "marks", wider)))
    out.sort(key=lambda item: item[0])
    return out


def _reader_line(label: Mapping[str, Any]) -> Drawn | None:
    """The label reader's own line, when it says the line is on its drawing's scale."""
    line = label.get("dim_line")
    value = _inches(label)
    if label.get("scale_ok") is not True or value is None or not isinstance(line, Sequence):
        return None
    try:
        (x0, y0), (x1, y1) = ((float(p[0]), float(p[1])) for p in line)
    except (TypeError, ValueError, IndexError):
        return None
    key = str(label.get("id"))
    if abs(y0 - y1) <= 0.5 and abs(x1 - x0) > 0:
        return Drawn(key, "h", (y0 + y1) / 2, min(x0, x1), max(x0, x1), value, "reader")
    if abs(x0 - x1) <= 0.5 and abs(y1 - y0) > 0:
        return Drawn(key, "v", (x0 + x1) / 2, min(y0, y1), max(y0, y1), value, "reader")
    return None


# ---- the fit -------------------------------------------------------------------------------------


def _groups(rated: list[tuple[float, Drawn, Box]]) -> list[list[tuple[float, Drawn, Box]]]:
    """The largest set of ratios within `AGREE` of its own median first, then the largest among
    the rest, until no set of `MIN_LABELS` is left (`view_scales._groups`)."""
    left = sorted(rated, key=lambda item: item[0])
    out: list[list[tuple[float, Drawn, Box]]] = []
    while len(left) >= MIN_LABELS:
        best: list[tuple[float, Drawn, Box]] = []
        for start in range(len(left)):
            centre = left[start][0]
            members = [item for item in left if abs(item[0] / centre - 1) <= AGREE]
            middle = median(item[0] for item in members)
            members = [item for item in left if abs(item[0] / middle - 1) <= AGREE]
            if len(members) > len(best):
                best = members
        if len(best) < MIN_LABELS:
            break
        out.append(best)
        chosen = {id(item) for item in best}
        left = [item for item in left if id(item) not in chosen]
    return out


def _fit_box(members: Iterable[tuple[float, Drawn, Box]]) -> Box:
    xs: list[float] = []
    ys: list[float] = []
    for _ratio, span, box in members:
        xs += [box[0], box[2]]
        ys += [box[1], box[3]]
        if span.axis == "h":
            xs += [span.a, span.b]
            ys.append(span.row)
        else:
            ys += [span.a, span.b]
            xs.append(span.row)
    return (
        min(xs) - BOX_MARGIN_PT,
        min(ys) - BOX_MARGIN_PT,
        max(xs) + BOX_MARGIN_PT,
        max(ys) + BOX_MARGIN_PT,
    )


def _fits(
    spans: Mapping[str, Drawn],
    boxes: Mapping[str, Box],
    sides: Mapping[str, str],
    views: Mapping[str, int],
) -> list[AxisFit]:
    rated: dict[tuple[str, int, str], list[tuple[float, Drawn, Box]]] = {}
    for key, span in spans.items():
        if span.inches < MIN_INCHES or span.length <= 0 or key not in views:
            continue
        rated.setdefault((sides.get(key, "unknown"), views[key], span.axis), []).append(
            (float(span.inches) / span.length, span, boxes[key])
        )
    out: list[AxisFit] = []
    for (side, view, axis), found in sorted(rated.items()):
        for group in _groups(found):
            ratios = [ratio for ratio, _, _ in group]
            middle = median(ratios)
            out.append(
                AxisFit(
                    side,
                    view,
                    axis,
                    middle,
                    tuple(sorted(span.id for _, span, _ in group)),
                    (max(ratios) - min(ratios)) / middle * 100,
                    _fit_box(group),
                )
            )
    return out


def _borrow(
    fits: Sequence[AxisFit],
    spans: Mapping[str, Drawn],
    sides: Mapping[str, str],
    views: Mapping[str, int],
) -> list[AxisFit]:
    """Fits for drawings with too few dimensions of their own (a countertop's separate leg or
    splash drawn beside its plan): a scale fitted on another drawing of the same side, when at
    least one of this drawing's values agrees with it within `AGREE` and none is off it by more
    than a value may be (`_agrees`). A detail at another scale has values that do not agree, so it
    borrows nothing."""
    fitted = {(fit.side, fit.view) for fit in fits}
    # Scales of the whole page's side (every drawing, both axes): a GV countertop page draws each
    # piece of the top as its own drawing, all at one scale, two or three sizes each.
    rated: dict[str, list[tuple[float, Drawn, Box]]] = {}
    for key, span in spans.items():
        if span.inches >= MIN_INCHES and span.length > 0:
            rated.setdefault(sides.get(key, "unknown"), []).append(
                (float(span.inches) / span.length, span, (0.0, 0.0, 0.0, 0.0))
            )
    page_scales = {
        side: [median(r for r, _, _ in group) for group in _groups(found)]
        for side, found in rated.items()
    }
    members: dict[tuple[str, int], list[Drawn]] = {}
    for key, span in spans.items():
        if key in views and span.inches >= MIN_INCHES and span.length > 0:
            members.setdefault((sides.get(key, "unknown"), views[key]), []).append(span)
    out: list[AxisFit] = []
    for (side, view), found in sorted(members.items()):
        if (side, view) in fitted:
            continue
        scales: list[float] = []
        for value in [fit.in_per_pt for fit in fits if fit.side == side] + page_scales.get(
            side, []
        ):
            if all(abs(value / v - 1) > AGREE for v in scales):
                scales.append(value)
        good = []
        for scale in scales:
            agree = [s for s in found if abs(float(s.inches) / s.length / scale - 1) <= AGREE]
            if agree and all(_agrees(s, scale)[1] for s in found):
                good.append((scale, agree))
        if len(good) != 1:
            continue
        scale, agree = good[0]
        for axis in sorted({s.axis for s in found}):
            xs = [v for s in found for v in ((s.a, s.b) if axis == "h" else (s.row,))]
            ys = [v for s in found for v in ((s.row,) if axis == "h" else (s.a, s.b))]
            out.append(
                AxisFit(
                    side,
                    view,
                    axis,
                    scale,
                    tuple(sorted(s.id for s in agree)),
                    0.0,
                    (min(xs), min(ys), max(xs), max(ys)),
                    borrowed=True,
                )
            )
    return out


def _agrees(span: Drawn, in_per_pt: float) -> tuple[float, bool]:
    drawn = span.length * in_per_pt
    allowed = max(AGREE_IN, AGREE_SHARE * float(span.inches), AGREE_PT * in_per_pt)
    return drawn, abs(drawn - float(span.inches)) <= allowed


def _reader_line_drawn_on(
    lines: _Lines, label: Mapping[str, Any], box: Box, own: Drawn, in_per_pt: float
) -> list[Drawn]:
    """The reader's line drawn on past where the reader stopped: each run of the line work along
    the same axis, through the reader's line (it holds it within `MARK_PT` at each end) from one
    mark to another, that agrees with the view's fit."""
    runs: set[tuple[float, float, float]] = set()
    for _distance, found in _candidates(lines, label, box):
        if found.axis != own.axis or abs(found.row - own.row) > ROW_REACH_PT:
            continue
        for low, high in ((found.a, found.b), *found.wider):
            if low > own.a + MARK_PT or high < own.b - MARK_PT:
                continue
            run = Drawn(own.id, own.axis, own.row, low, high, own.inches, "scale")
            if _agrees(run, in_per_pt)[1]:
                runs.add((round(low, 1), round(high, 1), own.row))
    kept: dict[tuple[float, float], float] = {}
    for low, high, row in sorted(runs):
        kept.setdefault((low, high), row)
    return [
        Drawn(own.id, own.axis, row, low, high, own.inches, "scale")
        for (low, high), row in kept.items()
    ]


# ---- one page ------------------------------------------------------------------------------------


def page_fit(labels: Sequence[Mapping[str, Any]], segments: Sequence[Segment] | None) -> PageFit:
    """One page's labels (as `dims.json` holds them) with their lines, the page's fits across and
    up, and each label's check against its view's fit (see the module). Without line work only the
    reader's own lines are used."""
    out = PageFit()
    boxes: dict[str, Box] = {}
    candidates: dict[str, list[Drawn]] = {}
    by_key: dict[str, Mapping[str, Any]] = {}
    lines = _Lines(segments) if segments else None
    for label in labels:
        if not isinstance(label, Mapping) or not label.get("id"):
            continue
        key = str(label["id"])
        box = _box(label.get("box"))
        if box is None or _inches(label) is None or ocr_read(label):
            continue  # a picture's pixels have no line (#1835)
        boxes[key] = box
        by_key[key] = label
        out.sides[key] = str(label.get("side") or "unknown")
        own = _reader_line(label)
        if own is not None:
            out.spans[key] = own
            continue
        if lines is None:
            continue
        found = _candidates(lines, label, box)
        if not found:
            continue
        nearest = found[0][0]
        rivals: list[Drawn] = []
        for distance, span in found:
            if distance > nearest + RIVAL_PT:
                break
            if not any(
                r.axis == span.axis
                and abs(r.a - span.a) <= MARK_PT
                and abs(r.b - span.b) <= MARK_PT
                for r in rivals
            ):
                rivals.append(span)
        if len(rivals) == 1:
            out.spans[key] = rivals[0]
        else:
            candidates[key] = rivals
    views = _Views(segments) if segments else None

    def view_of(span: Drawn) -> int | None:
        return views.of_span(span) if views is not None else 0

    for key, span in out.spans.items():
        view = view_of(span)
        if view is not None:
            out.views[key] = view
    out.fits = _fits(out.spans, boxes, out.sides, out.views)
    out.fits += _borrow(out.fits, out.spans, out.sides, out.views)
    # The scale picks a line where several lie near a label.
    for key, rivals in candidates.items():
        fitting = []
        for span in rivals:
            view = view_of(span)
            fit = None if view is None else out.fit_at(out.sides[key], view, span.axis)
            if fit is not None and _agrees(span, fit[0].in_per_pt)[1]:
                fitting.append(span)
        if len(fitting) == 1:
            chosen = fitting[0]
            out.spans[key] = Drawn(
                chosen.id, chosen.axis, chosen.row, chosen.a, chosen.b, chosen.inches, "scale"
            )
            view = view_of(chosen)
            if view is not None:
                out.views[key] = view
    for key, span in list(out.spans.items()):
        view = out.views.get(key)
        fit = None if view is None else out.fit_at(out.sides[key], view, span.axis)
        if fit is None:
            continue
        drawn, agrees = _agrees(span, fit[0].in_per_pt)
        if not agrees and span.wider:
            # The nearest marks may be another drawn edge the line crosses: a longer run of the
            # same line that agrees, the only one, is the label's.
            longer = [
                Drawn(key, span.axis, span.row, low, high, span.inches, "scale")
                for low, high in span.wider
            ]
            fitting = [s for s in longer if _agrees(s, fit[0].in_per_pt)[1]]
            if len(fitting) == 1:
                span = out.spans[key] = fitting[0]
                drawn, agrees = _agrees(span, fit[0].in_per_pt)
        if not agrees and span.how == "reader" and lines is not None:
            # The label reader stops its line at the first mark it meets, which may be another
            # line crossing it (a 16" line read as 13 3/8" long): the same line drawn on to a
            # further mark, the only such run that agrees, is the label's. With several, the
            # label's own line is not known: no check, so a cut line never doubts a true value.
            runs = _reader_line_drawn_on(lines, by_key[key], boxes[key], span, fit[0].in_per_pt)
            if len(runs) > 1:
                continue  # several runs on agree: which is the label's is not known, no check
            if runs:
                span = out.spans[key] = runs[0]
                drawn, agrees = _agrees(span, fit[0].in_per_pt)
        if abs(drawn / float(span.inches) - 1) > REJECT_SHARE:
            if span.how != "reader":
                del out.spans[key]  # not the label's own line
            continue
        out.checks[key] = Check(key, span.axis, span.inches, drawn, agrees, fit[1])
    return out
