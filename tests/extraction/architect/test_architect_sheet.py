"""Reading the architect's own file: drawings printed as the page's content, view by view (#1163).

Verification for: `extraction/architect/reader.read_architect_page(on_architect_file=True)`,
`extraction/architect/page_views.find_page_views`, `extraction/architect/views.judge_by_document`
and the reader on a page whose media box does not start at (0, 0). The sheets are
`architect_sheet.py`'s, invented; no client value appears here.
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

import pdfplumber
import pytest

from extraction.architect.page_views import PageView, find_page_views
from extraction.architect.reader import (
    MEASURED_ARCHITECT_SETTINGS,
    ArchitectPage,
    ArchitectSpan,
    read_architect_page,
)
from extraction.architect.text import TextChar, orientation_of
from extraction.architect.views import Role
from extraction.geometry.rows import Box
from extraction.rows import ink_from_page
from extraction.stamp_text import drawing_ink
from tests.extraction.architect.architect_sheet import (
    PAGE_HEIGHT,
    WALL_BOTTOM,
    WALL_X,
    architect_sheet,
    pasted_sheet,
)

SETTINGS = MEASURED_ARCHITECT_SETTINGS


def _read(data: bytes, *, architect_document: bool = True) -> ArchitectPage:
    return read_architect_page(
        data, 0, settings=SETTINGS, dpi=150, on_architect_file=architect_document
    )


def _spans(page: ArchitectPage, view: int | None = None) -> dict[str, ArchitectSpan]:
    return {
        span.text.replace("’", "'"): span
        for row in page.rows
        if view is None or row.view_annotation_index == view
        for span in row.spans
        if span.text is not None
    }


def _usable(page: ArchitectPage) -> list[Fraction]:
    return [span.inches for row in page.rows for span in row.spans if span.inches is not None]


@pytest.fixture(scope="module")
def page() -> ArchitectPage:
    return _read(architect_sheet())


# --- one view ----------------------------------------------------------------------------------


def test_a_drawing_printed_as_page_content_is_one_view_with_its_title_bubble_and_scale(
    page: ArchitectPage,
) -> None:
    (view,) = page.views

    assert view.source == "content"
    assert view.annotation_index == 1
    assert view.title == "SYNTHETIC ELEVATION"
    assert view.bubble is not None and "3" in view.bubble and "ID 9.9" in view.bubble
    assert view.scale_note is not None and "1/4" in view.scale_note
    assert view.scale_paper_per_real == Fraction(1, 48)
    # Drawn on the page itself: nothing was pasted, so the printed scale holds on the page.
    assert view.paste_factor == Decimal(1)
    assert view.absolute_points_per_inch == Decimal("1.5")


def test_the_role_is_the_document_kind_and_the_content_agreeing(page: ArchitectPage) -> None:
    (view,) = page.views

    assert view.judgment.content.role is Role.ARCH
    assert view.judgment.agreed is Role.ARCH
    assert view.judgment.by_document_kind
    assert view.judgment.heading_role is None
    assert "uploaded as the architect's drawings" in view.judgment.reason


def test_its_dimensions_are_read_with_the_same_two_judgments_as_a_pasted_drawing(
    page: ArchitectPage,
) -> None:
    spans = _spans(page)

    for text, inches in (("3' - 4\"", 40), ("2' - 2\"", 26)):
        span = spans[text]
        assert span.held_reason is None, span.held_reason
        assert span.inches == Fraction(inches)
        assert span.on_outline is True, span.outline_reason
        assert span.witness_inches is not None and abs(span.witness_inches - inches) < 1
    centre = spans["1' - 5\""]
    assert centre.inches == Fraction(17) and centre.on_outline is False
    contradicted = spans["2' - 7\""]
    assert contradicted.printed_inches == Fraction(31)
    assert contradicted.inches is None
    assert contradicted.held_reason is not None and "drawn length" in contradicted.held_reason


def test_nothing_outside_the_view_is_read(page: ArchitectPage) -> None:
    """The red label is never read, and the title block's small print is not a dimension."""
    (view,) = page.views
    printed = {span.printed_inches for row in page.rows for span in row.spans}

    assert Fraction(117) not in printed
    assert all(view.box.x0 <= span.x0_pt <= view.box.x1 for row in page.rows for span in row.spans)
    # The title block sits under the view, never inside its extent.
    assert view.box.bottom < Decimal(800 - 80)


def test_the_same_page_on_the_vendors_file_reads_nothing_as_before() -> None:
    """Off the architect's own file only pasted drawings are read: unchanged."""
    page = _read(architect_sheet(), architect_document=False)

    assert page.views == () and page.rows == ()


# --- several views, and views not clearly apart ------------------------------------------------


def test_a_sheet_with_two_clearly_separated_views_gives_two_views_each_with_its_rows() -> None:
    page = _read(architect_sheet(views=2))

    assert [view.annotation_index for view in page.views] == [1, 2]
    assert [view.title for view in page.views] == [
        "SYNTHETIC ELEVATION",
        "SECOND SYNTHETIC ELEVATION",
    ]
    assert all(view.judgment.agreed is Role.ARCH for view in page.views)
    first, second = page.views
    assert first.box.x1 < second.box.x0
    for view in page.views:
        spans = _spans(page, view.annotation_index)
        assert spans["3' - 4\""].inches == Fraction(40)
        assert spans["2' - 2\""].inches == Fraction(26)
        assert all(
            view.box.x0 <= span.x0_pt <= span.x1_pt <= view.box.x1 for span in spans.values()
        )


def test_views_not_clearly_apart_decide_no_role_and_give_no_value() -> None:
    page = _read(architect_sheet(views=2, crowded=True))

    assert len(page.views) == 2
    for view in page.views:
        assert view.judgment.agreed is None
        assert "not clearly apart" in view.judgment.reason
    assert _usable(page) == []
    assert any(
        "not clearly apart" in (span.held_reason or "") for row in page.rows for span in row.spans
    )


def test_a_vendor_style_drawing_on_the_architects_file_decides_nothing() -> None:
    page = _read(architect_sheet(vendor=True))

    (view,) = page.views
    assert view.judgment.content.role is Role.SHOP
    assert view.judgment.agreed is None
    assert not view.judgment.by_document_kind
    assert _usable(page) == []


# --- a media box that does not start at (0, 0) ---------------------------------------------------


def test_a_page_with_a_shifted_media_box_is_read_the_same_without_error() -> None:
    plain = _read(architect_sheet())
    shifted = _read(architect_sheet(origin=(300, 400)))

    assert [view.title for view in shifted.views] == [view.title for view in plain.views]
    assert {text: span.inches for text, span in _spans(shifted).items()} == {
        text: span.inches for text, span in _spans(plain).items()
    }
    # Positions stay in pdfplumber's own frame for that page: x moved by the media box's left
    # edge, `top` by its offset from the page's top.
    before, after = _spans(plain)["3' - 4\""], _spans(shifted)["3' - 4\""]
    assert after.x0_pt - before.x0_pt == Decimal(300)
    assert after.label_pixels == before.label_pixels


@pytest.mark.parametrize("architect_document", [False, True])
def test_a_pasted_drawing_on_a_shifted_page_is_read_without_the_crop_crash(
    architect_document: bool,
) -> None:
    """The sheet cut out of a larger set: its media box starts at (300, 400)."""
    page = _read(pasted_sheet(origin=(300, 400)), architect_document=architect_document)

    (view,) = page.views
    assert view.source == "pasted"
    spans = _spans(page)
    assert spans["3' - 4\""].printed_inches == Fraction(40)
    assert spans["3' - 4\""].on_outline is True
    witness = spans["3' - 4\""].witness_inches
    assert witness is not None and abs(witness - 40) < 1
    # A pasted drawing keeps the heading + content rule on either file (#1163 review): no heading
    # here, and no vendor's drawing beside it, so no role and every value held.
    assert view.judgment.agreed is None
    assert not view.judgment.by_document_kind
    assert _usable(page) == []


def test_a_pasted_drawing_reads_the_same_wherever_the_page_starts() -> None:
    plain = _read(pasted_sheet())
    shifted = _read(pasted_sheet(origin=(300, 400)))

    assert {text: span.printed_inches for text, span in _spans(shifted).items()} == {
        text: span.printed_inches for text, span in _spans(plain).items()
    }
    assert shifted.views[0].box.x0 - plain.views[0].box.x0 == Decimal(300)


# --- the view finder alone -----------------------------------------------------------------------


def _page_views(data: bytes) -> tuple[PageView, ...]:
    import io

    with pdfplumber.open(io.BytesIO(data)) as document:
        pdf_page = document.pages[0]
        ink = ink_from_page(pdf_page, drawing_boxes=())
        chars = [
            TextChar(
                text=str(char["text"]),
                box=Box(
                    Decimal(str(char["x0"])),
                    Decimal(str(char["top"])),
                    Decimal(str(char["x1"])),
                    Decimal(str(char["bottom"])),
                ),
                orientation=orientation_of(char.get("matrix"), char.get("upright")),
            )
            for char in pdf_page.chars
            if drawing_ink(char) and str(char.get("text", "")).strip()
        ]
    return find_page_views(chars, ink, text=SETTINGS.text, settings=SETTINGS.views)


def test_the_view_finder_numbers_views_and_keeps_the_border_and_title_block_out() -> None:
    (view,) = _page_views(architect_sheet())

    assert view.number == 1
    assert view.separated
    extent = view.extent
    # The border runs at 20 pt from every edge; the drawing and its label block are well inside.
    assert extent.x0 > 20 and extent.top > 20 and extent.x1 < 580 and extent.bottom < 720


def test_a_scale_note_with_no_title_over_it_is_no_view_and_nothing_is_read() -> None:
    """Nothing names the drawing: no view is guessed, so no value is read."""
    data = architect_sheet(titled=False)

    assert _page_views(data) == ()
    page = _read(data)
    assert page.views == () and page.rows == ()


def test_a_wall_drawn_a_little_past_the_title_is_still_the_drawings() -> None:
    (view,) = _page_views(architect_sheet(wall=True))

    assert view.extent.x1 >= WALL_X
    assert view.extent.bottom >= PAGE_HEIGHT - WALL_BOTTOM


# --- review fixes (#1163 review) -----------------------------------------------------------------


def test_an_approval_stamp_on_an_architect_page_does_not_hide_its_drawing() -> None:
    """A stamp holding no dimension (an approval stamp, a seal) is not a pasted drawing: the page's
    own content is still read, and the stamp is noted."""
    page = _read(architect_sheet(approval_stamp=True))

    (view,) = page.views
    assert view.source == "content" and view.judgment.agreed is Role.ARCH
    spans = _spans(page)
    assert spans["3' - 4\""].inches == Fraction(40)
    assert spans["2' - 2\""].inches == Fraction(26)
    assert any("holds no dimension label and no scale" in note for note in page.notes)


def test_a_page_with_no_view_says_why() -> None:
    page = _read(architect_sheet(titled=False))

    assert page.views == ()
    assert any(note.startswith("no view found") for note in page.notes)


def test_a_drawing_joined_to_the_sheet_border_is_said_never_dropped_silently() -> None:
    page = _read(architect_sheet(joined_border=True))

    assert _usable(page) == []
    assert any("border" in note and "joined" in note for note in page.notes)


def test_a_combined_set_on_the_architects_slot_keeps_the_heading_and_content_rule() -> None:
    """The same combined set uploaded in the architect's slot as other bytes: its pasted drawings
    are decided by their headings and content, never by the slot, and nothing else is read."""
    from tests.extraction.architect.combined_sheet import combined_sheet

    data = combined_sheet()
    plain = _read(data, architect_document=False)
    slot = _read(data, architect_document=True)

    assert [(v.annotation_index, v.judgment.agreed) for v in slot.views] == [
        (v.annotation_index, v.judgment.agreed) for v in plain.views
    ]
    assert not any(view.judgment.by_document_kind for view in slot.views)
    assert all(view.source == "pasted" for view in slot.views)
    assert {t: s.inches for t, s in _spans(slot).items()} == {
        t: s.inches for t, s in _spans(plain).items()
    }


@pytest.mark.parametrize("origin", [(0, 0), (300, 400)])
def test_a_crop_box_inside_the_media_box_reads_the_same(origin: tuple[int, int]) -> None:
    plain = _read(architect_sheet(origin=origin))
    cropped = _read(architect_sheet(origin=origin, crop=(10, 15, 5, 8)))

    assert {t: s.inches for t, s in _spans(cropped).items()} == {
        t: s.inches for t, s in _spans(plain).items()
    }
    assert [(s.x0_pt, s.x1_pt) for s in _spans(cropped).values()] == [
        (s.x0_pt, s.x1_pt) for s in _spans(plain).values()
    ]


def test_a_pasted_drawing_under_an_offset_crop_box_is_found_in_its_box() -> None:
    """The crop box 60 pt below the media box's top: the stamp's rectangle (measured from the crop
    box) and the page's ink must be in one frame, or the drawing's rows fall outside its box."""
    plain = _read(pasted_sheet(), architect_document=False)
    cropped = _read(pasted_sheet(crop=(10, 15, 5, 60)), architect_document=False)

    assert {t: s.printed_inches for t, s in _spans(cropped).items()} == {
        t: s.printed_inches for t, s in _spans(plain).items()
    }
    assert _spans(cropped)


def test_notes_printed_beside_a_view_are_not_part_of_it() -> None:
    from tests.extraction.architect.architect_sheet import NOTES_X

    page = _read(architect_sheet(notes=True))

    (view,) = page.views
    assert view.box.x1 < NOTES_X
    assert "2' - 0\"" not in _spans(page)
    assert any("standing apart beside a view" in note for note in page.notes)


def test_a_title_block_title_over_its_scale_field_is_not_a_view() -> None:
    page = _read(architect_sheet(title_block_note=True))

    assert [view.title for view in page.views] == ["SYNTHETIC ELEVATION"]
    assert any(note.startswith("not a view: 'DRAWING TITLE'") for note in page.notes)


def test_drawing_standing_right_above_another_views_title_is_not_taken() -> None:
    page = _read(architect_sheet(stacked=True))

    assert len(page.views) == 2
    for view in page.views:
        assert view.judgment.agreed is None
        assert "not clearly apart" in view.judgment.reason
    assert _usable(page) == []


def test_a_sheet_printed_at_half_size_holds_every_value_and_says_so() -> None:
    page = _read(architect_sheet(printed_at=0.5))

    (view,) = page.views
    assert view.points_per_inch is None
    assert "printed at 50.0% of its stated scale" in view.scale_reason
    assert "paste factor" not in view.scale_reason
    assert _usable(page) == []
    assert all(
        "printed at 50.0%" in (span.held_reason or "")
        for row in page.rows
        for span in row.spans
        if span.text is not None
    )


def test_a_7pt_tick_slash_is_a_tick() -> None:
    spans = _spans(_read(architect_sheet(ticks="big")))

    assert spans["3' - 4\""].inches == Fraction(40)
    assert spans["2' - 2\""].inches == Fraction(26)


def test_an_arrowhead_is_never_a_tick() -> None:
    """Arrowheads 7 by 3 pt at each end: within the size limit, but lying along the row."""
    page = _read(architect_sheet(ticks="arrow"))

    arrow_ends = {
        Decimal(str(x)) + Decimal(d)
        for x in (110, 170, 209, 135.5, 250, 286)
        for d in ("-3.5", "3.5")
    }
    assert not any(tick in arrow_ends for row in page.rows for tick in row.ticks)
    assert all(
        span.inches == span.printed_inches
        for row in page.rows
        for span in row.spans
        if span.inches is not None
    )


def test_a_combined_sheet_in_the_architects_slot_gives_its_own_drawing_no_role() -> None:
    """A vendor's sheet whose own drawing is page content (feet and inches, an architectural
    scale) beside the architect's drawing pasted under its "ID SET" heading, uploaded in the
    architect's slot as other bytes: the pasted drawing keeps its heading's role, the page's own
    drawing gets none from the slot."""
    page = _read(architect_sheet(pasted_with_heading=True))

    pasted = [view for view in page.views if view.source == "pasted"]
    content = [view for view in page.views if view.source == "content"]
    assert [view.judgment.agreed for view in pasted] == [Role.ARCH]
    assert len(content) == 1
    (own,) = content
    assert own.judgment.content.role is Role.ARCH  # its content alone would say the architect's
    assert own.judgment.agreed is None and not own.judgment.by_document_kind
    assert "combined sheet" in own.judgment.reason
    assert all(
        span.inches is None
        for row in page.rows
        if row.view_source == "content"
        for span in row.spans
    )


def test_page_notes_carry_their_kind() -> None:
    assert [note.kind for note in _read(architect_sheet(titled=False)).notes] == ["no_view_found"]
    stamped = _read(architect_sheet(approval_stamp=True)).notes
    assert "stamp_not_drawing" in [note.kind for note in stamped]
