"""The item pictures and their item map (#1322), on invented geometry only.

Verification for: `workflow/item_pictures.py`. Every row, label and drawing here is made up: a
"vanity" frame with a piece row, a duplicate of it, a row of material tags, a notes row, a row whose
only marks are stone speckle, and a second drawing beside it. Pictures are drawn by a fake renderer
(a flat page of one grey), so a test can tell what the builder painted from what the page holds. One
test runs end to end on a hand-built PDF (`tests/extraction/test_rows.py`'s stamped sheet).
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from itertools import pairwise

import pytest

from evidence.coordinates import PageTransform
from evidence.crop import encode_png
from extraction.geometry.rows import (
    MEASURED_SETTINGS,
    Box,
    CountertopRowCandidate,
    InkCharacter,
    InkLine,
    LabelKind,
    PageInk,
    PageRows,
    Slot,
    SlotLabel,
    StoredBox,
    TickSource,
)
from extraction.rows import page_rows_and_ink
from extraction.slot_reader.claude_output import visual_tokens
from workflow.item_pictures import (
    MEASURED_ITEM_SETTINGS,
    ItemFrame,
    ItemPage,
    LabelSort,
    RowReason,
    build_item_map,
    build_item_pictures,
    item_frames,
    item_pictures_enabled,
    junk_reason,
    label_sort,
    pdf_item_pictures,
    view_regions,
)

D = Decimal
SETTINGS = MEASURED_ITEM_SETTINGS
_STORED = StoredBox(D(0), D(0), D(0), D(0))


def _text(x0: float, top: float, text: str, *, width: float = 12, height: float = 6) -> SlotLabel:
    return SlotLabel(
        kind=LabelKind.TEXT,
        box=Box(D(str(x0)), D(str(top)), D(str(x0 + width)), D(str(top + height))),
        stored=_STORED,
        lines=(text,),
        strokes=0,
        touches_edge=False,
        stacked=False,
        distance_pt=D(3),
    )


def _marks(x0: float, top: float, width: float, height: float, strokes: int = 2) -> SlotLabel:
    return SlotLabel(
        kind=LabelKind.GLYPHS,
        box=Box(D(str(x0)), D(str(top)), D(str(x0 + width)), D(str(top + height))),
        stored=_STORED,
        lines=(),
        strokes=strokes,
        touches_edge=False,
        stacked=False,
        distance_pt=D(3),
    )


def _row(y: float, ticks: list[float], labels: list[SlotLabel | None]) -> CountertopRowCandidate:
    slots = []
    for index, (left, right) in enumerate(pairwise(ticks)):
        label = labels[index] if index < len(labels) else None
        slots.append(
            Slot(
                index=index,
                x0=D(str(left)),
                x1=D(str(right)),
                box=Box(D(str(left)), D(str(y - 10)), D(str(right)), D(str(y + 10))),
                stored=_STORED,
                labels=() if label is None else (label,),
            )
        )
    return CountertopRowCandidate(
        y=D(str(y)),
        ticks=tuple(D(str(x)) for x in ticks),
        tick_source=TickSource.SLASH,
        slots=tuple(slots),
        overall=None,
        findings=(),
        labelled=sum(1 for label in labels if label is not None),
        rank=None,
        rejected_because=None,
    )


def _piece_row(y: float = 300) -> CountertopRowCandidate:
    """`2 | 30 | 2` above its line: the vanity's piece row."""
    return _row(
        y,
        [100, 110, 250, 260],
        [
            _text(101, y - 9, '2"', width=8),
            _text(170, y - 9, '30"'),
            _text(251, y - 9, '2"', width=8),
        ],
    )


def _ink(width: float = 600, height: float = 500, *, drawings: tuple[Box, ...] = ()) -> PageInk:
    """A vanity drawn as a box with a few strokes, plus a far-away note, in one drawing."""
    lines = (
        # the cabinet and the stone
        InkLine(D(100), D(200), D(260), D(200)),
        InkLine(D(100), D(200), D(100), D(290)),
        InkLine(D(260), D(200), D(260), D(290)),
        InkLine(D(100), D(290), D(260), D(290)),
        InkLine(D(100), D(300), D(260), D(300)),
    )
    characters = (InkCharacter("3", Box(D(170), D(291), D(176), D(297))),)
    return PageInk(
        width=D(str(width)),
        height=D(str(height)),
        lines=lines,
        curves=(),
        characters=characters,
        drawing_boxes=drawings,
    )


# ---------------------------------------------------------------------------
# The switch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "on"), [("", False), ("0", False), ("off", False), ("1", True), ("yes", True)]
)
def test_the_switch_is_off_unless_set_to_a_yes_word(raw: str, on: bool) -> None:
    assert item_pictures_enabled({"GV_ITEM_PICTURES": raw}) is on
    assert item_pictures_enabled({}) is False


def test_an_unknown_switch_word_is_refused_not_read_as_on() -> None:
    with pytest.raises(ValueError, match="GV_ITEM_PICTURES"):
        item_pictures_enabled({"GV_ITEM_PICTURES": "maybe"})


def test_the_setting_is_off_by_default_and_read_as_the_worker_reads_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import Settings

    monkeypatch.setenv("GV_DATABASE_URL", "postgresql+psycopg://user:pw@localhost:1/db")
    monkeypatch.delenv("GV_ITEM_PICTURES", raising=False)
    assert Settings(_env_file=None).item_pictures is False  # type: ignore[call-arg]
    monkeypatch.setenv("GV_ITEM_PICTURES", "on")
    assert Settings(_env_file=None).item_pictures is True  # type: ignore[call-arg]
    monkeypatch.setenv("GV_ITEM_PICTURES", "perhaps")
    with pytest.raises(ValueError, match="GV_ITEM_PICTURES"):
        Settings(_env_file=None)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# What a label looks like, and which rows are junk
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "sort"),
    [
        ('30"', LabelSort.NUMBER),
        ("762", LabelSort.NUMBER),
        ("[24 1/2]", LabelSort.NUMBER),
        ('95"(5EQ)', LabelSort.NUMBER),
        ('4"+1" Filler', LabelSort.NUMBER),
        ("EQ", LabelSort.NUMBER),
        ("AB-12", LabelSort.TAG),
        ("QX-001.C", LabelSort.TAG),
        ("01.02.2030", LabelSort.WORDS),
        ("PROJECT NAME", LabelSort.WORDS),
        ("LED", LabelSort.WORDS),
    ],
)
def test_a_text_label_is_sorted_by_its_shape(text: str, sort: LabelSort) -> None:
    assert label_sort(_text(0, 0, text), SETTINGS) is sort


def test_pen_strokes_are_small_marks_only_against_the_drawings_own_label_size() -> None:
    """Against a drawing whose labels are about 7 pt, speckle (1.3 pt) and a faucet's outline
    (1 pt across) are small marks and a pen-stroke `10"` (6.5 pt) is not. Without a label size
    nothing is called small."""
    reference = D(7)
    assert label_sort(_marks(0, 0, 6, 1.3), SETTINGS, reference) is LabelSort.SMALL_MARKS
    assert label_sort(_marks(0, 0, 1, 5.5), SETTINGS, reference) is LabelSort.SMALL_MARKS
    assert label_sort(_marks(0, 0, 7, 6.5, 3), SETTINGS, reference) is LabelSort.CHARACTERS
    assert label_sort(_marks(0, 0, 6, 1.3), SETTINGS) is LabelSort.CHARACTERS


def test_a_drawing_pasted_small_keeps_its_numbers() -> None:
    """The same item at 0.6 of its size: its pen-stroke numbers (about 3.2 pt, under any fixed
    3.5 pt bar) are still characters against the drawing's own label size, its row is still
    boxed first, and the faucet's speckle row still ranks after it."""

    def drawing(scale: float) -> tuple[CountertopRowCandidate, CountertopRowCandidate]:
        def pt(value: float) -> float:
            return round(value * scale, 3)

        row = _row(
            pt(300),
            [pt(100), pt(110), pt(250), pt(260)],
            [
                _marks(pt(101), pt(291), pt(8), pt(5.3), 4),
                _marks(pt(170), pt(291), pt(12), pt(6.5), 5),
                _marks(pt(251), pt(291), pt(8), pt(5.3), 4),
            ],
        )
        faucet = _row(
            pt(240),
            [pt(150), pt(165), pt(180), pt(195)],
            [_marks(pt(151), pt(238), pt(5), pt(1.3)), _marks(pt(166), pt(238), pt(5), pt(1.3))],
        )
        return faucet, row

    for scale in (1.0, 0.6):
        faucet, row = drawing(scale)
        frame = ItemFrame(Box(D(0), D(0), D(600), D(500)), "pasted drawing")
        item_map = build_item_map(0, _rows(faucet, row), _ink(), [frame], SETTINGS)
        assert [boxed.rank for boxed in item_map.boxed] == [2, 1], scale
        assert [(note.rank, note.reason) for note in item_map.ranked_last] == [
            (1, RowReason.SMALL_MARKS)
        ]
        assert not item_map.row_notes


def test_a_drawing_with_rows_is_never_left_without_a_box() -> None:
    """Every row of the drawing is a word row: they are boxed anyway, in rank order, and said."""
    notes = _row(420, [100, 160, 220], [_text(110, 411, "PROJECT NAME"), _text(170, 411, "SHEET")])
    frame = ItemFrame(Box(D(0), D(0), D(600), D(500)), "pasted drawing")
    item_map = build_item_map(0, _rows(notes), _ink(), [frame], SETTINGS)
    assert [row.rank for row in item_map.boxed] == [1]
    assert any("boxed anyway" in note for note in item_map.notes)


def test_a_row_found_twice_is_dropped_as_a_duplicate() -> None:
    first = _piece_row(300)
    again = replace(_piece_row(302), ticks=tuple(t + D(1) for t in first.ticks))
    reason = junk_reason(again, [(1, first)], SETTINGS)
    assert reason is not None and reason[0] is RowReason.DUPLICATE and "ranked 1" in reason[1]


def test_rows_that_cannot_carry_printed_numbers_are_dropped_with_their_reason() -> None:
    tags = _row(400, [100, 160, 220], [_text(110, 391, "AB-12"), _text(170, 391, "AB-12")])
    notes = _row(420, [100, 160, 220], [_text(110, 411, "PROJECT NAME"), _text(170, 411, "SHEET")])
    speckle = _row(
        440,
        [100, 115, 130, 145],
        [_marks(101, 438, 5, 1.3), _marks(116, 438, 5, 1.3), _marks(131, 438, 13, 3)],
    )
    assert junk_reason(tags, [], SETTINGS)[0] is RowReason.TAGS  # type: ignore[index]
    assert junk_reason(notes, [], SETTINGS)[0] is RowReason.WORDS  # type: ignore[index]
    assert junk_reason(speckle, [], SETTINGS, D(7))[0] is RowReason.SMALL_MARKS  # type: ignore[index]


def test_only_the_rows_own_labels_count_and_an_inch_mark_belongs_to_its_number() -> None:
    """`4` | `"` | `A`: a number, its inch mark printed apart, and one letter. Not a word row.
    Words the builder found for a slot further away (another line's) do not count."""
    row = _row(
        300,
        [100, 140, 180, 220],
        [_text(110, 291, "4"), _text(150, 291, '"'), _text(190, 291, "A")],
    )
    assert junk_reason(row, [], SETTINGS) is None
    far = replace(
        row.slots[0],
        labels=(row.slots[0].labels[0], _text(110, 260, "PROJECT NAME"), _text(110, 250, "SHEET")),
    )
    assert junk_reason(replace(row, slots=(far, *row.slots[1:])), [], SETTINGS) is None


@pytest.mark.parametrize("text", ['36" REF', "C/L", "2'-0\" TYP", "[24] MIN"])
def test_a_number_with_a_qualifier_is_a_number(text: str) -> None:
    assert label_sort(_text(0, 0, text), SETTINGS) is LabelSort.NUMBER
    row = _row(300, [100, 140, 180], [_text(110, 291, text), _text(150, 291, text)])
    assert junk_reason(row, [], SETTINGS) is None


def test_a_dimension_row_is_kept_whether_its_numbers_are_text_or_pen_strokes() -> None:
    pen = _row(300, [100, 140, 180], [_marks(110, 290, 7, 6.5, 3), _marks(150, 290, 8, 5.4, 2)])
    one_tag = _row(
        300,
        [100, 140, 180, 220],
        [_text(110, 291, "610"), _text(150, 291, "AB-12"), _text(190, 291, "457")],
    )
    assert junk_reason(_piece_row(), [], SETTINGS) is None
    assert junk_reason(pen, [], SETTINGS) is None
    assert junk_reason(one_tag, [], SETTINGS) is None


# ---------------------------------------------------------------------------
# The item map: never silent
# ---------------------------------------------------------------------------


def _rows(*candidates: CountertopRowCandidate) -> PageRows:
    return PageRows(candidates=tuple(candidates), rejected=())


def test_every_candidate_is_boxed_or_listed_with_its_reason() -> None:
    frame = ItemFrame(Box(D(50), D(150), D(350), D(480)), "pasted drawing")
    piece = _piece_row(300)
    twice = _piece_row(301)
    tags = _row(400, [100, 160, 220], [_text(110, 391, "AB-12"), _text(170, 391, "AB-12")])
    outside = _row(100, [400, 450, 500], [_text(410, 91, "12"), _text(460, 91, "12")])
    rows = _rows(piece, twice, tags, outside)

    item_map = build_item_map(0, rows, _ink(), [frame], SETTINGS)

    assert [(row.number, row.rank) for row in item_map.boxed] == [(1, 1)]
    noted = {note.rank: note.reason for note in item_map.row_notes}
    assert noted == {2: RowReason.DUPLICATE, 3: RowReason.TAGS, 4: RowReason.OUTSIDE}
    assert len(item_map.boxed) + len(item_map.row_notes) == len(rows.candidates)


def test_at_most_six_rows_are_boxed_per_drawing_and_the_rest_are_listed() -> None:
    frame = ItemFrame(Box(D(0), D(0), D(600), D(500)), "pasted drawing")
    candidates = [_piece_row(60 + 40 * index) for index in range(8)]
    candidates = [
        replace(row, ticks=tuple(t + D(index * 5) for t in row.ticks))
        for index, row in enumerate(candidates)
    ]
    item_map = build_item_map(0, _rows(*candidates), _ink(), [frame], SETTINGS)

    assert [row.rank for row in item_map.boxed] == [1, 2, 3, 4, 5, 6]
    assert [(note.rank, note.reason) for note in item_map.row_notes] == [
        (7, RowReason.BEYOND),
        (8, RowReason.BEYOND),
    ]


def test_two_vendor_drawings_are_two_items_and_a_drawing_with_no_row_is_said() -> None:
    left = ItemFrame(Box(D(50), D(150), D(290), D(480)), "pasted drawing")
    right = ItemFrame(Box(D(300), D(150), D(590), D(480)), "pasted drawing")
    empty = ItemFrame(Box(D(0), D(0), D(40), D(40)), "pasted drawing")
    other = _row(350, [320, 400, 480], [_text(330, 341, '12"'), _text(410, 341, '12"')])
    item_map = build_item_map(0, _rows(_piece_row(), other), _ink(), [left, right, empty], SETTINGS)

    assert [item.number for item in item_map.items] == [1, 2]
    assert [[row.number for row in item.boxed] for item in item_map.items] == [[1], [2]]
    assert [view.tag for item in item_map.items for view in item.views] == ["V1", "V2"]
    assert any("drawing 3" in note and "no dimension row" in note for note in item_map.notes)
    assert any("2 vendor drawings" in note for note in item_map.notes)


def test_frames_are_the_pasted_drawings_not_inside_another_or_else_the_page() -> None:
    outer = Box(D(10), D(10), D(300), D(300))
    inner = Box(D(20), D(20), D(30), D(30))
    assert [frame.box for frame in item_frames(_ink(drawings=(inner, outer)))] == [outer]
    (page,) = item_frames(_ink())
    assert page.source == "page" and page.box == Box(D(0), D(0), D(600), D(500))


def test_views_are_split_at_white_bands_and_small_pieces_join_the_nearest_view() -> None:
    """A front view and, 70 pt below it, a plan: two views. A caption 6 pt under the front view
    joins it; a note far from both stays a piece of its own."""

    def square(x0: int, top: int, size: int) -> list[InkLine]:
        x1, bottom = x0 + size, top + size
        lines = [InkLine(D(x0), D(top), D(x1), D(top)), InkLine(D(x0), D(bottom), D(x1), D(bottom))]
        lines += [InkLine(D(x), D(top), D(x), D(bottom)) for x in range(x0, x1 + 1, 10)]
        return lines

    caption = [
        InkCharacter(letter, Box(D(130 + 6 * index), D(206), D(136 + 6 * index), D(212)))
        for index, letter in enumerate("FRONT")
    ]
    note = [
        InkCharacter(letter, Box(D(380 + 6 * index), D(40), D(386 + 6 * index), D(46)))
        for index, letter in enumerate("NOTES")
    ]
    ink = PageInk(
        width=D(600),
        height=D(600),
        lines=tuple(square(100, 100, 100) + square(100, 270, 100)),
        curves=(),
        characters=tuple(caption + note),
        drawing_boxes=(),
    )
    views = view_regions(ink, Box(D(50), D(30), D(420), D(400)), SETTINGS)
    assert views == (
        Box(D(380), D(40), D(410), D(46)),
        Box(D(100), D(100), D(200), D(212)),
        Box(D(100), D(270), D(200), D(370)),
    )


# ---------------------------------------------------------------------------
# The pictures
# ---------------------------------------------------------------------------

_GREY = bytes((128, 128, 128))


def _flat(box: tuple[int, int, int, int], dpi: int) -> bytes:
    """A fake renderer: the page is mid-grey everywhere, so anything white was painted out and
    anything coloured was drawn by the builder."""
    width, height = box[2] - box[0], box[3] - box[1]
    return encode_png(width, height, _GREY * (width * height))


def _blank(box: tuple[int, int, int, int], dpi: int) -> bytes:
    width, height = box[2] - box[0], box[3] - box[1]
    return encode_png(width, height, bytes((255, 255, 255)) * (width * height))


def _page(rows: PageRows, ink: PageInk) -> ItemPage:
    transform = PageTransform(
        dpi=72,
        rotation=0,
        media_box=(D(0), D(0), ink.width, ink.height),
        crop_box=(D(0), D(0), ink.width, ink.height),
    )
    return ItemPage(0, rows, ink, transform, ink.height)


def _built(render=_blank, frame_box: Box | None = None):  # type: ignore[no-untyped-def]
    frame = ItemFrame(frame_box or Box(D(50), D(150), D(350), D(480)), "pasted drawing")
    rows = _rows(_piece_row(300))
    ink = _ink()
    page = _page(rows, ink)
    item_map = build_item_map(0, rows, ink, [frame], SETTINGS)
    return build_item_pictures(page, item_map, render=render, settings=SETTINGS)


def test_the_same_page_gives_the_same_bytes() -> None:
    first, second = _built(), _built()
    assert [picture.png for picture in first.pictures] == [
        picture.png for picture in second.pictures
    ]
    assert len(first.pictures) == 1


def test_a_picture_fits_the_readers_limits_and_is_zoomed_to_them() -> None:
    (picture,) = _built().pictures
    assert max(picture.width, picture.height) <= SETTINGS.long_edge_px
    assert picture.tokens == visual_tokens(picture.width, picture.height) <= SETTINGS.front_tokens
    assert picture.dpi > 300  # a 160 pt vanity is drawn far larger than the whole sheet's ~100 dpi


def test_a_box_holds_all_its_own_labels_and_its_border_crosses_none() -> None:
    result = _built()
    (picture,) = result.pictures
    ((number, box),) = picture.boxes
    scale = picture.dpi / 72
    left, top = picture.region_px[0], picture.region_px[1]
    for slot in _piece_row(300).slots:
        assert slot.label is not None
        label = slot.label.box
        x0 = round(float(label.x0) * scale) - left + SETTINGS.gutter_px
        x1 = round(float(label.x1) * scale) - left + SETTINGS.gutter_px
        y0, y1 = round(float(label.top) * scale) - top, round(float(label.bottom) * scale) - top
        inner = (
            box[0] + SETTINGS.box_px,
            box[1] + SETTINGS.box_px,
            box[2] - SETTINGS.box_px,
            box[3] - SETTINGS.box_px,
        )
        assert inner[0] < x0 and x1 < inner[2] and inner[1] < y0 and y1 < inner[3]
    assert number == 1


def test_a_picture_shows_only_the_vendors_drawing_and_a_tag_never_covers_ink() -> None:
    """The view is cut inside its own drawing, so no other drawing on the sheet is shown. On a
    page that is grey everywhere there is no blank spot beside the box: the tag goes to the white
    margin on the left, joined to its box by a leader."""
    frame = Box(D(95), D(150), D(265), D(480))
    result = _built(render=_flat, frame_box=frame)
    (picture,) = result.pictures
    scale = picture.dpi / 72
    region = picture.region_px
    assert round(95 * scale) - 1 <= region[0] and region[2] <= round(265 * scale) + 1
    ((_number, tag, leader),) = picture.tags
    assert leader and tag[2] <= SETTINGS.gutter_px


def test_on_blank_paper_the_tag_sits_beside_its_box_without_a_leader() -> None:
    (picture,) = _built().pictures
    ((_number, tag, leader),) = picture.tags
    ((_n, box),) = picture.boxes
    assert not leader
    assert not (tag[0] < box[2] and box[0] < tag[2] and tag[1] < box[3] and box[1] < tag[3])


def _pixels(picture, box: Box) -> tuple[int, int, int, int]:  # type: ignore[no-untyped-def]
    scale = picture.dpi / 72
    left, top = picture.region_px[0], picture.region_px[1]
    return (
        round(float(box.x0) * scale) - left + SETTINGS.gutter_px,
        round(float(box.top) * scale) - top,
        round(float(box.x1) * scale) - left + SETTINGS.gutter_px,
        round(float(box.bottom) * scale) - top,
    )


def _overlap(first: tuple[int, int, int, int], second: tuple[int, int, int, int]) -> bool:
    return (
        first[0] < second[2]
        and second[0] < first[2]
        and first[1] < second[3]
        and second[1] < first[3]
    )


def test_no_tag_sits_on_a_label_and_no_border_crosses_its_own_labels() -> None:
    """Two rows close together, so the second's labels lie just under the first's box: neither
    tag covers any label, and neither border crosses its own labels."""
    upper = _piece_row(300)
    lower = _row(
        316,
        [105, 180, 255],
        [_text(130, 308, '12"'), _text(200, 308, '12"')],
    )
    frame = ItemFrame(Box(D(50), D(150), D(350), D(480)), "pasted drawing")
    rows = _rows(upper, lower)
    ink = _ink()
    item_map = build_item_map(0, rows, ink, [frame], SETTINGS)
    (picture,) = build_item_pictures(
        _page(rows, ink), item_map, render=_blank, settings=SETTINGS
    ).pictures
    labels = [
        _pixels(picture, label.box)
        for row in (upper, lower)
        for slot in row.slots
        for label in slot.labels
    ]
    for _number, tag, _leader in picture.tags:
        assert not any(_overlap(tag, label) for label in labels)
    for (number, box), row in zip(picture.boxes, (upper, lower), strict=True):
        edges = [
            (box[0], box[1], box[2], box[1] + SETTINGS.box_px),
            (box[0], box[3] - SETTINGS.box_px, box[2], box[3]),
            (box[0], box[1], box[0] + SETTINGS.box_px, box[3]),
            (box[2] - SETTINGS.box_px, box[1], box[2], box[3]),
        ]
        own = [_pixels(picture, slot.label.box) for slot in row.slots if slot.label is not None]
        assert not any(_overlap(edge, label) for edge in edges for label in own), number


def test_a_leader_tag_stays_inside_the_picture() -> None:
    """On a grey page every tag goes to the margin; a row at the very bottom of its drawing still
    gets a tag wholly inside the picture."""
    bottom = _row(470, [100, 180, 260], [_text(120, 462, '12"'), _text(200, 462, '12"')])
    frame = ItemFrame(Box(D(50), D(150), D(350), D(480)), "pasted drawing")
    rows = _rows(_piece_row(300), bottom)
    ink = _ink()
    item_map = build_item_map(0, rows, ink, [frame], SETTINGS)
    (picture,) = build_item_pictures(
        _page(rows, ink), item_map, render=_flat, settings=SETTINGS
    ).pictures
    for _number, tag, leader in picture.tags:
        assert leader
        assert 0 <= tag[0] and tag[2] <= picture.width and 0 <= tag[1] and tag[3] <= picture.height


def _three_views() -> tuple[PageRows, PageInk]:
    """Three squares far apart, each holding one dimension row: three views of one drawing."""

    def square(x0: int, top: int) -> list[InkLine]:
        lines = [InkLine(D(x0), D(y), D(x0 + 100), D(y)) for y in (top, top + 50, top + 100)]
        lines += [InkLine(D(x), D(top), D(x), D(top + 100)) for x in range(x0, x0 + 101, 10)]
        return lines

    ink = PageInk(
        width=D(600),
        height=D(600),
        lines=tuple(square(50, 50) + square(350, 50) + square(50, 350)),
        curves=(),
        characters=(),
        drawing_boxes=(),
    )
    rows = _rows(
        _row(160, [50, 100, 150], [_text(60, 151, '12"'), _text(110, 151, '12"')]),
        _row(160, [350, 400, 450], [_text(360, 151, '13"'), _text(410, 151, '13"')]),
        _row(460, [50, 100, 150], [_text(60, 451, '14"'), _text(110, 451, '14"')]),
    )
    return rows, ink


def test_one_items_pictures_stay_within_the_question_cap_and_v1_is_never_left_out() -> None:
    rows, ink = _three_views()
    frame = ItemFrame(Box(D(0), D(0), D(600), D(600)), "pasted drawing")
    item_map = build_item_map(0, rows, ink, [frame], SETTINGS)
    assert [view.tag for item in item_map.items for view in item.views] == ["V1", "V2", "V3"]

    full = build_item_pictures(_page(rows, ink), item_map, render=_blank, settings=SETTINGS)
    assert full.item_tokens(1) <= SETTINGS.question_tokens
    assert [picture.tag for picture in full.pictures] == ["V1", "V2", "V3"]

    first, second, _third = full.pictures
    cap = first.tokens + second.tokens + SETTINGS.view_minimum_tokens - 1
    tight = replace(SETTINGS, question_tokens=cap)
    capped = build_item_pictures(_page(rows, ink), item_map, render=_blank, settings=tight)
    assert capped.pictures[0].tag == "V1"
    assert capped.item_tokens(1) <= cap
    assert [picture.tag for picture in capped.pictures] == ["V1", "V2"]
    (note,) = capped.notes
    assert "V3" in note and "[3]" in note


def test_a_view_that_cannot_fit_is_refused_not_sent_oversize() -> None:
    from workflow.item_pictures import PictureTooLarge

    stubborn = replace(SETTINGS, min_dpi=2000, max_dpi=2000)
    frame = ItemFrame(Box(D(50), D(150), D(350), D(480)), "pasted drawing")
    rows, ink = _rows(_piece_row(300)), _ink()
    item_map = build_item_map(0, rows, ink, [frame], stubborn)
    with pytest.raises(PictureTooLarge, match="even at 2000 dpi"):
        build_item_pictures(_page(rows, ink), item_map, render=_blank, settings=stubborn)


def test_the_tag_typeface_is_pinned() -> None:
    """The tags are drawn in Pillow's built-in typeface (`ImageFont.load_default(size=...)`), the
    one the row picture already uses. Its pixels are pinned here: a Pillow that draws them
    differently would change every picture's bytes, and so must fail loudly, not drift."""
    import hashlib

    from workflow.item_pictures import _text_mask

    width, mask = _text_mask("0123456789V", 28)
    assert (width, hashlib.sha256(repr(mask).encode()).hexdigest()) == (
        178,
        "f024f3a89f99bf2908366ea9442e22ff31ede6cf847a224a20b2f6ae516b491d",
    )


def test_a_renderer_that_returns_the_wrong_size_is_refused() -> None:
    def wrong(box: tuple[int, int, int, int], dpi: int) -> bytes:
        return encode_png(1, 1, b"\xff\xff\xff")

    with pytest.raises(ValueError, match="renderer returned"):
        _built(render=wrong)


# ---------------------------------------------------------------------------
# End to end on a hand-built PDF: GV's coloured marks are never boxed
# ---------------------------------------------------------------------------


def test_a_pdf_page_is_pictured_and_a_red_row_is_never_boxed() -> None:
    from tests.extraction.test_rows import (
        APPEARANCE,
        NESTED_TICKS,
        _sheet,
        _slash,
    )
    from tests.extraction.test_rows import (
        _text as pdf_text,
    )

    red = (
        b"1 0 0 RG 1 0 0 rg 0.3 w 150 560 m 350 560 l S\n"
        + pdf_text(170, 564, "11")
        + pdf_text(245, 564, "22")
        + pdf_text(320, 564, "33")
        + b"".join(_slash(x, 560) for x in (150.0, 200.0, 300.0, 350.0)).replace(b" f\n", b" f\n")
    )
    data = _sheet(APPEARANCE + b"q " + red + b" Q\n", NESTED_TICKS)
    rows = page_rows_and_ink(data, 0, dpi=150, settings=MEASURED_SETTINGS)

    first = pdf_item_pictures(data, 0, rows.candidates.rows, rows.ink)
    second = pdf_item_pictures(data, 0, rows.candidates.rows, rows.ink)

    assert [row.candidate.y for row in first.item_map.boxed] == [D(150)]
    assert [picture.png for picture in first.pictures] == [
        picture.png for picture in second.pictures
    ]
    assert first.pictures and first.pictures[0].boxes
