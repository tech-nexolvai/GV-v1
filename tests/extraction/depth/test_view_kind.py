"""A view's kind as code judges it, and the views on a vendor panel (#1256). Made-up values."""

from __future__ import annotations

from decimal import Decimal

import pytest

from extraction.architect.text import Orientation, TextChar
from extraction.depth.spans import MEASURED_SPAN_SETTINGS, Span, WallBand, find_spans
from extraction.depth.view_kind import (
    MEASURED_VIEW_SETTINGS,
    VendorView,
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


def test_a_title_over_a_scale_note_is_a_caption_when_the_title_finder_leaves_drawing_out() -> None:
    """A short title over its scale note (`PLAN VIEW` over `1:10`, printed no taller than the
    dimensions) still cuts the panel when `page_views` cannot use it: here its drawing has too few
    strokes for `page_views`, which says so, and the panel is cut by its captions instead."""
    chars = word("PLAN VIEW", 100, 250, height=5) + word("1:10", 110, 256, height=5)
    chars += small_text(8, 20, 30)
    lines = [line(60, 60, 300, 60), line(60, 200, 300, 200), line(60, 60, 60, 200)]
    survey = find_vendor_views(chars, ink(lines), PANEL, MEASURED_VIEW_SETTINGS)
    (view,) = survey.views
    assert view.heading == "PLAN VIEW"
    assert view.found_by == "heading words"
    assert view.heading_box is not None and view.heading_box.bottom > d(256)
    assert any("cut by its captions" in note for note in survey.notes)


# --- review #1274: views the cut cannot keep apart; a second, geometric plan cue ---------------


def _vendor_view(
    number: int, heading: str, extent: tuple[int, int, int, int], caption: tuple[int, int, int, int]
) -> VendorView:
    box = Box(*(Decimal(value) for value in extent))
    return VendorView(
        number=number,
        heading=heading,
        heading_box=Box(*(Decimal(value) for value in caption)),
        extent=box,
        separated=True,
        reason="made up",
        found_by="heading words",
    )


def test_a_view_that_swallows_another_views_caption_holds_both() -> None:
    from extraction.depth.view_kind import ViewSurvey, separate_views

    front = _vendor_view(1, "FRONT VIEW", (16, 213, 552, 592), (240, 580, 330, 592))
    plan = _vendor_view(2, "TOP PLAN", (147, 518, 473, 527), (280, 518, 340, 527))
    survey = separate_views(ViewSurvey((front, plan), ()))
    assert [view.cut_apart for view in survey.views] == [False, False]
    assert all("not cut apart" in view.reason for view in survey.views)


def test_views_side_by_side_stay_apart() -> None:
    from extraction.depth.view_kind import ViewSurvey, separate_views

    left = _vendor_view(1, "PLAN VIEW", (0, 0, 300, 200), (100, 190, 200, 200))
    right = _vendor_view(2, "FRONT VIEW", (300, 0, 600, 200), (400, 190, 500, 200))
    survey = separate_views(ViewSurvey((left, right), ()))
    assert [view.cut_apart for view in survey.views] == [True, True]


def _overall(start: float, end: float, offset: float) -> Span:
    from tests.extraction.depth.test_spans import span

    return span(1, Axis.HORIZONTAL, start, end, offset)


def test_a_plan_without_a_wall_band_is_a_plan_when_projected_from_a_front_view() -> None:
    from extraction.depth.view_kind import CodeViewKind, plan_by_projection

    unknown = code_view_kind(kind_from_heading("A - PLAN VIEW"), [])
    assert unknown.kind is None
    projected = plan_by_projection(
        unknown, _overall(100, 400, 50), [(2, _overall(100, 400, 300))], reach=Decimal("1.5")
    )
    assert projected.kind is ViewKind.PLAN and "projected" in projected.reason
    # The heading alone never decides: no front view drawn at the same width and place.
    alone = plan_by_projection(unknown, _overall(100, 400, 50), [], reach=Decimal("1.5"))
    assert alone.kind is None
    shifted = plan_by_projection(
        unknown, _overall(100, 400, 50), [(2, _overall(120, 420, 300))], reach=Decimal("1.5")
    )
    assert shifted.kind is None
    # A view headed front view is never turned into a plan, whatever is projected.
    front_headed = code_view_kind(kind_from_heading("B - FRONT VIEW"), [])
    kept = plan_by_projection(
        front_headed, _overall(100, 400, 50), [(2, _overall(100, 400, 300))], reach=Decimal("1.5")
    )
    assert kept is front_headed
    assert isinstance(kept, CodeViewKind)


def test_a_sliver_of_shared_border_is_not_one_drawing_taking_in_another() -> None:
    from extraction.depth.view_kind import ViewSurvey, separate_views

    left = _vendor_view(1, "PLAN VIEW", (0, 0, 304, 200), (100, 190, 200, 200))
    right = _vendor_view(2, "FRONT VIEW", (300, 0, 600, 200), (400, 190, 500, 200))
    assert [v.cut_apart for v in separate_views(ViewSurvey((left, right), ())).views] == [
        True,
        True,
    ]
    wide = _vendor_view(1, "PLAN VIEW", (0, 0, 420, 200), (100, 190, 200, 200))
    survey = separate_views(ViewSurvey((wide, right), ()))
    assert [v.cut_apart for v in survey.views] == [False, False]


@pytest.mark.parametrize(
    "heading", ["WALL CABINET PLAN", "UPPER CABINET PLAN", "REFLECTED CEILING PLAN", "CEILING PLAN"]
)
def test_a_wall_cabinet_or_ceiling_plan_is_never_a_countertop_plan(heading: str) -> None:
    named = kind_from_heading(heading)
    assert named.kind is ViewKind.OTHER and "not a plan of the countertop" in named.reason


@pytest.mark.parametrize(
    "heading", ["BASE CABINET PLAN 1:15", "FLOOR PLAN", "A - PLAN VIEW", "TOP PLAN"]
)
def test_base_cabinet_and_floor_plans_stay_countertop_plans(heading: str) -> None:
    # The answer key reads countertop depths on a base cabinet plan and on a floor plan.
    assert kind_from_heading(heading).kind is ViewKind.PLAN


def test_drawings_that_overlap_without_touching_a_caption_hold_both() -> None:
    from extraction.depth.view_kind import ViewSurvey, separate_views

    # Captions printed outside both extents; the drawings share a third of their area.
    left = _vendor_view(1, "PLAN VIEW", (0, 0, 300, 200), (100, 260, 200, 270))
    right = _vendor_view(2, "FRONT VIEW", (200, 0, 500, 200), (300, 260, 400, 270))
    survey = separate_views(ViewSurvey((left, right), ()))
    assert [view.cut_apart for view in survey.views] == [False, False]
    assert all("overlaps the other's drawing" in view.reason for view in survey.views)


def test_finding_views_always_checks_they_are_cut_apart(monkeypatch: pytest.MonkeyPatch) -> None:
    import extraction.depth.view_kind as module

    front = _vendor_view(1, "FRONT VIEW", (16, 213, 552, 592), (240, 580, 330, 592))
    plan = _vendor_view(2, "TOP PLAN", (147, 518, 473, 527), (280, 518, 340, 527))
    monkeypatch.setattr(
        module, "_find_vendor_views", lambda *_args: module.ViewSurvey((front, plan), ())
    )
    survey = module.find_vendor_views(
        [], PageInk(Decimal(600), Decimal(600), (), (), (), ()), Box(Decimal(0), Decimal(0), Decimal(600), Decimal(600)), MEASURED_VIEW_SETTINGS  # type: ignore[arg-type]
    )
    assert [view.cut_apart for view in survey.views] == [False, False]


def test_projection_needs_a_heading_that_says_plan_and_another_line() -> None:
    from extraction.depth.view_kind import plan_by_projection

    reach = Decimal("1.5")
    front = [(2, _overall(100, 400, 300))]
    unheaded = code_view_kind(kind_from_heading(None), [])
    assert unheaded.kind is None
    assert plan_by_projection(unheaded, _overall(100, 400, 50), front, reach=reach) is unheaded
    headed = code_view_kind(kind_from_heading("PLAN VIEW"), [])
    # The same line as the front view's overall: not another view projected from it.
    assert plan_by_projection(headed, _overall(100, 400, 300), front, reach=reach) is headed
    projected = plan_by_projection(headed, _overall(100, 400, 50), front, reach=reach)
    assert projected.kind is ViewKind.PLAN and projected.by_projection
