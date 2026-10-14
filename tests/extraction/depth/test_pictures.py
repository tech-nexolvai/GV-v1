"""The depth reader's pictures (#1256): what is painted where, on a made-up page."""

from __future__ import annotations

import hashlib
from uuid import UUID

from evidence.crop import RenderedPage, decode_rgb_png
from extraction.depth.pictures import (
    SPAN_COLOURS,
    MarkedSpan,
    both_turns,
    end_marks_picture,
    lettered_outlines_picture,
    numbered_spans_picture,
    upright_closeup,
    view_picture,
)
from extraction.geometry.dimension_lines import Axis

WIDTH, HEIGHT = 200, 120
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)


def page(black: list[tuple[int, int, int, int]] | None = None) -> RenderedPage:
    pixels = bytearray(bytes(WHITE) * WIDTH * HEIGHT)
    for x0, y0, x1, y1 in black or []:
        for y in range(y0, y1):
            for x in range(x0, x1):
                index = (y * WIDTH + x) * 3
                pixels[index : index + 3] = bytes(BLACK)
    return RenderedPage(
        document_version_id=UUID(int=1),
        page_index=0,
        page_content_hash=hashlib.sha256(b"made-up page").hexdigest(),
        rotation=0,
        render_failed=False,
        width_px=WIDTH,
        height_px=HEIGHT,
        dpi=100,
        rgb_bytes=bytes(pixels),
    )


def pixel(png: bytes, x: int, y: int) -> tuple[int, int, int]:
    width, _height, rgb = decode_rgb_png(png)
    index = (y * width + x) * 3
    return rgb[index], rgb[index + 1], rgb[index + 2]


def size(png: bytes) -> tuple[int, int]:
    width, height, _rgb = decode_rgb_png(png)
    return width, height


def test_the_view_picture_hides_the_heading_and_keeps_the_drawing() -> None:
    rendered = page([(20, 90, 60, 100), (100, 20, 104, 80)])  # a heading, and a drawn line
    png = view_picture(rendered, (0, 0, WIDTH, HEIGHT), [(20, 90, 60, 100)], pad=2, max_side=1000)
    assert pixel(png, 30, 95) == WHITE
    assert pixel(png, 101, 50) == BLACK


def test_numbered_spans_are_drawn_along_their_own_axis_in_their_colours() -> None:
    rendered = page()
    vertical = MarkedSpan(1, Axis.VERTICAL, (150, 20), (150, 100))
    horizontal = MarkedSpan(2, Axis.HORIZONTAL, (20, 60), (120, 60))
    png = numbered_spans_picture(
        rendered,
        (0, 0, WIDTH, HEIGHT),
        [vertical, horizontal],
        headings=[],
        thickness=3,
        badge=12,
        max_side=1000,
    )
    assert pixel(png, 150, 90) == SPAN_COLOURS[0][1]
    assert pixel(png, 110, 60) == SPAN_COLOURS[1][1]
    # Off both lines and their badges: untouched.
    assert pixel(png, 60, 100) == WHITE


def test_end_marks_lie_across_a_vertical_spans_axis_at_its_top_and_bottom() -> None:
    rendered = page()
    span = MarkedSpan(1, Axis.VERTICAL, (100, 20), (100, 100))
    png = end_marks_picture(
        rendered, (0, 0, WIDTH, HEIGHT), span, thickness=2, reach=8, max_side=1000
    )
    assert pixel(png, 107, 20) != WHITE  # across the axis at the top end
    assert pixel(png, 93, 100) != WHITE  # and at the bottom end
    assert pixel(png, 100, 60) == WHITE  # never along the span's middle


def test_end_marks_lie_across_a_horizontal_spans_axis_at_its_left_and_right() -> None:
    rendered = page()
    span = MarkedSpan(1, Axis.HORIZONTAL, (20, 60), (180, 60))
    png = end_marks_picture(
        rendered, (0, 0, WIDTH, HEIGHT), span, thickness=2, reach=8, max_side=1000
    )
    assert pixel(png, 20, 53) != WHITE
    assert pixel(png, 180, 67) != WHITE
    assert pixel(png, 100, 60) == WHITE


def test_an_upright_close_up_turns_either_way() -> None:
    # An L-shaped mark, so the two turns differ.
    rendered = page([(10, 10, 14, 40), (10, 36, 30, 40)])
    label = (5, 5, 35, 45)
    up = upright_closeup(rendered, label, rotation=90)
    down = upright_closeup(rendered, label, rotation=270)
    assert size(up) == size(down) == (40, 30)
    assert up != down
    assert both_turns(rendered, label) == (up, down)


def test_lettered_outlines_are_framed_in_their_colours() -> None:
    rendered = page()
    png = lettered_outlines_picture(
        rendered,
        (0, 0, WIDTH, HEIGHT),
        [("A", (40, 20, 160, 100)), ("B", (60, 40, 140, 80))],
        headings=[],
        thickness=2,
        badge=12,
        max_side=1000,
    )
    assert pixel(png, 100, 20) == SPAN_COLOURS[0][1]
    assert pixel(png, 100, 79) == SPAN_COLOURS[1][1]
    assert pixel(png, 100, 60) == WHITE


def test_pictures_are_the_same_bytes_every_time_and_shrink_to_the_limit() -> None:
    rendered = page([(20, 20, 60, 60)])
    first = view_picture(rendered, (0, 0, WIDTH, HEIGHT), [], pad=0, max_side=100)
    again = view_picture(rendered, (0, 0, WIDTH, HEIGHT), [], pad=0, max_side=100)
    assert first == again
    assert max(size(first)) <= 100


def test_a_badge_never_covers_the_middle_where_the_vendor_prints_the_number() -> None:
    # The vendor's label for a vertical dimension sits left of the line at its middle.
    rendered = page([(130, 55, 146, 65)])
    span = MarkedSpan(1, Axis.VERTICAL, (150, 20), (150, 100))
    png = numbered_spans_picture(
        rendered, (0, 0, WIDTH, HEIGHT), [span], headings=[], thickness=3, badge=12, max_side=1000
    )
    assert pixel(png, 138, 60) == BLACK


def test_the_spans_and_outlines_pictures_hide_every_heading_too() -> None:
    rendered = page([(20, 100, 60, 110)])  # a heading under the drawing
    heading = [(20, 100, 60, 110)]
    spans = numbered_spans_picture(
        rendered, (0, 0, WIDTH, HEIGHT), [], headings=heading, thickness=3, badge=12, max_side=1000
    )
    outlines = lettered_outlines_picture(
        rendered, (0, 0, WIDTH, HEIGHT), [], headings=heading, thickness=2, badge=12, max_side=1000
    )
    assert pixel(spans, 40, 105) == WHITE
    assert pixel(outlines, 40, 105) == WHITE
