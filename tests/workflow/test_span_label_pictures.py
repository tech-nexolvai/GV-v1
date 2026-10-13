"""The grounded Claude label question's pictures (#1213), on made-up pages with no model call.

Verification for `workflow/slot_reader.py`: `_row_area_png` (Picture 1, the drawing around the
chosen row with the asked span boxed in thin magenta), `_marked_close_up_png` (Picture 2, today's
close-up with a white strip above and below carrying two magenta marks at the span's ends) and
`_row_x_px` (the row's reach), and how `read_slot_pages` sends them.

**Why.** The whole page used to be sent at about 100 dpi with every label question, and its magenta
box covered narrow labels; the close-up carried no mark of where the span is, so a reader copied the
neighbour's label on a narrow piece; and a close-up wider than the readers' 2576 px limit was
refused, so some overall widths were never asked. The pictures shipped are exactly the ones the
demo-test measured (2026-10-13); a local pixel-equality proof against those pictures stays outside
the repository. Every length here is stated at 300 dpi and scaled by the render's dpi.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from decimal import Decimal
from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PIL import Image

from evidence.coordinates import PageTransform
from evidence.crop import RenderedPage, _crop_rgb, decode_rgb_png
from extraction.form_reader.bedrock import AttemptUsage
from extraction.geometry.rows import MEASURED_SETTINGS
from extraction.slot_reader.claude_output import long_edge_limit, png_size
from extraction.slot_reader.runs import E2_CROP_SETTINGS, SlotPlan, plan_slots
from tests.workflow.test_slot_reader import (
    DPI,
    OPUS,
    SONNET,
    FakeReaders,
    claude_runtime,
    named_sheet,
    runtime,
    slot_page,
)
from workflow.slot_reader import (
    SlotPage,
    _claude_span_plan,
    _crop_png,
    _marked_close_up_png,
    _pixels,
    _row_area_png,
    _row_x_px,
    _span_view_png,
    _upright_png,
    read_slot_pages,
)

MAGENTA = (230, 0, 200)
WHITE = (255, 255, 255)
GREY = (90, 90, 90)


def fake_page(width: int, height: int, *, dpi: int = 300) -> RenderedPage:
    """A grey page with a darker pixel every 7 columns, so every crop shows where it was cut."""
    rows = []
    for y in range(height):
        row = bytearray()
        for x in range(width):
            shade = 40 if x % 7 == 0 else 90 + (y % 5)
            row += bytes((shade, shade, shade))
        rows.append(bytes(row))
    return RenderedPage(
        document_version_id=uuid4(),
        page_index=0,
        page_content_hash="0" * 64,
        rotation=0,
        render_failed=False,
        width_px=width,
        height_px=height,
        dpi=dpi,
        rgb_bytes=b"".join(rows),
    )


def pixels(png: bytes) -> Image.Image:
    return Image.open(BytesIO(png)).convert("RGB")


def column_colours(image: Image.Image, x: int, rows: range) -> set[tuple[int, int, int]]:
    return {image.getpixel((x, y)) for y in rows}  # type: ignore[misc]


# ---------------------------------------------------------------------------------------------
# Picture 2: the marked close-up
# ---------------------------------------------------------------------------------------------


def test_the_close_up_keeps_the_crop_between_two_white_strips() -> None:
    """Input: a 400 x 60 px crop at 300 dpi. Outcome: the crop's own pixels, unchanged, with an
    18 px white strip above and below (400 x 96)."""
    page = fake_page(900, 400)
    crop = (100, 200, 500, 260)
    image = pixels(_marked_close_up_png(page, crop, (200, 200, 300, 260), max_long_edge=2576))

    assert image.size == (400, 96)
    middle = image.crop((0, 18, 400, 78)).tobytes()
    assert middle == _crop_rgb(page, crop)
    for y in (*range(18), *range(78, 96)):
        for x in (0, 50, 150, 250, 399):
            assert image.getpixel((x, y)) == WHITE, (x, y)


def test_the_close_up_marks_the_span_s_two_ends_above_and_below() -> None:
    """Each end gets a 5 px wide magenta mark, from the picture's edge to 2 px short of the
    drawing, in both strips; nothing else is marked."""
    page = fake_page(900, 400)
    crop = (100, 200, 500, 260)
    image = pixels(_marked_close_up_png(page, crop, (200, 200, 300, 260), max_long_edge=2576))

    for end in (100, 200):  # the span's ends, 100 and 200 px into the crop
        for x in range(end - 2, end + 3):
            assert column_colours(image, x, range(16)) == {MAGENTA}
            assert column_colours(image, x, range(16, 18)) == {WHITE}
            assert column_colours(image, x, range(78, 80)) == {WHITE}
            assert column_colours(image, x, range(80, 96)) == {MAGENTA}
        for x in (end - 3, end + 3):
            assert MAGENTA not in column_colours(image, x, range(96))
    marked = {x for x in range(400) if image.getpixel((x, 0)) == MAGENTA}
    assert marked == {*range(98, 103), *range(198, 203)}


def test_a_mark_at_the_crop_s_edge_stays_whole_inside_the_picture() -> None:
    """An end at or past the crop's edge is drawn 2 px in, so its 5 px mark is never cut."""
    page = fake_page(900, 400)
    crop = (100, 200, 500, 260)
    image = pixels(_marked_close_up_png(page, crop, (90, 200, 520, 260), max_long_edge=2576))

    marked = {x for x in range(400) if image.getpixel((x, 0)) == MAGENTA}
    assert marked == {*range(5), *range(395, 400)}


def test_a_narrow_close_up_is_shown_twice_as_large() -> None:
    """Below 300 px wide the whole marked picture is enlarged 2x; at 300 px it is not."""
    page = fake_page(900, 400)
    narrow = pixels(
        _marked_close_up_png(page, (100, 200, 399, 240), (150, 200, 200, 240), max_long_edge=2576)
    )
    exact = pixels(
        _marked_close_up_png(page, (100, 200, 400, 240), (150, 200, 200, 240), max_long_edge=2576)
    )

    assert narrow.size == (2 * 299, 2 * (40 + 36))
    assert exact.size == (300, 40 + 36)


def test_a_close_up_wider_than_the_readers_limit_is_shrunk_to_fit_never_refused() -> None:
    """A 3000 px wide crop becomes exactly the limit wide, its height shrunk by the same factor."""
    page = fake_page(3200, 300)
    crop = (100, 100, 3100, 200)
    for limit in (2576, 1568):
        image = pixels(_marked_close_up_png(page, crop, (500, 100, 2600, 200), max_long_edge=limit))
        assert image.size == (limit, round(136 * limit / 3000))
    fits = pixels(
        _marked_close_up_png(page, (0, 100, 2576, 200), (10, 100, 20, 200), max_long_edge=2576)
    )
    assert fits.size == (2576, 136), "exactly the limit wide is not shrunk"


def test_the_readers_limit_is_the_documented_long_edge() -> None:
    assert long_edge_limit(OPUS) == long_edge_limit(SONNET) == 2576
    assert long_edge_limit("anthropic.claude-some-older-model") == 1568


def test_the_close_up_s_strips_and_marks_scale_with_the_render_s_dpi() -> None:
    """At 150 dpi the strip is 9 px, a mark 3 px wide and 1 px short of the drawing."""
    page = fake_page(600, 200, dpi=150)
    crop = (100, 50, 500, 80)
    image = pixels(_marked_close_up_png(page, crop, (200, 50, 300, 80), max_long_edge=2576))

    assert image.size == (400, 30 + 18)
    assert image.crop((0, 9, 400, 39)).tobytes() == _crop_rgb(page, crop)
    marked = {x for x in range(400) if image.getpixel((x, 0)) == MAGENTA}
    assert marked == {*range(99, 102), *range(199, 202)}
    assert column_colours(image, 100, range(8)) == {MAGENTA}
    assert column_colours(image, 100, range(8, 9)) == {WHITE}
    assert column_colours(image, 100, range(39, 40)) == {WHITE}
    assert column_colours(image, 100, range(40, 48)) == {MAGENTA}


# ---------------------------------------------------------------------------------------------
# Picture 1: the area around the row
# ---------------------------------------------------------------------------------------------


def test_the_row_area_reaches_past_the_row_and_above_and_below_the_span_at_half_size() -> None:
    """Input: a 3000 x 2000 px page at 300 dpi, a row from x 1000 to 2000 and a span label band
    from y 1000 to 1100. Outcome: 150 px past each end of the row (6% of the row is less), 450 px
    above and below the band, shrunk 2x (about 150 dpi): 1300 x 1000 px -> 650 x 500 px."""
    page = fake_page(3000, 2000)
    image = pixels(_row_area_png(page, (1000, 2000), (1200, 1000, 1300, 1100)))

    assert image.size == (650, 500)


def test_a_long_row_reaches_six_percent_past_its_ends() -> None:
    page = fake_page(6000, 1200)
    image = pixels(_row_area_png(page, (500, 5500), (1000, 400, 1100, 500)))

    # 6% of 5000 px is 300 px a side, and the band reaches from the page top (not -50) to 950:
    # 5600 x 950 px, shrunk ceil(5600 / 1800) = 4 times.
    assert image.size == (5600 // 4, 950 // 4)


def test_the_row_area_stops_at_the_page_edges() -> None:
    page = fake_page(1200, 900)
    image = pixels(_row_area_png(page, (50, 1150), (100, 100, 200, 160)))

    # Left 0 (not -100), top 0 (not -350), right 1200 (not 1300), bottom 610.
    assert image.size == (1200 // 2, 610 // 2)


def test_the_span_is_boxed_in_thin_magenta_outside_its_label_band() -> None:
    """The box stands 6 px left and right and 8 px above and below the band, 6 px thick outward
    (3 px as sent); inside it the drawing is untouched."""
    page = fake_page(3000, 2000)
    image = pixels(_row_area_png(page, (1000, 2000), (1200, 1000, 1300, 1100)))

    # Area origin (850, 550). The box's inner edges: x 344 and 456, y 442 and 558, at full size;
    # its outer edges 5 px further out. Sent at half size.
    def magenta(x: int, y: int) -> bool:
        red, green, blue = image.getpixel((x, y))  # type: ignore[misc]
        return red > 180 and green < 60 and blue > 150

    middle_y = 500 // 2
    assert magenta((344 - 3) // 2, middle_y) and magenta((456 + 3) // 2, middle_y)
    assert magenta(400 // 2, (442 - 3) // 2) and magenta(400 // 2, (558 + 3) // 2)
    assert not magenta(400 // 2, middle_y), "the label inside the box stays as drawn"
    assert not magenta((344 - 12) // 2, middle_y) and not magenta((456 + 12) // 2, middle_y)
    assert not any(magenta(x, 20) for x in range(image.width)), "nothing else is marked"


def test_the_row_area_shrinks_by_two_at_least_and_more_to_stay_within_1800_px() -> None:
    page = fake_page(4400, 1500)
    image = pixels(_row_area_png(page, (150, 4250), (1000, 500, 1100, 560)))

    # 4400 px wide (both page edges), 960 px tall: ceil(4400 / 1800) = 3.
    assert image.size == (4400 // 3, 960 // 3)


def test_the_row_area_s_reach_and_shrink_scale_with_the_render_s_dpi() -> None:
    """At 150 dpi: 75 px past the row, 225 px above and below, already about 150 dpi: no shrink."""
    page = fake_page(1500, 1000, dpi=150)
    image = pixels(_row_area_png(page, (500, 1000), (600, 500, 650, 550)))

    assert image.size == (500 + 150, 50 + 450)


# ---------------------------------------------------------------------------------------------
# The row's reach, and what read_slot_pages sends
# ---------------------------------------------------------------------------------------------


def _span_plan(page_bytes: bytes) -> tuple[SlotPage, SlotPlan]:
    page = slot_page(page_bytes)
    plan = plan_slots(
        page.rows.candidates.rows,
        page.rows.ink,
        settings=E2_CROP_SETTINGS,
        row_settings=MEASURED_SETTINGS,
    )
    return page, _claude_span_plan(page, plan)


def test_a_claude_reading_with_no_readers_is_refused_before_any_picture_is_cut() -> None:
    """The close-up is sized to the readers' long-edge limit; with no reader there is no limit,
    and the reading stops with a clear reason instead of shrinking the close-up to nothing."""
    page = slot_page(named_sheet())
    readers = FakeReaders(lambda _model, _png: '2"')
    configured = claude_runtime(readers)
    configured = replace(configured, form=replace(configured.form, reader_ids=()))

    with pytest.raises(ValueError, match="needs at least one configured reader"):
        read_slot_pages([page], runtime=configured, record_attempt=lambda _a: None)
    assert not readers.requests and not readers.row_requests


def test_the_row_reaches_from_its_first_label_to_its_last_pieces_and_overall() -> None:
    page, plan = _span_plan(named_sheet())
    owners = (*plan.slots, *((plan.overall,) if plan.overall is not None else ()))
    boxes = [_pixels(page.rows, label.box, page.rendered) for o in owners for label in o.labels]

    assert _row_x_px(page, plan) == (min(b[0] for b in boxes), max(b[2] for b in boxes))


def _packet_page() -> SlotPage:
    page = slot_page(named_sheet())
    return replace(
        page,
        transform=PageTransform(
            dpi=DPI,
            rotation=0,
            media_box=(Decimal(0), Decimal(0), Decimal(612), Decimal(792)),
            crop_box=(Decimal(0), Decimal(0), Decimal(612), Decimal(792)),
        ),
    )


class MemoryStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put(self, key: str, data: BytesIO, *, content_type: str) -> SimpleNamespace:
        content = data.read()
        self.objects[key] = content
        return SimpleNamespace(sha256=hashlib.sha256(content).hexdigest())


def test_the_claude_label_question_is_shown_the_row_area_and_the_marked_close_up() -> None:
    """Every span is asked of both readers with Picture 1 = the row area and Picture 2 = the
    marked close-up, and its stored question packet holds exactly those pictures."""
    page = _packet_page()
    _, plan = _span_plan(named_sheet())
    row_x = _row_x_px(page, plan)
    expected = set()
    for owner in (*plan.slots, *((plan.overall,) if plan.overall is not None else ())):
        box = _pixels(page.rows, owner.labels[0].box, page.rendered)
        crop = _pixels(page.rows, owner.labels[0].crop, page.rendered)
        expected.add(
            (
                _row_area_png(page.rendered, row_x, box),
                _marked_close_up_png(page.rendered, crop, box, max_long_edge=2576),
            )
        )
    readers = FakeReaders(lambda _model, _png: '2"')
    attempts: list[AttemptUsage] = []
    store = MemoryStore()

    read_slot_pages(
        [page],
        runtime=claude_runtime(readers, question_packets=True),
        record_attempt=attempts.append,
        store=store,
    )

    sent = {pictures for _model, pictures in readers.span_pictures}
    assert sent == expected
    assert len(readers.span_pictures) == 2 * len(expected)
    for _model, (view, close_up) in readers.span_pictures:
        assert png_size(close_up)[0] <= 2576
        assert view != _span_view_png(page, plan.slots[0]), "never the whole page again"
    stored = {
        (
            store.objects[a.question_packet["images"]["full_view"]["storage_key"]],
            store.objects[a.question_packet["images"]["close_up"]["storage_key"]],
        )
        for a in attempts
        if a.question_packet is not None
        and "close_up" in a.question_packet["images"]
        and a.prompt_id.startswith("claude-slot-span")
    }
    assert stored == expected


def test_a_sideways_label_s_upright_picture_is_the_marked_close_up_turned() -> None:
    from tests.workflow.test_slot_reader import sideways_sheet

    page, plan = _span_plan(sideways_sheet())
    readers = FakeReaders(lambda _model, _png: '2"')

    read_slot_pages([page], runtime=claude_runtime(readers), record_attempt=lambda _a: None)

    marked = {
        _marked_close_up_png(
            page.rendered,
            _pixels(page.rows, owner.labels[0].crop, page.rendered),
            _pixels(page.rows, owner.labels[0].box, page.rendered),
            max_long_edge=2576,
        )
        for owner in (*plan.slots, *((plan.overall,) if plan.overall is not None else ()))
    }
    three = [pictures for _model, pictures in readers.span_pictures if len(pictures) == 3]
    assert three
    for _view, close_up, upright in three:
        assert close_up in marked
        assert upright == _upright_png(close_up)


def test_the_other_readers_keep_today_s_pictures() -> None:
    """The non-Claude crop question (`CROP_PROMPT`) still sends the plain close-up, and its packet
    still holds the whole marked page."""
    page = _packet_page()
    readers = FakeReaders(lambda _model, _png: '2"')
    attempts: list[AttemptUsage] = []
    store = MemoryStore()

    read_slot_pages(
        [page],
        runtime=replace(runtime(readers), question_packets=True),
        record_attempt=attempts.append,
        store=store,
    )

    plan = plan_slots(
        page.rows.candidates.rows,
        page.rows.ink,
        settings=E2_CROP_SETTINGS,
        row_settings=MEASURED_SETTINGS,
    )
    plain = {
        _crop_png(page.rendered, _pixels(page.rows, label.crop, page.rendered))
        for owner in (*plan.slots, *((plan.overall,) if plan.overall is not None else ()))
        for label in owner.labels
    }
    asked = [png for _model, png in readers.requests]
    assert asked and set(asked) <= plain
    views = {
        _span_view_png(page, owner)
        for owner in (*plan.slots, *((plan.overall,) if plan.overall is not None else ()))
    }
    label_packets = [
        a.question_packet
        for a in attempts
        if a.question_packet is not None and a.prompt_id.startswith("slot-crop")
    ]
    assert label_packets
    for packet in label_packets:
        assert store.objects[packet["images"]["full_view"]["storage_key"]] in views
        assert store.objects[packet["images"]["close_up"]["storage_key"]] in plain
    width, height, _rgb = decode_rgb_png(asked[0])
    assert width >= 1 and height >= 1
