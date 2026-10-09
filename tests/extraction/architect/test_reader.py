"""Reading the architect's dimensions on a synthetic combined sheet (#1052).

Verification for: `extraction/architect/reader.py`. The sheet is `combined_sheet.py`'s, invented;
no client value appears here.
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

import pytest

from extraction.architect.reader import (
    MEASURED_ARCHITECT_SETTINGS,
    ArchitectPage,
    ArchitectSpan,
    read_architect_page,
)
from extraction.architect.views import Role
from tests.extraction.architect.combined_sheet import combined_sheet


def _read(**kwargs: bool | str) -> ArchitectPage:
    return read_architect_page(
        combined_sheet(**kwargs), 0, settings=MEASURED_ARCHITECT_SETTINGS, dpi=150
    )


def _spans(page: ArchitectPage) -> dict[str, ArchitectSpan]:
    return {
        span.text.replace("’", "'"): span
        for row in page.rows
        for span in row.spans
        if span.text is not None
    }


@pytest.fixture(scope="module")
def page() -> ArchitectPage:
    return _read()


def test_both_judgments_agree_on_each_drawing(page: ArchitectPage) -> None:
    views = {view.annotation_index: view for view in page.views}

    assert views[1].judgment.heading_role is Role.ARCH
    assert views[1].judgment.content.role is Role.ARCH
    assert views[1].judgment.agreed is Role.ARCH
    assert views[3].judgment.heading_role is Role.SHOP
    assert views[3].judgment.content.role is Role.SHOP
    assert views[3].judgment.agreed is Role.SHOP
    assert views[1].read and not views[3].read


def test_the_scale_is_witnessed_twice_and_agrees(page: ArchitectPage) -> None:
    (view,) = [view for view in page.views if view.annotation_index == 1]

    assert view.scale_note is not None and "1/4" in view.scale_note
    assert view.scale_paper_per_real == Fraction(1, 48)
    assert view.paste_factor == Decimal(1)
    assert view.absolute_points_per_inch == Decimal("1.5")
    assert view.points_per_inch is not None
    assert abs(view.points_per_inch - Decimal("1.5")) < Decimal("0.01")


def test_the_cabinet_widths_are_read_exactly_witnessed_and_on_the_outline(
    page: ArchitectPage,
) -> None:
    spans = _spans(page)

    for text, inches in (("3' - 4\"", 40), ("2' - 2\"", 26)):
        span = spans[text]
        assert span.held_reason is None, span.held_reason
        assert span.inches == Fraction(inches)
        assert span.on_outline is True, span.outline_reason
        assert span.witness_inches is not None
        assert abs(span.witness_inches - inches) < 1


def test_a_centre_line_dimension_is_read_and_never_on_the_outline(page: ArchitectPage) -> None:
    span = _spans(page)["1' - 5\""]

    assert span.inches == Fraction(17)
    assert span.on_outline is False
    assert "centre-line" in span.outline_reason


def test_without_its_mark_a_dashed_centre_line_is_still_not_on_the_outline() -> None:
    span = _spans(_read(centre_mark=False))["1' - 5\""]

    assert span.on_outline is False
    assert "dashed" in span.outline_reason


def test_a_label_its_drawn_length_contradicts_is_held_with_the_reason(page: ArchitectPage) -> None:
    span = _spans(page)["2' - 7\""]

    assert span.printed_inches == Fraction(31)
    assert span.inches is None
    assert span.held_reason is not None and "drawn length" in span.held_reason


def test_coloured_text_is_never_an_architect_value(page: ArchitectPage) -> None:
    printed = {span.printed_inches for row in page.rows for span in row.spans}

    assert Fraction(117) not in printed
    assert all("9' - 9" not in (span.text or "") for row in page.rows for span in row.spans)


def test_spans_carry_what_the_pairing_needs(page: ArchitectPage) -> None:
    """Ticks in page points, exact inches or None, the outline answer, and the view's scale."""
    (row,) = [row for row in page.rows if len(row.spans) == 2]

    assert row.points_per_inch is not None
    assert [(span.x0_pt, span.x1_pt) for span in row.spans] == [
        (Decimal(110), Decimal(170)),
        (Decimal(170), Decimal(209)),
    ]
    assert [row.rank for row in page.rows] == list(range(1, len(page.rows) + 1))


def test_a_sheet_with_no_heading_decides_nothing_and_holds_every_value() -> None:
    page = _read(headings=False)
    (view,) = [view for view in page.views if view.annotation_index == 1]

    assert view.judgment.heading_role is None
    assert view.judgment.agreed is None
    spans = [span for row in page.rows for span in row.spans if span.text is not None]
    assert spans, "the architect's labels are still read and reported"
    assert all(span.inches is None for span in spans)
    assert all("not decided by code" in (span.held_reason or "") for span in spans)


def test_a_span_over_hatched_blocking_is_read_but_never_on_the_outline() -> None:
    """Its ends start at the floor line like a cabinet's sides; the hatching says it is not one."""
    span = _spans(_read(extras=True))["3' - 0\""]

    assert span.inches == Fraction(36)
    assert span.on_outline is False
    assert "hatched material" in span.outline_reason


def test_a_span_from_a_wall_to_a_credenzas_side_is_a_clearance() -> None:
    span = _spans(_read(extras=True))["1' - 8\""]

    assert span.inches == Fraction(20)
    assert span.on_outline is False
    assert "clearance between different things" in span.outline_reason


def test_the_cabinets_stay_on_the_outline_beside_the_hatching_and_the_wall() -> None:
    spans = _spans(_read(extras=True))

    assert spans["3' - 4\""].on_outline is True, spans["3' - 4\""].outline_reason
    assert spans["2' - 2\""].on_outline is True, spans["2' - 2\""].outline_reason


def test_a_drawing_pasted_as_a_square_annotation_is_read_like_a_stamp() -> None:
    """Some viewers paste a drawing as a `/Square` whose appearance holds the drawing's text and
    line-work. It is the same drawing: the same judgments, the same values, the same edges."""
    page = _read(architect_subtype="Square")
    views = {view.annotation_index: view for view in page.views}
    spans = _spans(page)

    assert views[1].judgment.agreed is Role.ARCH
    assert spans["3' - 4\""].inches == Fraction(40)
    assert spans["3' - 4\""].on_outline is True
    assert spans["2' - 2\""].inches == Fraction(26)


def test_a_pasted_picture_is_reported_and_never_read() -> None:
    """A `/Square` holding only an image has no text or line-work: it is listed with the reason, and
    nothing is read from it."""
    page = _read(picture=True)

    assert [picture.annotation_index for picture in page.pictures] == [4]
    assert "picture" in page.pictures[0].reason
    assert {view.annotation_index for view in page.views} == {1, 3}


def test_reviewer_markup_never_becomes_a_drawing_or_a_value() -> None:
    """A reviewer's red `/Square` over the cabinets and a typed `/FreeText` dimension: neither is a
    pasted drawing, and the note's number is never read."""
    page = _read(reviewer_marks=True, architect_subtype="Square")

    assert {view.annotation_index for view in page.views} == {1, 3}
    assert page.pictures == ()
    printed = {span.printed_inches for row in page.rows for span in row.spans}
    assert Fraction(48) not in printed
    assert all("4' - 0" not in (span.text or "") for row in page.rows for span in row.spans)
    spans = _spans(page)
    assert spans["3' - 4\""].inches == Fraction(40)
    assert spans["3' - 4\""].on_outline is True


def test_the_reader_holds_no_float(page: ArchitectPage) -> None:
    def leaves(value: object) -> list[object]:
        if isinstance(value, (tuple, list, frozenset)):
            return [leaf for item in value for leaf in leaves(item)]
        if hasattr(value, "__dataclass_fields__"):
            return [
                leaf for name in value.__dataclass_fields__ for leaf in leaves(getattr(value, name))
            ]
        return [value]

    assert not any(isinstance(leaf, float) for leaf in leaves(page))
