"""Which drawing is which on a combined sheet, read from its labels (#710).

Positions are stored ones (0..1, `y` down the page). The layouts are the client's, measured on the 13
combined pages of `AI_Set_2.pdf`; no client dimension appears here.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest

from evidence.coordinates import ImagePoint, StoredPoint
from evidence.polygon import Polygon
from extraction.annotations import DrawingLayer, MarkupNote, VendorStamp
from extraction.panels import printed_headings, propose_panel_roles, read_panels
from vocabulary.panel_headings import HeadingMeaning, looks_like_heading
from vocabulary.panel_headings import heading_meaning as vocabulary_meaning

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


# --- #1193: headings read by meaning, never borrowed ------------------------------------------------
# Synthetic labels and layouts only. 24 pt is the size GV types its headings at; 8 pt its notes.

HEADING_PT = Decimal(24)
NOTE_PT = Decimal(8)


def _typed(
    text: str,
    top: str,
    bottom: str,
    *,
    left: str = "0.03",
    right: str = "0.3",
    size: Decimal | None = HEADING_PT,
    index: int = 99,
) -> MarkupNote:
    return MarkupNote(
        layer=DrawingLayer.REVIEWER_MARKUP,
        subtype="/FreeText",
        text=text,
        author="REVIEWER-1",
        extent=_polygon(top, bottom, left=left, right=right),
        image_extent=(ImagePoint(0, 0),),
        rotation_degrees=0,
        annotation_index=index,
        font_size=size,
    )


def _placed(index: int, top: str, bottom: str, left: str, right: str) -> VendorStamp:
    return VendorStamp(
        annotation_index=index,
        extent=_polygon(top, bottom, left=left, right=right),
        image_extent=(ImagePoint(0, 0),),
    )


@pytest.mark.parametrize(
    ("wording", "role"),
    [
        ("VENDOR'S SHOP DRAWING ELEVATION", "shop"),
        ("VENDOR'S SHOP DRAWING", "shop"),
        ("VENDOR SHOP DRAWING", "shop"),
        ("vendors shop drawings", "shop"),
        ("SHOP DRAWING", "shop"),
        ("SHOP DRAWING ELEVATION", "shop"),
        ("VENDOR’S SHOP DRAWING", "shop"),
        ("ID SET ELEVATION", "arch"),
        ("ID SET", "arch"),
        ("ARCH SET", "arch"),
        ("ARCH SET ELEVATION", "arch"),
        ("ARCHITECTURAL ELEVATION", "arch"),
        ("BRAND PROTOTYPE", "reference"),
        ("BRAND PROTOTYPE ELEVATION", "reference"),
    ],
)
def test_each_heading_wording_is_read_by_its_meaning(wording: str, role: str) -> None:
    """GV writes the same heading several ways; each is read by what it means, not word for word."""
    upper = _drawing(0, "0.07", "0.42")
    lower = _drawing(1, "0.47", "0.95")
    notes = [_typed("ID SET ELEVATION", "0.03", "0.06"), _typed(wording, "0.43", "0.46")]

    assert _roles([upper, lower], notes)[1] == role


@pytest.mark.parametrize(
    "note",
    [
        "ID SET ELEVATION NOTES",
        "VENDOR TO VERIFY",
        "STONE TOP by Vendor",
        "led strip by Vendor",
        "SEE SHOP DRAWING",
        "SHOP DRAWING NOTES: VERIFY",
    ],
)
def test_a_note_is_never_taken_for_a_heading(note: str) -> None:
    """A note that only contains heading words is not a heading: alone above a drawing it suggests
    nothing, and between two headed drawings it changes neither."""
    alone = [_typed(note, "0.03", "0.06")]
    assert vocabulary_meaning(note) is None
    assert _roles([_drawing(0, "0.07", "0.42")], alone) == {0: None}

    page = [ID_LABEL, VENDOR_LABEL, _typed(note, "0.70", "0.72", size=NOTE_PT)]
    assert _roles([_drawing(0, "0.07", "0.42"), _drawing(1, "0.47", "0.95")], page) == {
        0: "arch",
        1: "shop",
    }


def test_a_heading_wording_printed_at_note_size_is_a_note() -> None:
    """A small `SHOP DRAWING` written on the architect's half is a note GV wrote, not a divider."""
    notes = [
        _typed("ID SET ELEVATION", "0.03", "0.06"),
        _typed("SHOP DRAWING", "0.30", "0.32", size=NOTE_PT),
        _typed("VENDOR SHOP DRAWING", "0.43", "0.46"),
    ]

    assert _roles([_drawing(0, "0.07", "0.42"), _drawing(1, "0.47", "0.95")], notes) == {
        0: "arch",
        1: "shop",
    }


def test_small_capital_notes_inside_a_drawing_are_not_headings() -> None:
    notes = [
        ID_LABEL,
        VENDOR_LABEL,
        _typed("CABINET CUTOUT", "0.60", "0.62", left="0.5", right="0.6", size=NOTE_PT),
        _typed("ELEVATION-B", "0.96", "0.97", left="0.4", right="0.5", size=NOTE_PT),
    ]

    layout = read_panels([_drawing(0, "0.07", "0.42"), _drawing(1, "0.47", "0.95")], notes)

    assert [p.role for p in layout.proposals] == ["arch", "shop"]
    assert {" ".join(note.text.split()) for note in layout.headings} == {
        "ID SET ELEVATION",
        "VENDOR'S SHOP DRAWING ELEVATION",
    }


def test_a_drawing_whose_own_heading_is_unknown_gets_no_role_and_borrows_nothing() -> None:
    """**The AI Set 3 failure.** The lower drawing's own heading is a wording nobody listed; it must
    not take the architect's heading from the top of the page."""
    notes = [_typed("ID SET ELEVATION", "0.03", "0.06"), _typed("VENDR SHOP DWG", "0.43", "0.46")]

    upper, lower = propose_panel_roles(
        [_drawing(0, "0.07", "0.42"), _drawing(1, "0.47", "0.95")], notes
    )

    assert upper.role == "arch"
    assert lower.role is None
    assert lower.heading == "VENDR SHOP DWG" and lower.known is False
    assert "'VENDR SHOP DWG'" in lower.reason and "not one we know" in lower.reason
    assert lower.place == "the lower drawing"


def test_an_unknown_label_needs_its_size_to_count_as_a_heading() -> None:
    """A capitals label the file gives no size for is a note: an unknown heading needs evidence."""
    unsized = _typed("VENDR SHOP DWG", "0.43", "0.46", size=None)

    assert printed_headings([ID_LABEL, unsized]) == (ID_LABEL,)


def test_a_brand_prototype_is_a_reference_drawing() -> None:
    notes = [
        _typed("ARCH SET ELEVATION", "0.03", "0.06"),
        _typed("BRAND PROTOTYPE ELEVATION", "0.43", "0.46"),
    ]

    upper, lower = propose_panel_roles(
        [_drawing(0, "0.07", "0.42"), _drawing(1, "0.47", "0.95")], notes
    )

    assert (upper.role, lower.role) == ("arch", "reference")
    assert "never checked" in lower.reason


def test_captions_under_side_by_side_drawings_name_the_drawing_above_each() -> None:
    """Some sheets print each heading *under* its drawing, side by side."""
    left = _placed(0, "0.20", "0.70", "0.05", "0.45")
    right = _placed(1, "0.10", "0.72", "0.52", "0.95")
    notes = [
        _typed("ID SET ELEVATION", "0.78", "0.81", left="0.13", right="0.33"),
        _typed("VENDOR'S SHOP DRAWING", "0.78", "0.81", left="0.60", right="0.88"),
    ]

    proposals = propose_panel_roles([left, right], notes)

    assert [(p.role, p.side, p.place) for p in proposals] == [
        ("arch", "below", "the left drawing"),
        ("shop", "below", "the right drawing"),
    ]


def test_captions_under_stacked_drawings_are_not_read_as_headings_of_the_next() -> None:
    """The architect's caption sits between the two drawings, just above the vendor's: it names the
    drawing above it, because the vendor's own caption is under the vendor's drawing."""
    upper = _placed(0, "0.09", "0.36", "0.22", "0.70")
    lower = _placed(1, "0.43", "0.92", "0.20", "0.73")
    notes = [
        _typed("ID SET ELEVATION", "0.38", "0.41", left="0.35", right="0.57"),
        _typed("VENDOR SHOP DRAWING", "0.93", "0.96", left="0.33", right="0.65"),
    ]

    assert _roles([upper, lower], notes) == {0: "arch", 1: "shop"}


def test_a_single_label_between_two_drawings_is_not_clear_and_decides_nothing() -> None:
    notes = [_typed("VENDOR SHOP DRAWING", "0.43", "0.46")]

    proposals = propose_panel_roles(
        [_drawing(0, "0.07", "0.42"), _drawing(1, "0.47", "0.95")], notes
    )

    assert [p.role for p in proposals] == [None, None]
    assert all("not clear" in p.reason for p in proposals)


def test_the_client_s_combined_layout_is_read_as_before() -> None:
    """AI Set 2's layout at its real sizes: the architect's heading in the top corner, the vendor's
    halfway down, a small mark pasted inside the vendor's drawing. Same roles as before #1193."""
    stamps = [
        _placed(2, "0.03", "0.41", "0.29", "0.70"),
        _placed(31, "0.69", "0.705", "0.518", "0.524"),
        _placed(40, "0.45", "0.95", "0.21", "0.68"),
    ]
    notes = [
        _typed("ID SET ELEVATION", "0.027", "0.058", left="0.024", right="0.21"),
        _typed("VENDOR'S SHOP DRAWING ELEVATION", "0.432", "0.463", left="0.022", right="0.411"),
        _typed("WALL", "0.52", "0.56", left="0.56", right="0.63", size=NOTE_PT),
    ]

    layout = read_panels(stamps, notes)

    assert [(p.annotation_index, p.role, p.nested) for p in layout.proposals] == [
        (2, "arch", False),
        (31, "shop", True),
        (40, "shop", False),
    ]
    assert layout.labelled


def test_a_page_without_headings_is_not_labelled() -> None:
    layout = read_panels([_drawing(0, "0.07", "0.42")], [_typed('2 1/2"', "0.5", "0.52")])

    assert not layout.labelled
    assert printed_headings([_typed('2 1/2"', "0.5", "0.52")]) == ()


def test_the_vocabulary_knows_each_meaning_and_nothing_wider() -> None:
    assert vocabulary_meaning("vendor's shop drawing elevation") is HeadingMeaning.SHOP
    assert vocabulary_meaning("ARCH SET") is HeadingMeaning.ARCH
    assert vocabulary_meaning("BRAND PROTOTYPE") is HeadingMeaning.REFERENCE
    assert vocabulary_meaning("VENDOR") is None
    assert vocabulary_meaning("ARCH") is None
    assert looks_like_heading("VENDR SHOP DWG", HEADING_PT)
    assert not looks_like_heading("VENDR SHOP DWG", NOTE_PT)
    assert not looks_like_heading("VENDR SHOP DWG", None)
    assert not looks_like_heading("Vendor shop", HEADING_PT)
    assert not looks_like_heading("1. SECTION TITLE", HEADING_PT)


# --- #1193 review: never borrow sideways, across a drawing or across a note -------------------------
# The reviewer's probe pages (synthetic), kept as tests.


def _side_by_side() -> list[VendorStamp]:
    return [_placed(0, "0.1", "0.9", "0.02", "0.48"), _placed(1, "0.1", "0.9", "0.52", "0.98")]


def test_a_single_heading_names_only_the_drawing_it_lines_up_with_side_by_side() -> None:
    """A heading over the left drawing only: the right one is not the vendor's by borrowing."""
    left, right = propose_panel_roles(
        _side_by_side(),
        [_typed("VENDOR SHOP DRAWING", "0.03", "0.07", left="0.05", right="0.4")],
    )

    assert (left.role, right.role) == ("shop", None)
    assert right.heading is None and "lines up with the drawing beside it" in right.reason


def test_a_single_caption_names_only_the_drawing_above_it_side_by_side() -> None:
    left, right = propose_panel_roles(
        [_placed(0, "0.1", "0.8", "0.02", "0.48"), _placed(1, "0.1", "0.8", "0.52", "0.98")],
        [_typed("ID SET ELEVATION", "0.82", "0.86", left="0.1", right="0.4")],
    )

    assert (left.role, right.role) == ("arch", None)


def test_a_capitals_comment_under_the_captions_changes_nothing() -> None:
    stamps = [_placed(0, "0.1", "0.8", "0.02", "0.48"), _placed(1, "0.1", "0.8", "0.52", "0.98")]
    notes = [
        _typed("ID SET ELEVATION", "0.82", "0.86", left="0.1", right="0.4"),
        _typed("VENDOR SHOP DRAWING", "0.82", "0.86", left="0.6", right="0.9"),
        _typed("SECOND SET OF COMMENTS", "0.87", "0.9", left="0.6", right="0.9"),
    ]

    assert _roles(stamps, notes) == {0: "arch", 1: "shop"}


def test_a_heading_never_names_a_drawing_across_another_drawing() -> None:
    """One heading over two stacked drawings: it names the one right under it. The one further down
    has no heading of its own and gets no role."""
    upper, lower = propose_panel_roles(
        [_drawing(0, "0.08", "0.40"), _drawing(1, "0.39", "0.95")],
        [_typed("VENDOR'S SHOP DRAWING ELEVATION", "0.04", "0.075", left="0.3", right="0.7")],
    )

    assert (upper.role, lower.role) == ("shop", None)
    assert lower.reason.startswith("no label of its own")
    assert "sits between them" in lower.reason


UPPER_STAMP = ("0.06", "0.42")
LOWER_STAMP = ("0.49", "0.95")


@pytest.mark.parametrize(
    ("divider", "size"),
    [
        ("Architect elevation (ID set)", HEADING_PT),
        ("ID SET 2 ELEVATION", HEADING_PT),
        ("ARCHITECTS VIEW", None),
    ],
    ids=["lower-case", "with-a-digit", "unknown-without-a-size"],
)
def test_a_divider_that_is_not_a_known_heading_stops_the_heading_above(
    divider: str, size: Decimal | None
) -> None:
    """**Never across a note.** A divider written in a way we do not read as a heading is still this
    drawing's own label: the drawing gets no role, quoting it, never the heading from the top."""
    upper, lower = propose_panel_roles(
        [_drawing(0, *UPPER_STAMP), _drawing(1, *LOWER_STAMP)],
        [
            _typed("VENDOR'S SHOP DRAWING ELEVATION", "0.01", "0.04"),
            _typed(divider, "0.43", "0.47", size=size),
        ],
    )

    assert upper.role == "shop"
    assert lower.role is None
    assert lower.heading == divider and not lower.known
    assert "is not one we know" in lower.reason


@pytest.mark.parametrize(
    ("divider", "role"),
    [
        ("VENDORʼS SHOP DRAWING ELEVATION", "shop"),
        ("VENDOR＇S SHOP DRAWING", "shop"),
        ("VENDOR'S SHOP-DRAWING", "shop"),
        ("I.D. SET ELEVATION", "arch"),
        ("ID-SET ELEVATION", "arch"),
        ("ARCHITECT SET", "arch"),
        ("ARCHITECT ELEVATION", "arch"),
    ],
)
def test_more_ways_of_writing_a_heading_are_read(divider: str, role: str) -> None:
    upper, lower = propose_panel_roles(
        [_drawing(0, *UPPER_STAMP), _drawing(1, *LOWER_STAMP)],
        [_typed("BRAND PROTOTYPE", "0.01", "0.04"), _typed(divider, "0.43", "0.47")],
    )

    assert (upper.role, lower.role) == ("reference", role)


def test_headings_with_no_stated_size_are_still_read() -> None:
    notes = [
        _typed("ID SET ELEVATION", "0.01", "0.03", size=None),
        _typed("VENDOR'S SHOP DRAWING ELEVATION", "0.43", "0.46", size=None),
    ]

    assert _roles([_drawing(0, "0.03", "0.42"), _drawing(1, "0.47", "0.95")], notes) == {
        0: "arch",
        1: "shop",
    }


def test_a_single_capitals_note_makes_the_page_headed_and_says_so() -> None:
    """A lone `VERIFY` at heading size: the page counts as headed, so its drawing is not read as the
    vendor's; the drawing is never dropped silently, and the reason names the label."""
    from workflow.unread_pages import unread_page_reason

    layout = read_panels([_drawing(0, "0.10", "0.90")], [_typed("VERIFY", "0.03", "0.06")])

    assert layout.labelled
    (only,) = layout.proposals
    assert only.role is None and only.heading == "VERIFY"
    code, reason = unread_page_reason(layout, {}, has_drawings_elsewhere=True)
    assert code == "no-vendor-drawing"
    assert "this page prints labels ('VERIFY') but none is one we know" in reason
    assert "the label above the drawing ('VERIFY') is not one we know" in reason
