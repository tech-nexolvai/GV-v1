"""Every dimension drawn in one view, on both axes, and every dimension the architect printed (#direct-compare).

The direct compare puts each dimension the architect printed beside the vendor's dimension of the
same span. Code needs, for both drawings, **where each dimension is drawn**: its axis, the line it
sits on, and the two ticks it runs between. This module finds that, and for the architect's view
also the number printed on each one.

**Both axes from one row finder.** `extraction/geometry/rows.build_rows` finds horizontal dimension
rows from their ticks (the drawing's slash marks, or witness lines crossing the row). A vertical
dimension (a height) is the same thing turned a quarter, so the page's ink is mirrored across its
diagonal (x and y swapped, `transposed`), the same finder runs on it, and every row it finds is
swapped back. A slash stays a slash under that mirror, so the tick sizes measured for the rows hold.

**Every architect label exactly once** (`architect_dimensions`). Every dimension printed in the
architect's view (`extraction/architect/text.find_printed`, real text, black and grey ink only)
becomes one `ArchitectDimension`:

* on a span: an upright label belongs to the nearest horizontal row within reach and the span its
  middle lies in; a sideways label to the nearest vertical row the same way. A line on the side
  drafting puts the number (below a width's label; for a height, the side its letters' feet face)
  is taken before one on the other side, so stacked heights each keep their own line;
* held, with the reason, when its text does not read (`labels.read_label`), when two labels sit on
  one span, or when the length drawn between its ticks, through the view's own witnessed scale,
  disagrees with it (the second judgment on the number: the same band the architect reader uses);
* not on any span, with the reason, when no dimension line lies under it.

Nothing is dropped. **What it never does:** read coloured ink (GV's markup), round, or compare a
value. Pure: plain values in, plain values out, page points with `top` downward.

Source: the direct-compare issue · Verification: `tests/extraction/compare/test_dimensions.py`
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum
from fractions import Fraction
from itertools import pairwise
from typing import Final

from extraction.architect.text import Orientation, PrintedDimension
from extraction.geometry.rows import (
    Box,
    CountertopRowCandidate,
    InkCharacter,
    InkCurve,
    InkLine,
    PageInk,
    Placement,
    RowSettings,
    TickSource,
    build_rows,
)

__all__ = [
    "NOT_ON_A_LINE",
    "ArchitectDimension",
    "Axis",
    "DimensionSettings",
    "DrawnDimension",
    "architect_dimensions",
    "drawn_dimensions",
    "labelled_lines",
    "transposed",
]

_TWO: Final = Decimal(2)

NOT_ON_A_LINE: Final = "no dimension line was found under this label, so where it runs is not known"


class Axis(StrEnum):
    HORIZONTAL = "horizontal"
    """A width or a position along the wall: runs left to right."""
    VERTICAL = "vertical"
    """A height: runs top to bottom."""


@dataclass(frozen=True, slots=True)
class DimensionSettings:
    """Every threshold, stated by the caller (no defaults: the repo's convention)."""

    tick_pair_pt: Decimal
    """Two ticks closer than this are one (a doubled slash)."""
    label_reach_pt: Decimal
    """A label belongs to a dimension line when its nearer edge is at most this far across from
    it (an edge, not its middle: a sideways height's lettering can be large)."""
    label_slack_pt: Decimal
    """A label may stand this far past a line's end ticks and still be on it."""
    witness_band: Decimal
    """The drawn length may differ from the printed value by this fraction of it..."""
    witness_floor_in: Decimal
    """...or by this many inches, whichever is larger."""


@dataclass(frozen=True, slots=True)
class DrawnDimension:
    """One dimension as drawn in a view: its axis, the line it sits on and its two ticks."""

    axis: Axis
    offset_pt: Decimal
    """Where the dimension line is across its axis: its y when horizontal, its x when vertical."""
    start_pt: Decimal
    end_pt: Decimal
    """The two ticks along the axis, `start_pt < end_pt`, in page points."""
    row: int
    """Which line of the view it is on (view-local, from 0, per axis)."""
    index: int
    """Its place along that line, from 0."""
    label_boxes: tuple[Box, ...]
    """Where something is printed on it (text or pen strokes), nearest first; empty when nothing."""
    label_text: str | None
    """The label as real text when it is real text; `None` for pen strokes or nothing."""
    tick_source: TickSource = TickSource.SLASH
    """How its ticks were found: the drawing's own slashes, or witness lines crossing it (an edge of
    something drawn can look like that too, so the vendor side uses slash ticks only)."""

    @property
    def length_pt(self) -> Decimal:
        return self.end_pt - self.start_pt

    @property
    def box(self) -> Box:
        """The dimension line from tick to tick, as a thin box in page points."""
        if self.axis is Axis.HORIZONTAL:
            return Box(self.start_pt, self.offset_pt, self.end_pt, self.offset_pt)
        return Box(self.offset_pt, self.start_pt, self.offset_pt, self.end_pt)


@dataclass(frozen=True, slots=True)
class ArchitectDimension:
    """One dimension the architect printed, where it runs, and its value or why it is held."""

    number: int
    """View-local, from 1, in reading order: the `A` number the pictures print."""
    text: str
    """As printed."""
    label_box: Box
    printed_inches: Fraction | None
    """What the label reads as, exactly; `None` when it does not read."""
    inches: Fraction | None
    """The value the compare may use: `None` whenever it is held."""
    held_reason: str | None
    span: DrawnDimension | None
    """Where it runs; `None` when no dimension line lies under it (`placement_reason` says so)."""
    placement_reason: str
    drawn_inches: Fraction | None
    """The length drawn between its ticks through the view's scale (the second judgment)."""


# --- both axes ------------------------------------------------------------------------------------


def _swap_box(box: Box) -> Box:
    return Box(box.top, box.x0, box.bottom, box.x1)


def transposed(ink: PageInk) -> PageInk:
    """The page's ink mirrored across its diagonal: x and y swapped everywhere. A vertical
    dimension of `ink` is a horizontal one of the result."""
    return PageInk(
        width=ink.height,
        height=ink.width,
        lines=tuple(InkLine(line.y0, line.x0, line.y1, line.x1) for line in ink.lines),
        curves=tuple(
            replace(
                curve,
                box=_swap_box(curve.box),
                points=tuple((y, x) for x, y in curve.points),
            )
            for curve in ink.curves
            if isinstance(curve, InkCurve)
        ),
        characters=tuple(
            InkCharacter(character.text, _swap_box(character.box)) for character in ink.characters
        ),
        drawing_boxes=tuple(_swap_box(box) for box in ink.drawing_boxes),
        architect_boxes=(),
    )


def _inside(view: Box, axis: Axis, row: CountertopRowCandidate) -> bool:
    if axis is Axis.HORIZONTAL:
        return view.x0 <= row.x0 and row.x1 <= view.x1 and view.top <= row.y <= view.bottom
    return view.top <= row.x0 and row.x1 <= view.bottom and view.x0 <= row.y <= view.x1


def _paired(ticks: Sequence[Decimal], within: Decimal) -> tuple[Decimal, ...]:
    merged: list[Decimal] = []
    index = 0
    while index < len(ticks):
        if index + 1 < len(ticks) and ticks[index + 1] - ticks[index] < within:
            merged.append((ticks[index] + ticks[index + 1]) / _TWO)
            index += 2
        else:
            merged.append(ticks[index])
            index += 1
    return tuple(merged)


def drawn_dimensions(
    ink: PageInk,
    view: Box,
    *,
    rows: RowSettings,
    settings: DimensionSettings,
    place: Placement,
) -> tuple[DrawnDimension, ...]:
    """Every dimension span drawn inside `view`, horizontal first, each line top to bottom (or left
    to right), each span along it in order. A line found twice is kept once; ticks from mere breaks
    in a line (table rules, hatching) are never used."""
    found: list[DrawnDimension] = []
    for axis in (Axis.HORIZONTAL, Axis.VERTICAL):
        if axis is Axis.HORIZONTAL:
            page_rows = build_rows(ink, rows, place=place)
        else:
            page_rows = build_rows(transposed(ink), rows, place=lambda x, top: place(top, x))
        seen: set[tuple[Decimal, tuple[Decimal, ...]]] = set()
        number = 0
        for row in sorted(
            (*page_rows.candidates, *page_rows.rejected), key=lambda row: (row.y, row.x0)
        ):
            if row.tick_source is TickSource.GAP or not _inside(view, axis, row):
                continue
            ticks = _paired(row.ticks, settings.tick_pair_pt)
            if len(ticks) < 2 or (row.y, ticks) in seen:
                continue
            seen.add((row.y, ticks))
            for index, (start, end) in enumerate(pairwise(ticks)):
                slot = next(
                    (s for s in row.slots if s.x0 <= (start + end) / _TWO <= s.x1),
                    None,
                )
                labels = () if slot is None else slot.labels
                boxes = tuple(
                    label.box if axis is Axis.HORIZONTAL else _swap_box(label.box)
                    for label in labels
                )
                text = None if not labels or axis is Axis.VERTICAL else labels[0].text
                found.append(
                    DrawnDimension(
                        axis=axis,
                        offset_pt=row.y,
                        start_pt=start,
                        end_pt=end,
                        row=number,
                        index=index,
                        label_boxes=boxes,
                        label_text=text,
                        tick_source=row.tick_source,
                    )
                )
            number += 1
    return tuple(found)


# --- the architect's printed dimensions -----------------------------------------------------------


def _along_across(axis: Axis, box: Box) -> tuple[Decimal, Decimal, Decimal]:
    """The label's middle along the axis, and its near and far edges across it."""
    if axis is Axis.HORIZONTAL:
        return box.centre_x, box.top, box.bottom
    return box.centre_y, box.x0, box.x1


def _gap(low: Decimal, high: Decimal, offset: Decimal) -> Decimal:
    """How far a label spanning `low..high` across the axis stands from a line at `offset`."""
    if low <= offset <= high:
        return Decimal(0)
    return min(abs(low - offset), abs(high - offset))


def _axis_of(label: PrintedDimension) -> Axis:
    return Axis.HORIZONTAL if label.orientation is Orientation.UPRIGHT else Axis.VERTICAL


def _place(
    label: PrintedDimension, spans: Sequence[DrawnDimension], settings: DimensionSettings
) -> DrawnDimension | None:
    """The span the label is printed on: the nearest line of its axis within reach whose extent
    (with the slack) holds the label's middle, and on it the span the middle lies in."""
    axis = _axis_of(label)
    along, low, high = _along_across(axis, label.box)
    lines: dict[tuple[Decimal, int], list[DrawnDimension]] = {}
    for span in spans:
        if span.axis is axis:
            lines.setdefault((span.offset_pt, span.row), []).append(span)
    options: list[tuple[bool, Decimal, DrawnDimension]] = []
    for (offset, _row), parts in lines.items():
        distance = _gap(low, high, offset)
        # Drafting puts the number on the line's upper side: above a width, and for a height on
        # the side its letters' tops face (left when it reads upwards, right when downwards).
        if axis is Axis.HORIZONTAL or label.orientation is Orientation.UP:
            preferred = offset >= (low + high) / _TWO
        else:
            preferred = offset <= (low + high) / _TWO
        if distance > settings.label_reach_pt:
            continue
        start, end = parts[0].start_pt, parts[-1].end_pt
        if not start - settings.label_slack_pt <= along <= end + settings.label_slack_pt:
            continue
        inner = next((part for part in parts if part.start_pt <= along < part.end_pt), None)
        if inner is None:
            inner = parts[0] if along < start else parts[-1]
        options.append((not preferred, distance, inner))
    if not options:
        return None
    options.sort(key=lambda option: (option[0], option[1], option[2].row, option[2].index))
    return options[0][2]


def _reading_order(label: PrintedDimension) -> tuple[int, Decimal, Decimal]:
    axis = _axis_of(label)
    return (0 if axis is Axis.HORIZONTAL else 1, label.box.top, label.box.x0)


def architect_dimensions(
    printed: Sequence[PrintedDimension],
    spans: Sequence[DrawnDimension],
    *,
    points_per_inch: Decimal | None,
    settings: DimensionSettings,
) -> tuple[ArchitectDimension, ...]:
    """Every printed dimension of one architect view, exactly once, numbered in reading order
    (widths top to bottom, then heights), each on its span or with the reason it is not."""
    ordered = sorted(printed, key=_reading_order)
    placed = [(label, _place(label, spans, settings)) for label in ordered]
    on_span: dict[tuple[str, Decimal, Decimal, Decimal], int] = {}
    for _label, span in placed:
        if span is not None:
            key = (span.axis.value, span.offset_pt, span.start_pt, span.end_pt)
            on_span[key] = on_span.get(key, 0) + 1
    result: list[ArchitectDimension] = []
    for number, (label, span) in enumerate(placed, start=1):
        reading = label.reading
        held = reading.held_reason
        drawn: Fraction | None = None
        if span is None:
            reason = NOT_ON_A_LINE
            held = held or NOT_ON_A_LINE
        else:
            reason = f"on a {span.axis.value} dimension line"
            if on_span[(span.axis.value, span.offset_pt, span.start_pt, span.end_pt)] > 1:
                held = held or "more than one label is printed on this span"
            if points_per_inch is not None and points_per_inch > 0:
                drawn = Fraction(span.length_pt) / Fraction(points_per_inch)
                if reading.inches is not None and held is None:
                    allowed = max(
                        Fraction(settings.witness_band) * reading.inches,
                        Fraction(settings.witness_floor_in),
                    )
                    if abs(drawn - reading.inches) > allowed:
                        held = (
                            f"the length drawn between its ticks ({float(drawn):.1f} in at the "
                            f"view's scale) is not the printed {reading.text}: it may measure "
                            "something else"
                        )
            elif held is None:
                held = "the view's scale is not known, so the drawn length cannot back the label"
        result.append(
            ArchitectDimension(
                number=number,
                text=reading.text,
                label_box=label.box,
                printed_inches=reading.inches,
                inches=None if held else reading.inches,
                held_reason=held,
                span=span,
                placement_reason=reason,
                drawn_inches=drawn,
            )
        )
    return tuple(result)


def labelled_lines(
    spans: Sequence[DrawnDimension], placed: Sequence[DrawnDimension] | None = None
) -> tuple[DrawnDimension, ...]:
    """The spans of every line that carries a label: the drawing's dimensions, not the outlines of
    a tag, a bubble or hatching that also have ticks. `placed` names the spans an architect's
    label was placed on; without it, a span's own label boxes count (the vendor's pen strokes)."""
    keys = (
        {(span.axis, span.row) for span in placed}
        if placed is not None
        else {(span.axis, span.row) for span in spans if span.label_boxes}
    )
    return tuple(span for span in spans if (span.axis, span.row) in keys)
