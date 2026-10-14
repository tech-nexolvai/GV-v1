"""Every doubt is a hold (#1256 review): this code is judgment 2 for each depth and sink value, so
whatever it cannot be sure of must hold, never allow or "read exactly". Made-up geometry."""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

import pytest

from extraction.architect.text import Orientation, TextChar
from extraction.depth.spans import (
    MEASURED_SPAN_SETTINGS,
    EndKind,
    OffsetStart,
    cutout_judgment,
    find_spans,
    front_offset_start,
    read_depth_label,
    sink_outlines,
    text_lanes,
)
from extraction.geometry.dimension_lines import Axis
from extraction.geometry.rows import Box, InkCurve
from tests.extraction.depth.test_spans import (
    SINK_WORDS,
    VIEW,
    d,
    ink,
    line,
    phrase,
    rectangle_path,
    rectangle_strokes,
    span,
    vertical_dimension,
    wall_hatch,
)

S = MEASURED_SPAN_SETTINGS


# --- H1: two outlines must never be counted as one ------------------------------------------------


@pytest.mark.parametrize("gap", [1, 2, 4, 5.9])
def test_nested_outlines_a_few_points_apart_are_two_and_held(gap: float) -> None:
    page = ink(
        rectangle_strokes(150, 150, 260, 240),
        (rectangle_path(150 + gap, 150 + gap, 260 - gap, 240 - gap),),
    )
    (sink,) = sink_outlines(page, VIEW, SINK_WORDS, S)
    assert len(sink.outlines) == 2
    assert not cutout_judgment(sink, []).code_allows


def test_a_rounded_outer_outline_drawn_as_lines_and_arcs_still_holds() -> None:
    # The outer outline's sides stop 8 pt short of its corners (arcs drawn separately), so code
    # does not see it as a rectangle; the second count, crossing strokes outward, still sees it.
    r = 8
    left, top, right, bottom = 150, 150, 260, 240
    sides = [
        line(left + r, top, right - r, top),
        line(left + r, bottom, right - r, bottom),
        line(left, top + r, left, bottom - r),
        line(right, top + r, right, bottom - r),
    ]
    page = ink(sides, (rectangle_path(165, 165, 245, 225),))
    (sink,) = sink_outlines(page, VIEW, [phrase("SINK", 205, 195, Orientation.UPRIGHT)], S)
    judgment = cutout_judgment(sink, [])
    assert not judgment.code_allows
    assert "second count" in judgment.reason


def test_one_outline_with_nothing_close_round_it_is_allowed_by_both_counts() -> None:
    page = ink([line(100, 100, 400, 100)], (rectangle_path(150, 150, 260, 240),))
    (sink,) = sink_outlines(page, VIEW, SINK_WORDS, S)
    assert sink.crossings == (1, 1, 1, 1)
    assert cutout_judgment(sink, []).code_allows


def test_one_outline_with_a_stroke_just_outside_one_side_is_held() -> None:
    page = ink([line(264, 140, 264, 250)], (rectangle_path(150, 150, 260, 240),))
    (sink,) = sink_outlines(page, VIEW, SINK_WORDS, S)
    assert not cutout_judgment(sink, []).code_allows


def test_a_corner_dot_is_not_a_drain() -> None:
    dot = InkCurve(
        box=Box(d(200), d(200), d(201), d(201)),
        points=tuple((d(200 + (i % 2)), d(200 + (i // 4) % 2)) for i in range(8)),
        closed=True,
    )
    assert sink_outlines(ink([], (rectangle_path(150, 150, 260, 240), dot)), VIEW, [], S) == ()


def drain(x: float, y: float, size: float = 10) -> InkCurve:
    points = (
        (d(x), d(y - size / 2)),
        (d(x + size / 4), d(y - size / 2 + 1)),
        (d(x + size / 2), d(y)),
        (d(x + size / 4), d(y + size / 2 - 1)),
        (d(x), d(y + size / 2)),
        (d(x - size / 4), d(y + size / 2 - 1)),
        (d(x - size / 2), d(y)),
        (d(x - size / 4), d(y - size / 2 + 1)),
    )
    return InkCurve(
        box=Box(d(x - size / 2), d(y - size / 2), d(x + size / 2), d(y + size / 2)),
        points=points,
        closed=True,
    )


def test_sinks_that_share_an_outline_are_held_as_a_whole() -> None:
    # One outer outline; the words sit inside an inner one, a drain-like mark outside it. Two marks
    # give two groups with different counts; both are one sink and both hold.
    page = ink(
        [line(100, 100, 400, 100)],
        (rectangle_path(150, 150, 260, 240), rectangle_path(160, 160, 200, 200), drain(230, 220)),
    )
    sinks = sink_outlines(page, VIEW, [phrase("SINK", 180, 180, Orientation.UPRIGHT)], S)
    assert len(sinks) == 2
    assert all(not cutout_judgment(sink, []).code_allows for sink in sinks)


# --- H2: stacked fractions are never read as whole numbers ----------------------------------------


def test_an_inch_label_with_lone_digits_beside_it_is_held() -> None:
    depth = span(1, Axis.VERTICAL, 100, 300, offset=50)
    phrases = [
        phrase('31"', 44, 200, Orientation.UP),
        phrase("3", 44, 215, Orientation.UP),
        phrase("4", 40, 215, Orientation.UP),
    ]
    lane = text_lanes([depth], phrases, S, chars=())[1]
    assert lane.inches is None


def char(text: str, x: float, top: float, height: float) -> TextChar:
    return TextChar(
        text=text,
        box=Box(d(x), d(top), d(x + height * 0.6), d(top + height)),
        orientation=Orientation.UPRIGHT,
    )


def test_a_label_whose_characters_are_not_one_size_is_held() -> None:
    # "31" then a smaller stacked "4" and the inch mark, joined into one phrase "314\"".
    across = span(1, Axis.HORIZONTAL, 100, 300, offset=60)
    chars = [
        char("3", 190, 50, 4),
        char("1", 193, 50, 4),
        char("4", 196, 52, 2.4),
        char('"', 198, 50, 4),
    ]
    label = phrase('314"', 195, 52, Orientation.UPRIGHT)
    lane = text_lanes([across], [label], S, chars=chars)[1]
    assert lane.inches is None
    assert "stacked" in lane.reason


# --- M3: millimetres may corroborate an inch value, never decide ----------------------------------


@pytest.mark.parametrize(
    ("text", "inches"),
    [("635 [25]", Fraction(25)), ("29 [1 1/8]", Fraction(9, 8)), ("[25]", Fraction(25))],
)
def test_millimetres_that_match_the_inches_corroborate(text: str, inches: Fraction) -> None:
    assert read_depth_label(text, mm_tolerance=S.mm_corroboration_mm).inches == inches


@pytest.mark.parametrize("text", ["25 [635]", "40 [1 1/8]", "700 [25]"])
def test_millimetres_that_do_not_match_the_inches_hold(text: str) -> None:
    label = read_depth_label(text, mm_tolerance=S.mm_corroboration_mm)
    assert label.inches is None
    assert label.held_reason


def test_a_separate_millimetre_line_that_does_not_match_holds_the_lane() -> None:
    depth = span(1, Axis.VERTICAL, 100, 300, offset=50)
    good = [phrase("635", 44, 200, Orientation.UP), phrase("[25]", 38, 200, Orientation.UP)]
    bad = [phrase("700", 44, 200, Orientation.UP), phrase("[25]", 38, 200, Orientation.UP)]
    assert text_lanes([depth], good, S, chars=())[1].inches == Fraction(25)
    assert text_lanes([depth], bad, S, chars=())[1].inches is None


# --- M1: an end meets only what reaches its witness line ------------------------------------------


def test_an_edge_far_along_the_same_line_is_not_what_the_end_meets() -> None:
    lines = vertical_dimension(100, 100, 300) + [line(400, 100, 560, 100)]
    (only,) = find_spans(ink(lines), VIEW, S).spans
    assert only.low_end.kind is EndKind.NOTHING


def test_a_wall_band_far_along_the_same_line_is_not_what_the_end_meets() -> None:
    lines = vertical_dimension(100, 100, 300) + wall_hatch(90, 100, 280, 598)
    (only,) = find_spans(ink(lines), VIEW, S).spans
    assert only.low_end.kind is EndKind.NOTHING


# --- M2: a front offset measured from the stone's edge is never CT007 -------------------------------


def plan_with_chain() -> list:  # type: ignore[type-arg]
    lines = (
        wall_hatch(90, 100, 60, 400)
        + [line(60, 100, 400, 100), line(60, 196, 400, 196)]
        + vertical_dimension(50, 100, 196)
    )
    for top, bottom in ((100, 150), (150, 176), (176, 196)):
        lines += vertical_dimension(80, top, bottom)
    return lines


def test_a_link_ending_at_the_stones_front_edge_starts_at_the_stone_edge() -> None:
    found = find_spans(ink(plan_with_chain()), VIEW, S)
    depth = next(s for s in found.spans if s.offset_pt == d(50))
    link = next(s for s in found.spans if s.offset_pt == d(80) and s.start_pt == d(176))
    start = front_offset_start(link, depth, S)
    assert start.kind is OffsetStart.STONE_EDGE
    assert not start.code_allows


def test_a_link_inside_the_stone_may_start_at_the_cabinet_face() -> None:
    found = find_spans(ink(plan_with_chain()), VIEW, S)
    depth = next(s for s in found.spans if s.offset_pt == d(50))
    link = next(s for s in found.spans if s.offset_pt == d(80) and s.start_pt == d(150))
    start = front_offset_start(link, depth, S)
    assert start.kind is OffsetStart.INSIDE
    assert start.code_allows


def test_with_no_wall_band_the_front_edge_is_unknown_and_held() -> None:
    lines = vertical_dimension(50, 100, 196) + vertical_dimension(80, 176, 196)
    found = find_spans(ink(lines), VIEW, S)
    depth = next(s for s in found.spans if s.offset_pt == d(50))
    link = next(s for s in found.spans if s.offset_pt == d(80))
    start = front_offset_start(link, depth, S)
    assert start.kind is OffsetStart.UNKNOWN
    assert not start.code_allows


# --- Low: a near tie is a tie -------------------------------------------------------------------


def test_a_label_almost_equally_near_two_spans_belongs_to_neither() -> None:
    first = span(1, Axis.HORIZONTAL, 100, 300, offset=50)
    second = span(2, Axis.HORIZONTAL, 100, 300, offset=60)
    label = phrase('25"', 200, Decimal("55.0001"), Orientation.UPRIGHT)  # type: ignore[arg-type]
    lanes = text_lanes([first, second], [label], S, chars=())
    assert lanes[1].inches is None and lanes[2].inches is None
