"""Measure a length on a drawing, by code, against the drawing's own printed dimensions (#1545).

A reviewer who meets a size the architect never printed scales it: they hold a ruler to the drawing,
check the ruler against two dimensions that ARE printed, and say "about 185 1/2 in". claude.ai did
that for GV's owner on a test set; our agent could only say "confirm the bay sizes". This module
is that ruler, in code, $0, no AI, the same answer every time:

- **Two points to measure** (`start`, `end`, PDF points of the page as shown, origin top-left), and
  an axis: `h` (left-right only), `v` (up-down only) or `along` (straight between the points).
- **Each point snaps** to the drawing's own line work when one is within `SNAP_PT`: the end of a
  drawn line, or (for `h` / `v`) an extension line across the axis, so a point picked off a picture
  lands where the drawing really ends. Only black or grey line work counts (GV's markup is coloured).
- **At least two printed dimensions calibrate the scale**: each gives inches per drawn point, and
  code checks they agree. When they differ by more than `MAX_SPREAD_PERCENT` the drawing is not to
  one scale there (or a pair was picked wrong) and code refuses: no number beats a wrong number.
- **The extent check** (optional `dimension`): the two ends of a printed dimension's extension lines.
  An architect's height "8'-11"" may run from the floor to the top of a box ABOVE the unit, not to
  the unit's top. Code measures both spans on the same axis and says how far the printed dimension
  runs past the unit's own outline at each end. A dimension that covers more than the unit measures
  something else, so a different vendor value is a "Your decision", never a Wrong (`FindingBook`
  turns such a finding into one).

The answer is "about X in (±T) — scale checked against N printed dims, spread Y%". A scaled value is
never a Wrong by itself (SKILL.md section 7): it only helps a person decide.

Loaded by name (`chat_tools`), like `verify_value`: it reads PDFs with pdfplumber and pikepdf.
Verification: `tests/review/assistant/test_measure.py` (synthetic PDFs).
"""

from __future__ import annotations

import io
import math
import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any, Final, Literal

import pdfplumber

from app.review.assistant.finding_checks import to_inches
from app.review.assistant.verify_value import (
    _placement,
    combined_sheet_at,
    path_shown,
    vendor_page_at,
)

__all__ = [
    "MAX_SPREAD_PERCENT",
    "MIN_CALIBRATIONS",
    "SNAP_PT",
    "MeasureRefused",
    "Measured",
    "measure_on_drawing",
]

type Axis = Literal["h", "v", "along"]
type Point = tuple[float, float]
type Segment = tuple[Point, Point]

#: How close (points) a picked point must be to the drawing's line work to snap onto it.
SNAP_PT: Final = 4.0
#: Calibrations whose scales differ by more than this (percent of their mean) are refused.
MAX_SPREAD_PERCENT: Final = 3.0
MIN_CALIBRATIONS: Final = 2
MAX_CALIBRATIONS: Final = 8
#: A drawn span shorter than this (points) is too short to measure or calibrate on.
_SHORTEST_PT: Final = 5.0
#: How unsure one end is (points): snapped onto line work, or as picked off a picture.
_END_SNAPPED_PT: Final = 0.25
_END_PICKED_PT: Final = 1.5
#: A line within this share of the axis is "across it" (an extension line) for snapping.
_STRAIGHT: Final = 0.05
#: A calibration pair whose minor side is under this share of its major side is read on that axis.
_AXIS_SHARE: Final = 0.1


class MeasureRefused(ValueError):
    """The measurement cannot be made honestly; the message says why, in plain words."""


@dataclass(frozen=True, slots=True)
class _Snapped:
    point: Point
    how: Literal["line end", "extension line", "as given"]
    moved: float


@dataclass(frozen=True, slots=True)
class _Calibration:
    printed: str
    inches: Decimal
    drawn_pt: float
    scale: float
    """Inches per drawn point."""
    snapped: tuple[str, str]


@dataclass(frozen=True, slots=True)
class Measured:
    value_in: float
    plus_minus_in: float
    axis: Axis
    drawn_pt: float
    scale_ratio: float
    """Real inches per drawn inch (e.g. 24 for 1/2" = 1'-0")."""
    spread_percent: float
    calibrations: tuple[_Calibration, ...]
    snapped: tuple[str, str]
    extent: dict[str, Any] | None = field(default=None)
    scale_from: tuple[str, ...] = ()
    """The labels code measured the drawing's scale from (`view_scales`), when no calibration
    was given."""

    def words(self) -> str:
        """The answer in one or two plain lines."""
        how = (
            f"scale measured by code from {len(self.scale_from)} printed dims of this drawing "
            f"({', '.join(self.scale_from[:6])}{', ...' if len(self.scale_from) > 6 else ''})"
            if self.scale_from
            else f"scale checked against {len(self.calibrations)} printed dims"
        )
        text = (
            f'about {_inches_text(self.value_in)}" (±{_inches_text(self.plus_minus_in)}") '
            f"— {how}, spread "
            f"{self.spread_percent:.1f}% (drawing about 1:{round(self.scale_ratio)}). Ends: "
            f"{self.snapped[0]}, {self.snapped[1]}. Scaled, not printed: use it only to help a "
            "person decide (status check), never as a Wrong by itself."
        )
        if self.extent is not None:
            text += " " + str(self.extent["words"])
        return text

    def as_dict(self) -> dict[str, Any]:
        return {
            "value_in": self.value_in,
            "plus_minus_in": self.plus_minus_in,
            "axis": self.axis,
            "drawn_pt": round(self.drawn_pt, 2),
            "scale_ratio": round(self.scale_ratio, 2),
            "spread_percent": round(self.spread_percent, 2),
            "calibrations": [
                {
                    "printed": c.printed,
                    "inches": float(c.inches),
                    "drawn_pt": round(c.drawn_pt, 2),
                    "ends": list(c.snapped),
                }
                for c in self.calibrations
            ],
            "ends": list(self.snapped),
            "scale_from": list(self.scale_from),
            "extent": self.extent,
            "words": self.words(),
        }


def _inches_text(value: float) -> str:
    """185.5 as "185 1/2", to the nearest 1/16 inch."""
    sixteenths = round(value * 16)
    whole, rest = divmod(sixteenths, 16)
    if not rest:
        return str(whole)
    numerator, denominator = rest, 16
    while numerator % 2 == 0:
        numerator //= 2
        denominator //= 2
    return f"{whole} {numerator}/{denominator}" if whole else f"{numerator}/{denominator}"


# ---- the page's line work ------------------------------------------------------------------------

_CACHE_LOCK: Final = threading.Lock()
_CACHE: OrderedDict[tuple[str, int, int, int], tuple[Segment, ...]] = OrderedDict()
_CACHE_SIZE: Final = 8


def _pieces(item: Mapping[str, Any]) -> list[list[tuple[float, float]]]:
    """A pdfplumber line, curve or rectangle as straight polylines in its `(x, top)` frame (a curve
    is kept by its points: its ends are what snapping needs)."""
    path = item.get("path")
    if path:
        out: list[list[tuple[float, float]]] = []
        current: list[tuple[float, float]] = []
        for command in path:
            op = command[0]
            if op == "m":
                if current:
                    out.append(current)
                current = [(float(command[1][0]), float(command[1][1]))]
            elif op in ("l", "c"):
                end = command[-1]
                current.append((float(end[0]), float(end[1])))
            elif op == "h" and current:
                current.append(current[0])
        if current:
            out.append(current)
        return out
    points = item.get("pts")
    if points:
        return [[(float(x), float(y)) for x, y in points]]
    x0, x1, top, bot = (float(item[k]) for k in ("x0", "x1", "top", "bottom"))
    return [[(x0, top), (x1, top), (x1, bot), (x0, bot), (x0, top)]]


def _segments(pdf: Path, page_index: int) -> tuple[Segment, ...]:
    """Every straight piece of the page's black or grey line work (its own content and its pasted
    drawings), in shown points, cached per file version."""
    stat = pdf.stat()
    key = (str(pdf.resolve()), stat.st_mtime_ns, stat.st_size, page_index)
    with _CACHE_LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return _CACHE[key]
    colour_rule = combined_sheet_at(pdf)  # the colour rule: GV's combined sheets only
    one_page = vendor_page_at(pdf, page_index)  # cut from the file held open (#1874)
    segments: list[Segment] = []
    with pdfplumber.open(io.BytesIO(one_page)) as document:
        page = document.pages[0]
        shown: Callable[[float, float], tuple[float, float]] = _placement(page)
        for item in (*page.lines, *page.rects, *page.curves):
            if not path_shown(item, colour_rule=colour_rule):
                continue
            for piece in _pieces(item):
                points = [shown(x, y) for x, y in piece]
                segments.extend((a, b) for a, b in pairwise(points) if a != b)
    result = tuple(segments)
    with _CACHE_LOCK:
        _CACHE[key] = result
        while len(_CACHE) > _CACHE_SIZE:
            _CACHE.popitem(last=False)
    return result


def _snap(point: Point, axis: Axis, segments: Sequence[Segment]) -> _Snapped:
    """The point moved onto the nearest line end, or (axis h / v) onto the nearest extension line
    across the axis, when one is within `SNAP_PT`; else as given."""
    px, py = point
    best = _Snapped(point, "as given", SNAP_PT + 1)
    for a, b in segments:
        for end in (a, b):
            moved = math.hypot(end[0] - px, end[1] - py)
            if moved <= SNAP_PT and moved < best.moved:
                best = _Snapped(end, "line end", moved)
        if axis == "along":
            continue
        dx, dy = abs(b[0] - a[0]), abs(b[1] - a[1])
        length = math.hypot(dx, dy)
        if axis == "h" and dx <= _STRAIGHT * length:
            x = (a[0] + b[0]) / 2
            low, high = sorted((a[1], b[1]))
            moved = abs(x - px)
            if low - SNAP_PT <= py <= high + SNAP_PT and moved <= SNAP_PT and moved < best.moved:
                best = _Snapped((x, py), "extension line", moved)
        elif axis == "v" and dy <= _STRAIGHT * length:
            y = (a[1] + b[1]) / 2
            low, high = sorted((a[0], b[0]))
            moved = abs(y - py)
            if low - SNAP_PT <= px <= high + SNAP_PT and moved <= SNAP_PT and moved < best.moved:
                best = _Snapped((px, y), "extension line", moved)
    if best.how == "as given":
        return _Snapped(point, "as given", 0.0)
    return best


def _span(a: Point, b: Point, axis: Axis) -> float:
    if axis == "h":
        return abs(b[0] - a[0])
    if axis == "v":
        return abs(b[1] - a[1])
    return math.hypot(b[0] - a[0], b[1] - a[1])


def _uncertain(snapped: _Snapped) -> float:
    return _END_PICKED_PT if snapped.how == "as given" else _END_SNAPPED_PT


def _words(snapped: _Snapped) -> str:
    if snapped.how == "as given":
        return "as given (no line within 4 pt)"
    article = "an" if snapped.how[0] in "aeiou" else "a"
    return f"snapped to {article} {snapped.how} {snapped.moved:.1f} pt away"


def _point(raw: object, name: str) -> Point:
    if not isinstance(raw, Mapping) or set(raw) != {"x", "y"}:
        raise MeasureRefused(f"{name} must be {{x, y}} in PDF points")
    values = [raw["x"], raw["y"]]
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise MeasureRefused(f"{name} must hold two numbers (PDF points)")
        if not math.isfinite(value) or abs(value) > 100_000:
            raise MeasureRefused(f"{name} is not a page position")
    return float(values[0]), float(values[1])


def _pair_axis(a: Point, b: Point) -> Axis:
    """A calibration pair's axis: the one it runs along, when it is nearly level or plumb."""
    dx, dy = abs(b[0] - a[0]), abs(b[1] - a[1])
    if dy <= _AXIS_SHARE * dx:
        return "h"
    if dx <= _AXIS_SHARE * dy:
        return "v"
    return "along"


def _calibration(raw: object, number: int, segments: Sequence[Segment]) -> _Calibration:
    where = f"calibrate[{number}]"
    if not isinstance(raw, Mapping) or set(raw) != {"from", "to", "printed"}:
        raise MeasureRefused(f"{where} must be {{from, to, printed}}")
    printed = raw["printed"]
    if not isinstance(printed, str) or not printed.strip():
        raise MeasureRefused(f"{where}.printed must be the dimension as printed, as text")
    inches = to_inches(printed)
    if inches is None or inches <= 0:
        raise MeasureRefused(f"{where}.printed {printed!r} is not one length code can read")
    a, b = _point(raw["from"], f"{where}.from"), _point(raw["to"], f"{where}.to")
    axis = _pair_axis(a, b)
    first, second = _snap(a, axis, segments), _snap(b, axis, segments)
    drawn = _span(first.point, second.point, axis)
    if drawn < _SHORTEST_PT:
        raise MeasureRefused(f"{where} is {drawn:.1f} pt long on the drawing: too short to scale")
    return _Calibration(
        printed=printed.strip(),
        inches=inches,
        drawn_pt=drawn,
        scale=float(inches) / drawn,
        snapped=(_words(first), _words(second)),
    )


def _extent(
    raw: object,
    unit: tuple[_Snapped, _Snapped],
    axis: Axis,
    scale: float,
    segments: Sequence[Segment],
    tolerance_in: float,
) -> dict[str, Any]:
    """How a printed dimension's span compares with the unit's own span on the same axis: how far
    it runs past the unit at each end (inches, scaled), and in plain words what that means."""
    if axis == "along":
        raise MeasureRefused("the extent check needs axis h or v")
    if not isinstance(raw, Mapping) or set(raw) != {"from", "to", "printed"}:
        raise MeasureRefused("dimension must be {from, to, printed}")
    printed = raw["printed"]
    inches = to_inches(printed) if isinstance(printed, str) else None
    if inches is None:
        raise MeasureRefused(f"dimension.printed {printed!r} is not one length code can read")
    a = _snap(_point(raw["from"], "dimension.from"), axis, segments)
    b = _snap(_point(raw["to"], "dimension.to"), axis, segments)
    index = 0 if axis == "h" else 1
    d_low, d_high = sorted((a.point[index], b.point[index]))
    u_low, u_high = sorted((unit[0].point[index], unit[1].point[index]))
    before = (u_low - d_low) * scale
    after = (d_high - u_high) * scale
    dimension_in = (d_high - d_low) * scale
    names = ("above", "below") if axis == "v" else ("left of", "right of")
    beyond = [
        f'about {_inches_text(amount)}" {name} the unit'
        for amount, name in ((before, names[0]), (after, names[1]))
        if amount > tolerance_in
    ]
    short = [
        name for amount, name in ((before, names[0]), (after, names[1])) if amount < -tolerance_in
    ]
    covers_more = bool(beyond)
    fits = abs(dimension_in - float(inches)) <= max(tolerance_in, float(inches) * 0.03)
    if covers_more:
        words = (
            f"Extent: the printed {printed.strip()} spans more than the unit, "
            f"{' and '.join(beyond)}: it measures something beyond the unit (a box, soffit, "
            "blocking or wall). A different vendor value is then Your decision "
            "(check_reason different_extent), not Wrong."
        )
    elif short:
        words = (
            f"Extent: the printed {printed.strip()} stops short of the unit's outline "
            f"({' and '.join(short)} end): it measures only part of the unit."
        )
    else:
        words = f"Extent: the printed {printed.strip()} spans the unit itself, end to end."
    if not fits:
        words += (
            f' Its drawn span scales to about {_inches_text(dimension_in)}", not '
            f"{printed.strip()}: the drawing may not be to scale here."
        )
    return {
        "printed": printed.strip(),
        "printed_in": float(inches),
        "dimension_scaled_in": round(dimension_in, 3),
        "beyond_low_in": round(before, 3),
        "beyond_high_in": round(after, 3),
        "covers_more": covers_more,
        "fits_scale": fits,
        "ends": [_words(a), _words(b)],
        "measures": (
            f"the printed {printed.strip()} runs past the unit's own outline, "
            f"{' and '.join(beyond)} (a box, soffit, blocking or wall beyond it), not the unit "
            "alone"
            if covers_more
            else None
        ),
        "words": words,
    }


def measure_on_drawing(
    pdf: Path,
    page: int,
    start: object,
    end: object,
    calibrate: object,
    axis: object = "along",
    dimension: object = None,
    *,
    scale: Mapping[str, Any] | None = None,
) -> Measured:
    """The length from `start` to `end` on page `page` (from 1) of `pdf`, in inches, scaled
    against the printed dimensions in `calibrate` (each `{from, to, printed}`, at least two), or,
    when `calibrate` is `None`, against `scale` (the drawing's scale code measured from its own
    printed dimensions: `view_scales`, `{in_per_pt, from_labels, spread_percent}`), and
    optionally the extent check of a printed `dimension` against it. Raises `MeasureRefused`."""
    if axis not in ("h", "v", "along"):
        raise MeasureRefused('axis must be "h", "v" or "along"')
    chosen: Axis = axis
    if calibrate is None:
        return _measured_by_scale(pdf, page, start, end, chosen, dimension, scale)
    if not isinstance(calibrate, Sequence) or isinstance(calibrate, str):
        raise MeasureRefused("calibrate must be a list of printed dimensions")
    if not MIN_CALIBRATIONS <= len(calibrate) <= MAX_CALIBRATIONS:
        raise MeasureRefused(
            f"calibrate needs {MIN_CALIBRATIONS} to {MAX_CALIBRATIONS} printed dimensions on this "
            "page, so code can check the scale"
        )
    a, b = _point(start, "from"), _point(end, "to")
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise MeasureRefused("page must be a whole number from 1")
    try:
        segments = _segments(pdf, page - 1)
    except IndexError as missing:
        raise MeasureRefused(f"page {page} is not in {pdf.name}") from missing
    calibrations = tuple(_calibration(c, n, segments) for n, c in enumerate(calibrate))
    scales = [c.scale for c in calibrations]
    mean = sum(scales) / len(scales)
    spread = (max(scales) - min(scales)) / mean * 100
    if spread > MAX_SPREAD_PERCENT:
        listed = "; ".join(
            f"{c.printed} is {c.drawn_pt:.1f} pt (1:{round(c.scale * 72)})" for c in calibrations
        )
        raise MeasureRefused(
            f"the printed dimensions do not agree on one scale (spread {spread:.1f}%, more than "
            f"{MAX_SPREAD_PERCENT:g}%): {listed}. Check each pair's ends, or use dimensions of the "
            "same view"
        )
    first, second = _snap(a, chosen, segments), _snap(b, chosen, segments)
    drawn = _span(first.point, second.point, chosen)
    if drawn < _SHORTEST_PT:
        raise MeasureRefused(f"the two points are {drawn:.1f} pt apart: too close to measure")
    value = drawn * mean
    plus_minus = (_uncertain(first) + _uncertain(second)) * mean + value * max(spread, 0.5) / 100
    plus_minus = math.ceil(plus_minus * 8) / 8
    extent = (
        None
        if dimension is None
        else _extent(dimension, (first, second), chosen, mean, segments, plus_minus)
    )
    return Measured(
        value_in=round(value * 16) / 16,
        plus_minus_in=plus_minus,
        axis=chosen,
        drawn_pt=drawn,
        scale_ratio=mean * 72,
        spread_percent=spread,
        calibrations=calibrations,
        snapped=(_words(first), _words(second)),
        extent=extent,
    )


def _measured_by_scale(
    pdf: Path,
    page: object,
    start: object,
    end: object,
    axis: Axis,
    dimension: object,
    scale: Mapping[str, Any] | None,
) -> Measured:
    """`measure_on_drawing` with the drawing's measured scale instead of a hand calibration."""
    if not isinstance(scale, Mapping) or not isinstance(scale.get("in_per_pt"), (int, float)):
        raise MeasureRefused(
            "code has no measured scale where these points are (fewer than three printed "
            "dimensions of this drawing agree on one): give calibrate, two or more printed "
            "dimensions of the same view"
        )
    a, b = _point(start, "from"), _point(end, "to")
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise MeasureRefused("page must be a whole number from 1")
    try:
        segments = _segments(pdf, page - 1)
    except IndexError as missing:
        raise MeasureRefused(f"page {page} is not in {pdf.name}") from missing
    mean = float(scale["in_per_pt"])
    spread = float(scale.get("spread_percent") or 0.0)
    first, second = _snap(a, axis, segments), _snap(b, axis, segments)
    drawn = _span(first.point, second.point, axis)
    if drawn < _SHORTEST_PT:
        raise MeasureRefused(f"the two points are {drawn:.1f} pt apart: too close to measure")
    value = drawn * mean
    plus_minus = (_uncertain(first) + _uncertain(second)) * mean + value * max(spread, 0.5) / 100
    plus_minus = math.ceil(plus_minus * 8) / 8
    extent = (
        None
        if dimension is None
        else _extent(dimension, (first, second), axis, mean, segments, plus_minus)
    )
    return Measured(
        value_in=round(value * 16) / 16,
        plus_minus_in=plus_minus,
        axis=axis,
        drawn_pt=drawn,
        scale_ratio=mean * 72,
        spread_percent=spread,
        calibrations=(),
        snapped=(_words(first), _words(second)),
        extent=extent,
        scale_from=tuple(str(label) for label in scale.get("from_labels") or ()),
    )
