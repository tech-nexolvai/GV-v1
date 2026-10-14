"""Spans on both axes and the exact checks on them (#1256). Pure geometry, made-up values."""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

import pytest

from extraction.architect.text import Orientation, PrintedPhrase
from extraction.depth.spans import (
    MEASURED_SPAN_SETTINGS,
    MM_ONLY,
    EndKind,
    Outline,
    Span,
    SpanEnd,
    closure_check,
    cutout_judgment,
    drawn_length_check,
    find_spans,
    overall_horizontal,
    read_depth_label,
    sink_outlines,
    span_on_outline,
    text_lane_agrees,
    text_lanes,
    view_scale,
)
from extraction.geometry.dimension_lines import Axis
from extraction.geometry.rows import Box, InkCurve, InkLine, PageInk

S = MEASURED_SPAN_SETTINGS
VIEW = Box(Decimal(0), Decimal(0), Decimal(600), Decimal(400))


def d(value: float | str) -> Decimal:
    return Decimal(str(value))


def line(x0: float, y0: float, x1: float, y1: float) -> InkLine:
    return InkLine(d(x0), d(y0), d(x1), d(y1))


def ink(lines: list[InkLine], curves: tuple[InkCurve, ...] = ()) -> PageInk:
    return PageInk(
        width=d(600),
        height=d(400),
        lines=tuple(lines),
        curves=curves,
        characters=(),
        drawing_boxes=(),
    )


def vertical_dimension(
    x: float, top: float, bottom: float, *, overshoot: float = 2.5
) -> list[InkLine]:
    """A vertical dimension line overshooting its two horizontal witness lines, as vendors draw."""
    return [
        line(x, top - overshoot, x, bottom + overshoot),
        line(x - 3, top, x + 40, top),
        line(x - 3, bottom, x + 40, bottom),
    ]


def horizontal_dimension(
    y: float, left: float, right: float, *, overshoot: float = 2.5
) -> list[InkLine]:
    return [
        line(left - overshoot, y, right + overshoot, y),
        line(left, y - 3, left, y + 40),
        line(right, y - 3, right, y + 40),
    ]


def phrase(
    text: str, x: float, y: float, orientation: Orientation, size: float = 4
) -> PrintedPhrase:
    return PrintedPhrase(
        text=text,
        box=Box(d(x - size), d(y - size), d(x + size), d(y + size)),
        orientation=orientation,
        height=d(size),
    )


def span(number: int, axis: Axis, start: float, end: float, offset: float = 50) -> Span:
    nothing = SpanEnd(d(start), EndKind.NOTHING, "test")
    return Span(
        number=number,
        axis=axis,
        offset_pt=d(offset),
        start_pt=d(start),
        end_pt=d(end),
        stroke_ends=(d(start), d(end)),
        witnesses=(),
        low_end=nothing,
        high_end=nothing,
    )


# --- spans on both axes --------------------------------------------------------------------


def test_a_vertical_span_is_found_and_measured_between_its_witness_lines() -> None:
    found = find_spans(ink(vertical_dimension(50, 100, 196)), VIEW, S)
    (only,) = found.spans
    assert only.axis is Axis.VERTICAL
    assert (only.start_pt, only.end_pt) == (d(100), d(196))
    assert only.length_pt == d(96)
    # The stroke itself overshoots; the span is where the witness lines cross it.
    assert only.stroke_ends == (d("97.5"), d("198.5"))


def test_a_horizontal_span_is_found_on_the_same_view() -> None:
    lines = vertical_dimension(50, 100, 196) + horizontal_dimension(30, 80, 280)
    found = find_spans(ink(lines), VIEW, S)
    axes = sorted(s.axis.value for s in found.spans)
    assert axes == ["horizontal", "vertical"]
    # Horizontal spans are numbered first.
    assert found.spans[0].axis is Axis.HORIZONTAL and found.spans[0].number == 1


def test_a_stroke_that_is_not_a_dimension_is_counted_never_dropped() -> None:
    lines = vertical_dimension(50, 100, 196) + [line(300, 300, 400, 300)]
    found = find_spans(ink(lines), VIEW, S)
    assert len(found.spans) == 1
    assert found.unclassified == 1


def test_overall_horizontal_is_the_longest_horizontal_span() -> None:
    spans = [
        span(1, Axis.HORIZONTAL, 0, 100),
        span(2, Axis.HORIZONTAL, 0, 200),
        span(3, Axis.VERTICAL, 0, 300),
    ]
    overall = overall_horizontal(spans)
    assert overall is not None and overall.number == 2
    assert overall_horizontal([span(1, Axis.VERTICAL, 0, 10)]) is None


# --- what the ends meet -------------------------------------------------------------------


def wall_hatch(
    top: float, bottom: float, left: float, right: float, step: float = 4
) -> list[InkLine]:
    """Slanted strokes clipped by a band from `top` to `bottom`, as a wall's hatch is drawn."""
    strokes = []
    x = left
    while x + (bottom - top) <= right:
        strokes.append(line(x, bottom, x + (bottom - top), top))
        x += step
    return strokes


def test_a_depth_on_a_plan_runs_from_the_wall_band_to_the_stones_front_edge() -> None:
    lines = (
        wall_hatch(90, 100, 60, 400)
        + [line(60, 100, 400, 100), line(60, 90, 400, 90)]  # the wall's two faces
        + [line(60, 196, 400, 196)]  # the stone's front edge
        + vertical_dimension(50, 100, 196)
    )
    found = find_spans(ink(lines), VIEW, S)
    (depth,) = [s for s in found.spans if s.axis is Axis.VERTICAL]
    assert depth.low_end.kind is EndKind.WALL_BAND
    assert depth.high_end.kind is EndKind.OUTLINE
    (band,) = [band for band in found.bands if band.axis is Axis.HORIZONTAL]
    assert band.at_edge


def test_a_band_with_drawing_on_both_sides_is_not_at_the_edge() -> None:
    # A front view: the stone slab drawn hatched, upper cabinets above and base cabinets below.
    lines = wall_hatch(190, 200, 60, 400) + [line(60, 120, 400, 120), line(60, 300, 400, 300)]
    found = find_spans(ink(lines), VIEW, S)
    (band,) = found.bands
    assert band.axis is Axis.HORIZONTAL
    assert not band.at_edge


def test_an_end_with_nothing_drawn_there_says_so() -> None:
    found = find_spans(ink(vertical_dimension(50, 100, 196)), VIEW, S)
    (only,) = found.spans
    assert only.low_end.kind is EndKind.NOTHING
    assert only.low_end.reason


# --- chains and closure, exact ----------------------------------------------------------------


def chain_lines(close: bool) -> list[InkLine]:
    # A chain at x = 80: 100..130, 130..180, 180..200; an overall at x = 50: 100..200.
    lines = vertical_dimension(50, 100, 200)
    for top, bottom in ((100, 130), (130, 180), (180, 200)):
        lines += vertical_dimension(80, top, bottom)
    return lines


def test_a_chain_and_its_overall_are_found_on_the_vertical_axis() -> None:
    found = find_spans(ink(chain_lines(True)), VIEW, S)
    (closure,) = found.closures
    assert closure.overall.axis is Axis.VERTICAL
    assert [part.length_pt for part in closure.parts] == [d(30), d(50), d(20)]


def test_a_chain_that_closes_and_one_that_does_not() -> None:
    found = find_spans(ink(chain_lines(True)), VIEW, S)
    (closure,) = found.closures
    parts = {
        part.number: value
        for part, value in zip(
            closure.parts, (Fraction(3, 4), Fraction(29, 4), Fraction(2)), strict=True
        )
    }
    closes = closure_check(closure, {**parts, closure.overall.number: Fraction(10)})
    assert closes.closes is True
    assert closes.parts_sum == Fraction(10)
    off = closure_check(closure, {**parts, closure.overall.number: Fraction(161, 16)})
    assert off.closes is False
    unknown = closure_check(closure, parts)
    assert unknown.closes is None


# --- the view's scale and the drawn-length check ------------------------------------------------


def test_drawn_length_at_the_views_own_scale_with_the_boundary_inclusive() -> None:
    overall = span(1, Axis.HORIZONTAL, 0, 200)
    scale = view_scale(overall, Fraction(50))
    assert scale.points_per_inch == Fraction(4)
    depth = span(2, Axis.VERTICAL, 100, 196)  # 96 pt = 24 in at 4 pt/in
    assert drawn_length_check(depth, Fraction(24), scale, tolerance=S.drawn_length_tolerance).agrees
    # |24 - v| = v / 10 exactly at v = 80/3: inclusive.
    edge = drawn_length_check(depth, Fraction(80, 3), scale, tolerance=Fraction(1, 10))
    assert edge.agrees and isinstance(edge.drawn_inches, Fraction)
    past = drawn_length_check(
        depth, Fraction(80, 3) + Fraction(1, 64), scale, tolerance=Fraction(1, 10)
    )
    assert not past.agrees
    # A misread digit (34 for 24) is far outside.
    assert not drawn_length_check(depth, Fraction(34), scale, tolerance=Fraction(1, 10)).agrees


def test_the_scale_comes_only_from_a_horizontal_overall_with_a_positive_value() -> None:
    with pytest.raises(ValueError):
        view_scale(span(1, Axis.VERTICAL, 0, 100), Fraction(24))
    with pytest.raises(ValueError):
        view_scale(span(1, Axis.HORIZONTAL, 0, 100), Fraction(0))


# --- labels: inches authoritative, mm never an operand ------------------------------------------


@pytest.mark.parametrize(
    ("text", "inches"),
    [
        ("635 [25]", Fraction(25)),
        ("452 [17 13/16]", Fraction(285, 16)),
        ('24"', Fraction(24)),
        ('21 5/8"', Fraction(173, 8)),
        ("[3/4]", Fraction(3, 4)),
    ],
)
def test_a_label_reads_its_inches_and_a_bracketed_inch_wins_over_mm(
    text: str, inches: Fraction
) -> None:
    label = read_depth_label(text)
    assert label.inches == inches
    assert label.held_reason is None


@pytest.mark.parametrize(
    "text",
    ["635", "635 mm", "[1 1/2] [1/4]", "ABOUT [25]", '213/8"'],
)
def test_a_label_with_mm_only_two_values_words_or_a_lost_space_is_held(text: str) -> None:
    label = read_depth_label(text)
    assert label.inches is None
    assert label.held_reason


def test_mm_only_is_held_with_the_mm_reason() -> None:
    assert read_depth_label("635").held_reason == MM_ONLY


# --- the vector-text lane, rotated labels -------------------------------------------------------


@pytest.mark.parametrize("orientation", [Orientation.UP, Orientation.DOWN])
def test_a_rotated_label_belongs_to_its_vertical_span(orientation: Orientation) -> None:
    depth = span(1, Axis.VERTICAL, 100, 196, offset=50)
    phrases = [phrase("635", 44, 140, orientation), phrase("[25]", 38, 140, orientation)]
    lane = text_lanes([depth], phrases, S)[1]
    assert lane.inches == Fraction(25)
    assert text_lane_agrees(lane, Fraction(25)) is True
    assert text_lane_agrees(lane, Fraction(25) + Fraction(1, 16)) is False


def test_an_upright_label_is_not_a_vertical_spans_text() -> None:
    depth = span(1, Axis.VERTICAL, 100, 196, offset=50)
    lane = text_lanes([depth], [phrase('24"', 44, 140, Orientation.UPRIGHT)], S)[1]
    assert lane.inches is None
    assert text_lane_agrees(lane, Fraction(24)) is None


def test_a_vertical_span_with_only_mm_printed_is_held() -> None:
    depth = span(1, Axis.VERTICAL, 100, 196, offset=50)
    lane = text_lanes([depth], [phrase("635", 44, 140, Orientation.UP)], S)[1]
    assert lane.inches is None
    assert lane.reason == MM_ONLY


def test_two_different_inch_values_at_one_span_are_held() -> None:
    depth = span(1, Axis.VERTICAL, 100, 196, offset=50)
    phrases = [phrase("[25]", 44, 120, Orientation.UP), phrase("[23]", 44, 170, Orientation.UP)]
    lane = text_lanes([depth], phrases, S)[1]
    assert lane.inches is None


def test_a_label_equally_near_two_spans_belongs_to_neither() -> None:
    first = span(1, Axis.VERTICAL, 100, 196, offset=40)
    second = span(2, Axis.VERTICAL, 100, 196, offset=60)
    lanes = text_lanes([first, second], [phrase("[25]", 50, 140, Orientation.UP)], S)
    assert lanes[1].inches is None and lanes[2].inches is None
    assert "neither" in lanes[1].reason


# --- sink outlines and the cut-out rule ----------------------------------------------------------


def rectangle_path(left: float, top: float, right: float, bottom: float) -> InkCurve:
    points = ((d(left), d(top)), (d(right), d(top)), (d(right), d(bottom)), (d(left), d(bottom)))
    return InkCurve(box=Box(d(left), d(top), d(right), d(bottom)), points=points, closed=True)


def rectangle_strokes(
    left: float, top: float, right: float, bottom: float, *, dashed: bool = False
) -> list[InkLine]:
    if not dashed:
        return [
            line(left, top, right, top),
            line(left, bottom, right, bottom),
            line(left, top, left, bottom),
            line(right, top, right, bottom),
        ]
    pieces: list[InkLine] = []
    for start, end, fixed, horizontal in (
        (left, right, top, True),
        (left, right, bottom, True),
        (top, bottom, left, False),
        (top, bottom, right, False),
    ):
        at = start
        while at < end:
            stop = min(at + 3, end)
            pieces.append(
                line(at, fixed, stop, fixed) if horizontal else line(fixed, at, fixed, stop)
            )
            at = stop + 2
    return pieces


SINK_WORDS = [phrase("SINK", 200, 200, Orientation.UPRIGHT)]


def test_one_outline_drawn_round_a_sink_may_be_automatic() -> None:
    page = ink([], (rectangle_path(150, 150, 260, 240),))
    (sink,) = sink_outlines(page, VIEW, SINK_WORDS, S)
    assert [outline.letter for outline in sink.outlines] == ["A"]
    judgment = cutout_judgment(sink, [phrase("SINK BY OTHERS", 400, 300, Orientation.UPRIGHT)])
    assert judgment.automatic


def test_two_outlines_and_no_word_is_held_for_a_person() -> None:
    page = ink(rectangle_strokes(160, 160, 250, 230), (rectangle_path(150, 150, 260, 240),))
    (sink,) = sink_outlines(page, VIEW, SINK_WORDS, S)
    assert [outline.letter for outline in sink.outlines] == ["A", "B"]
    assert sink.outlines[0].box.width > sink.outlines[1].box.width  # outer first
    judgment = cutout_judgment(sink, [phrase("SINK BY OTHERS", 400, 300, Orientation.UPRIGHT)])
    assert not judgment.automatic
    assert "person" in judgment.reason


def test_a_dashed_bowl_counts_as_a_second_outline() -> None:
    page = ink(
        rectangle_strokes(160, 160, 250, 230, dashed=True), (rectangle_path(150, 150, 260, 240),)
    )
    (sink,) = sink_outlines(page, VIEW, SINK_WORDS, S)
    assert len(sink.outlines) == 2
    assert sink.outlines[1].drawn_as == "dashed strokes"
    assert not cutout_judgment(sink, []).automatic


def test_two_outlines_with_printed_words_naming_the_cut_out_may_be_automatic() -> None:
    page = ink(rectangle_strokes(160, 160, 250, 230), (rectangle_path(150, 150, 260, 240),))
    (sink,) = sink_outlines(page, VIEW, SINK_WORDS, S)
    judgment = cutout_judgment(
        sink, [phrase('SINK OPENING 22" x 16"', 400, 300, Orientation.UPRIGHT)]
    )
    assert judgment.automatic
    assert judgment.naming_words


def test_a_rectangle_without_a_sink_mark_is_not_a_sink() -> None:
    page = ink([], (rectangle_path(150, 150, 260, 240),))
    assert sink_outlines(page, VIEW, [], S) == ()


def test_a_span_on_an_outlines_edges() -> None:
    outline = Outline("A", Box(d(150), d(150), d(260), d(240)), "closed path")
    assert span_on_outline(span(1, Axis.HORIZONTAL, 150, 260, offset=140), outline, S)
    assert span_on_outline(span(2, Axis.VERTICAL, 150, 240, offset=140), outline, S)
    assert not span_on_outline(span(3, Axis.HORIZONTAL, 140, 260, offset=140), outline, S)


def test_cut_out_words_about_another_hole_do_not_name_the_sinks_cut_out() -> None:
    page = ink(rectangle_strokes(160, 160, 250, 230), (rectangle_path(150, 150, 260, 240),))
    (sink,) = sink_outlines(page, VIEW, SINK_WORDS, S)
    elsewhere = phrase("TRASH CUTOUT", 450, 300, Orientation.UPRIGHT)
    assert not cutout_judgment(sink, [elsewhere]).automatic
    inside = phrase("CUT-OUT", 200, 220, Orientation.UPRIGHT)
    assert cutout_judgment(sink, [inside]).automatic
