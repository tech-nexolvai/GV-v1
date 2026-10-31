"""Which part each dimension measures (`dim_parts`): made-up lines, words and values only."""

from __future__ import annotations

from collections.abc import Sequence
from fractions import Fraction
from types import SimpleNamespace
from typing import Any

from app.review.assistant import dim_parts
from app.review.assistant.ledger import format_inches

Seg = tuple[tuple[float, float], tuple[float, float]]


def _rect(x0: float, y0: float, x1: float, y1: float) -> list[Seg]:
    return [((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)), ((x1, y1), (x0, y1)), ((x0, y1), (x0, y0))]


def _phrase(text: str, x0: float, y0: float, x1: float, y1: float) -> dict[str, Any]:
    return {"text": text, "box": [x0, y0, x1, y1], "n": len(text.split())}


def _dim(
    ident: str,
    text: str,
    inches: Fraction | int,
    orientation: str,
    a: float,
    b: float,
    across: float,
    far: float,
    route: str = "text",
) -> tuple[dict[str, Any], list[Seg]]:
    """A label with its dimension line from `a` to `b` (along) at `across`, and its extension lines
    running from the dimension line to `far` (across); with the line work they make."""
    if orientation == "h":
        line = [[a, across], [b, across]]
        ext = [[[a, far], [a, across]], [[b, far], [b, across]]]
        box = [(a + b) / 2 - 6, across - 8, (a + b) / 2 + 6, across - 1]
    else:
        line = [[across, a], [across, b]]
        ext = [[[far, a], [across, a]], [[far, b], [across, b]]]
        box = [across - 8, (a + b) / 2 - 6, across - 1, (a + b) / 2 + 6]
    label = {
        "id": ident,
        "text": text,
        "inches": str(float(Fraction(inches))),
        "box": box,
        "orientation": orientation,
        "route": route,
        "dim_line": line,
        "extension": ext,
        "scale_ok": True,
    }
    pieces: list[Seg] = [((line[0][0], line[0][1]), (line[1][0], line[1][1]))]
    pieces += [((e[0][0], e[0][1]), (e[1][0], e[1][1])) for e in ext]
    return label, pieces


def _leader(points: Sequence[tuple[float, float]]) -> list[Seg]:
    return [(points[i], points[i + 1]) for i in range(len(points) - 1)]


def _scene(
    labels: list[tuple[dict[str, Any], list[Seg]]], drawn: list[Seg]
) -> tuple[list[dict[str, Any]], list[Seg]]:
    out = [label for label, _ in labels]
    segs = list(drawn)
    for _, pieces in labels:
        segs += pieces
    return out, segs


def _panel_and_cabinet() -> list[Seg]:
    """A 10 pt end panel (x 100-110) beside a cabinet (x 110-200), y 200-300, with a shelf and a
    door line, on a floor."""
    return (
        _rect(100, 200, 110, 300)
        + [((110, 200), (200, 200)), ((200, 200), (200, 300)), ((200, 300), (110, 300))]
        + [((112, 250), (198, 250)), ((112, 252), (198, 252)), ((155, 202), (155, 298))]
        + [((80, 300), (230, 300))]
    )


def _by_id(parts: dim_parts.PageParts) -> dict[str, dim_parts.DimPart]:
    return {d.id: d for d in parts.dims}


# ---- step 1: a dimension, its edges and the callout pointing at what it spans ----------------------


def test_a_width_is_tied_to_the_callout_whose_leader_points_into_what_it_spans() -> None:
    # Extension lines stop 4 pt short of the object (a drafter's gap): still walked to its edges.
    panel = _dim("D1", '2"', 2, "h", 100, 110, 180, 196)
    cabinet = _dim("D2", '18"', 18, "h", 110, 200, 180, 196)
    labels, segs = _scene([panel, cabinet], _panel_and_cabinet())
    segs += _leader([(228, 248), (105, 248)])
    found = [
        _phrase("END PANEL WD-02", 230, 245, 290, 251),
        _phrase("FRONT VIEW", 130, 330, 170, 338),
    ]
    parts = _by_id(dim_parts.link_page(1, labels, found, segs, (600, 400)))
    d1 = parts["D1"]
    assert d1.part == "END PANEL"
    assert d1.how_sure == "leader"
    assert d1.view.name == "FRONT VIEW"
    assert d1.direction == "width"
    assert [e.at for e in d1.edges if e is not None] == [100, 110]
    assert all(e is not None and e.corner == 200 for e in d1.edges)
    as_dict = d1.as_dict()
    assert as_dict["value"] == '2"' and as_dict["part"] == "END PANEL"
    assert as_dict["object_edges"][0] == [[100, 200], [100, 300]]
    # The cabinet's width: the leader points into the panel, not into the cabinet.
    assert parts["D2"].part is None
    assert parts["D2"].reason == "no callout points at what it spans"


def test_an_arrow_touching_a_face_counts_on_the_side_it_points_into() -> None:
    # A leg 10 pt wide (x 100-110) with the gap beside it (x 110-200): the arrow touches the leg's
    # right face coming from the right, so it names the leg's width, never the gap's.
    leg = _dim("D1", '2"', 2, "h", 100, 110, 180, 196)
    gap = _dim("D2", '18"', 18, "h", 110, 200, 180, 196)
    drawn = _rect(100, 200, 110, 300) + _rect(200, 200, 210, 300) + [((80, 300), (230, 300))]
    drawn += [((60, 200), (230, 200)), ((60, 195), (230, 195))]  # the top over them
    labels, segs = _scene([leg, gap], drawn)
    segs += _leader([(258, 248), (110, 248)])
    found = [_phrase("PIPE LEG", 260, 245, 290, 251), _phrase("FRONT VIEW", 130, 330, 170, 338)]
    parts = _by_id(dim_parts.link_page(1, labels, found, segs, (600, 400)))
    assert parts["D1"].part == "LEG"
    assert parts["D2"].part is None


# ---- refusing when unclear -------------------------------------------------------------------------


def test_no_corner_on_the_extension_lines_means_no_edge_and_no_part() -> None:
    lonely = _dim("D1", '2"', 2, "h", 400, 410, 180, 196)  # nothing drawn under it
    labels, segs = _scene([lonely], _panel_and_cabinet())
    found = [_phrase("END PANEL", 420, 245, 450, 251)]
    (d1,) = dim_parts.link_page(1, labels, found, segs, (600, 400)).dims
    assert d1.part is None
    assert d1.reason == "no clear object edge at either end"


def test_a_callout_pointing_at_a_shelf_is_not_the_cabinet_s_height() -> None:
    height = _dim("D1", '20"', 20, "v", 200, 300, 260, 214)  # the cabinet's height, right side
    labels, segs = _scene([height], _panel_and_cabinet())
    segs += _leader([(238, 251), (150, 251)])  # into the shelf
    found = [_phrase("ADJ. SHELF", 240, 248, 270, 254), _phrase("FRONT VIEW", 130, 330, 170, 338)]
    (d1,) = dim_parts.link_page(1, labels, found, segs, (600, 400)).dims
    assert all(e is not None for e in d1.edges)
    assert d1.part is None
    assert d1.reason == "no callout points at what it spans"


def test_two_parts_pointing_at_one_span_or_one_callout_naming_two_is_no_part() -> None:
    panel = _dim("D1", '2"', 2, "h", 100, 110, 180, 196)
    labels, segs = _scene([panel], _panel_and_cabinet())
    segs += _leader([(228, 248), (105, 248)]) + _leader([(228, 268), (105, 268)])
    two = [_phrase("END PANEL", 230, 245, 270, 251), _phrase("FILLER", 230, 265, 260, 271)]
    (d1,) = dim_parts.link_page(1, labels, two, segs, (600, 400)).dims
    assert d1.part is None and d1.reason == "callouts name different parts: END PANEL, FILLER"
    labels, segs = _scene([panel], _panel_and_cabinet())
    segs += _leader([(228, 248), (105, 248)])
    one = [_phrase("TOP & SIDE SPLASH", 230, 245, 290, 251)]
    (d1,) = dim_parts.link_page(1, labels, one, segs, (600, 400)).dims
    assert d1.part is None and d1.reason == "callouts name different parts: SPLASH, TOP"


def test_words_floating_in_an_open_band_name_nothing() -> None:
    # A note between the wall cabinets' underside (y 100) and the counter (y 190) with no leader:
    # the band's height is not the note's part. The same kind of words inside a closed panel whose
    # top and bottom are the dimension's edges do name it.
    drawn = _rect(60, 60, 560, 100) + _rect(60, 190, 560, 200)
    drawn += [((300, 60), (300, 100)), ((300, 190), (300, 200))]
    band = _dim("D1", '18"', 18, "v", 100, 190, 40, 56)
    labels, segs = _scene([band], drawn)
    found = [_phrase("STONE TOP BY GV", 280, 140, 330, 146)]
    (d1,) = dim_parts.link_page(1, labels, found, segs, (700, 400)).dims
    assert all(e is not None for e in d1.edges)
    assert d1.part is None
    labels, segs = _scene([band], drawn + _rect(250, 100, 350, 190))
    found = [_phrase("REMOVABLE PANEL", 280, 140, 330, 146)]
    (d1,) = dim_parts.link_page(1, labels, found, segs, (700, 400)).dims
    assert d1.part == "PANEL" and d1.how_sure == "words"


# ---- upright dimensions, two views on one row, the same part across views -------------------------


def _base(
    x0: float, toe_top: float, title: str, ident: str, value: Fraction
) -> tuple[list[tuple[dict[str, Any], list[Seg]]], list[Seg], list[dict[str, Any]]]:
    """A base cabinet at x0..x0+100 standing on a toe kick (top at `toe_top`, floor at 300, face
    5 pt in), its toe kick height dimensioned on the left (upright, extension lines stopping 4 pt
    short), a TOE KICK callout from the right and its view title under it."""
    drawn = _rect(x0, 200, x0 + 100, toe_top)
    drawn += [((x0 + 5, toe_top), (x0 + 5, 300)), ((x0 - 10, 300), (x0 + 110, 300))]
    drawn += [((x0 + 50, 202), (x0 + 50, toe_top - 2)), ((x0 + 2, 230), (x0 + 98, 230))]
    drawn += [((x0 + 2, y), (x0 + 98, y)) for y in (215, 245, 260)]
    drawn += _leader([(x0 + 128, (toe_top + 300) / 2), (x0 + 5, (toe_top + 300) / 2)])
    toe = _dim(ident, format_inches(value), value, "v", toe_top, 300, x0 - 40, x0 - 4)
    words = [
        _phrase(
            "TOE KICK WD-04", x0 + 130, (toe_top + 300) / 2 - 3, x0 + 180, (toe_top + 300) / 2 + 3
        ),
        _phrase(title, x0 + 30, 330, x0 + 80, 338),
    ]
    return [toe], drawn, words


def test_an_upright_dimension_ties_to_its_part_and_reads_as_a_height() -> None:
    dims, drawn, words = _base(100, 280, "FRONT VIEW", "D1", Fraction(4))
    labels, segs = _scene(dims, drawn)
    (d1,) = dim_parts.link_page(1, labels, words, segs, (600, 400)).dims
    assert d1.orientation == "v"
    assert d1.part == "TOE KICK" and d1.how_sure == "leader"
    assert d1.direction == "height"
    assert d1.span == (280, 300)


def test_two_views_on_one_row_keep_their_own_titles_and_directions() -> None:
    a_dims, a_drawn, a_words = _base(100, 280, "FRONT VIEW A", "D1", Fraction(4))
    s_dims, s_drawn, s_words = _base(400, 282, "SECTION VIEW S1", "D2", Fraction(7, 2))
    depth = _dim("D3", '20"', 20, "h", 400, 500, 180, 196)
    labels, segs = _scene(a_dims + s_dims + [depth], a_drawn + s_drawn)
    parts = _by_id(dim_parts.link_page(1, labels, a_words + s_words, segs, (800, 400)))
    assert parts["D1"].view.name == "FRONT VIEW A"
    assert parts["D2"].view.name == "SECTION VIEW S1"
    assert parts["D3"].view.name == "SECTION VIEW S1"
    assert parts["D3"].direction == "depth"  # across a section
    assert parts["D2"].direction == "height"


def test_the_same_part_with_two_sizes_in_two_views_is_a_please_check_never_red() -> None:
    a_dims, a_drawn, a_words = _base(100, 280, "FRONT VIEW A", "D1", Fraction(4))
    s_dims, s_drawn, s_words = _base(400, 282, "SECTION VIEW S1", "D2", Fraction(7, 2))
    labels, segs = _scene(a_dims + s_dims, a_drawn + s_drawn)
    page = dim_parts.link_page(1, labels, a_words + s_words, segs, (800, 400))
    (found,) = dim_parts.disagreements([page])
    assert found.part == "TOE KICK" and found.direction == "height"
    assert found.line() == (
        'TOE KICK height: p1 FRONT VIEW A 4" (p1.D1); p1 SECTION VIEW S1 3 1/2" (p1.D2).'
    )
    assert found.code_proved  # two code-read printed values tied by leaders
    assert dim_parts.DIM_PARTS_RED is False
    assert dim_parts.red_ready([found]) == []  # red stays off until a measured run
    lines = dim_parts.facts([page])
    assert "Same part, different sizes (please check which is right):" in lines
    # The same size in both views: nothing to say.
    s_dims, s_drawn, s_words = _base(400, 280, "SECTION VIEW S1", "D2", Fraction(4))
    labels, segs = _scene(a_dims + s_dims, a_drawn + s_drawn)
    page = dim_parts.link_page(1, labels, a_words + s_words, segs, (800, 400))
    assert dim_parts.disagreements([page]) == []


def test_the_same_part_on_the_item_s_other_drawing_is_compared_too() -> None:
    a_dims, a_drawn, a_words = _base(100, 280, "SECTION VIEW S1", "D1", Fraction(4))
    b_dims, b_drawn, b_words = _base(100, 282, "SECTION VIEW S2", "D7", Fraction(7, 2))
    here = dim_parts.link_page(3, *_scene_args(a_dims, a_drawn, a_words))
    other = dim_parts.link_page(13, *_scene_args(b_dims, b_drawn, b_words))
    (found,) = dim_parts.disagreements([here, other])
    assert [where for where, _ in found.sides] == ["p3 SECTION VIEW S1", "p13 SECTION VIEW S2"]


def _scene_args(
    dims: list[tuple[dict[str, Any], list[Seg]]], drawn: list[Seg], words: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[Seg], tuple[float, float]]:
    labels, segs = _scene(dims, drawn)
    return labels, words, segs, (600, 400)


def test_parts_an_item_has_several_of_are_never_compared() -> None:
    # Two doors of different widths in two views are two doors, not one door drawn two ways.
    a = dim_parts.DimPart(
        "D1",
        1,
        '15"',
        Fraction(15),
        "text",
        True,
        "h",
        (0, 1),
        dim_parts.View("FRONT VIEW A", "elevation"),
        (None, None),
        part="DOOR",
        how_sure="leader",
    )
    b = dim_parts.DimPart(
        "D2",
        1,
        '18"',
        Fraction(18),
        "text",
        True,
        "h",
        (0, 1),
        dim_parts.View("FRONT VIEW B", "elevation"),
        (None, None),
        part="DOOR",
        how_sure="leader",
    )
    page = dim_parts.PageParts(1, [a, b])
    assert dim_parts.disagreements([page]) == []


def test_parts_in_reads_the_more_specific_part_first() -> None:
    assert dim_parts.parts_in("END PANEL WD-02") == ["END PANEL"]
    assert dim_parts.parts_in("ToeKick- WD-04") == ["TOE KICK"]
    assert dim_parts.parts_in("ST-02 Quartz top & Side splash by GV") == ["SPLASH", "TOP"]
    assert dim_parts.parts_in("under mount soft closing drawer slides") == []
    assert dim_parts.parts_in("Scribe to fit (5in filler to field cut)") == ["FILLER"]


# ---- the page reviewer's hook ----------------------------------------------------------------------


def test_the_hook_gives_facts_and_logs_the_counts() -> None:
    a_dims, a_drawn, a_words = _base(100, 280, "FRONT VIEW A", "D1", Fraction(4))
    s_dims, s_drawn, s_words = _base(400, 282, "SECTION VIEW S1", "D2", Fraction(7, 2))
    extra = _dim("D3", '40"', 40, "h", 700, 900, 180, 196)  # nothing under it
    labels, segs = _scene(a_dims + s_dims + [extra], a_drawn + s_drawn)
    review = SimpleNamespace(
        page=1, file="set.pdf", found=a_words + s_words, size=(1000, 400), knowledge={}
    )
    context = SimpleNamespace(built=True, pair_pages=lambda page: [], phrases=lambda page: [])
    run = SimpleNamespace(
        review=review,
        _labels_on=lambda page: labels,
        _context=lambda: context,
        _dim_parts_lines=lambda file, page: segs,
    )
    (block,) = dim_parts.blocks(run)
    assert "TOE KICK height" in block["text"]
    assert "p1: 2 of 3 dimensions tied to a part" in block["text"]
    assert review.knowledge["dims_linked"] == 2
    assert review.knowledge["dims_unlinked"] == 1
    assert review.knowledge["part_disagreements"][0]["part"] == "TOE KICK"
    assert review.knowledge["part_disagreements"][0]["code_proved"] is True


def test_the_hook_says_nothing_when_the_page_cannot_be_read() -> None:
    review = SimpleNamespace(page=1, file="set.pdf", found=[], size=(100, 100), knowledge={})

    def broken(file: str, page: int) -> list[Seg]:
        raise OSError("unreadable")

    run = SimpleNamespace(
        review=review,
        _labels_on=lambda page: [{"id": "D1"}],
        _context=lambda: None,
        _dim_parts_lines=broken,
    )
    assert dim_parts.blocks(run) == []
    assert review.knowledge == {}


def test_a_disagreement_becomes_one_of_code_s_numbered_questions() -> None:
    from app.review.assistant import view_questions

    a_dims, a_drawn, a_words = _base(100, 280, "FRONT VIEW A", "D1", Fraction(4))
    s_dims, s_drawn, s_words = _base(400, 282, "SECTION VIEW S1", "D2", Fraction(7, 2))
    labels, segs = _scene(a_dims + s_dims, a_drawn + s_drawn)
    earlier = view_questions.Question(1, "D", "TOE KICK: tagged in one view only.", (), "tag")
    review = SimpleNamespace(
        page=1,
        file="set.pdf",
        found=a_words + s_words,
        size=(800, 400),
        knowledge={},
        questions=[earlier],
        answers={},
    )
    context = SimpleNamespace(built=True, pair_pages=lambda page: [], phrases=lambda page: [])
    run = SimpleNamespace(
        review=review,
        _labels_on=lambda page: labels,
        _context=lambda: context,
        _dim_parts_lines=lambda file, page: segs,
    )
    assert dim_parts.blocks(run)
    (block,) = dim_parts.question_blocks(run)
    assert 'Q2 [A] TOE KICK height: p1 FRONT VIEW A 4"; p1 SECTION VIEW S1 3 1/2".' in block["text"]
    (asked,) = review.questions[1:]
    assert asked.made_by == "dim_parts" and len(asked.boxes) == 2
    assert view_questions.open_questions(review) == ["Q1", "Q2"]
    # Nothing to add when there is no disagreement, or no question list on the review.
    assert dim_parts.question_blocks(SimpleNamespace(review=SimpleNamespace(knowledge={}))) == []
