"""The same span in two views (`cross_view`): made-up drawings, lines and values only (no job's
names, sheets or sizes); no network and no paid call."""

from __future__ import annotations

from collections.abc import Sequence
from fractions import Fraction
from types import SimpleNamespace
from typing import Any

from app.review.assistant import cross_view
from app.review.assistant.cross_view import align, page_pairs, page_views
from app.review.assistant.ledger import format_inches

Seg = tuple[tuple[float, float], tuple[float, float]]
#: Points per inch of the made-up drawings (a scale of 1:24).
PPI = 3.0
PAGE = (1000.0, 800.0)


def _drawing(
    x0: float, y0: float, x1: float, y1: float, xs: Sequence[float] = (), ys: Sequence[float] = ()
) -> list[Seg]:
    """A view's line work: its outline in pieces, with lines across it at `xs` and `ys`."""
    out: list[Seg] = []
    for k in range(4):
        a, b = x0 + (x1 - x0) * k / 4, x0 + (x1 - x0) * (k + 1) / 4
        out += [((a, y0), (b, y0)), ((a, y1), (b, y1))]
        c, d = y0 + (y1 - y0) * k / 4, y0 + (y1 - y0) * (k + 1) / 4
        out += [((x0, c), (x0, d)), ((x1, c), (x1, d))]
    out += [((x, y0), (x, y1)) for x in xs]
    out += [((x0, y), (x1, y)) for y in ys]
    return out


def _dim(
    ident: str,
    inches: Fraction | int,
    orientation: str,
    a: float,
    b: float,
    across: float,
    far: float,
    text: str | None = None,
) -> dict[str, Any]:
    """A label printing `inches` over its dimension line from `a` to `b` (along) at `across`, its
    extension lines running from `far` (the object) to the line."""
    if orientation == "h":
        line = [[a, across], [b, across]]
        ext = [[[a, far], [a, across]], [[b, far], [b, across]]]
        box = [(a + b) / 2 - 6, across - 8, (a + b) / 2 + 6, across - 1]
    else:
        line = [[across, a], [across, b]]
        ext = [[[far, a], [across, a]], [[far, b], [across, b]]]
        box = [across - 8, (a + b) / 2 - 6, across - 1, (a + b) / 2 + 6]
    return {
        "id": ident,
        "text": text or format_inches(Fraction(inches)),
        "inches": str(float(Fraction(inches))),
        "box": box,
        "orientation": orientation,
        "route": "text",
        "dim_line": line,
        "extension": ext,
        "scale_ok": True,
    }


def _title(text: str, x: float, y: float) -> dict[str, Any]:
    return {"text": text, "box": [x, y, x + 6.0 * len(text), y + 8]}


def _x(inches: float | Fraction, origin: float = 100.0) -> float:
    return origin + float(inches) * PPI


def _plan_and_front(
    front_bay: Fraction = Fraction(48), drawn_bay: float = 48.0
) -> tuple[list[dict[str, Any]], list[Seg], list[dict[str, Any]]]:
    """A top view (y 100-160) drawn over its front view (y 300-420), lined up in x: a 1" end
    panel, a 49" bay, a 1" divider, a 48" bay, a 1" end panel (100" overall). The front view prints
    `front_bay` over its second bay, drawn `drawn_bay` long (the end panel after it moves along)."""
    plan_x = [_x(v) for v in (0, 1, 50, 51, 99, 100)]
    front_x = [_x(v) for v in (0, 1, 50, 51, 51 + drawn_bay, 52 + drawn_bay)]
    labels = [
        _dim("P1", 1, "h", plan_x[0], plan_x[1], 180, 160),
        _dim("P2", 49, "h", plan_x[1], plan_x[2], 180, 160),
        _dim("P3", 48, "h", plan_x[3], plan_x[4], 180, 160),
        _dim("P4", 1, "h", plan_x[4], plan_x[5], 180, 160),
        _dim("P5", 100, "h", plan_x[0], plan_x[5], 192, 160),
        _dim("F1", 1, "h", front_x[0], front_x[1], 280, 300),
        _dim("F2", 49, "h", front_x[1], front_x[2], 280, 300),
        _dim("F3", front_bay, "h", front_x[3], front_x[4], 280, 300),
        _dim("F4", 1, "h", front_x[4], front_x[5], 280, 300),
        _dim(
            "F5",
            Fraction(round((front_x[5] - front_x[0]) / PPI * 16), 16),
            "h",
            front_x[0],
            front_x[5],
            268,
            300,
        ),
        _dim("F6", 30, "v", 300, 390, 80, front_x[0]),
        _dim("F7", 10, "v", 390, 420, 80, front_x[0]),
        _dim("F8", 40, "v", 300, 420, 70, front_x[0]),
    ]
    segs = _drawing(plan_x[0], 100, plan_x[5], 160, plan_x[1:5]) + _drawing(
        front_x[0], 300, front_x[5], 420, front_x[1:5], (390,)
    )
    found = [_title("TOP VIEW", 220, 205), _title("FRONT VIEW", 220, 440)]
    return labels, segs, found


def _spans(labels: list[dict[str, Any]], segs: list[Seg], found: list[dict[str, Any]]) -> Any:
    own = page_views(1, labels, found, segs, PAGE)
    pairs, matched = page_pairs(own)
    return own, pairs, matched


def _by(pairs: Sequence[cross_view.SpanPair]) -> dict[tuple[str, str], cross_view.SpanPair]:
    return {(p.a[0].id, p.b[0].id if p.b else ""): p for p in pairs}


# ---- aligned views ------------------------------------------------------------------------------


def test_a_plan_over_its_elevation_shares_its_widths_by_projection() -> None:
    labels, segs, found = _plan_and_front()
    own, pairs, matched = _spans(labels, segs, found)
    assert [(v.name, v.kind) for v in own.views] == [
        ("TOP VIEW", "plan"),
        ("FRONT VIEW", "elevation"),
    ]
    assert matched == 1
    got = _by(pairs)
    assert set(got) == {("P1", "F1"), ("P2", "F2"), ("P3", "F3"), ("P4", "F4"), ("P5", "F5")}
    assert all(p.how == "aligned" and not p.differs for p in pairs)
    assert cross_view.differences(pairs) == []


def test_a_typed_over_value_on_the_same_projected_span_is_a_question_ready_for_red() -> None:
    labels, segs, found = _plan_and_front(front_bay=Fraction(97, 2))  # 48 1/2" over a 48" line
    _own, pairs, _ = _spans(labels, segs, found)
    (asked,) = cross_view.differences(pairs)
    assert (asked.a[0].id, asked.b[0].id) == ("P3", "F3") and asked.how == "aligned"
    assert asked.code_proved  # two clean text values over one span proved by projection ...
    assert not cross_view.CROSS_VIEW_RED and cross_view.red_ready(pairs) == []  # ... red stays off
    text = cross_view.question_text(asked)
    assert 'TOP VIEW prints 48"' in text and 'FRONT VIEW prints 48 1/2"' in text
    assert "line up on the sheet" in text and 'both drawn 48" long' in text


def test_one_bay_drawn_longer_in_one_view_is_matched_across_the_stretch() -> None:
    labels, segs, found = _plan_and_front(front_bay=Fraction(49), drawn_bay=49.0)
    _own, pairs, _ = _spans(labels, segs, found)
    got = _by(pairs)
    assert got[("P3", "F3")].how == "stretched" and got[("P3", "F3")].differs
    assert got[("P5", "F5")].how == "stretched" and got[("P5", "F5")].b[0].value == '101"'
    assert got[("P4", "F4")].how == "shifted" and not got[("P4", "F4")].differs
    assert got[("P2", "F2")].how == "aligned"
    asked = cross_view.differences(pairs)
    assert {(p.a[0].id, p.b[0].id) for p in asked} == {("P3", "F3"), ("P5", "F5")}
    assert not any(p.code_proved for p in asked)  # a stretch is never projection proof
    assert "draws a piece between them longer" in cross_view.question_text(asked[0])


def test_a_stretch_too_long_to_be_one_piece_is_not_matched() -> None:
    labels, segs, found = _plan_and_front(front_bay=Fraction(60), drawn_bay=60.0)
    _own, pairs, _ = _spans(labels, segs, found)
    assert ("P3", "F3") not in _by(pairs) and ("P5", "F5") not in _by(pairs)
    assert ("P2", "F2") in _by(pairs)


def test_a_label_far_from_its_line_proves_no_span() -> None:
    labels, segs, found = _plan_and_front()
    labels[7] = _dim(
        "F3", 30, "h", labels[7]["dim_line"][0][0], labels[7]["dim_line"][1][0], 280, 300
    )
    _own, pairs, _ = _spans(labels, segs, found)
    assert ("P3", "F3") not in _by(pairs)  # 30" over a 48" line: another dimension's label
    assert cross_view.differences(pairs) == []


def test_two_elevations_never_share_widths() -> None:
    a = cross_view.ViewDims(1, "FRONT VIEW A", "elevation", (0, 0, 100, 100), PPI)
    b = cross_view.ViewDims(1, "FRONT VIEW B", "elevation", (0, 200, 100, 300), PPI)
    plan = cross_view.ViewDims(1, "TOP PLAN", "plan", (0, 400, 100, 500), PPI)
    section = cross_view.ViewDims(1, "SECTION VIEW", "section", (200, 0, 300, 100), PPI)
    found = cross_view._pairs(cross_view.PageViews(1, [a, b, plan, section]))
    assert [(x.name, y.name, axis) for x, y, axis in found] == [
        ("FRONT VIEW A", "FRONT VIEW B", "v"),
        ("FRONT VIEW A", "TOP PLAN", "h"),
        ("FRONT VIEW A", "SECTION VIEW", "v"),
        ("FRONT VIEW B", "TOP PLAN", "h"),
        ("FRONT VIEW B", "SECTION VIEW", "v"),
    ]


# ---- pasted views ---------------------------------------------------------------------------------


def _elevation_and_section(
    offset: float, section_counter: Fraction = Fraction(30), print_overall: bool = True
) -> tuple[list[dict[str, Any]], list[Seg], list[dict[str, Any]]]:
    """An elevation (x 100-400) and a section beside it (x 600-700), the section `offset` points
    lower on the sheet: toe kick 4", cabinet 30" (the section prints `section_counter`), top 1 1/2",
    each view also printing its overall (the section only when `print_overall`)."""
    ys = [_x(v, 300) for v in (0, Fraction(3, 2), Fraction(63, 2), Fraction(71, 2))]
    ss = [y + offset for y in ys]
    labels = [
        _dim("E1", Fraction(3, 2), "v", ys[0], ys[1], 80, 100),
        _dim("E2", 30, "v", ys[1], ys[2], 80, 100),
        _dim("E3", 4, "v", ys[2], ys[3], 80, 100),
        _dim("E4", Fraction(71, 2), "v", ys[0], ys[3], 70, 100),
        _dim("E5", 100, "h", 100, 400, ys[3] + 20, ys[3]),
        _dim("S1", Fraction(3, 2), "v", ss[0], ss[1], 720, 700),
        _dim("S2", section_counter, "v", ss[1], ss[2], 720, 700),
        _dim("S3", 4, "v", ss[2], ss[3], 720, 700),
        _dim("S5", 24, "h", 600, 672, ss[3] + 20, ss[3]),
    ]
    if print_overall:
        labels.append(_dim("S4", Fraction(71, 2), "v", ss[0], ss[3], 730, 700))
    segs = _drawing(100, ys[0], 400, ys[3], (200, 300), ys[1:3]) + _drawing(
        600, ss[0], 700, ss[3], (650,), ss[1:3]
    )
    found = [_title("FRONT VIEW", 220, ys[3] + 40), _title("SECTION VIEW", 610, ss[3] + 40)]
    return labels, segs, found


def test_side_by_side_views_share_their_heights() -> None:
    labels, segs, found = _elevation_and_section(0.0)
    _own, pairs, matched = _spans(labels, segs, found)
    assert matched == 1
    assert set(_by(pairs)) == {("E1", "S1"), ("E2", "S2"), ("E3", "S3"), ("E4", "S4")}
    assert {p.how for p in pairs} == {"aligned"}


def test_pasted_views_match_by_the_same_sequence_of_heights() -> None:
    labels, segs, found = _elevation_and_section(37.0, section_counter=Fraction(31))
    _own, pairs, _ = _spans(labels, segs, found)
    got = _by(pairs)
    assert {p.how for p in pairs} == {"pasted"}
    # The section's 30" line printed 31": the same drawn span, two printed values.
    assert got[("E2", "S2")].differs and not got[("E2", "S2")].code_proved


def test_an_overall_one_view_does_not_print_is_asked_only_by_projection() -> None:
    labels, segs, found = _elevation_and_section(0.0, print_overall=False)
    _own, pairs, _ = _spans(labels, segs, found)
    (asked,) = cross_view.differences(pairs)
    assert (
        asked.missing and asked.first.name == "FRONT VIEW" and asked.second.name == "SECTION VIEW"
    )
    assert "Is its overall height missing?" in cross_view.question_text(asked)
    # The section prints that value somewhere (no line read): nothing to ask.
    printed = found + [{"text": '35 1/2"', "box": [640, 330, 670, 338]}]
    _own, pairs, _ = _spans(labels, segs, printed)
    assert cross_view.differences(pairs) == []
    # Pasted views (no projection): never asked.
    labels, segs, found = _elevation_and_section(37.0, print_overall=False)
    _own, pairs, _ = _spans(labels, segs, found)
    assert pairs and cross_view.differences(pairs) == []


# ---- the matching itself --------------------------------------------------------------------------


def test_an_ambiguous_sequence_is_refused() -> None:
    # Equal widths: three stations fit at two places of the other view's four.
    assert align([0, 10, 20], [0, 10, 20, 30], aligned=False) is None
    # Unequal: one place only.
    match = align([0, 10, 25], [5, 15, 30, 40], aligned=False)
    assert match is not None and [(i, j) for i, j, _r in match.pairs] == [(0, 0), (1, 1), (2, 2)]
    # Too few edges to be sure.
    assert align([0, 10], [5, 15], aligned=False) is None


def test_aligned_views_keep_the_projection_over_a_repeated_width() -> None:
    match = align([0, 10, 20, 30, 40], [0, 10, 20, 30, 40, 50], aligned=True)
    assert match is not None and all(i == j for i, j, _r in match.pairs)
    assert match.offsets == (0.0,)
    # Lined up on the sheet but no run at offset 0 of two edges: nothing.
    assert align([0, 10, 20], [3, 13, 23], aligned=True) is None


def test_two_scales_are_compared_in_inches() -> None:
    a = cross_view.ViewDims(1, "TOP VIEW", "plan", (0, 0, 400, 100), 3.0)
    b = cross_view.ViewDims(2, "the untitled plan", "plan", (0, 0, 300, 100), 2.0)
    for n, (lo, hi, value) in enumerate([(0, 30, 30), (30, 70, 40), (70, 95, 25), (0, 95, 95)]):
        for view, ppi, origin in ((a, 3.0, 50.0), (b, 2.0, 10.0)):
            view.dims.append(
                cross_view.Dim(
                    f"D{n}",
                    view.page,
                    f'{value}"',
                    Fraction(value),
                    "h",
                    origin + lo * ppi,
                    origin + hi * ppi,
                    (0, 0, 1, 1),
                    "text",
                    True,
                    Fraction(value),
                    True,
                )
            )
    b.dims[1] = cross_view.Dim(
        "D1",
        2,
        '41"',
        Fraction(41),
        "h",
        70.0,
        150.0,
        (0, 0, 1, 1),
        "text",
        True,
        Fraction(40),
        False,
    )
    pairs, matched = page_pairs(cross_view.PageViews(1, [a]), [cross_view.PageViews(2, [b])])
    assert matched == 1
    (asked,) = cross_view.differences(pairs)
    assert asked.how == "pasted" and (asked.a[0].value, asked.b[0].value) == ('40"', '41"')
    assert 'p2 the untitled plan prints 41"' in cross_view.question_text(asked)


def test_another_drawing_matching_two_views_is_left_out() -> None:
    def view(page: int, name: str, origin: float) -> cross_view.ViewDims:
        out = cross_view.ViewDims(page, name, "plan", (0, 0, 400, 100), 3.0)
        for n, (lo, hi) in enumerate([(0, 30), (30, 70), (70, 95)]):
            out.dims.append(
                cross_view.Dim(
                    f"D{n}",
                    page,
                    "x",
                    Fraction(hi - lo),
                    "h",
                    origin + lo * 3,
                    origin + hi * 3,
                    (0, 0, 1, 1),
                    "text",
                    True,
                    Fraction(hi - lo),
                    True,
                )
            )
        return out

    mine = cross_view.PageViews(1, [view(1, "TOP VIEW", 0)])
    other = cross_view.PageViews(2, [view(2, "PLAN A", 0), view(2, "PLAN B", 500)])
    assert page_pairs(mine, [other]) == ([], 0)


# ---- the page reviewer's hook ---------------------------------------------------------------------


def _run(
    labels: list[dict[str, Any]], segs: list[Seg], found: list[dict[str, Any]], asked: Any
) -> Any:
    review = SimpleNamespace(
        page=1, file="set.pdf", found=found, size=PAGE, knowledge={}, questions=asked, answers={}
    )
    context = SimpleNamespace(built=True, pair_pages=lambda page: [], phrases=lambda page: [])
    return SimpleNamespace(
        review=review,
        _labels_on=lambda page: labels,
        _context=lambda: context,
        _dim_parts_lines=lambda file, page: segs,
    )


def test_the_hook_adds_numbered_questions_and_logs_the_counts() -> None:
    from app.review.assistant import view_questions

    labels, segs, found = _plan_and_front(front_bay=Fraction(49), drawn_bay=49.0)
    earlier = view_questions.Question(1, "D", "TOE KICK: tagged in one view only.", (), "tag")
    run = _run(labels, segs, found, [earlier])
    (block,) = cross_view.question_blocks(run)
    assert 'Q2 [A] Same width span in two views: p1 TOP VIEW prints 48"' in block["text"]
    assert "Q3 [A]" in block["text"]
    assert [q.made_by for q in run.review.questions[1:]] == ["cross_view", "cross_view"]
    assert view_questions.open_questions(run.review) == ["Q1", "Q2", "Q3"]
    assert run.review.knowledge["spans_matched"] == 5
    assert run.review.knowledge["span_disagreements"] == 2
    assert len(run.review.knowledge["cross_view"]) == 2


def test_the_hook_logs_but_asks_nothing_without_a_question_list_or_a_readable_page() -> None:
    labels, segs, found = _plan_and_front()
    run = _run(labels, segs, found, None)
    assert cross_view.question_blocks(run) == []
    assert run.review.knowledge["spans_matched"] == 5
    assert run.review.knowledge["span_disagreements"] == 0

    def broken(file: str, page: int) -> list[Seg]:
        raise OSError("unreadable")

    run = _run(labels, segs, found, [])
    run._dim_parts_lines = broken
    assert cross_view.question_blocks(run) == []
    assert run.review.knowledge == {}
