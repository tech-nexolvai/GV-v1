"""Reading the architect's own file: drawings printed as the page's content, view by view (#1163).

Verification for: `extraction/architect/reader.read_architect_page(architect_document=True)`,
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
        data, 0, settings=SETTINGS, dpi=150, architect_document=architect_document
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
    if architect_document:
        assert view.judgment.agreed is Role.ARCH and view.judgment.by_document_kind
        assert spans["3' - 4\""].inches == Fraction(40)
        assert spans["3' - 4\""].on_outline is True
    else:
        # No heading on the shop file: no role, every value held, exactly as on any page.
        assert view.judgment.agreed is None
        assert _usable(page) == []


def test_a_pasted_drawing_reads_the_same_wherever_the_page_starts() -> None:
    plain = _read(pasted_sheet())
    shifted = _read(pasted_sheet(origin=(300, 400)))

    assert {text: span.inches for text, span in _spans(shifted).items()} == {
        text: span.inches for text, span in _spans(plain).items()
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
