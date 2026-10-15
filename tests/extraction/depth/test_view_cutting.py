"""Cutting a vendor panel into views: more views cut apart, never wrongly (#1313). Made-up drawings.

Each test is one cause that kept views from being cut apart, or one way a cut could go wrong. The
panel is 800 x 600 points; captions are 12 points tall and the dimension-sized text 4, so the
joining reach is 12 points (`side_reach_em` 3 x the median phrase height).
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

import pytest

from extraction.architect.page_views import (
    INK_OUTSIDE_VIEWS,
    PageNote,
    PageView,
    PageViewSurvey,
)
from extraction.architect.text import Orientation, ScaleKind, ScaleNote, TextChar
from extraction.depth.view_kind import MEASURED_VIEW_SETTINGS, VendorView, find_vendor_views
from extraction.geometry.rows import Box, InkCharacter, InkCurve, InkLine, PageInk


def d(value: float) -> Decimal:
    return Decimal(str(value))


PANEL = Box(d(0), d(0), d(800), d(600))


def line(x0: float, y0: float, x1: float, y1: float) -> InkLine:
    return InkLine(d(x0), d(y0), d(x1), d(y1))


def rect(x0: float, y0: float, x1: float, y1: float) -> list[InkLine]:
    """A drawn box: four strokes meeting at the corners (one piece of drawing)."""
    return [line(x0, y0, x1, y0), line(x1, y0, x1, y1), line(x1, y1, x0, y1), line(x0, y1, x0, y0)]


def frame(x0: float, y0: float, x1: float, y1: float) -> InkCurve:
    """A sheet frame drawn as one closed rectangle path."""
    corners = ((d(x0), d(y0)), (d(x1), d(y0)), (d(x1), d(y1)), (d(x0), d(y1)))
    return InkCurve(Box(d(x0), d(y0), d(x1), d(y1)), corners, closed=True)


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


def small_text() -> list[TextChar]:
    """Dimension-sized text far apart, so the captions stand out and the median phrase is 4."""
    return [
        TextChar(
            text="1",
            box=Box(d(20 + 90 * index), d(2), d(22 + 90 * index), d(6)),
            orientation=Orientation.UPRIGHT,
        )
        for index in range(8)
    ]


def drawn_text(chars: list[TextChar]) -> list[InkCharacter]:
    """The same characters as the drawing's ink holds them."""
    return [InkCharacter(char.text, char.box) for char in chars]


def panel(
    lines: list[InkLine], curves: tuple[InkCurve, ...] = (), text: list[TextChar] | None = None
) -> PageInk:
    return PageInk(
        width=d(800),
        height=d(600),
        lines=tuple(lines),
        curves=curves,
        characters=tuple(drawn_text(text or [])),
        drawing_boxes=(),
    )


def find(chars: list[TextChar], ink: PageInk) -> tuple[dict[str, VendorView], tuple[str, ...]]:
    survey = find_vendor_views([*chars, *small_text()], ink, PANEL, MEASURED_VIEW_SETTINGS)
    return {view.heading or "": view for view in survey.views}, survey.notes


def within(inner: Box, outer: Box) -> bool:
    return (
        outer.x0 <= inner.x0
        and inner.x1 <= outer.x1
        and outer.top <= inner.top
        and inner.bottom <= outer.bottom
    )


# A plan over a front view, each captioned under its drawing.
PLAN = rect(100, 40, 700, 180)
FRONT = rect(100, 260, 700, 540)
CAPTIONS = word("PLAN", 350, 200) + word("FRONT VIEW", 350, 560)


# --- causes: views that could not be cut apart -------------------------------------------------


def test_the_vendors_sheet_frame_no_longer_covers_every_view() -> None:
    """The vendor's own frame round the drawing area was given to one view, whose extent then
    took in every other caption: no view on the panel was cut apart. The frame is peeled off."""
    views, notes = find(CAPTIONS, panel(PLAN + FRONT, (frame(10, 10, 790, 590),)))
    assert views["PLAN"].cut_apart and views["FRONT VIEW"].cut_apart
    assert views["PLAN"].extent == Box(d(100), d(40), d(700), d(180))
    assert views["FRONT VIEW"].extent == Box(d(100), d(260), d(700), d(540))
    assert any("sheet frame: 4 straight stroke(s)" in note for note in notes)


def test_a_stroke_running_below_every_caption_is_left_out_not_stretched_into_a_view() -> None:
    """An open path from the plan down past the last caption was taken whole by its middle, so
    one view's extent ran over the other's caption."""
    hook = InkCurve(
        Box(d(120), d(100), d(300), d(585)),
        ((d(120), d(100)), (d(120), d(585)), (d(300), d(585))),
    )
    views, notes = find(CAPTIONS, panel(PLAN + FRONT, (hook,)))
    assert views["PLAN"].cut_apart and views["FRONT VIEW"].cut_apart
    assert views["FRONT VIEW"].extent.top == d(260)
    assert any("below every caption" in note for note in notes)


def test_a_legend_beside_a_view_is_left_out_of_its_cut() -> None:
    """Notes or a legend in a view's part of the panel became part of the view."""
    legend = rect(600, 40, 760, 120) + [line(600, 80, 760, 80), line(680, 40, 680, 120)]
    views, notes = find(word("PLAN", 250, 200), panel(rect(100, 40, 500, 180) + legend))
    plan = views["PLAN"]
    assert plan.cut_apart
    assert plan.extent == Box(d(100), d(40), d(500), d(180))
    assert any("standing apart beside a view" in note for note in notes)


def test_captions_of_one_row_printed_at_slightly_different_heights_split_the_row() -> None:
    """A front view's caption printed a few points higher than the plan's beside it made two rows:
    the front view's part ran across the whole panel and took the plan's drawing."""
    captions = word("FRONT VIEW", 60, 500) + word("TOP PLAN", 600, 515)
    views, _notes = find(captions, panel(rect(50, 100, 300, 480) + rect(450, 350, 750, 500)))
    front, plan = views["FRONT VIEW"], views["TOP PLAN"]
    assert front.cut_apart and plan.cut_apart
    assert front.extent.x1 <= d(300)
    assert plan.extent == Box(d(450), d(350), d(750), d(500))


def test_two_captions_split_at_the_gap_between_their_drawings_not_halfway() -> None:
    """Captions printed at one end of their drawings: halfway between them falls inside the front
    view, whose far end was given to the plan."""
    captions = word("FRONT VIEW", 60, 500) + word("TOP PLAN", 700, 500)
    views, _notes = find(captions, panel(rect(50, 100, 440, 480) + rect(480, 300, 760, 480)))
    front, plan = views["FRONT VIEW"], views["TOP PLAN"]
    assert front.cut_apart and plan.cut_apart
    assert front.extent == Box(d(50), d(100), d(440), d(480))
    assert plan.extent == Box(d(480), d(300), d(760), d(480))


def test_the_split_follows_the_gap_between_drawings_past_a_captions_edge() -> None:
    """The section's caption is printed left of its drawing, 5 points from the front view's: the
    gap between the drawings runs on over the caption, and is what divides the two (#1313
    review: looking only between the captions found 5 points and held both)."""
    captions = word("FRONT VIEW", 60, 500) + word("SECTION", 330, 500)
    views, _notes = find(captions, panel(rect(50, 100, 325, 480) + rect(370, 100, 750, 480)))
    front, section = views["FRONT VIEW"], views["SECTION"]
    assert front.cut_apart and section.cut_apart
    assert front.extent == Box(d(50), d(100), d(325), d(480))
    assert section.extent == Box(d(370), d(100), d(750), d(480))


# --- never wrongly: what a cut must never take in ----------------------------------------------


def test_the_next_views_dimension_printed_beside_a_caption_belongs_to_no_view() -> None:
    """The front view's top dimension number is printed level with the plan's caption: it could be
    either view's, so it is in neither cut; and since a view read without its own number could be
    misread, both views beside it are held, and a note says what was left out (#1313 review)."""
    number = word("96", 500, 203, height=8)
    dimension = [line(100, 215, 700, 215), line(100, 215, 100, 260), line(700, 215, 700, 260)]
    views, notes = find(CAPTIONS + number, panel(PLAN + FRONT + dimension, text=number))
    for view in views.values():
        assert not any(within(char.box, view.extent) for char in number)
        assert not view.cut_apart
        assert "printed level with a row of captions (a number or a long stroke)" in view.reason
    assert any("level with a row of captions" in note for note in notes)


def test_a_dimension_on_a_captions_line_is_never_taken_as_its_label() -> None:
    """The section's overall depth (its line and number) starts 3 points from the front view's
    caption, on its line. It is joined to the caption but runs far past it, so it is no label
    (never painted out); it is left out, and the section, which would be read with only a partial
    dimension, is held (#1313 review)."""
    captions = word("FRONT VIEW", 100, 200) + word("SECTION", 350, 560)
    number = word("24", 420, 201, height=6)
    overall = [line(175, 208, 700, 208), line(175, 213, 175, 257), line(700, 213, 700, 257)]
    part = word("20", 300, 245, height=6)
    partial = [line(175, 252, 600, 252), line(600, 250, 600, 257)]
    ink = panel(
        rect(100, 40, 700, 180) + rect(175, 260, 700, 540) + overall + partial,
        text=number + part,
    )
    views, notes = find(captions + number + part, ink)
    front, section = views["FRONT VIEW"], views["SECTION"]
    assert front.heading_box is not None and front.heading_box.x1 < d(200)
    assert not section.cut_apart
    assert "printed level with a row of captions (a number or a long stroke)" in section.reason
    assert any("level with a row of captions" in note for note in notes)


def test_a_captions_own_bubble_and_scale_are_its_label_with_a_note() -> None:
    """A tall caption's scale note under it and its bubble beside it are its label (hidden in
    every picture, with a note), never the drawing of the view below, whose cut starts under them.
    """
    bubble = InkCurve(
        Box(d(333), d(198), d(349), d(214)),
        ((d(333), d(198)), (d(349), d(198)), (d(349), d(214)), (d(333), d(214))),
        closed=True,
    )
    scale = word("1:18", 352, 213, height=5)
    front = rect(100, 222, 700, 540)
    views, notes = find(
        word("PLAN", 350, 200) + scale + word("FRONT VIEW", 350, 560),
        panel(PLAN + front, (bubble,), text=scale),
    )
    plan, front_view = views["PLAN"], views["FRONT VIEW"]
    assert plan.heading_box is not None
    assert all(within(char.box, plan.heading_box) for char in scale)
    assert within(bubble.box, plan.heading_box)
    assert not any(within(char.box, front_view.extent) for char in scale)
    assert front_view.extent.top == d(222)
    assert any("taken as the label of 'PLAN'" in note for note in notes)


def test_readable_ink_left_out_inside_a_views_cut_holds_it() -> None:
    """A note printed level with the plan's caption, inside the box of the plan's cut (the plan's
    wall runs down beside the caption) but not joined to it, is no part of the plan: the picture
    would show it, so the plan is held with that reason (#1313 review)."""
    note = word("NOTE", 500, 202, height=4)
    views, _notes = find(word("PLAN", 350, 200) + note, panel(rect(100, 40, 700, 210), text=note))
    plan = views["PLAN"]
    assert all(within(char.box, plan.extent) for char in note)
    assert not plan.cut_apart
    assert "ink that are not its drawing's" in plan.reason


def test_a_view_whose_cut_takes_in_a_caption_sized_title_is_held() -> None:
    """A sketch titled at a caption's size stands right against the plan: the cut cannot leave it
    out, so the plan is held, with the reason."""
    title = word("CONCEPT IMAGE", 110, 150)
    sketch = rect(100, 40, 295, 180)
    views, _notes = find(word("PLAN", 450, 200) + title, panel(sketch + rect(300, 40, 700, 180)))
    plan = views["PLAN"]
    assert not plan.cut_apart
    assert "printed as large as a caption" in plan.reason and "nothing on it is read" in plan.reason


def test_a_stroke_from_one_view_well_into_the_next_holds_both() -> None:
    """A long stroke runs from the plan's drawing down into the front view's: the cut between the
    rows splits a drawing, so neither view is read."""
    views, _notes = find(CAPTIONS, panel(PLAN + FRONT + [line(100, 60, 100, 400)]))
    for view in views.values():
        assert not view.cut_apart
        assert "the cut splits a drawing" in view.reason


def test_two_drawings_closer_than_the_joining_reach_on_one_row_are_held() -> None:
    """Six points apart, code cannot tell which drawing the ink at the split belongs to."""
    captions = word("FRONT VIEW", 60, 500) + word("SECTION", 600, 500)
    views, _notes = find(captions, panel(rect(50, 100, 400, 480) + rect(406, 100, 750, 480)))
    for view in views.values():
        assert not view.cut_apart
        assert "no clear gap divides it" in view.reason


def test_readable_ink_just_beyond_the_joining_reach_holds_the_view() -> None:
    """A long stroke 20 points from the plan (beyond the 12-point reach, within twice it) may be
    the plan's own dimension: left out, the plan would be missing it, so the plan is held."""
    views, _notes = find(
        word("PLAN", 250, 200), panel(rect(100, 40, 500, 180) + [line(520, 40, 520, 180)])
    )
    plan = views["PLAN"]
    assert not plan.cut_apart
    assert "just beyond reach of its drawing" in plan.reason


def test_a_view_standing_right_above_another_rows_caption_is_held() -> None:
    """A drawing whose bottom sits right on the next row's caption may be that caption's drawing.
    Here the dimension text is 6 points, so the joining reach is 18: the plan's part ends 14
    points above the front view's caption, a row of its own (12 points below the plan's)."""
    text = [
        TextChar("1", Box(d(20 + 90 * i), d(2), d(23 + 90 * i), d(8)), Orientation.UPRIGHT)
        for i in range(8)
    ]
    captions = word("PLAN", 600, 300) + word("FRONT VIEW", 100, 324)
    survey = find_vendor_views(
        [*captions, *text],
        panel(rect(50, 40, 750, 310)),
        PANEL,
        MEASURED_VIEW_SETTINGS,
    )
    (plan,) = [view for view in survey.views if view.heading == "PLAN"]
    assert not plan.cut_apart
    assert "stands right above the caption 'FRONT VIEW'" in plan.reason


# --- the sheet frame -----------------------------------------------------------------------------


def test_the_sheet_frame_is_the_outermost_strokes_round_every_caption() -> None:
    from extraction.depth.view_kind import sheet_frame

    captions = [Box(d(350), d(200), d(379), d(212)), Box(d(350), d(560), d(422), d(572))]
    ink = panel(PLAN + FRONT, (frame(10, 10, 790, 590), frame(14, 14, 786, 586)))
    unframed, peeled = sheet_frame(ink, captions, PANEL, MEASURED_VIEW_SETTINGS)
    assert peeled == 8  # both rectangles, four sides each, outside in
    assert unframed.curves == ()
    assert set(PLAN + FRONT) <= set(unframed.lines)


def test_a_drawings_own_long_line_is_never_taken_for_the_frame() -> None:
    from extraction.depth.view_kind import sheet_frame

    captions = [Box(d(350), d(200), d(379), d(212))]
    # A wall line across the whole drawing, above every caption, with the drawing's ink beyond it.
    ink = panel(rect(100, 40, 700, 180) + [line(90, 60, 710, 60), line(400, 20, 400, 40)])
    assert sheet_frame(ink, captions, PANEL, MEASURED_VIEW_SETTINGS) == (ink, 0)


def test_a_drawing_against_the_frame_keeps_its_own_edge() -> None:
    """Once the frame is off, a drawing's top edge may be the outermost stroke left; it ends on
    nothing frame-like, so it is never taken (#1313 review)."""
    from extraction.depth.view_kind import sheet_frame

    captions = [Box(d(350), d(520), d(379), d(532))]
    body = rect(10, 10, 600, 300) + [line(30, 300, 30, 340), line(600, 300, 600, 340)]
    ink = panel(body, (frame(10, 10, 790, 590),))
    unframed, peeled = sheet_frame(ink, captions, PANEL, MEASURED_VIEW_SETTINGS)
    assert peeled == 4
    assert set(body) <= set(unframed.lines)


def test_lone_parallel_lines_are_not_a_frame() -> None:
    """A frame has a corner; a line above and a line below the drawings are drawing."""
    from extraction.depth.view_kind import sheet_frame

    captions = [Box(d(350), d(200), d(379), d(212))]
    ink = panel([line(50, 20, 750, 20), line(50, 590, 750, 590), *rect(100, 40, 700, 180)])
    assert sheet_frame(ink, captions, PANEL, MEASURED_VIEW_SETTINGS) == (ink, 0)


# --- views page_views finds only once the frame is peeled ---------------------------------------


def _page_view(number: int, title: str, extent: Box, *, separated: bool) -> PageView:
    scale = ScaleNote(
        "1:10",
        ScaleKind.RATIO,
        Fraction(1, 10),
        Box(extent.x0, extent.bottom, extent.x0 + 20, extent.bottom + 6),
    )
    return PageView(
        number=number,
        title=title,
        bubble=None,
        scale=scale,
        label_box=scale.box,
        extent=extent,
        separated=separated,
        reason="made up",
    )


def test_a_view_found_after_peeling_the_frame_is_read_only_when_clearly_separated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once the frame is off, `page_views` may still drop a drawing that runs across to a
    neighbour, leaving a piece of a view: such a view is not cut apart."""
    import extraction.depth.view_kind as module

    border = PageNote(
        "left out as the sheet's border, round a view title: 400 strokes and 20 characters "
        "(drawing ink may be joined to the border)",
        INK_OUTSIDE_VIEWS,
    )
    clean = _page_view(1, "PLAN VIEW", Box(d(100), d(40), d(700), d(180)), separated=True)
    piece = _page_view(2, "FRONT VIEW", Box(d(300), d(260), d(500), d(540)), separated=False)
    answers = iter([PageViewSurvey((), (border,)), PageViewSurvey((clean, piece), ())])
    monkeypatch.setattr(module, "survey_page_views", lambda *_args, **_kwargs: next(answers))
    survey = find_vendor_views(
        [*CAPTIONS, *small_text()],
        panel(PLAN + FRONT, (frame(10, 10, 790, 590),)),
        PANEL,
        MEASURED_VIEW_SETTINGS,
    )
    plan, front = survey.views
    assert plan.found_by == front.found_by == "title over scale"
    assert plan.cut_apart and not front.cut_apart
    assert "found once the sheet frame was peeled off" in front.reason
    assert any("sheet frame" in note for note in survey.notes)
