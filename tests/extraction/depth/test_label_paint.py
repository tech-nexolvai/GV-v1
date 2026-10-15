"""A view's painted-out label never hides a dimension the readers need (#1327). Made-up drawings.

A vendor's section prints its label (a bubble, the title, the scale note under it) close under its
own dimension. Every picture paints the label out so the readers name the view from the drawing
alone; painting the label's whole box also painted out part of that dimension's line and number.
The panel is 800 x 600 points and the picture is cut at 72 dpi, so a point is a pixel.
"""

from __future__ import annotations

import math
from decimal import Decimal
from uuid import UUID

from evidence.crop import RenderedPage, decode_rgb_png
from extraction.architect.text import Orientation, TextChar
from extraction.depth.pictures import view_picture
from extraction.depth.read import (
    MEASURED_DEPTH_READER_SETTINGS,
    PreparedView,
    prepare_panel,
)
from extraction.depth.view_kind import MEASURED_VIEW_SETTINGS, VendorView, find_vendor_views
from extraction.geometry.rows import Box, InkCharacter, InkCurve, InkLine, PageInk

S = MEASURED_DEPTH_READER_SETTINGS
PANEL = Box(Decimal(0), Decimal(0), Decimal(800), Decimal(600))


def d(value: float) -> Decimal:
    return Decimal(str(value))


def line(x0: float, y0: float, x1: float, y1: float) -> InkLine:
    return InkLine(d(x0), d(y0), d(x1), d(y1))


def word(text: str, x: float, top: float, height: float = 12) -> list[TextChar]:
    width = d(height) * Decimal("0.6")
    return [
        TextChar(
            text=character,
            box=Box(d(x) + index * width, d(top), d(x) + (index + 1) * width, d(top) + d(height)),
            orientation=Orientation.UPRIGHT,
        )
        for index, character in enumerate(text)
        if character != " "
    ]


def ring(x: float, y: float, radius: float) -> InkCurve:
    points = tuple(
        (
            d(round(x + radius * math.cos(2 * math.pi * k / 24), 2)),
            d(round(y + radius * math.sin(2 * math.pi * k / 24), 2)),
        )
        for k in range(24)
    )
    return InkCurve(Box(d(x - radius), d(y - radius), d(x + radius), d(y + radius)), points, True)


def section(
    *, dimension_at: float = 360, number_top: float = 352, label_top: float = 358
) -> tuple[list[TextChar], PageInk]:
    """A section (a box with hatching) with its width dimensioned under it (line at
    `dimension_at`, its number above the line), and its label under that: a bubble whose ring's top
    is at `label_top` holding a letter, the title, and the scale note under the title."""
    drawing = [
        line(300, 100, 500, 100),
        line(500, 100, 500, 300),
        line(500, 300, 300, 300),
        line(300, 300, 300, 100),
        *(line(300 + 10 * k, 100, 310 + 10 * k, 110) for k in range(1, 15)),
    ]
    dimension = [
        line(297.5, dimension_at, 502.5, dimension_at),
        line(300, 300, 300, dimension_at + 3),
        line(500, 300, 500, dimension_at + 3),
    ]
    centre = label_top + 14
    chars = [
        *word('18"', 395, number_top, 6),
        *word("C", 287, centre - 6, 8),
        *word("SECTION VIEW", 310, centre + 14),
        *word("1:20", 310, centre + 28),
    ]
    ink = PageInk(
        width=d(800),
        height=d(600),
        lines=tuple(drawing + dimension),
        curves=(ring(290, centre, 14),),
        characters=tuple(InkCharacter(char.text, char.box) for char in chars),
        drawing_boxes=(),
    )
    return chars, ink


def pixels(box: Box) -> tuple[int, int, int, int]:
    return int(box.x0), int(box.top), int(box.x1), int(box.bottom)


def prepare(
    chars: list[TextChar], ink: PageInk
) -> tuple[tuple[PreparedView, ...], tuple[str, ...]]:
    return prepare_panel(
        chars=chars,
        ink=ink,
        panel_box=PANEL,
        panel=1,
        page_index=0,
        pixels=pixels,
        settings=S,
    )


def the_view(chars: list[TextChar], ink: PageInk) -> VendorView:
    (view,) = find_vendor_views(chars, ink, PANEL, MEASURED_VIEW_SETTINGS).views
    return view


def test_a_title_over_scale_label_names_where_its_own_lettering_is() -> None:
    chars, ink = section()
    view = the_view(chars, ink)

    assert view.found_by == "title over scale"
    title, scale, bubble = view.lettering
    assert (title.x0, title.top, title.bottom) == (d(310), d(386), d(398))  # SECTION VIEW
    assert (scale.x0, scale.top, scale.bottom) == (d(310), d(400), d(412))  # 1:20
    assert (bubble.x0, bubble.top, bubble.bottom) == (d(287), d(366), d(374))  # the bubble's C
    # The whole label box is unchanged: the ring's top down to the scale note's bottom.
    assert view.heading_box is not None
    assert (view.heading_box.top, view.heading_box.bottom) == (d(358), d(412))


def test_a_label_clear_of_every_dimension_is_painted_whole_as_before() -> None:
    chars, ink = section(dimension_at=330, number_top=322)
    view = the_view(chars, ink)
    (prepared,), notes = prepare(chars, ink)

    assert prepared.panel_headings == (view.heading_box,)
    assert prepared.view.cut_apart
    assert not any("label" in note for note in notes)


def test_a_label_box_over_the_views_own_number_paints_only_its_lettering() -> None:
    # The ring's top (358) is above the dimension line (360) and the number's bottom (358): the
    # label's whole box takes in part of both.
    chars, ink = section()
    view = the_view(chars, ink)
    (prepared,), notes = prepare(chars, ink)

    assert prepared.panel_headings == view.lettering
    assert prepared.view.cut_apart and prepared.code_kind.kind is not None
    (note,) = [note for note in notes if "lettering" in note]
    assert "the printed characters '18'" in note and "dimension 1 of view 1" in note

    # In the picture, the number stays and the title is gone.
    rgb = bytearray(b"\xff" * (800 * 600 * 3))
    for x0, y0, x1, y1 in ((395, 352, 405, 358), (310, 386, 396, 398)):
        for y in range(y0, y1):
            for x in range(x0, x1):
                rgb[(y * 800 + x) * 3 : (y * 800 + x) * 3 + 3] = b"\x00\x00\x00"
    rendered = RenderedPage(
        document_version_id=UUID(int=1),
        page_index=0,
        page_content_hash="a" * 64,
        rotation=0,
        render_failed=False,
        width_px=800,
        height_px=600,
        dpi=72,
        rgb_bytes=bytes(rgb),
    )
    picture = view_picture(
        rendered,
        (250, 80, 550, 430),
        [pixels(box) for box in prepared.panel_headings],
        pad=3,
        max_side=1800,
    )
    width, _height, got = decode_rgb_png(picture)

    def at(x: int, y: int) -> bytes:
        return got[((y - 80) * width + (x - 250)) * 3 : ((y - 80) * width + (x - 250)) * 3 + 3]

    assert at(398, 356) == b"\x00\x00\x00"  # the number
    assert at(350, 392) == b"\xff\xff\xff"  # the title


def test_the_pictures_pad_counts_a_number_just_above_the_label_box() -> None:
    # The number's bottom is 2 points above the ring's top: clear of the box itself, but not of
    # the box grown by the whole-view picture's pad (3 pixels here).
    chars, ink = section(dimension_at=355.5, number_top=348)
    view = the_view(chars, ink)
    (prepared,), _notes = prepare(chars, ink)

    assert prepared.panel_headings == view.lettering


def test_a_dimension_through_the_title_holds_the_view() -> None:
    # The dimension line runs through the title's words: painting even the lettering hides it.
    chars, ink = section(dimension_at=392, number_top=330)
    view = the_view(chars, ink)
    (prepared,), notes = prepare(chars, ink)

    assert prepared.panel_headings == (view.heading_box,)
    assert not prepared.view.cut_apart
    assert prepared.code_kind.kind is None
    assert "cannot be painted out of the pictures without hiding" in prepared.code_kind.reason
    assert any("view(s) 1 held, nothing on them is read" in note for note in notes)


def test_a_digit_in_the_corner_of_the_bubbles_square_is_not_its_lettering() -> None:
    # A neighbour's digit stands inside the square round the bubble but outside its ring: it is
    # no letter of the bubble, so it is never painted as one and stays in the pictures.
    chars, ink = section()
    digit = word("5", 276, 358, 6)
    chars = [*chars, *digit]
    ink = PageInk(
        width=ink.width,
        height=ink.height,
        lines=ink.lines,
        curves=ink.curves,
        characters=(*ink.characters, *(InkCharacter(c.text, c.box) for c in digit)),
        drawing_boxes=(),
    )
    view = the_view(chars, ink)
    (prepared,), notes = prepare(chars, ink)

    _title, _scale, bubble = view.lettering
    assert (bubble.x0, bubble.top, bubble.bottom) == (d(287), d(366), d(374))  # the C alone
    assert prepared.panel_headings == view.lettering
    (note,) = [note for note in notes if "lettering" in note]
    assert "5" in note.split("printed characters ")[1].split(" and ")[0]


def test_a_label_box_over_only_a_dimensions_end_paints_only_its_lettering() -> None:
    # The dimension's left end (its witness line and tick) lies inside the label's box, beside the
    # bubble; its number is printed well clear. That end is what says where the dimension stops.
    chars, ink = section(dimension_at=362, number_top=330)
    view = the_view(chars, ink)
    (prepared,), notes = prepare(chars, ink)

    assert prepared.panel_headings == view.lettering
    assert prepared.view.cut_apart
    (note,) = [note for note in notes if "lettering" in note]
    assert "an end of dimension 1 of view 1" in note
