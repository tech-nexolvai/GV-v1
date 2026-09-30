"""Which drawing is which on a combined sheet, read from its labels (#710).

Positions are stored ones (0..1, `y` down the page). The layouts are the client's, measured on the 13
combined pages of `AI_Set_2.pdf`; no client dimension appears here.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from evidence.coordinates import ImagePoint, StoredPoint
from evidence.polygon import Polygon
from extraction.annotations import DrawingLayer, MarkupNote, VendorStamp
from extraction.panels import propose_panel_roles

DOCUMENT = UUID("22222222-2222-4222-8222-222222222222")


def _polygon(top: str, bottom: str, left: str = "0.3", right: str = "0.9") -> Polygon:
    t, b, lft, r = (Decimal(v) for v in (top, bottom, left, right))
    return Polygon(
        points=(StoredPoint(lft, t), StoredPoint(r, t), StoredPoint(r, b), StoredPoint(lft, b)),
        space="stored",
        document_version_id=DOCUMENT,
        page=0,
    )


def _drawing(index: int, top: str, bottom: str) -> VendorStamp:
    return VendorStamp(
        annotation_index=index, extent=_polygon(top, bottom), image_extent=(ImagePoint(0, 0),)
    )


def _label(text: str, top: str, bottom: str) -> MarkupNote:
    return MarkupNote(
        layer=DrawingLayer.REVIEWER_MARKUP,
        subtype="/FreeText",
        text=text,
        author="REVIEWER-1",
        extent=_polygon(top, bottom, left="0.03", right="0.3"),
        image_extent=(ImagePoint(0, 0),),
        rotation_degrees=0,
        annotation_index=99,
    )


ID_LABEL = _label("ID SET ELEVATION ", "0.03", "0.06")
VENDOR_LABEL = _label("VENDOR'S SHOP DRAWING ELEVATION ", "0.43", "0.46")


def _roles(stamps: list[VendorStamp], notes: list[MarkupNote]) -> dict[int, str | None]:
    return {p.annotation_index: p.role for p in propose_panel_roles(stamps, notes)}


def test_each_drawing_takes_the_label_above_it() -> None:
    """The client's layout: ID label at the top, the vendor label halfway down as a divider."""
    roles = _roles(
        [_drawing(0, "0.03", "0.42"), _drawing(3, "0.47", "0.95")], [ID_LABEL, VENDOR_LABEL]
    )

    assert roles == {0: "arch", 3: "shop"}


def test_a_divider_label_touching_the_upper_drawing_does_not_move_it() -> None:
    """**The layout that broke two earlier rules.** The vendor label sits right against the bottom of
    the upper drawing, nearer to it than to the drawing it heads. It still labels what is below it.
    """
    roles = _roles(
        [_drawing(11, "0.02", "0.45"), _drawing(10, "0.44", "0.97")], [ID_LABEL, VENDOR_LABEL]
    )

    assert roles == {11: "arch", 10: "shop"}


def test_a_sheet_laid_out_the_other_way_round_is_read_the_other_way_round() -> None:
    """This reads labels, not positions. Nothing says the top drawing is the architect's."""
    vendor_first = _label("VENDOR'S SHOP DRAWING ELEVATION", "0.03", "0.06")
    id_second = _label("ID SET ELEVATION", "0.43", "0.46")

    roles = _roles(
        [_drawing(0, "0.07", "0.42"), _drawing(1, "0.47", "0.95")], [vendor_first, id_second]
    )

    assert roles == {0: "shop", 1: "arch"}


def test_a_small_drawing_in_the_vendor_section_is_the_vendor_s() -> None:
    roles = _roles(
        [_drawing(0, "0.03", "0.42"), _drawing(1, "0.47", "0.95"), _drawing(2, "0.60", "0.61")],
        [ID_LABEL, VENDOR_LABEL],
    )

    assert roles[2] == "shop"


def test_without_labels_nothing_is_suggested_and_it_says_why() -> None:
    proposals = propose_panel_roles([_drawing(0, "0.03", "0.42"), _drawing(1, "0.47", "0.95")], [])

    assert [p.role for p in proposals] == [None, None]
    assert all("label" in p.reason for p in proposals)


def test_a_drawing_with_no_label_above_it_gets_no_suggestion() -> None:
    roles = _roles([_drawing(0, "0.01", "0.02")], [ID_LABEL, VENDOR_LABEL])

    assert roles == {0: None}


def test_the_label_is_matched_whole_ignoring_case_spacing_and_curly_apostrophes() -> None:
    variants = [
        _label("id set   elevation", "0.03", "0.06"),
        _label("VENDOR’S SHOP DRAWING ELEVATION", "0.43", "0.46"),
    ]

    assert _roles([_drawing(0, "0.07", "0.42"), _drawing(1, "0.47", "0.95")], variants) == {
        0: "arch",
        1: "shop",
    }


def test_a_longer_note_that_starts_with_a_label_is_not_the_label() -> None:
    notes = [_label("ID SET ELEVATION NOTES: VERIFY ON SITE", "0.03", "0.06")]

    assert _roles([_drawing(0, "0.07", "0.42")], notes) == {0: None}


def test_two_different_labels_equally_close_suggest_nothing() -> None:
    tied = [
        _label("ID SET ELEVATION", "0.03", "0.06"),
        _label("VENDOR'S SHOP DRAWING ELEVATION", "0.03", "0.06"),
    ]

    proposals = propose_panel_roles([_drawing(0, "0.07", "0.42")], tied)

    assert proposals[0].role is None
    assert "equally close" in proposals[0].reason


def test_the_suggestion_names_the_label_it_came_from() -> None:
    proposal = propose_panel_roles([_drawing(0, "0.07", "0.42")], [ID_LABEL])[0]

    assert proposal.heading == "ID SET ELEVATION"
