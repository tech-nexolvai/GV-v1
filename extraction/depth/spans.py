"""The dimension spans of one vendor view, on both axes, and the code checks on them (#1256).

Every piece of today's reading is horizontal: the width reader's rows are horizontal strokes grouped
by height. A countertop's depth on a plan runs **top to bottom**, and in a section it runs left to
right across the stone, so the depth and sink reader needs the view's dimensions on **both** axes.
This module finds them and gives the reader's second judgment (design §4, judgment 2) its pieces.
It reads no number off a picture and asks no model.

**What it finds.**

* **Spans.** `extraction/geometry/dimension_lines.detect` already finds dimension lines on both
  axes by their witness lines (a dimension line is crossed at both ends by a perpendicular stroke).
  It is called unchanged. A vendor's dimension line overshoots its witness lines by a few points (a
  CAD habit; measured 2.6 pt on a real sheet), so each span's ends are taken from **where its
  witness lines cross it**, not from where the stroke stops: that is the distance the number
  states.
* **What each end meets.** A wall band (the hatched or filled wall behind the stone, `wall_bands`),
  a solid outline edge (a long perpendicular stroke at the end that is not the span's own witness
  line or another dimension), or nothing. On a plan the depth runs from the wall band to the stone's
  front edge; this is what lets code say so.
* **Sink outlines.** Closed rectangles (one closed path, or four strokes, dashed pieces joined)
  around a sink's words or its round drain, lettered A, B, ... from the outside in, and whether the
  cut-out may be automatic: only when printed words name it (`OPENING`, `CUT-OUT`) or exactly one
  outline is drawn (Decision log 2026-10-14). Two outlines and no word is a hold.
* **Chains and closures.** Spans drawn end to end at one offset, and an overall span covering a
  chain exactly. Whether the printed parts sum to the printed overall is exact `Fraction`
  arithmetic (`closure_check`).

**The code checks, all exact.** Geometry (which strokes are a span, what an end meets, which
rectangles are a sink's) is compared on exact `Decimal` page points, as pdfplumber gives them,
against the named lengths of `SpanSettings`; no float is compared anywhere. The checks on values
are on `Fraction`s, and each is named here and tested at its boundary:

* `view_scale`: points per inch from the view's own horizontal overall and its printed value. A
  plan is drawn to one scale both ways, so a vertical span's drawn length can be checked with it.
* `drawn_length_check`: the drawn length at that scale is within ±1/10 of the value, and (when asked) within a stated number of inches, inclusive.
* `closure_check`: the parts' printed values sum exactly to the overall's.
* `text_lanes` / `text_lane_agrees`: on a page whose numbers are real text, the vector text at a
  span must **equal** the two readers' sealed reading exactly. Inches only: a bracketed inch value
  (`635 [25]`) wins over the millimetres, and millimetres are never an operand. A label with only
  millimetres, or a number with no unit, is held.

**Never silent.** Every stroke detect did not take is counted in `ViewSpans.unclassified`, every
span end says what it met or that it met nothing, and every hold carries its reason.

Pure: plain values in, plain values out, page points with `top` downward (pdfplumber's frame).

Source: issue #1256 (phase 5 design §4, §7 PR 1) · Verification:
`tests/extraction/depth/test_spans.py`
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from enum import StrEnum
from fractions import Fraction
from itertools import pairwise
from typing import Final, Literal
from uuid import UUID

from evidence.coordinates import StoredPoint
from extraction.architect.labels import read_label
from extraction.architect.outline import (
    OutlineSettings,
    Stroke,
    hatched_regions,
    slanted_strokes,
)
from extraction.architect.reader import MEASURED_ARCHITECT_SETTINGS
from extraction.architect.text import Orientation, PrintedPhrase, TextChar
from extraction.geometry.containment import DimensionExtent
from extraction.geometry.dimension_lines import Axis, DimensionLine, detect
from extraction.geometry.rows import Box, InkCurve, PageInk
from units.normalise import plain_marks

__all__ = [
    "CUTOUT_WORDS",
    "MEASURED_SPAN_SETTINGS",
    "ChainClosure",
    "ClosureCheck",
    "CutoutJudgment",
    "DepthLabel",
    "DrawnLength",
    "EndKind",
    "FrontOffsetStart",
    "OffsetStart",
    "Outline",
    "SinkOutlines",
    "Span",
    "SpanChain",
    "SpanEnd",
    "SpanSettings",
    "TextLane",
    "ViewScale",
    "ViewSpans",
    "WallBand",
    "closure_check",
    "cutout_judgment",
    "drawn_length_check",
    "find_spans",
    "front_offset_start",
    "overall_horizontal",
    "read_depth_label",
    "sink_outlines",
    "span_on_outline",
    "text_lane_agrees",
    "text_lanes",
    "view_scale",
    "wall_bands",
]

_TWO: Final = Decimal(2)
_ZERO: Final = Decimal(0)

#: Page points are divided by this to make `detect`'s stored coordinates (0..1). A power of ten, so
#: the division and the multiplication back are exact `Decimal` shifts: a span's ends come back as
#: the very numbers pdfplumber gave.
_TO_STORED: Final = Decimal(10000)
#: `detect` refuses geometry from two sheets compared as one; one view is one sheet, so the
#: identity it is given is a fixed placeholder that no stored row carries.
_UNSTORED: Final = UUID(int=0)


@dataclass(frozen=True, slots=True)
class SpanSettings:
    """Every length and ratio this module uses. No defaults: `MEASURED_SPAN_SETTINGS` is the one
    measured set."""

    straightness_pt: Decimal
    """How far off its axis a stroke may drift and still be horizontal or vertical."""
    witness_tolerance_pt: Decimal
    """How far from a dimension line's end its witness line may cross it (the overshoot)."""
    crossing_margin_pt: Decimal
    """How far past the dimension line a witness line must reach (a T, not a box's corner)."""
    minimum_span_pt: Decimal
    """A dimension line's stroke is at least this long."""
    same_offset_pt: Decimal
    """Two spans this close across their axis are on one line (a chain)."""
    end_meet_pt: Decimal
    """Two ends this close along the axis are one place: chain links, overall ends, an end on a
    wall band or an outline edge, a span on a sink outline's edge."""
    outline_minimum_pt: Decimal
    """A solid outline edge is a perpendicular stroke at least this long."""
    label_reach_pt: Decimal
    """A label belongs to a span when its middle is within this distance across of the line."""
    band_aspect: Decimal
    """A wall band is at least this many times as long as it is thick..."""
    band_minimum_fraction: Decimal
    """...and runs at least this fraction of the view's extent along its axis."""
    band_clear_fraction: Decimal
    """A band is at the drawing's edge when, on one side of it, no stroke parallel to it and at
    least this fraction of its length is drawn (dimension lines and witness lines aside)."""
    filled_band_lightness: Decimal
    """A filled path at most this light (0 black, 1 white) can be a wall drawn solid."""
    hatch: OutlineSettings
    """The architect reader's hatching settings, reused for the wall hatch."""
    cutout_minimum_pt: Decimal
    cutout_maximum_pt: Decimal
    """A sink outline's sides are within these lengths."""
    corner_reach_pt: Decimal
    """A rounded corner: how far an outline's sides may stop short of its corners."""
    dash_gap_pt: Decimal
    """Collinear pieces this close end to end are one stroke (a dashed outline's side)..."""
    dash_minimum_pt: Decimal
    """...when each is at least this long (a speckle pattern's tiny strokes never join)."""
    drain_maximum_fraction: Decimal
    """A sink's round drain is at most this fraction of its outline's width."""
    drawn_length_tolerance: Fraction
    """The drawn length at the view's scale is within this fraction of the value (design: 1/10)."""
    drawn_length_within_in: Fraction
    """...and within this many inches of it (#1265 review: the measured lengths were exact to a
    fiftieth, and a tenth cannot tell 24" from 23 3/4"). The stricter of the two decides."""
    witness_gap_pt: Decimal
    """How far along a witness line the edge it is drawn from may stop short of it: CAD draws a
    witness line from a small gap off the edge (measured 2.3 pt on a vendor plan)."""
    mm_corroboration_mm: Fraction
    """Millimetres printed with an inch value corroborate it only within this many millimetres of
    25.4 times the inches; further off, the label holds. Millimetres never decide (CLAUDE.md)."""
    same_size_fraction: Decimal
    """A label's characters differ in height by at most this fraction of the tallest..."""
    baseline_drift_em: Decimal
    """...and their middles across the line by at most this many character heights; otherwise
    the label is a stacked fraction (or parts of two labels) and holds."""
    tie_margin_pt: Decimal
    """A label within this distance of being as near one span as another belongs to neither."""
    same_outline_pt: Decimal
    """Two rectangles this close on every side are the same outline drawn twice. Small on purpose: a
    sink's rim and its bowl can be a point or two apart."""
    outline_search_fraction: Decimal
    """The second outline count looks this fraction of the outer outline's shorter side beyond it."""
    drain_minimum_pt: Decimal
    """A sink's drain is at least this wide: a corner dot is not a drain."""


# ---------------------------------------------------------------------------
# Strokes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Stroke:
    """One straight horizontal or vertical stroke, in page points."""

    axis: Axis
    at: Decimal
    """Its y for a horizontal stroke, its x for a vertical one."""
    start: Decimal
    end: Decimal
    """Its extent along its axis, `start <= end`."""
    pieces: int = 1
    """How many collinear pieces were joined into it (more than one: a dashed line)."""

    @property
    def length(self) -> Decimal:
        return self.end - self.start


def _inside(x: Decimal, y: Decimal, box: Box) -> bool:
    return box.x0 <= x <= box.x1 and box.top <= y <= box.bottom


def _legs(ink: PageInk, view: Box) -> Iterable[tuple[Decimal, Decimal, Decimal, Decimal]]:
    """Every straight two-point piece of ink with both ends in `view`."""
    for line in ink.lines:
        if _inside(line.x0, line.y0, view) and _inside(line.x1, line.y1, view):
            yield line.x0, line.y0, line.x1, line.y1
    for curve in ink.curves:
        points = curve.points + ((curve.points[0],) if curve.closed and curve.points else ())
        for (ax, ay), (bx, by) in pairwise(points):
            if _inside(ax, ay, view) and _inside(bx, by, view):
                yield ax, ay, bx, by


def _strokes(ink: PageInk, view: Box, settings: SpanSettings) -> list[_Stroke]:
    found: list[_Stroke] = []
    straight = settings.straightness_pt
    for x0, y0, x1, y1 in _legs(ink, view):
        if abs(y0 - y1) <= straight and x0 != x1:
            found.append(_Stroke(Axis.HORIZONTAL, (y0 + y1) / _TWO, min(x0, x1), max(x0, x1)))
        elif abs(x0 - x1) <= straight and y0 != y1:
            found.append(_Stroke(Axis.VERTICAL, (x0 + x1) / _TWO, min(y0, y1), max(y0, y1)))
    return found


def _joined(strokes: Sequence[_Stroke], settings: SpanSettings) -> list[_Stroke]:
    """Collinear pieces end to end as one stroke each: a dashed line's dashes.

    Only pieces at least `dash_minimum_pt` long join, with gaps up to `dash_gap_pt`: a stone's
    speckle pattern is made of tiny strokes, and joining those would draw lines that are not there.
    """
    joined: list[_Stroke] = []
    for axis in (Axis.HORIZONTAL, Axis.VERTICAL):
        ordered = sorted(
            (
                stroke
                for stroke in strokes
                if stroke.axis is axis and stroke.length >= settings.dash_minimum_pt
            ),
            key=lambda stroke: (stroke.at, stroke.start),
        )
        line: list[_Stroke] = []
        for stroke in ordered:
            if (
                line
                and abs(line[0].at - stroke.at) <= settings.straightness_pt
                and stroke.start - max(piece.end for piece in line) <= settings.dash_gap_pt
            ):
                line.append(stroke)
                continue
            if line:
                joined.append(_line_of(axis, line))
            line = [stroke]
        if line:
            joined.append(_line_of(axis, line))
    return joined


def _line_of(axis: Axis, pieces: Sequence[_Stroke]) -> _Stroke:
    return _Stroke(
        axis,
        pieces[0].at,
        min(piece.start for piece in pieces),
        max(piece.end for piece in pieces),
        pieces=len(pieces),
    )


# ---------------------------------------------------------------------------
# Wall bands
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WallBand:
    """A long, thin hatched or filled band in a view: how a wall is drawn."""

    box: Box
    axis: Axis
    """The way it runs: a horizontal band runs along the view's width."""
    source: Literal["hatching", "filled"]
    at_edge: bool
    """On one side of it no stroke parallel to it (and at least `band_clear_fraction` of its
    length) is drawn: it is the edge of the drawing, as the wall behind a plan's stone is. The
    stone slab of a front view has cabinets on both sides of it."""


def wall_bands(
    ink: PageInk,
    view: Box,
    *,
    dimension_strokes: Sequence[tuple[Decimal, Decimal, Decimal, Decimal]],
    settings: SpanSettings,
) -> tuple[WallBand, ...]:
    """The view's wall bands.

    **Hatching**, found two ways and both kept. (1) The architect reader's `hatched_regions`: a
    family of parallel slanted strokes side by side, as the box round them; it finds a hatch drawn
    in dashes. (2) A wall's hatch is clipped by the band, so in a horizontal band every full stroke
    runs between the same two heights, and in a vertical band between the same two lefts and
    rights: strokes grouped by that extent (and by which way they lean), at least
    `hatch.hatch_minimum_lines` of them each within twice `hatch.hatch_spacing_pt` of the next,
    are a band. The second splits an L-shaped wall (back and side walls hatched as one), whose box
    the first would give as one square-ish area.

    **Fills.** A filled black or grey path at most `filled_band_lightness` light.

    A band must be at least `band_aspect` times as long as thick and run `band_minimum_fraction` of
    the view. `dimension_strokes` (dimension lines and their witness lines, as `(x0, y0, x1, y1)`)
    and the view's own frame (a stroke reaching both sides of the view) are not counted when judging
    whether a band is at the drawing's edge: a plan's width dimensions are drawn beyond its wall.
    """
    strokes = slanted_strokes(ink, view, settings.hatch)
    regions: list[tuple[Box, Literal["hatching", "filled"]]] = [
        (box, "hatching")
        for box in (
            *hatched_regions(strokes, (), view=view, settings=settings.hatch),
            *_hatch_bands(strokes, settings),
        )
    ]
    for curve in ink.curves:
        if (
            curve.fill_lightness is not None
            and curve.fill_lightness <= settings.filled_band_lightness
            and _inside(curve.box.x0, curve.box.top, view)
            and _inside(curve.box.x1, curve.box.bottom, view)
        ):
            regions.append((curve.box, "filled"))
    others = [
        stroke
        for stroke in _strokes(ink, view, settings)
        if not _on_dimension(stroke, dimension_strokes, settings)
        and not _frame(stroke, view, settings)
    ]
    bands: list[WallBand] = []
    for box, source in regions:
        if any(_same_box(box, band.box, settings.end_meet_pt) for band in bands):
            continue  # the same hatch found both ways
        if (
            box.width >= settings.band_aspect * box.height
            and box.width >= settings.band_minimum_fraction * view.width
        ):
            axis = Axis.HORIZONTAL
        elif (
            box.height >= settings.band_aspect * box.width
            and box.height >= settings.band_minimum_fraction * view.height
        ):
            axis = Axis.VERTICAL
        else:
            continue
        bands.append(WallBand(box, axis, source, _at_edge(box, axis, others, settings)))
    return tuple(bands)


def _hatch_bands(strokes: Sequence[Stroke], settings: SpanSettings) -> list[Box]:
    hatch = settings.hatch
    step = settings.end_meet_pt
    families: dict[tuple[str, bool, Decimal, Decimal], list[tuple[Decimal, Box]]] = {}
    for stroke in strokes:
        box = Box(
            min(stroke.x0, stroke.x1),
            min(stroke.y0, stroke.y1),
            max(stroke.x0, stroke.x1),
            max(stroke.y0, stroke.y1),
        )
        rising = (stroke.x1 - stroke.x0) * (stroke.y1 - stroke.y0) < 0
        across_h = (_snap(box.top, step), _snap(box.bottom, step))
        across_v = (_snap(box.x0, step), _snap(box.x1, step))
        families.setdefault(("h", rising, *across_h), []).append((box.centre_x, box))
        families.setdefault(("v", rising, *across_v), []).append((box.centre_y, box))
    bands: list[Box] = []
    for members in families.values():
        members.sort(key=lambda member: member[0])
        run: list[Box] = [members[0][1]]
        previous = members[0][0]
        for position, box in members[1:]:
            if position - previous <= 2 * hatch.hatch_spacing_pt:
                run.append(box)
            else:
                if len(run) >= hatch.hatch_minimum_lines:
                    bands.append(_union(run))
                run = [box]
            previous = position
        if len(run) >= hatch.hatch_minimum_lines:
            bands.append(_union(run))
    return bands


def _snap(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).quantize(Decimal(1)) * step


def _union(boxes: Sequence[Box]) -> Box:
    return Box(
        min(box.x0 for box in boxes),
        min(box.top for box in boxes),
        max(box.x1 for box in boxes),
        max(box.bottom for box in boxes),
    )


def _on_dimension(
    stroke: _Stroke,
    dimension_strokes: Sequence[tuple[Decimal, Decimal, Decimal, Decimal]],
    settings: SpanSettings,
) -> bool:
    """Whether the stroke lies along a dimension line or a witness line (collinear and overlapping)."""
    for x0, y0, x1, y1 in dimension_strokes:
        straight = settings.straightness_pt
        if (
            stroke.axis is Axis.HORIZONTAL
            and y1 - y0 <= straight
            and abs(stroke.at - y0) <= straight
            and stroke.start <= x1
            and x0 <= stroke.end
        ):
            return True
        if (
            stroke.axis is Axis.VERTICAL
            and x1 - x0 <= straight
            and abs(stroke.at - x0) <= straight
            and stroke.start <= y1
            and y0 <= stroke.end
        ):
            return True
    return False


def _frame(stroke: _Stroke, view: Box, settings: SpanSettings) -> bool:
    """Whether the stroke reaches both sides of the view: a panel's frame, not the drawing."""
    low, high = (view.x0, view.x1) if stroke.axis is Axis.HORIZONTAL else (view.top, view.bottom)
    return stroke.start <= low + settings.end_meet_pt and stroke.end >= high - settings.end_meet_pt


def _at_edge(box: Box, axis: Axis, strokes: Sequence[_Stroke], settings: SpanSettings) -> bool:
    if axis is Axis.HORIZONTAL:
        low, high, start, end = box.top, box.bottom, box.x0, box.x1
    else:
        low, high, start, end = box.x0, box.x1, box.top, box.bottom
    reach = settings.band_clear_fraction * (end - start)
    before = after = False
    for stroke in strokes:
        if stroke.axis is not axis:
            continue
        overlap = min(stroke.end, end) - max(stroke.start, start)
        if overlap < reach:
            continue
        if stroke.at < low - settings.end_meet_pt:
            before = True
        elif stroke.at > high + settings.end_meet_pt:
            after = True
    return not (before and after)


type _Quad = tuple[Decimal, Decimal, Decimal, Decimal]


def _key(x0: Decimal, y0: Decimal, x1: Decimal, y1: Decimal) -> _Quad:
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def _stroke_key(stroke: _Stroke) -> _Quad:
    if stroke.axis is Axis.HORIZONTAL:
        return (stroke.start, stroke.at, stroke.end, stroke.at)
    return (stroke.at, stroke.start, stroke.at, stroke.end)


# ---------------------------------------------------------------------------
# Spans
# ---------------------------------------------------------------------------


class EndKind(StrEnum):
    """What a span's end meets in the drawing."""

    WALL_BAND = "wall band"
    OUTLINE = "outline edge"
    NOTHING = "nothing found"


@dataclass(frozen=True, slots=True)
class SpanEnd:
    at_pt: Decimal
    kind: EndKind
    reason: str


@dataclass(frozen=True, slots=True)
class Span:
    """One dimension span in a view, on either axis."""

    number: int
    """View-local, from 1: horizontal spans first, each axis by offset then start. The number the
    numbered-spans picture prints."""
    axis: Axis
    offset_pt: Decimal
    """Where the dimension line sits across its axis: its y if horizontal, its x if vertical."""
    start_pt: Decimal
    end_pt: Decimal
    """Where its witness lines cross it, along its axis, `start_pt < end_pt`: the distance the
    printed number states."""
    stroke_ends: tuple[Decimal, Decimal]
    """Where the dimension line's own stroke stops (it often overshoots its witness lines)."""
    witnesses: tuple[tuple[Decimal, Decimal, Decimal, Decimal], ...]
    """Its witness lines as `(x0, y0, x1, y1)`."""
    low_end: SpanEnd
    high_end: SpanEnd

    @property
    def length_pt(self) -> Decimal:
        """The drawn length between the witness lines, exact."""
        return self.end_pt - self.start_pt

    def point(self, along: Decimal) -> tuple[Decimal, Decimal]:
        """The page point on the dimension line at `along`."""
        if self.axis is Axis.HORIZONTAL:
            return along, self.offset_pt
        return self.offset_pt, along


@dataclass(frozen=True, slots=True)
class SpanChain:
    """Two or more spans drawn end to end on one line."""

    axis: Axis
    parts: tuple[Span, ...]


@dataclass(frozen=True, slots=True)
class ChainClosure:
    """A chain, and an overall span on the same axis covering exactly the same extent."""

    overall: Span
    parts: tuple[Span, ...]


@dataclass(frozen=True, slots=True)
class ViewSpans:
    """Everything this module found in one view."""

    spans: tuple[Span, ...]
    bands: tuple[WallBand, ...]
    chains: tuple[SpanChain, ...]
    closures: tuple[ChainClosure, ...]
    unclassified: int
    """How many distinct straight strokes were not a dimension line or a witness line: most of the
    drawing, and where a dimension the detector missed would be."""


def _stored(x: Decimal, y: Decimal) -> StoredPoint:
    return StoredPoint(x / _TO_STORED, y / _TO_STORED)


def _page(value: Decimal) -> Decimal:
    return value * _TO_STORED


def find_spans(ink: PageInk, view: Box, settings: SpanSettings) -> ViewSpans:
    """Every dimension span in `view` on both axes, what each end meets, the wall bands, chains and
    closures. `ink` is the page's black and grey ink (pdfplumber's frame)."""
    segments: list[DimensionExtent] = []
    strokes = _strokes(ink, view, settings)
    off_page = 0
    for stroke in strokes:
        if stroke.at < _ZERO or stroke.start < _ZERO:
            off_page += 1  # left of or above the page's origin: counted as unclassified below
            continue
        if stroke.axis is Axis.HORIZONTAL:
            start, end = _stored(stroke.start, stroke.at), _stored(stroke.end, stroke.at)
        else:
            start, end = _stored(stroke.at, stroke.start), _stored(stroke.at, stroke.end)
        segments.append(DimensionExtent(start, end, _UNSTORED, 0))
    detected = detect(
        segments,
        witness_tolerance=settings.witness_tolerance_pt / _TO_STORED,
        minimum_span=settings.minimum_span_pt / _TO_STORED,
        straightness=settings.straightness_pt / _TO_STORED,
        crossing_margin=settings.crossing_margin_pt / _TO_STORED,
    )
    every = [_raw(line) for line in detected.lines]
    raw = _deduplicated(every, settings)
    dimension_strokes = [stroke for found in every for stroke in (found.stroke, *found.witnesses)]
    bands = wall_bands(ink, view, dimension_strokes=dimension_strokes, settings=settings)
    # The span's own witness lines and the dimension lines themselves are not an edge of the
    # drawing; a stroke collinear with a witness line can be (a witness is drawn from the edge).
    # A "witness" that runs well past its dimension line on both sides is a drawing edge the line
    # happens to cross (#1265 review: a stone's front edge crossed by another dimension), and
    # stays an edge.
    own = {
        _key(*stroke)
        for found in every
        for stroke in (found.stroke, *found.witnesses)
        if not _crossed_edge(found, stroke, settings)
    }
    others = [
        stroke
        for stroke in strokes
        if stroke.length >= settings.outline_minimum_pt and _stroke_key(stroke) not in own
    ]
    ordered = sorted(
        raw,
        key=lambda found: (found.axis is Axis.VERTICAL, found.offset, found.start, found.end),
    )
    spans = tuple(
        Span(
            number=number,
            axis=found.axis,
            offset_pt=found.offset,
            start_pt=found.start,
            end_pt=found.end,
            stroke_ends=found.stroke_ends,
            witnesses=found.witnesses,
            low_end=_end(found.axis, found.start, found.witnesses, bands, others, settings),
            high_end=_end(found.axis, found.end, found.witnesses, bands, others, settings),
        )
        for number, found in enumerate(ordered, start=1)
    )
    spans = tuple(_side_edge_corners(span, others, settings) for span in spans)
    chains = _chains(spans, settings)
    return ViewSpans(
        spans=spans,
        bands=bands,
        chains=chains,
        closures=_closures(spans, chains, settings),
        unclassified=len(detected.unclassified) + off_page,
    )


@dataclass(frozen=True, slots=True)
class _Raw:
    axis: Axis
    offset: Decimal
    start: Decimal
    end: Decimal
    stroke_ends: tuple[Decimal, Decimal]
    stroke: tuple[Decimal, Decimal, Decimal, Decimal]
    witnesses: tuple[tuple[Decimal, Decimal, Decimal, Decimal], ...]


def _crossed_edge(found: _Raw, stroke: _Quad, settings: SpanSettings) -> bool:
    """Whether one of a dimension line's witnesses runs more than twice `witness_tolerance_pt` past
    the line on both sides: an edge the line crosses, not a witness drawn to it."""
    if stroke == found.stroke:
        return False
    x0, y0, x1, y1 = stroke
    low, high = (y0, y1) if found.axis is Axis.HORIZONTAL else (x0, x1)
    margin = 2 * settings.witness_tolerance_pt
    return found.offset - low > margin and high - found.offset > margin


def _raw(line: DimensionLine) -> _Raw:
    """A detected dimension line in page points, its ends moved to where its witnesses cross it."""
    start, end = line.extent.start, line.extent.end
    if line.axis is Axis.HORIZONTAL:
        offset = _page(start.y)
        low, high = sorted((_page(start.x), _page(end.x)))
        crossings = [_page(witness.start.x) for witness in line.witness_lines]
    else:
        offset = _page(start.x)
        low, high = sorted((_page(start.y), _page(end.y)))
        crossings = [_page(witness.start.y) for witness in line.witness_lines]
    near_low = min(crossings, key=lambda at: (abs(at - low), at))
    near_high = min(crossings, key=lambda at: (abs(at - high), -at))
    if near_low >= near_high:
        near_low, near_high = low, high
    return _Raw(
        axis=line.axis,
        offset=offset,
        start=near_low,
        end=near_high,
        stroke_ends=(low, high),
        stroke=_key(_page(start.x), _page(start.y), _page(end.x), _page(end.y)),
        witnesses=tuple(
            _key(
                _page(witness.start.x),
                _page(witness.start.y),
                _page(witness.end.x),
                _page(witness.end.y),
            )
            for witness in line.witness_lines
        ),
    )


def _deduplicated(found: Sequence[_Raw], settings: SpanSettings) -> list[_Raw]:
    """One span per dimension: the same distance on the same line, drawn as two strokes, is one."""
    kept: list[_Raw] = []
    for candidate in found:
        if any(
            other.axis is candidate.axis
            and abs(other.offset - candidate.offset) <= settings.same_offset_pt
            and abs(other.start - candidate.start) <= settings.end_meet_pt
            and abs(other.end - candidate.end) <= settings.end_meet_pt
            for other in kept
        ):
            continue
        kept.append(candidate)
    return kept


def _end(
    axis: Axis,
    at: Decimal,
    witnesses: Sequence[_Quad],
    bands: Sequence[WallBand],
    others: Sequence[_Stroke],
    settings: SpanSettings,
) -> SpanEnd:
    """What the span's end at `at` (along its axis) meets.

    Only what **reaches the end's own witness line** counts: a band edge or a solid edge on the same
    line as the end (within `end_meet_pt`) whose extent across comes within `witness_gap_pt` of the
    witness line's (the gap CAD leaves between an edge and its witness line). An edge on the same
    line far along it measures something else.
    """
    reach = settings.end_meet_pt
    if axis is Axis.VERTICAL:
        own = [(x0, x1) for x0, y0, x1, y1 in witnesses if abs(y0 - at) <= reach and y0 == y1]
    else:
        own = [(y0, y1) for x0, y0, x1, y1 in witnesses if abs(x0 - at) <= reach and x0 == x1]
    if not own:
        return SpanEnd(at, EndKind.NOTHING, "no witness line found at this end")

    gap = settings.witness_gap_pt

    def touches(low: Decimal, high: Decimal) -> bool:
        return any(low <= w_high + gap and high >= w_low - gap for w_low, w_high in own)

    for band in bands:
        if band.axis is axis:
            continue
        if band.axis is Axis.HORIZONTAL:
            edges, extent = (band.box.top, band.box.bottom), (band.box.x0, band.box.x1)
        else:
            edges, extent = (band.box.x0, band.box.x1), (band.box.top, band.box.bottom)
        if any(abs(edge - at) <= reach for edge in edges) and touches(*extent):
            return SpanEnd(
                at, EndKind.WALL_BAND, f"the end lies on the edge of a {band.source} wall band"
            )
    for stroke in others:
        if (
            stroke.axis is not axis
            and abs(stroke.at - at) <= reach
            and touches(stroke.start, stroke.end)
        ):
            return SpanEnd(
                at,
                EndKind.OUTLINE,
                "the end lies on a solid edge of the drawing that reaches its witness line "
                f"({stroke.length:.1f} pt long, not a dimension's own line)",
            )
    return SpanEnd(
        at,
        EndKind.NOTHING,
        "no wall band and no solid edge of the drawing reaches the end's witness line",
    )


def _side_edge_corners(span: Span, others: Sequence[_Stroke], settings: SpanSettings) -> Span:
    """An end that met nothing lies on the outline when a solid edge **parallel** to the span runs
    exactly the span's whole extent (both ends within `end_meet_pt`): the stone's side edge, drawn
    from the wall to the front, whose corner is this end (#1265 review: a plan whose front edge is
    not drawn across, only its side edge down to the front corner)."""
    if EndKind.NOTHING not in (span.low_end.kind, span.high_end.kind):
        return span
    reach = settings.end_meet_pt
    side = next(
        (
            stroke
            for stroke in others
            if stroke.axis is span.axis
            and abs(stroke.start - span.start_pt) <= reach
            and abs(stroke.end - span.end_pt) <= reach
        ),
        None,
    )
    if side is None:
        return span
    reason = (
        "the end is a corner of the outline: a solid edge parallel to the span runs its whole "
        f"length ({side.length:.1f} pt), from one end to the other"
    )

    def corner(end: SpanEnd) -> SpanEnd:
        return (
            end if end.kind is not EndKind.NOTHING else SpanEnd(end.at_pt, EndKind.OUTLINE, reason)
        )

    return replace(span, low_end=corner(span.low_end), high_end=corner(span.high_end))


def _chains(spans: Sequence[Span], settings: SpanSettings) -> tuple[SpanChain, ...]:
    chains: list[list[Span]] = []
    for span in sorted(spans, key=lambda item: (item.axis, item.offset_pt, item.start_pt)):
        for chain in chains:
            last = chain[-1]
            if (
                last.axis is span.axis
                and abs(last.offset_pt - span.offset_pt) <= settings.same_offset_pt
                and abs(last.end_pt - span.start_pt) <= settings.end_meet_pt
            ):
                chain.append(span)
                break
        else:
            chains.append([span])
    return tuple(SpanChain(chain[0].axis, tuple(chain)) for chain in chains if len(chain) > 1)


def _closures(
    spans: Sequence[Span], chains: Sequence[SpanChain], settings: SpanSettings
) -> tuple[ChainClosure, ...]:
    found: list[ChainClosure] = []
    for chain in chains:
        first, last = chain.parts[0], chain.parts[-1]
        for span in spans:
            if (
                span.axis is chain.axis
                and span not in chain.parts
                and abs(span.start_pt - first.start_pt) <= settings.end_meet_pt
                and abs(span.end_pt - last.end_pt) <= settings.end_meet_pt
            ):
                found.append(ChainClosure(span, chain.parts))
    return tuple(found)


# ---------------------------------------------------------------------------
# Where a front offset starts (CT007)
# ---------------------------------------------------------------------------


class OffsetStart(StrEnum):
    """Where a span in front of a sink starts, as code can see it."""

    STONE_EDGE = "stone edge"
    """It ends where the depth ends at the stone's front edge: it measures from the stone's edge,
    which includes the overhang, so it is never the front offset (the client's own depth formula)."""
    INSIDE = "inside the stone"
    """It stops short of the stone's front edge: it may start at the cabinet face; the readers say."""
    UNKNOWN = "unknown"
    """Code cannot tell where the stone's front edge is, or the span is not along the depth."""


@dataclass(frozen=True, slots=True)
class FrontOffsetStart:
    kind: OffsetStart
    reason: str

    @property
    def code_allows(self) -> bool:
        """Only a span code sees stopping short of the stone's front edge; an unknown start holds."""
        return self.kind is OffsetStart.INSIDE


def front_offset_start(span: Span, depth: Span, settings: SpanSettings) -> FrontOffsetStart:
    """Where a candidate front-offset span starts, from the depth span of the same view.

    The depth runs from the wall band to the stone's front edge, so its front edge is the end that
    does **not** meet the wall band. A span on the same axis that ends there (within `end_meet_pt`)
    starts at the stone's edge and is never CT007 (Decision log 2026-10-14: held for a person, never
    adjusted). With no wall band at exactly one end of the depth, the front edge is unknown and the
    span holds.
    """
    if span.axis is not depth.axis:
        return FrontOffsetStart(OffsetStart.UNKNOWN, "the span does not run along the depth")
    low_wall = depth.low_end.kind is EndKind.WALL_BAND
    high_wall = depth.high_end.kind is EndKind.WALL_BAND
    if low_wall == high_wall:
        return FrontOffsetStart(
            OffsetStart.UNKNOWN,
            "the depth does not run from a wall band at exactly one end, so the stone's front "
            "edge is not known: held",
        )
    front = depth.end_pt if low_wall else depth.start_pt
    back = depth.start_pt if low_wall else depth.end_pt
    reach = settings.end_meet_pt
    if abs(span.start_pt - front) <= reach or abs(span.end_pt - front) <= reach:
        return FrontOffsetStart(
            OffsetStart.STONE_EDGE,
            "the span ends on the stone's front edge, where the depth ends: it measures from the "
            "stone's edge (overhang included), never the front offset; held for a person",
        )
    low, high = min(front, back), max(front, back)
    if low < span.start_pt and span.end_pt < high:
        return FrontOffsetStart(
            OffsetStart.INSIDE,
            "the span stops short of the stone's front edge; whether it starts at the cabinet "
            "face is the readers' to say",
        )
    return FrontOffsetStart(OffsetStart.UNKNOWN, "the span is not inside the depth's extent: held")


# ---------------------------------------------------------------------------
# Exact checks: the view's scale, the drawn length, the chain's sum
# ---------------------------------------------------------------------------


def overall_horizontal(spans: Sequence[Span]) -> Span | None:
    """The view's horizontal overall: its longest horizontal span, or `None` without one.

    Several spans drawn over the same extent are the same overall; the first by number is
    returned. Which overall it is and its printed value are the caller's (a sealed reading, or the
    text lane): this only finds the candidate.
    """
    horizontal = [span for span in spans if span.axis is Axis.HORIZONTAL]
    if not horizontal:
        return None
    return max(horizontal, key=lambda span: (span.length_pt, -span.number))


@dataclass(frozen=True, slots=True)
class ViewScale:
    """How many page points one real inch takes in this view, exactly."""

    overall_number: int
    points_per_inch: Fraction


def view_scale(overall: Span, printed_inches: Fraction) -> ViewScale:
    """The view's scale from its horizontal overall's drawn length and printed value."""
    if printed_inches <= 0:
        raise ValueError("an overall's printed value must be greater than zero")
    if overall.axis is not Axis.HORIZONTAL:
        raise ValueError("the view's scale is taken from its horizontal overall")
    return ViewScale(overall.number, Fraction(overall.length_pt) / printed_inches)


@dataclass(frozen=True, slots=True)
class DrawnLength:
    """Whether a span's drawn length agrees with a value, at the view's own scale."""

    span_number: int
    drawn_inches: Fraction
    value_inches: Fraction
    agrees: bool
    reason: str


def drawn_length_check(
    span: Span,
    value_inches: Fraction,
    scale: ViewScale,
    *,
    tolerance: Fraction,
    within_inches: Fraction | None = None,
) -> DrawnLength:
    """The span's drawn length at `scale`, against `value_inches`.

    **The decision, exactly:** `|drawn − value| ≤ tolerance × value`, on `Fraction`s, inclusive at
    the boundary; with `within_inches`, also `|drawn − value| ≤ within_inches` (the stricter
    decides). The design's tolerance is 1/10: a scale taken from one overall and a drawing
    traced by hand are not exact, and the check is there to catch a reading of the wrong span or a
    misread digit (24 read as 34 is 42% off), not to grade the drafting.
    """
    if value_inches <= 0:
        raise ValueError("a value checked against a drawn length must be greater than zero")
    drawn = Fraction(span.length_pt) / scale.points_per_inch
    allowed = tolerance * value_inches
    if within_inches is not None:
        allowed = min(allowed, within_inches)
    agrees = abs(drawn - value_inches) <= allowed
    reason = (
        f"span {span.number} is drawn {float(drawn):.2f} in long at the view's scale, "
        + ("within" if agrees else "outside")
        + f" {allowed} in of the value"
    )
    return DrawnLength(span.number, drawn, value_inches, agrees, reason)


@dataclass(frozen=True, slots=True)
class ClosureCheck:
    """Whether a chain's printed parts sum exactly to its printed overall."""

    closes: bool | None
    """`None` when a value is not known."""
    parts_sum: Fraction | None
    overall: Fraction | None
    reason: str


def closure_check(closure: ChainClosure, values: Mapping[int, Fraction]) -> ClosureCheck:
    """Sum the parts' values (by span number) and compare with the overall's, exactly."""
    numbers = [part.number for part in closure.parts]
    missing = [number for number in (*numbers, closure.overall.number) if number not in values]
    if missing:
        return ClosureCheck(
            None,
            None,
            None,
            "not every value is known (span " + ", ".join(str(n) for n in missing) + ")",
        )
    total = sum((values[number] for number in numbers), Fraction(0))
    overall = values[closure.overall.number]
    closes = total == overall
    return ClosureCheck(
        closes,
        total,
        overall,
        "the parts sum exactly to the overall" if closes else "the parts do not sum to the overall",
    )


# ---------------------------------------------------------------------------
# The vector-text lane
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DepthLabel:
    """What one printed label says in inches, or why it is held."""

    text: str
    inches: Fraction | None
    held_reason: str | None
    bracketed: bool
    """The inch value came from brackets (`635 [25]`); millimetres beside it only corroborate."""


_BRACKETED: Final = re.compile(r"\[([^\[\]]*)\]")
_BARE_NUMBER: Final = re.compile(r"(\d+(?:\.\d+)?)\s*(?:mm|MM)?")
_MM_PER_INCH: Final = Fraction(254, 10)
MM_ONLY: Final = (
    "only millimetres (or a number with no unit) are printed: inches are authoritative and "
    "millimetres are never used, so it is held"
)


def _corroborates(millimetres: str, inches: Fraction, tolerance: Fraction) -> bool:
    """Whether a printed millimetre number is the inch value's, within `tolerance` mm, exactly."""
    found = _BARE_NUMBER.fullmatch(millimetres.strip())
    if found is None:
        return False
    return abs(Fraction(found.group(1)) - _MM_PER_INCH * inches) <= tolerance


def read_depth_label(text: str, *, mm_tolerance: Fraction) -> DepthLabel:
    """A vendor's dimension label in inches.

    * `635 [25]`, `452 [17 13/16]`: the bracketed value is the inches. The millimetres outside may
      only corroborate it: they must be within `mm_tolerance` mm of 25.4 times the inches, or the
      label holds (`25 [635]` is a swapped or misread pair, never 635 inches).
    * `24"`, `21 5/8"`, `2'-0"`: read exactly (`architect.labels.read_label`, which also holds a
      fraction a drawing never prints, such as `163/8"`).
    * `635`, `635 mm`: millimetres only, or no unit: held.
    * Two bracketed values, or other words with the number: held.
    """
    plain = plain_marks(text.strip())
    brackets = _BRACKETED.findall(plain)
    outside = _BRACKETED.sub(" ", plain).strip()
    if len(brackets) > 1:
        return DepthLabel(text, None, "more than one bracketed value is printed in one label", True)
    if brackets:
        if outside and not _BARE_NUMBER.fullmatch(outside):
            return DepthLabel(
                text, None, f"other words are printed with the value ({outside})", True
            )
        inner = brackets[0].strip()
        reading = read_label(inner if '"' in inner or "'" in inner else f'{inner}"')
        if (
            reading.inches is not None
            and outside
            and not _corroborates(outside, reading.inches, mm_tolerance)
        ):
            return DepthLabel(
                text,
                None,
                f"the millimetres printed ({outside}) are not the inches' ({brackets[0]}): "
                "the pair does not corroborate, so it is held",
                True,
            )
        return DepthLabel(text, reading.inches, reading.held_reason, True)
    if _BARE_NUMBER.fullmatch(plain):
        return DepthLabel(text, None, MM_ONLY, False)
    reading = read_label(plain)
    return DepthLabel(text, reading.inches, reading.held_reason, False)


@dataclass(frozen=True, slots=True)
class TextLane:
    """The vector text printed at one span, as a value in inches, or why there is none."""

    span_number: int
    printed: tuple[str, ...]
    inches: Fraction | None
    reason: str
    boxes: tuple[Box, ...] = field(default=())
    """Where the span's own label is printed (its phrases): the reviewer-ink check looks here."""


def _reads_along(phrase: PrintedPhrase, axis: Axis) -> bool:
    if axis is Axis.HORIZONTAL:
        return phrase.orientation is Orientation.UPRIGHT
    return phrase.orientation in (Orientation.UP, Orientation.DOWN)


def text_lanes(
    spans: Sequence[Span],
    phrases: Sequence[PrintedPhrase],
    settings: SpanSettings,
    *,
    chars: Sequence[TextChar],
) -> dict[int, TextLane]:
    """Each span's vector-text value.

    A phrase belongs to the span it reads along (upright text for a horizontal span, sideways text
    for a vertical one) whose extent holds the phrase's middle and whose line is nearest across,
    within `label_reach_pt`. A phrase within `tie_margin_pt` of being as near another span belongs
    to neither, and both say so. A phrase with no digit is not a label. `chars` are the characters
    the phrases were built from: a label whose characters are not one size on one line is a stacked
    fraction and holds (`_lane`).
    """
    owned: dict[int, list[PrintedPhrase]] = {span.number: [] for span in spans}
    shared: dict[int, list[str]] = {span.number: [] for span in spans}
    for phrase in phrases:
        if not any(character.isdigit() for character in phrase.text):
            continue
        x, y = phrase.box.centre_x, phrase.box.centre_y
        near: list[tuple[Decimal, Span]] = []
        for span in spans:
            if not _reads_along(phrase, span.axis):
                continue
            along, across = (x, y) if span.axis is Axis.HORIZONTAL else (y, x)
            if not span.start_pt <= along <= span.end_pt:
                continue
            distance = abs(across - span.offset_pt)
            if distance <= settings.label_reach_pt:
                near.append((distance, span))
        if not near:
            continue
        near.sort(key=lambda item: (item[0], item[1].number))
        if len(near) > 1 and near[1][0] - near[0][0] <= settings.tie_margin_pt:
            for distance, span in near:
                if distance - near[0][0] <= settings.tie_margin_pt:
                    shared[span.number].append(phrase.text)
            continue
        owned[near[0][1].number].append(phrase)
    lanes: dict[int, TextLane] = {}
    for span in spans:
        kept, ignored = _others_pairs(owned[span.number], phrases, settings)
        lane = _lane(span.number, kept, shared[span.number], chars, settings)
        if ignored and lane.inches is not None:
            lane = replace(
                lane,
                reason=lane.reason
                + " (not counted: "
                + ", ".join(repr(text) for text in ignored)
                + ", each printed with its own bracketed inches: another label)",
            )
        lanes[span.number] = replace(lane, boxes=tuple(phrase.box for phrase in kept))
    return lanes


def _along_across(phrase: PrintedPhrase) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """The phrase's extent along its own reading direction, and across it."""
    box = phrase.box
    if phrase.orientation is Orientation.UPRIGHT:
        return box.x0, box.x1, box.top, box.bottom
    return box.top, box.bottom, box.x0, box.x1


def _own_bracket(number: PrintedPhrase, phrases: Sequence[PrintedPhrase]) -> PrintedPhrase | None:
    """The bracketed inches printed as this millimetre number's own pair: right under or over it
    (overlapping it along, within one character height across), or right after it on its line."""
    low, high, top, bottom = _along_across(number)
    height = number.height
    for other in phrases:
        if other is number or other.orientation is not number.orientation:
            continue
        if not _BRACKETED.fullmatch(plain_marks(other.text.strip())):
            continue
        o_low, o_high, o_top, o_bottom = _along_across(other)
        overlap = min(high, o_high) - max(low, o_low)
        # Right under or over it: the two boxes may touch or overlap a little across (half a
        # character), never more.
        stacked = overlap * 2 >= min(high - low, o_high - o_low) and (
            -height / 2 <= o_top - bottom <= height or -height / 2 <= top - o_bottom <= height
        )
        inline = abs((o_top + o_bottom) / 2 - (top + bottom) / 2) * 2 <= height and Decimal(
            0
        ) <= o_low - high <= height * Decimal("1.5")
        if stacked or inline:
            return other
    return None


def _others_pairs(
    owned: Sequence[PrintedPhrase], phrases: Sequence[PrintedPhrase], settings: SpanSettings
) -> tuple[list[PrintedPhrase], list[str]]:
    """The span's own phrases, less every millimetre number printed as a pair with a bracketed
    inch value this span does not own, when the two corroborate (`20` over `[3/4]`): that pair is
    another label whose number reached this span (#1265 review). A number whose pair does not
    corroborate, or that has no pair, stays, and holds the lane as before."""
    kept: list[PrintedPhrase] = []
    ignored: list[str] = []
    for phrase in owned:
        label = read_depth_label(phrase.text, mm_tolerance=settings.mm_corroboration_mm)
        if label.held_reason == MM_ONLY:
            bracket = _own_bracket(phrase, phrases)
            if bracket is not None and bracket not in owned:
                pair = read_depth_label(
                    f"{phrase.text} {bracket.text}", mm_tolerance=settings.mm_corroboration_mm
                )
                if pair.inches is not None:
                    ignored.append(phrase.text)
                    continue
        kept.append(phrase)
    return kept, ignored


def _stacked(phrase: PrintedPhrase, chars: Sequence[TextChar], settings: SpanSettings) -> bool:
    """Whether the phrase's characters differ in size, or in where they sit across the line: the
    pieces of a stacked fraction read into one run (`24 3/4` printed stacked reads as `244`)."""
    box = phrase.box
    inside = [
        char
        for char in chars
        if box.x0 <= char.box.centre_x <= box.x1 and box.top <= char.box.centre_y <= box.bottom
    ]
    if len(inside) < 2:
        return False
    upright = phrase.orientation is Orientation.UPRIGHT
    sizes = [char.box.height if upright else char.box.width for char in inside]
    middles = [char.box.centre_y if upright else char.box.centre_x for char in inside]
    tallest = max(sizes)
    return (
        tallest - min(sizes) > settings.same_size_fraction * tallest
        or max(middles) - min(middles) > settings.baseline_drift_em * tallest
    )


def _lane(
    number: int,
    phrases: Sequence[PrintedPhrase],
    shared: Sequence[str],
    chars: Sequence[TextChar],
    settings: SpanSettings,
) -> TextLane:
    """One span's value from the phrases it owns. **Every doubt holds**: another label near enough
    to share, characters of two sizes (a stacked fraction), two inch values, a number beside the
    inch value that is not its millimetres (a stacked fraction's lone digit), or a label that does
    not read."""
    printed = tuple(phrase.text for phrase in phrases)
    if shared:
        return TextLane(
            number,
            printed + tuple(shared),
            None,
            "a label sits (almost) equally near this span and another ("
            + ", ".join(repr(text) for text in shared)
            + "): it belongs to neither",
        )
    if not phrases:
        return TextLane(
            number,
            (),
            None,
            "no text is printed at this span (where the numbers are pen strokes, the drawn length "
            "at the view's scale is the code check)",
        )
    stacked = [phrase.text for phrase in phrases if _stacked(phrase, chars, settings)]
    if stacked:
        return TextLane(
            number,
            printed,
            None,
            f"the label {stacked[0]!r} is printed in characters of different sizes or heights: a "
            "stacked fraction, held",
        )
    labels = [
        read_depth_label(phrase.text, mm_tolerance=settings.mm_corroboration_mm)
        for phrase in phrases
    ]
    values = {label.inches for label in labels if label.inches is not None}
    bare = [label.text for label in labels if label.held_reason == MM_ONLY]
    other_holds = [
        label.held_reason
        for label in labels
        if label.inches is None and label.held_reason not in (None, MM_ONLY)
    ]
    if other_holds:
        return TextLane(
            number, printed, None, f"a label at this span cannot be read: {other_holds[0]}"
        )
    if len(values) > 1:
        return TextLane(number, printed, None, "two different inch values are printed at this span")
    if not values:
        return TextLane(number, printed, None, MM_ONLY)
    (value,) = values
    strays = [text for text in bare if not _corroborates(text, value, settings.mm_corroboration_mm)]
    if strays:
        return TextLane(
            number,
            printed,
            None,
            f"the number {strays[0]!r} beside the inch value is not its millimetres (a stacked "
            "fraction's digit, or another label): held",
        )
    return TextLane(number, printed, value, "the vector text at this span reads exactly")


def text_lane_agrees(lane: TextLane, sealed_inches: Fraction) -> bool | None:
    """Whether the vector text equals the two readers' sealed reading **exactly**; `None` when the
    span has no readable vector text (the drawn length is then the code check)."""
    if lane.inches is None:
        return None
    return lane.inches == sealed_inches


# ---------------------------------------------------------------------------
# Sink outlines
# ---------------------------------------------------------------------------

#: Words that name the cut-out itself (design §4; Decision log 2026-10-14).
CUTOUT_WORDS: Final = re.compile(r"\bOPENING\b|\bCUT\s*-?\s*OUTS?\b", re.IGNORECASE)
#: Words that say a rectangle is a sink, not a cabinet or an appliance.
_SINK_WORDS: Final = re.compile(r"\bSINK\b|\bBOWL\b|\bBASIN\b|\bLAV(?:ATORY)?\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Outline:
    """One closed rectangle around a sink, and the letter its picture prints."""

    letter: str
    box: Box
    drawn_as: Literal["closed path", "four strokes", "dashed strokes"]


@dataclass(frozen=True, slots=True)
class SinkOutlines:
    """The rectangles drawn around one sink, outer first."""

    outlines: tuple[Outline, ...]
    marker: str
    """Why these are a sink's: its words or its drain inside."""
    crossings: tuple[int, int, int, int]
    """The second, shape-independent count: how many separate lines are crossed going left, right,
    up and down from the sink's mark, out to `outline_search_fraction` of the outer outline's
    shorter side beyond it. One outline drawn gives one crossing each way, whatever its corners."""
    shares_outline: bool
    """Another mark's group has an outline in common with this one: one sink seen twice, with
    possibly different counts. Such a sink holds as a whole."""


@dataclass(frozen=True, slots=True)
class CutoutJudgment:
    """Code's half of the cut-out rule for one sink: one judgment, never a decision."""

    code_allows: bool
    naming_words: tuple[str, ...]
    reason: str


def _closed_rectangle(curve: InkCurve, settings: SpanSettings) -> bool:
    """One closed path drawn as a rectangle, its corners possibly rounded: every point lies near
    the path's box border, within `corner_reach_pt` and a tenth of its shorter side (a circle's
    points at 45 degrees lie 0.15 of its width inside, so a circle is never a rectangle)."""
    points = curve.points
    if len(points) < 4:
        return False
    closed = curve.closed or (
        abs(points[0][0] - points[-1][0]) <= settings.straightness_pt
        and abs(points[0][1] - points[-1][1]) <= settings.straightness_pt
    )
    if not closed:
        return False
    box = curve.box
    reach = min(settings.corner_reach_pt, min(box.width, box.height) / 10)
    return all(
        min(abs(x - box.x0), abs(x - box.x1), abs(y - box.top), abs(y - box.bottom)) <= reach
        for x, y in points
    )


def _sized(box: Box, settings: SpanSettings) -> bool:
    return all(
        settings.cutout_minimum_pt <= side <= settings.cutout_maximum_pt
        for side in (box.width, box.height)
    )


type _DrawnAs = Literal["closed path", "four strokes", "dashed strokes"]


def _rectangles_round(
    ink: PageInk, view: Box, x: Decimal, y: Decimal, settings: SpanSettings
) -> list[tuple[Box, _DrawnAs]]:
    """Every rectangle drawn as one in `view` that holds the point `(x, y)`.

    One closed path drawn as a rectangle; or four strokes, one per side, each reaching from one of
    its side's corners to the other within `corner_reach_pt` (a rounded corner's gap). A side is
    looked for among the strokes as drawn and among collinear pieces joined end to end, so a dashed
    outline counts too. **Limitation**: an outline whose sides run on past its corners into other
    lines is not seen as a rectangle, so this can count fewer outlines than are drawn; the
    "one outline" rule is code's half only and the readers are shown the drawing.
    """
    found: list[tuple[Box, _DrawnAs]] = []
    for curve in ink.curves:
        box = curve.box
        if (
            _inside(box.x0, box.top, view)
            and _inside(box.x1, box.bottom, view)
            and box.x0 < x < box.x1
            and box.top < y < box.bottom
            and _sized(box, settings)
            and _closed_rectangle(curve, settings)
        ):
            found.append((box, "closed path"))
    reach = settings.corner_reach_pt
    raw = _strokes(ink, view, settings)
    candidates = [*raw, *(stroke for stroke in _joined(raw, settings) if stroke.pieces > 1)]
    above = [
        s for s in candidates if s.axis is Axis.HORIZONTAL and s.at < y and s.start < x < s.end
    ]
    below = [
        s for s in candidates if s.axis is Axis.HORIZONTAL and s.at > y and s.start < x < s.end
    ]
    left = [s for s in candidates if s.axis is Axis.VERTICAL and s.at < x and s.start < y < s.end]
    right = [s for s in candidates if s.axis is Axis.VERTICAL and s.at > x and s.start < y < s.end]

    def side(stroke: _Stroke, low: Decimal, high: Decimal) -> bool:
        return abs(stroke.start - low) <= reach and abs(stroke.end - high) <= reach

    for top in above:
        for bottom in below:
            if not (
                abs(top.start - bottom.start) <= 2 * reach
                and abs(top.end - bottom.end) <= 2 * reach
            ):
                continue
            height = bottom.at - top.at
            if not settings.cutout_minimum_pt <= height <= settings.cutout_maximum_pt:
                continue
            for west in left:
                if not (side(west, top.at, bottom.at) and abs(west.at - top.start) <= reach):
                    continue
                for east in right:
                    if not (side(east, top.at, bottom.at) and abs(east.at - top.end) <= reach):
                        continue
                    if not _corners_agree(top, bottom, west, east, settings):
                        continue
                    box = Box(west.at, top.at, east.at, bottom.at)
                    if not _sized(box, settings):
                        continue
                    dashed = any(stroke.pieces > 1 for stroke in (top, bottom, west, east))
                    found.append((box, "dashed strokes" if dashed else "four strokes"))
    kept: list[tuple[Box, _DrawnAs]] = []
    for box, drawn_as in found:
        if not any(_same_box(box, other, settings.same_outline_pt) for other, _drawn in kept):
            kept.append((box, drawn_as))
    return kept


def _corners_agree(
    top: _Stroke, bottom: _Stroke, west: _Stroke, east: _Stroke, settings: SpanSettings
) -> bool:
    """Whether four sides are one rectangle's: at each corner the horizontal side stops as far
    short of the vertical side as the vertical side stops short of the horizontal one (a sharp
    corner: both nothing; a rounded one: both its radius), within `same_outline_pt` (plus
    `dash_gap_pt` where a side is dashed). Solid sides taken from two outlines drawn close together
    do not agree, so they never make a third outline; a mix with a dashed side can, which only
    adds an outline, and more outlines only ever hold."""
    pairs = (
        (top.start - west.at, west.start - top.at, top, west),
        (east.at - top.end, east.start - top.at, top, east),
        (bottom.start - west.at, bottom.at - west.end, bottom, west),
        (east.at - bottom.end, bottom.at - east.end, bottom, east),
    )

    def tight(first: _Stroke, second: _Stroke) -> Decimal:
        # A dashed side stops wherever its last dash ends, up to one gap short of the corner.
        dashed = first.pieces > 1 or second.pieces > 1
        return settings.same_outline_pt + (settings.dash_gap_pt if dashed else Decimal(0))

    return all(
        abs(along - across) <= tight(first, second)
        and along >= -tight(first, second)
        and across >= -tight(first, second)
        for along, across, first, second in pairs
    )


def _same_box(first: Box, second: Box, reach: Decimal) -> bool:
    return (
        abs(first.x0 - second.x0) <= reach
        and abs(first.x1 - second.x1) <= reach
        and abs(first.top - second.top) <= reach
        and abs(first.bottom - second.bottom) <= reach
    )


def _drains(ink: PageInk, view: Box, settings: SpanSettings) -> list[Box]:
    """Round marks: curves with many points, about as wide as tall (within a fifth), and at least
    `drain_minimum_pt` wide (a corner dot is not a drain)."""
    found: list[Box] = []
    for curve in ink.curves:
        box = curve.box
        if not (_inside(box.x0, box.top, view) and _inside(box.x1, box.bottom, view)):
            continue
        if len(curve.points) < 8 or box.width < settings.drain_minimum_pt or box.height <= 0:
            continue
        if abs(box.width - box.height) * 5 <= max(box.width, box.height):
            found.append(box)
    return found


def sink_outlines(
    ink: PageInk, view: Box, phrases: Sequence[PrintedPhrase], settings: SpanSettings
) -> tuple[SinkOutlines, ...]:
    """The rectangles drawn around each sink in a (plan) view, outer first, lettered A, B, ...

    A sink is found by its words (`SINK`, `BOWL`, `BASIN`, `LAV`) printed inside a rectangle, or by
    a round drain inside one at least `1 / drain_maximum_fraction` times as wide. Every rectangle
    that holds the same mark is one of that sink's outlines (`_rectangles_round`: counted
    generously, so a dashed bowl under the stone is an outline too). Two marks with the same
    rectangles are one sink.
    """
    marks: list[tuple[Decimal, Decimal, str, Box | None]] = [
        (phrase.box.centre_x, phrase.box.centre_y, f"the words {phrase.text!r} inside", None)
        for phrase in phrases
        if _SINK_WORDS.search(phrase.text)
        and _inside(phrase.box.centre_x, phrase.box.centre_y, view)
    ]
    marks.extend(
        (drain.centre_x, drain.centre_y, "a round drain inside", drain)
        for drain in _drains(ink, view, settings)
    )
    found: list[tuple[tuple[Box, ...], tuple[Outline, ...], str, tuple[int, int, int, int]]] = []
    for x, y, why, drain in marks:
        holding = [
            (box, drawn)
            for box, drawn in _rectangles_round(ink, view, x, y, settings)
            if drain is None or drain.width <= settings.drain_maximum_fraction * box.width
        ]
        if not holding:
            continue
        holding.sort(key=lambda item: item[0].width * item[0].height, reverse=True)
        key = tuple(box for box, _drawn in holding)
        if any(
            len(key) == len(other)
            and all(
                _same_box(a, b, settings.same_outline_pt) for a, b in zip(key, other, strict=True)
            )
            for other, _outlines, _why, _crossings in found
        ):
            continue
        outlines = tuple(
            Outline(chr(ord("A") + index), box, drawn) for index, (box, drawn) in enumerate(holding)
        )
        found.append((key, outlines, why, _crossings(ink, view, x, y, key[0], settings)))
    groups: list[SinkOutlines] = []
    for index, (key, outlines, why, crossings) in enumerate(found):
        shares = any(
            _same_box(box, theirs, settings.same_outline_pt)
            for other, (their_key, _o, _w, _c) in enumerate(found)
            if other != index
            for box in key
            for theirs in their_key
        )
        groups.append(SinkOutlines(outlines, why, crossings, shares))
    return tuple(groups)


def _crossings(
    ink: PageInk, view: Box, x: Decimal, y: Decimal, outer: Box, settings: SpanSettings
) -> tuple[int, int, int, int]:
    """How many separate lines are crossed going left, right, up and down from `(x, y)`, out to
    `outline_search_fraction` of `outer`'s shorter side beyond `outer`. Lines are strokes as drawn
    and dashed pieces joined, at least half `cutout_minimum_pt` long; two within `same_outline_pt`
    of each other are one line."""
    raw = _strokes(ink, view, settings)
    lines = [
        stroke
        for stroke in (*raw, *(joined for joined in _joined(raw, settings) if joined.pieces > 1))
        if stroke.length >= settings.cutout_minimum_pt / 2
    ]
    beyond = settings.outline_search_fraction * min(outer.width, outer.height)

    def count(axis: Axis, low: Decimal, high: Decimal, through: Decimal) -> int:
        positions = sorted(
            stroke.at
            for stroke in lines
            if stroke.axis is axis
            and low <= stroke.at <= high
            and stroke.start <= through <= stroke.end
        )
        distinct = 0
        last: Decimal | None = None
        for position in positions:
            if last is None or position - last > settings.same_outline_pt:
                distinct += 1
            last = position
        return distinct

    return (
        count(Axis.VERTICAL, outer.x0 - beyond, x, y),
        count(Axis.VERTICAL, x, outer.x1 + beyond, y),
        count(Axis.HORIZONTAL, outer.top - beyond, y, x),
        count(Axis.HORIZONTAL, y, outer.bottom + beyond, x),
    )


def cutout_judgment(sink: SinkOutlines, panel_phrases: Sequence[PrintedPhrase]) -> CutoutJudgment:
    """Code's half of the cut-out rule: it allows only when printed words name the cut-out or one
    outline is drawn (Decision log 2026-10-14). **Every doubt holds**, and a person picks the hole
    with one click:

    * the sink shares an outline with another mark's group (one sink seen twice);
    * two or more outlines and no naming word;
    * one outline found, but the second count (`SinkOutlines.crossings`) does not cross exactly one
      line every way: a second outline code could not see as a rectangle (rounded corners drawn
      apart, sides running on) is still there.

    **Words name this sink's cut-out** only when a phrase says cut-out or opening (`CUTOUT_WORDS`)
    and either also says sink (`SINK OPENING ...`) or is printed inside the sink's outer outline. A
    panel's "TRASH CUTOUT" note names another hole, and must not let a sink's cut-out through.
    """
    if sink.shares_outline:
        return CutoutJudgment(
            False,
            (),
            "this sink shares an outline with another mark's outlines: one sink seen twice, held "
            "as a whole for a person",
        )
    outer = sink.outlines[0].box
    words = tuple(
        phrase.text
        for phrase in panel_phrases
        if CUTOUT_WORDS.search(phrase.text)
        and (
            _SINK_WORDS.search(phrase.text)
            or _inside(phrase.box.centre_x, phrase.box.centre_y, outer)
        )
    )
    if words:
        return CutoutJudgment(
            True, words, "printed words name the cut-out: " + "; ".join(repr(w) for w in words)
        )
    if len(sink.outlines) > 1:
        return CutoutJudgment(
            False,
            (),
            f"{len(sink.outlines)} outlines are drawn round the sink and no printed word names the "
            "cut-out: a person picks the hole with one click",
        )
    if sink.crossings != (1, 1, 1, 1):
        return CutoutJudgment(
            False,
            (),
            "one outline was found, but the second count crosses "
            + "/".join(str(count) for count in sink.crossings)
            + " lines (left/right/up/down) round the sink, not one each way: another outline may "
            "be drawn, so a person picks the hole",
        )
    return CutoutJudgment(True, (), "one sink outline is drawn, and the second count agrees")


def span_on_outline(span: Span, outline: Outline, settings: SpanSettings) -> bool:
    """Whether the span's two ends lie on two opposite edges of the outline (within `end_meet_pt`):
    a cut-out width spans its left and right edges, a cut-out depth its top and bottom."""
    box, reach = outline.box, settings.end_meet_pt
    low, high = (box.x0, box.x1) if span.axis is Axis.HORIZONTAL else (box.top, box.bottom)
    return abs(span.start_pt - low) <= reach and abs(span.end_pt - high) <= reach


#: Measured on AI Set 1 and AI Set 3's vendor panels (2026-10-14, #1256 dry run). The vendor's
#: dimension lines overshoot their witness lines by 2.6 pt (`witness_tolerance_pt` 3); labels sit
#: within 12 pt of their line, as the architect reader found; the rest are the architect reader's
#: own measured lengths where one exists.
MEASURED_SPAN_SETTINGS: Final = SpanSettings(
    straightness_pt=Decimal("0.3"),
    witness_tolerance_pt=Decimal(3),
    crossing_margin_pt=Decimal("0.5"),
    minimum_span_pt=Decimal(6),
    same_offset_pt=Decimal(1),
    end_meet_pt=Decimal("1.5"),
    outline_minimum_pt=Decimal(12),
    label_reach_pt=Decimal(12),
    band_aspect=Decimal(4),
    band_minimum_fraction=Decimal("0.5"),
    band_clear_fraction=Decimal("0.3"),
    filled_band_lightness=Decimal("0.9"),
    hatch=MEASURED_ARCHITECT_SETTINGS.outline,
    cutout_minimum_pt=Decimal(15),
    cutout_maximum_pt=Decimal(300),
    corner_reach_pt=Decimal(6),
    dash_gap_pt=Decimal(4),
    dash_minimum_pt=Decimal(2),
    drain_maximum_fraction=Decimal("0.4"),
    drawn_length_tolerance=Fraction(1, 10),
    drawn_length_within_in=Fraction(1, 4),
    witness_gap_pt=Decimal(4),
    mm_corroboration_mm=Fraction(2),
    same_size_fraction=Decimal("0.1"),
    baseline_drift_em=Decimal("0.3"),
    tie_margin_pt=Decimal(1),
    same_outline_pt=Decimal("0.5"),
    outline_search_fraction=Decimal("0.35"),
    drain_minimum_pt=Decimal(4),
)
