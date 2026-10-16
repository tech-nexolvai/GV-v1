"""Every dimension drawn in a view on both axes, and every architect label exactly once.

Verification for: `extraction/compare/dimensions.py`. Pages are written by hand from literal
content streams (as in `tests/extraction/geometry/test_rows.py`): no client drawing is read, and
every number here is made up.
"""

from __future__ import annotations

import io
from decimal import Decimal
from fractions import Fraction

import pdfplumber

from evidence.coordinates import StoredPoint
from extraction.architect.labels import read_label
from extraction.architect.text import Orientation, PrintedDimension
from extraction.compare.dimensions import (
    NOT_ON_A_LINE,
    Axis,
    DimensionSettings,
    DrawnDimension,
    architect_dimensions,
    drawn_dimensions,
    labelled_lines,
    transposed,
)
from extraction.geometry.rows import MEASURED_SETTINGS, Box, InkCharacter, InkLine, PageInk
from extraction.rows import ink_from_page
from tests.extraction.test_reader import _pdf

WIDTH, HEIGHT = Decimal(400), Decimal(300)
SETTINGS = DimensionSettings(
    tick_pair_pt=Decimal(2),
    label_reach_pt=Decimal(12),
    label_slack_pt=Decimal(2),
    witness_band=Decimal("0.04"),
    witness_floor_in=Decimal(1),
)
WHOLE_PAGE = Box(Decimal(0), Decimal(0), WIDTH, HEIGHT)


def _place(x: Decimal, top: Decimal) -> StoredPoint:
    return StoredPoint(x / WIDTH, top / HEIGHT)


def _slash(x: float, y: float) -> bytes:
    return (
        f"{x - 1:.2f} {y - 1:.2f} m {x + 1:.2f} {y + 1:.2f} l {x + 1.4:.2f} {y + 1:.2f} l "
        f"{x - 0.6:.2f} {y - 1:.2f} l h f\n"
    ).encode()


def _line(x0: float, y0: float, x1: float, y1: float) -> bytes:
    return f"0.3 w {x0:.2f} {y0:.2f} m {x1:.2f} {y1:.2f} l S\n".encode()


def _text(x: float, y: float, text: str) -> bytes:
    return f"BT /F1 6 Tf 1 0 0 1 {x:.2f} {y:.2f} Tm ({text}) Tj ET\n".encode()


#: A width chain at PDF y=100 (top 200): ticks at 50, 100, 200, 350, each piece labelled.
CHAIN = _line(50, 100, 350, 100) + b"".join(_slash(x, 100) for x in (50, 100, 200, 350))
CHAIN_LABELS = _text(70, 104, "12") + _text(145, 104, "24") + _text(270, 104, "36")
#: A height chain at PDF x=30: ticks at PDF y 40, 120, 260 (tops 260, 180, 40).
HEIGHTS = _line(30, 40, 30, 260) + b"".join(_slash(30, y) for y in (40, 120, 260))
HEIGHT_LABELS = _text(18, 75, "8") + _text(18, 185, "9")


def _ink(content: bytes) -> PageInk:
    with pdfplumber.open(io.BytesIO(_pdf(content, box=b"[0 0 400 300]"))) as document:
        return ink_from_page(document.pages[0], drawing_boxes=())


def _drawn(content: bytes, view: Box = WHOLE_PAGE) -> tuple[DrawnDimension, ...]:
    return drawn_dimensions(
        _ink(content), view, rows=MEASURED_SETTINGS, settings=SETTINGS, place=_place
    )


def _label(text: str, box: Box, orientation: Orientation = Orientation.UPRIGHT) -> PrintedDimension:
    return PrintedDimension(reading=read_label(text), box=box, orientation=orientation, phrase=text)


def _span(
    axis: Axis, offset: int, start: int, end: int, *, row: int = 0, index: int = 0
) -> DrawnDimension:
    return DrawnDimension(
        axis=axis,
        offset_pt=Decimal(offset),
        start_pt=Decimal(start),
        end_pt=Decimal(end),
        row=row,
        index=index,
        label_boxes=(),
        label_text=None,
    )


def _box(x0: int, top: int, x1: int, bottom: int) -> Box:
    return Box(Decimal(x0), Decimal(top), Decimal(x1), Decimal(bottom))


# --- drawn dimensions on both axes ----------------------------------------------------------------


def test_a_width_chain_gives_one_horizontal_span_per_piece() -> None:
    """Input: a chain of three labelled pieces. Outcome: three horizontal spans tick to tick."""
    spans = [s for s in _drawn(CHAIN + CHAIN_LABELS) if s.axis is Axis.HORIZONTAL]

    assert [(round(s.start_pt), round(s.end_pt)) for s in spans] == [
        (50, 100),
        (100, 200),
        (200, 350),
    ]
    assert {s.offset_pt for s in spans} == {Decimal(200)}  # pdfplumber's frame: 300 - 100
    assert [s.index for s in spans] == [0, 1, 2]
    assert all(s.label_boxes for s in spans)


def test_a_height_chain_is_found_by_the_same_finder_turned_a_quarter() -> None:
    """Input: a vertical chain of two pieces with slash ticks. Outcome: two vertical spans, their
    ends in page `top` coordinates, top to bottom, on the line's x."""
    spans = [s for s in _drawn(HEIGHTS + HEIGHT_LABELS) if s.axis is Axis.VERTICAL]

    assert [(round(s.start_pt), round(s.end_pt)) for s in spans] == [(40, 180), (180, 260)]
    assert {round(s.offset_pt) for s in spans} == {30}
    assert spans[0].box == Box(
        spans[0].offset_pt, spans[0].start_pt, spans[0].offset_pt, spans[0].end_pt
    )


def test_a_line_outside_the_view_is_not_the_views() -> None:
    """Input: the width chain, with a view box that holds only the heights. Outcome: no width."""
    spans = _drawn(CHAIN + CHAIN_LABELS + HEIGHTS, view=_box(0, 0, 45, 300))

    assert spans and all(s.axis is Axis.VERTICAL for s in spans)


def test_transposed_swaps_x_and_y_everywhere() -> None:
    ink = PageInk(
        width=Decimal(10),
        height=Decimal(20),
        lines=(InkLine(Decimal(1), Decimal(2), Decimal(3), Decimal(4)),),
        curves=(),
        characters=(InkCharacter("7", _box(1, 2, 3, 5)),),
        drawing_boxes=(_box(0, 1, 2, 3),),
    )

    flipped = transposed(ink)

    assert (flipped.width, flipped.height) == (Decimal(20), Decimal(10))
    assert flipped.lines == (InkLine(Decimal(2), Decimal(1), Decimal(4), Decimal(3)),)
    assert flipped.characters[0].box == _box(2, 1, 5, 3)
    assert flipped.drawing_boxes == (_box(1, 0, 3, 2),)
    assert transposed(flipped) == ink


def test_only_lines_that_carry_a_label_count_as_dimensions() -> None:
    labelled = _span(Axis.HORIZONTAL, 10, 0, 50, row=0)
    outline = _span(Axis.HORIZONTAL, 40, 0, 50, row=1)
    tagged = DrawnDimension(
        axis=Axis.HORIZONTAL,
        offset_pt=Decimal(70),
        start_pt=Decimal(0),
        end_pt=Decimal(9),
        row=2,
        index=0,
        label_boxes=(_box(1, 60, 8, 68),),
        label_text=None,
    )

    assert labelled_lines([labelled, outline], placed=[labelled]) == (labelled,)
    assert labelled_lines([outline, tagged]) == (tagged,)


# --- every architect label exactly once -----------------------------------------------------------

#: A width line at top 100 from 0 to 120 pt in two spans; a height line at x 200; scale 2 pt/in.
WIDTHS = (
    _span(Axis.HORIZONTAL, 100, 0, 48, index=0),
    _span(Axis.HORIZONTAL, 100, 48, 120, index=1),
)
HEIGHT_LINE = (_span(Axis.VERTICAL, 200, 20, 92),)
PPI = Decimal(2)


def test_each_label_lands_on_the_span_it_is_printed_on() -> None:
    """Input: a width label over each width span, a sideways label beside the height. Outcome:
    each on its own span, numbered widths first, its exact inches kept."""
    labels = [
        _label("3' - 0\"", _box(84, 90, 100, 96)),
        _label("2' - 0\"", _box(20, 90, 36, 96)),
        _label("3' - 0\"", _box(190, 50, 196, 66), Orientation.UP),
    ]

    dims = architect_dimensions(
        labels, (*WIDTHS, *HEIGHT_LINE), points_per_inch=PPI, settings=SETTINGS
    )

    assert [(d.number, d.text, d.inches) for d in dims] == [
        (1, "2' - 0\"", Fraction(24)),
        (2, "3' - 0\"", Fraction(36)),
        (3, "3' - 0\"", Fraction(36)),
    ]
    assert [d.span for d in dims] == [WIDTHS[0], WIDTHS[1], HEIGHT_LINE[0]]
    assert all(d.held_reason is None for d in dims)
    assert dims[0].drawn_inches == Fraction(24)


def test_a_label_with_no_line_under_it_is_kept_with_the_reason() -> None:
    dims = architect_dimensions(
        [_label("1' - 0\"", _box(300, 250, 316, 256))],
        WIDTHS,
        points_per_inch=PPI,
        settings=SETTINGS,
    )

    (only,) = dims
    assert only.span is None
    assert only.placement_reason == NOT_ON_A_LINE
    assert only.inches is None and only.held_reason == NOT_ON_A_LINE
    assert only.printed_inches == Fraction(12)


def test_two_labels_on_one_span_are_both_held() -> None:
    labels = [_label("2' - 0\"", _box(5, 90, 21, 96)), _label("1' - 0\"", _box(25, 90, 41, 96))]

    dims = architect_dimensions(labels, WIDTHS, points_per_inch=PPI, settings=SETTINGS)

    assert len(dims) == 2
    assert all(d.inches is None for d in dims)
    assert all(d.held_reason == "more than one label is printed on this span" for d in dims)


def test_a_drawn_length_that_disagrees_with_the_label_holds_it() -> None:
    """Input: `5' - 0"` (60) printed on a span drawn 24 in long at the view's scale. Outcome:
    held, the second judgment on the number refusing it."""
    dims = architect_dimensions(
        [_label("5' - 0\"", _box(20, 90, 36, 96))], WIDTHS, points_per_inch=PPI, settings=SETTINGS
    )

    (only,) = dims
    assert only.inches is None
    assert only.printed_inches == Fraction(60)
    assert only.held_reason is not None and "length drawn" in only.held_reason


def test_without_a_scale_nothing_is_usable() -> None:
    dims = architect_dimensions(
        [_label("2' - 0\"", _box(20, 90, 36, 96))], WIDTHS, points_per_inch=None, settings=SETTINGS
    )

    assert dims[0].inches is None
    assert dims[0].held_reason == (
        "the view's scale is not known, so the drawn length cannot back the label"
    )


def test_a_held_label_keeps_its_own_reason() -> None:
    dims = architect_dimensions(
        [_label("2' - 0\" VIF", _box(16, 90, 44, 96))],
        WIDTHS,
        points_per_inch=PPI,
        settings=SETTINGS,
    )

    assert dims[0].inches is None
    assert dims[0].held_reason == read_label("2' - 0\" VIF").held_reason


def test_stacked_heights_each_keep_their_own_line() -> None:
    """Input: two height lines 10 pt apart, each label reading upwards just left of its own line,
    so the second label sits as near the first line as its own. Outcome: each on its own line."""
    first = _span(Axis.VERTICAL, 200, 20, 92, row=0)
    second = _span(Axis.VERTICAL, 210, 20, 140, row=1)
    labels = [
        _label("3' - 0\"", _box(194, 40, 199, 56), Orientation.UP),
        _label("5' - 0\"", _box(201, 60, 206, 76), Orientation.UP),
    ]

    dims = architect_dimensions(labels, (first, second), points_per_inch=PPI, settings=SETTINGS)

    assert [d.span for d in dims] == [first, second]
    assert all(d.held_reason is None for d in dims)


def test_every_label_appears_exactly_once_whatever_happens_to_it() -> None:
    labels = [
        _label("2' - 0\"", _box(20, 90, 36, 96)),
        _label("3' - 0\"", _box(84, 90, 100, 96)),
        _label("3' - 0\"", _box(190, 50, 196, 66), Orientation.UP),
        _label('6" WOOD', _box(300, 10, 330, 16)),
        _label("9' - 0\"", _box(300, 250, 316, 256)),
    ]

    dims = architect_dimensions(
        labels, (*WIDTHS, *HEIGHT_LINE), points_per_inch=PPI, settings=SETTINGS
    )

    assert sorted(d.label_box.x0 for d in dims) == sorted(label.box.x0 for label in labels)
    assert [d.number for d in dims] == [1, 2, 3, 4, 5]
