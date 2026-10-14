"""A view's kind as code judges it, and the views on a vendor panel (#1256). Made-up values."""

from __future__ import annotations

from decimal import Decimal

import pytest

from extraction.architect.text import Orientation, TextChar
from extraction.depth.spans import MEASURED_SPAN_SETTINGS, WallBand, find_spans
from extraction.depth.view_kind import (
    MEASURED_VIEW_SETTINGS,
    ViewKind,
    code_view_kind,
    find_vendor_views,
    kind_from_heading,
)
from extraction.geometry.dimension_lines import Axis
from extraction.geometry.rows import Box, InkLine, PageInk
from vocabulary.page_types import PageType


def d(value: float) -> Decimal:
    return Decimal(str(value))


def line(x0: float, y0: float, x1: float, y1: float) -> InkLine:
    return InkLine(d(x0), d(y0), d(x1), d(y1))


def ink(lines: list[InkLine]) -> PageInk:
    return PageInk(
        width=d(800), height=d(600), lines=tuple(lines), curves=(), characters=(), drawing_boxes=()
    )


# --- heading words -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("heading", "kind"),
    [
        ("A PLAN VIEW", ViewKind.PLAN),
        ("TOP PLAN", ViewKind.PLAN),
        ("B FRONT VIEW", ViewKind.ELEVATION),
        ("ELEVATION", ViewKind.ELEVATION),
        ("C SIDE VIEW", ViewKind.SECTION),
        ("SIDE ELEVATION", ViewKind.SECTION),
        ("SECTION AT VANITY", ViewKind.SECTION),
        ("SINK DETAIL", ViewKind.DETAIL),
        ("plan view", ViewKind.PLAN),
    ],
)
def test_heading_words_name_a_kind(heading: str, kind: ViewKind) -> None:
    assert kind_from_heading(heading).kind is kind


@pytest.mark.parametrize("heading", [None, "", "PLANNING NOTES", "PLAN AND ELEVATION", "QTY 1"])
def test_no_kind_word_or_two_kinds_is_unknown(heading: str | None) -> None:
    found = kind_from_heading(heading)
    assert found.kind is None
    assert found.reason


def test_the_kinds_use_the_shared_page_vocabulary() -> None:
    assert ViewKind.PLAN.page_type is PageType.PLAN
    assert ViewKind.SECTION.page_type is PageType.SECTION
    assert ViewKind.OTHER.page_type is None


# --- code's judgment: one judgment, the heading checked against the drawing ----------------------


def band(axis: Axis, *, at_edge: bool) -> WallBand:
    return WallBand(Box(d(0), d(0), d(100), d(10)), axis, "hatching", at_edge)


def test_a_plan_heading_with_a_wall_band_along_a_long_edge_is_a_plan() -> None:
    judged = code_view_kind(kind_from_heading("A PLAN VIEW"), [band(Axis.HORIZONTAL, at_edge=True)])
    assert judged.kind is ViewKind.PLAN
    assert judged.judged_by == "code"


def test_a_plan_heading_over_a_front_view_is_unknown() -> None:
    # The vendor's heading can be wrong: "PLAN VIEW" under a front elevation, whose only band is
    # the stone slab with cabinets on both sides of it.
    judged = code_view_kind(
        kind_from_heading("A PLAN VIEW"), [band(Axis.HORIZONTAL, at_edge=False)]
    )
    assert judged.kind is None
    assert "heading may be wrong" in judged.reason


def test_a_front_view_heading_over_a_plan_is_unknown() -> None:
    judged = code_view_kind(
        kind_from_heading("B FRONT VIEW"), [band(Axis.HORIZONTAL, at_edge=True)]
    )
    assert judged.kind is None


def test_a_front_view_with_side_walls_only_is_an_elevation() -> None:
    judged = code_view_kind(kind_from_heading("B FRONT VIEW"), [band(Axis.VERTICAL, at_edge=True)])
    assert judged.kind is ViewKind.ELEVATION


def test_a_section_heading_is_taken_from_its_words() -> None:
    judged = code_view_kind(
        kind_from_heading("SECTION AT VANITY"), [band(Axis.HORIZONTAL, at_edge=True)]
    )
    assert judged.kind is ViewKind.SECTION


def hatch(top: float, bottom: float, left: float, right: float) -> list[InkLine]:
    strokes = []
    x = left
    while x + (bottom - top) <= right:
        strokes.append(line(x, bottom, x + (bottom - top), top))
        x += 4
    return strokes


def test_heading_says_plan_but_the_geometry_is_an_elevation() -> None:
    """End to end on drawn geometry: a front view (upper cabinets over a hatched stone slab over
    base cabinets) headed "PLAN VIEW" is not judged a plan; the same heading over a plan (wall hatch
    along the back edge, nothing beyond it) is."""
    view = Box(d(0), d(0), d(500), d(400))
    elevation = (
        [line(50, 60, 450, 60), line(50, 150, 450, 150)]  # upper cabinets
        + hatch(190, 200, 50, 450)  # the stone slab, hatched
        + [line(50, 200, 450, 200), line(50, 340, 450, 340)]  # base cabinets
    )
    plan = hatch(90, 100, 50, 450) + [line(50, 100, 450, 100), line(50, 196, 450, 196)]
    heading = kind_from_heading("A PLAN VIEW")
    as_elevation = code_view_kind(
        heading, find_spans(ink(elevation), view, MEASURED_SPAN_SETTINGS).bands
    )
    as_plan = code_view_kind(heading, find_spans(ink(plan), view, MEASURED_SPAN_SETTINGS).bands)
    assert as_elevation.kind is None
    assert as_plan.kind is ViewKind.PLAN


# --- finding the views on a panel by their captions ---------------------------------------------


def word(text: str, x: float, top: float, height: float = 12) -> list[TextChar]:
    width = d(height) * Decimal("0.6")
    chars = []
    for index, character in enumerate(text):
        if character == " ":
            continue
        left = d(x) + index * width
        chars.append(
            TextChar(
                text=character,
                box=Box(left, d(top), left + width, d(top) + d(height)),
                orientation=Orientation.UPRIGHT,
            )
        )
    return chars


def small_text(count: int, x: float, top: float) -> list[TextChar]:
    """Dimension-sized text, so the captions stand out as headings."""
    return [
        TextChar(
            text="1",
            box=Box(d(x + 40 * index), d(top), d(x + 40 * index + 2), d(top + 4)),
            orientation=Orientation.UPRIGHT,
        )
        for index in range(count)
    ]


PANEL = Box(d(0), d(0), d(800), d(600))


def test_captions_under_two_drawings_make_two_views() -> None:
    chars = (
        word("PLAN", 100, 250)
        + word("SECTION AT", 500, 250)
        + word("VANITY", 500, 263)
        + small_text(8, 20, 30)
    )
    lines = [
        line(60, 60, 300, 60),
        line(60, 200, 300, 200),
        line(460, 60, 700, 60),
        line(460, 200, 700, 200),
    ]
    survey = find_vendor_views(chars, ink(lines), PANEL, MEASURED_VIEW_SETTINGS)
    headings = [view.heading for view in survey.views]
    assert headings == ["PLAN", "SECTION AT VANITY"]
    plan, section = survey.views
    assert plan.found_by == "heading words"
    assert plan.extent.x1 < section.extent.x0
    assert all(view.separated for view in survey.views)
    assert plan.heading_box is not None and plan.heading_box.top == d(250)


def test_a_stroke_crossing_from_one_view_into_another_marks_both_not_separated() -> None:
    chars = word("PLAN", 100, 250) + word("SECTION AT", 500, 250) + small_text(8, 20, 30)
    lines = [line(60, 60, 700, 60), line(60, 200, 300, 200), line(460, 200, 700, 200)]
    survey = find_vendor_views(chars, ink(lines), PANEL, MEASURED_VIEW_SETTINGS)
    assert [view.separated for view in survey.views] == [False, False]
    assert "not clearly separated" in survey.views[0].reason


def test_a_panel_with_no_caption_and_no_scale_note_says_so() -> None:
    survey = find_vendor_views(
        small_text(4, 20, 30), ink([line(60, 60, 300, 60)]), PANEL, MEASURED_VIEW_SETTINGS
    )
    assert survey.views == ()
    assert survey.notes and "no view found" in survey.notes[0]


def test_ink_below_every_caption_is_noted() -> None:
    chars = word("PLAN", 100, 250) + small_text(8, 20, 30)
    lines = [line(60, 60, 300, 60), line(60, 400, 300, 400)]
    survey = find_vendor_views(chars, ink(lines), PANEL, MEASURED_VIEW_SETTINGS)
    assert len(survey.views) == 1
    assert any("below every caption" in note for note in survey.notes)
