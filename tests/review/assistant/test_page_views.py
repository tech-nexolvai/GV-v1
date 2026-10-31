"""One way to split a page into views (`page_views`), used by `view_questions` and `dim_parts`.

Made-up drawings only (frames of lines, titles and sizes with invented values); no PDF, no model.
"""

from __future__ import annotations

from typing import Any

from app.review.assistant import dim_parts, page_views, view_questions

PAGE = (1200.0, 800.0)


def _frame(x0: float, y0: float, x1: float, y1: float) -> list[Any]:
    """One connected drawing: its outline and five lines across it each way (24 pieces)."""
    lines: list[Any] = [
        ((x0, y0), (x1, y0)),
        ((x1, y0), (x1, y1)),
        ((x1, y1), (x0, y1)),
        ((x0, y1), (x0, y0)),
    ]
    for k in range(1, 6):
        x = x0 + (x1 - x0) * k / 6
        y = y0 + (y1 - y0) * k / 6
        lines += [((x, y0), (x, y1)), ((x0, y), (x1, y))]
    return lines


def _chain(x0: float, x1: float, y: float, reach: float) -> list[Any]:
    """A dimension chain: one line with twelve short extension lines `reach` long."""
    out: list[Any] = [((x0, y), (x1, y))]
    for k in range(12):
        x = x0 + (x1 - x0) * k / 11
        out.append(((x, y - reach), (x, y)))
    return out


def _at(text: str, x: float, y: float) -> dict[str, Any]:
    return {"text": text, "box": (x, y, x + 5.0 * len(text), y + 6.0)}


# ---- the drawings -----------------------------------------------------------------------------------


def test_each_connected_drawing_is_a_view_and_the_border_is_none() -> None:
    border = [((5, 5), (1195, 5)), ((1195, 5), (1195, 795)), ((1195, 795), (5, 795))]
    border += [((5, 795), (5, 5))] + [((5, 5 + k), (20, 5 + k)) for k in range(0, 790, 60)]
    segments = _frame(100, 100, 400, 400) + _frame(500, 100, 700, 400) + border
    found = page_views.drawings(segments, PAGE)
    assert found.boxes == [(100, 100, 400, 400), (500, 100, 700, 400)]
    assert found.view_of(0) == 0 and found.view_of(14) == 1  # 14 pieces a frame
    assert found.view_of(len(segments) - 1) is None  # the border


def test_a_drawing_inside_a_view_is_part_of_it() -> None:
    # A sink (its own lines, not touching the plan's) drawn inside a plan.
    plan, sink = _frame(100, 100, 600, 300), _frame(250, 150, 350, 250)
    found = page_views.drawings(plan + sink, PAGE)
    assert found.boxes == [(100, 100, 600, 300)]
    assert found.group_of(0) == found.group_of(len(plan)) == ("view", 0)


def test_a_thin_drawing_beside_a_view_joins_it_and_a_scrap_does_not_move_its_edge() -> None:
    view = _frame(100, 100, 400, 400)
    chain = _chain(100, 400, 430, 25)  # 25 points tall: grows the view's box
    scrap = _chain(100, 400, 90, 6)  # 6 points tall, just above: joins it, box unchanged
    found = page_views.drawings(view + chain + scrap, PAGE)
    assert len(found.boxes) == 1
    assert found.boxes[0] == (100, 100, 400, 430)
    assert found.view_of(len(view)) == 0 and found.view_of(len(view) + len(chain)) == 0


def test_a_small_drawing_on_its_own_is_no_view_but_keeps_its_own_drawing() -> None:
    view = _frame(100, 100, 400, 400)
    tick = [((800, 600), (803, 603)), ((803, 603), (806, 600))]  # two pieces, far away
    found = page_views.drawings(view + tick, PAGE)
    first, second = len(view), len(view) + 1
    assert found.view_of(first) is None
    assert found.group_of(first) == found.group_of(second) != found.group_of(0)


# ---- the titles and their views -----------------------------------------------------------------


def test_titles_take_their_bubble_and_skip_notes_title_blocks_and_part_callouts() -> None:
    items = [
        _at("SECTION VIEW", 560, 440),
        _at("S1", 540, 440),
        _at("ELEVATION A", 200, 440),  # its bubble inside the phrase
        _at("SEE DETAIL 3", 900, 300),  # a note
        _at("SHEET PLAN NO", 900, 700),  # the title block
        _at("PANEL SECTION", 900, 500),  # a part callout (the caller says so)
    ]
    found, members = page_views.titles(items, lambda text: "PANEL" in text)
    assert [name for name, _box in found] == ["ELEVATION A", "SECTION VIEW S1"]
    assert {id(items[0]), id(items[1]), id(items[2])} == members


def test_a_title_under_a_view_s_dimension_text_is_that_view_s() -> None:
    boxes = [(100.0, 100.0, 400.0, 400.0), (500.0, 100.0, 700.0, 400.0)]
    # The left view's overall printed 40 points under it, its title 50 under that.
    texts = [(220.0, 440.0, 260.0, 446.0)]
    owners = page_views.owners(boxes, [("FRONT VIEW", (200.0, 496.0, 260.0, 502.0))], texts)
    assert owners == {0: [("FRONT VIEW", (200.0, 496.0, 260.0, 502.0))]}
    # Without that text it is too far under the view.
    assert page_views.owners(boxes, [("FRONT VIEW", (200.0, 496.0, 260.0, 502.0))], []) == {}


# ---- both callers split the same way -------------------------------------------------------------


def _counter(x0: float, y0: float, x1: float, y1: float) -> list[Any]:
    """A counter top in plan: its outline and ten short lines along its back edge."""
    lines: list[Any] = [
        ((x0, y0), (x1, y0)),
        ((x1, y0), (x1, y1)),
        ((x1, y1), (x0, y1)),
        ((x0, y1), (x0, y0)),
    ]
    lines += [((x0 + 40 * k, y0), (x0 + 40 * k, y0 + 10)) for k in range(1, 11)]
    return lines


def _page() -> tuple[list[dict[str, Any]], list[Any]]:
    plan = _counter(100, 100, 600, 300)
    sink = _frame(250, 150, 350, 250)  # inside the counter, touching none of its lines
    section = _frame(800, 100, 1000, 400)
    found = [
        _at("TOP PLAN", 300, 330),
        _at("SECTION VIEW", 860, 430),
        _at("S2", 840, 430),
        _at("[24]", 900, 410),
    ]
    return found, plan + sink + section


def test_dim_parts_and_view_questions_name_the_same_views() -> None:
    found, segments = _page()
    views, _assigned = view_questions.views_of(found, segments, PAGE)
    assert [v.name for v in views] == ["TOP PLAN", "SECTION VIEW S2"]
    parts_views = dim_parts.views_of(found, segments, PAGE)
    plan_piece, section_piece = 0, len(segments) - 1
    assert parts_views.of(plan_piece, (300.0, 200.0)).name == "TOP PLAN"
    assert parts_views.of(section_piece, (900.0, 200.0)).name == "SECTION VIEW S2"


def test_a_sink_inside_a_plan_is_one_drawing_for_the_size_links() -> None:
    # Before the shared splitter, dim_parts took the sink for a drawing of its own, so a size from
    # the counter's edge to the sink's was refused as "edges in different drawings".
    found, segments = _page()
    parts_views = dim_parts.views_of(found, segments, PAGE)
    sink_piece = len(_counter(100, 100, 600, 300))
    assert parts_views.same(0, sink_piece)
    assert not parts_views.same(0, len(segments) - 1)
