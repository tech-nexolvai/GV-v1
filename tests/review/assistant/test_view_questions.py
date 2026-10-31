"""Code's questions for the page reviewer (`view_questions`): views, the part x view ledger, each
kind of question, the depth make-up against a sister section, and the reviewer settling them.

Made-up drawings and values only (no job's names, sheets or sizes); no network and no paid call.
The reviewer's loop runs on the scripted stand-in engine of `test_page_reviewer`.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.review.assistant import page_reviewer, view_questions
from app.review.assistant.page_reviewer import system_prompt
from app.review.assistant.view_questions import (
    MAX_QUESTION_NUDGES,
    MAX_QUESTIONS,
    Makeup,
    Question,
    inch_values,
    ledger_of,
    makeups,
    questions,
    views_of,
)
from tests.review.assistant.test_page_reviewer import (
    _notes,
    _results,
    _review,
    _support_issue,
    _text,
    _tool,
)

# ---- a made-up page -------------------------------------------------------------------------------


def _frame(x0: float, y0: float, x1: float, y1: float) -> list[Any]:
    """A drawing's line work: its outline and a few lines across it (one connected drawing)."""
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


def _at(text: str, x: float, y: float, w: float = 0.0, h: float = 6.0) -> dict[str, Any]:
    return {"text": text, "box": [x, y, x + (w or 5.0 * len(text)), y + h]}


def _up(text: str, x: float, y: float) -> dict[str, Any]:
    """A turned size: a tall, narrow box."""
    return {"text": text, "box": [x, y, x + 6.0, y + 5.0 * len(text)]}


PAGE = (1200.0, 800.0)
#: Three drawings: an elevation (left), a section (middle), a plan (top right); titles under each.
SEGMENTS = _frame(100, 100, 400, 400) + _frame(500, 100, 700, 400) + _frame(800, 100, 1100, 200)


def _page(
    elevation: Sequence[dict[str, Any]],
    section: Sequence[dict[str, Any]],
    plan: Sequence[dict[str, Any]] = (),
) -> list[dict[str, Any]]:
    return [
        _at("FRONT VIEW", 200, 440),
        _at("SECTION VIEW", 560, 440),
        _at("S1", 540, 440, 10),
        _at("TOP PLAN", 900, 230),
        _at("DRAWN BY", 900, 700),
        *elevation,
        *section,
        *plan,
    ]


# ---- views and ledger -----------------------------------------------------------------------------


def test_the_page_splits_into_titled_views_from_its_line_work() -> None:
    found = _page([_at("[30]", 150, 300)], [_at("[24]", 600, 300)], [_at("[60]", 900, 150)])
    views, assigned = views_of(found, SEGMENTS, PAGE)
    assert [(v.name, v.kind) for v in views] == [
        ("FRONT VIEW", "elevation"),
        ("SECTION VIEW S1", "section"),
        ("TOP PLAN", "plan"),
    ]
    where = {item["text"]: v for item, v in assigned}
    assert where["[30]"] == 0 and where["[24]"] == 1 and where["[60]"] == 2
    assert where["DRAWN BY"] is None  # the title block belongs to no view


def test_without_line_work_the_titles_alone_split_the_page() -> None:
    found = _page([_at("[30]", 200, 380)], [_at("[24]", 580, 380)])
    views, assigned = views_of(found, None, PAGE)
    names = [v.name for v in views]
    assert sorted(names) == ["FRONT VIEW", "SECTION VIEW S1", "TOP PLAN"]
    where = {item["text"]: v for item, v in assigned}
    assert [names[v] for v in (where["[30]"], where["[24]"]) if v is not None] == [
        "FRONT VIEW",
        "SECTION VIEW S1",
    ]


def test_an_untitled_small_drawing_is_a_detail_and_a_picture_is_no_view() -> None:
    detail = _frame(900, 500, 1000, 600)
    picture = [((50, 600), (90, 600)), ((90, 600), (90, 640)), ((90, 640), (50, 640))]
    found = _page([], []) + [_at("[1 1/2]", 920, 520)]
    views, _ = views_of(found, SEGMENTS + detail + picture, PAGE)
    assert views[-1].kind == "detail" and "untitled detail" in views[-1].name
    assert len(views) == 4


def test_inch_values_read_brackets_turned_text_and_lone_quoted_sizes_only() -> None:
    assert inch_values("610 [24]") == [Decimal(24)]
    assert inch_values("]4/3[") == [Decimal("0.75")]  # a turned value read backwards
    assert inch_values("]2/1 23[") == [Decimal("32.5")]
    assert inch_values("[24'']") == [Decimal(24)]
    assert inch_values('18"') == [Decimal(18)]
    assert inch_values('(5"filler to field') == []  # a size in a note is not one


def test_the_ledger_lists_each_views_callouts_tags_suppliers_and_sizes() -> None:
    found = _page(
        [
            _at("END PANEL", 120, 200),
            _at("WD-07", 120, 207),
            _at("BIN BY GC/OWNER", 250, 250),
            _up("]03[", 380, 200),
        ],
        [_at("ToeKick- WD-09", 550, 350), _at("[24]", 600, 300)],
    )
    ledger = ledger_of(found, SEGMENTS, PAGE)
    callouts = {(c.parts, c.tags, c.supplier, c.view) for c in ledger.callouts}
    assert (("END PANEL",), ("WD-07",), None, 0) in callouts  # its tag on the line under it
    assert (("BIN",), (), "GC/OWNER", 0) in callouts
    assert (("TOE KICK",), ("WD-09",), None, 1) in callouts
    sizes = {(v.inches, v.view, v.turned) for v in ledger.values}
    assert (Decimal(30), 0, True) in sizes and (Decimal(24), 1, False) in sizes


def test_one_tag_ends_a_callout_and_a_column_of_tags_under_it_stays_apart() -> None:
    found = _page(
        [_at("SHELF", 120, 200), _at("WD-07", 120, 207), _at("WD-08", 120, 214)],
        [_at("PANEL WD-05", 550, 200), _at("WD-06", 550, 207)],
    )
    ledger = ledger_of(found, SEGMENTS, PAGE)
    tags = {c.parts: c.tags for c in ledger.callouts}
    assert tags[("SHELF",)] == ("WD-07",) and tags[("PANEL",)] == ("WD-05",)


# ---- each question ----------------------------------------------------------------------------------


def _asked(
    found: list[dict[str, Any]], segments: list[Any] = SEGMENTS, **more: Any
) -> list[Question]:
    return questions(ledger_of(found, segments, PAGE), **more)


def test_a_part_tagged_in_one_view_and_not_in_another_is_asked() -> None:
    made = _asked(
        _page(
            [_at("TOE KICK", 150, 380)], [_at("ToeKick- WD-09", 550, 350), _at("WD-09", 600, 200)]
        )
    )
    (asked,) = [q for q in made if q.made_by == "tag"]
    assert asked.kind == "D" and "TOE KICK" in asked.text and "WD-09" in asked.text
    assert "FRONT VIEW" in asked.text and "no finish tag" in asked.text
    assert asked.boxes[0] == (150.0, 380.0, 190.0, 386.0)


def test_who_supplies_it_said_in_one_view_and_not_in_another_is_asked() -> None:
    made = _asked(_page([_at("BIN BY GC/OWNER", 150, 300)], [_at("BIN", 600, 300)]))
    (asked,) = [q for q in made if q.made_by == "supplier"]
    assert asked.kind == "E" and "BIN" in asked.text and "SECTION VIEW S1" in asked.text


def test_a_section_with_no_finish_tag_is_asked_when_other_views_tag() -> None:
    made = _asked(_page([_at("WD-07", 150, 300), _at("[30]", 150, 200)], [_at("[24]", 600, 300)]))
    (asked,) = [q for q in made if q.made_by == "untagged_view"]
    assert "SECTION VIEW S1" in asked.text and "WD-07" in asked.text


def _section(*parts: str) -> list[dict[str, Any]]:
    """A section printing its overall depth and the given depth parts, across the page."""
    out = [_at("[24]", 560, 120)]
    out += [_at(part, 520 + 40 * k, 380) for k, part in enumerate(parts)]
    return out


def _sister(parts: tuple[str, ...], page: int, tier: int = 2) -> Makeup:
    values = tuple(Decimal(p) for p in parts)
    return Makeup("SECTION VIEW S2", Decimal(24), values, values, (0, 0, 1, 1), page, None, tier)


def test_the_depth_make_up_is_a_sum_of_printed_parts_to_the_overall() -> None:
    (here,) = makeups(ledger_of(_page([], _section("[23]", "[1]", "[12]")), SEGMENTS, PAGE))
    assert here.overall == 24 and here.parts == (Decimal(23), Decimal(1))


def test_fewer_depth_parts_than_the_sets_usual_sister_section_is_asked() -> None:
    usual = ("20.5", "2.75", "0.75")
    sisters = [_sister(usual, 7), _sister(usual, 9), _sister(("12", "11", "0.5", "0.5"), 5)]
    made = _asked(_page([], _section("[23]", "[1]")), sisters=sisters)
    (asked,) = [q for q in made if q.made_by == "depth"]
    assert asked.kind == "A"
    assert "here 23 + 1 = 24" in asked.text
    assert "p7 SECTION VIEW S2 and 1 more" in asked.text
    assert "20 1/2 + 2 3/4 + 3/4 = 24" in asked.text
    assert "not printed here: 20 1/2, 2 3/4, 3/4" in asked.text


def test_one_odd_sister_is_no_pattern_but_the_same_items_drawing_is() -> None:
    here = _page([], _section("[23]", "[1]"))
    odd = [_sister(("12", "11", "0.5", "0.5"), 5)]
    assert not [q for q in _asked(here, sisters=odd) if q.made_by == "depth"]
    same_item = [_sister(("20.5", "2.75", "0.75"), 3, tier=0)]
    assert [q for q in _asked(here, sisters=same_item) if q.made_by == "depth"]


def test_a_section_whose_depth_parts_never_reach_its_overall_is_asked() -> None:
    made = _asked(_page([], _section("[20]", "[3]")))
    (asked,) = [q for q in made if q.made_by == "depth"]
    assert "no printed depth sizes add up to it" in asked.text


def test_a_details_thickness_missing_from_the_elevations_heights_is_asked() -> None:
    detail = _frame(900, 500, 1000, 600)
    found = _page([_up("]4/3[", 380, 120), _up("]03[", 380, 200), _up("]13[", 60, 150)], [])
    found += [_at("[1 1/2]", 920, 520), _at("[3/4]", 920, 560)]
    (asked,) = [q for q in _asked(found, SEGMENTS + detail) if q.made_by == "thickness"]
    assert "1 1/2" in asked.text and "FRONT VIEW's height sizes print 3/4, 30, 31" in asked.text


def test_the_overall_height_printed_in_some_elevations_and_not_another_is_asked() -> None:
    segments = SEGMENTS + _frame(100, 500, 400, 700)
    found = _page([_up("]09[", 380, 150)], [_up("]09[", 680, 150)]) + [
        _at("FRONT VIEW B", 200, 730),
        _up("]04[", 380, 550),
    ]
    found[1] = _at("SIDE VIEW", 560, 440)  # the section is a second elevation here
    (asked,) = [q for q in _asked(found, segments) if q.made_by == "height"]
    assert "print 90" in asked.text and "FRONT VIEW B's tallest height size is 40" in asked.text


def test_a_tag_printed_bare_here_and_in_full_elsewhere_is_asked() -> None:
    found = _page([_at("ST-05", 150, 300), _at("TOP", 150, 200)], [])
    made = _asked(found, elsewhere={"ST-05": ("ST-05 STONE TOP BY GV", 4)})
    (asked,) = [q for q in made if q.made_by == "bare_tag"]
    assert "p4 prints 'ST-05 STONE TOP BY GV'" in asked.text


def test_a_size_in_one_view_only_with_a_near_size_in_another_is_asked() -> None:
    made = _asked(_page([_at("[100 1/2]", 200, 380)], [], [_at("[100]", 900, 180)]))
    (asked,) = [q for q in made if q.made_by == "near"]
    assert "100 1/2" in asked.text and "100 at" in asked.text
    # A height is never compared with a width.
    found = _page([_up("]001[", 380, 150)], [], [_at("[100 1/2]", 900, 180)])
    assert not [q for q in _asked(found) if q.made_by == "near"]


def test_a_part_tagged_in_one_view_and_not_called_out_in_another_is_asked() -> None:
    found = _page([_at("END PANEL WD-07", 150, 300)], [_at("[24]", 600, 300)])
    (asked,) = [q for q in _asked(found) if q.made_by == "missing_tagged"]
    assert "END PANEL" in asked.text and "not called out in SECTION VIEW S1" in asked.text


def test_questions_are_ranked_numbered_and_capped() -> None:
    found = _page(
        [_at("TOE KICK", 150, 380), _at("BIN BY GC/OWNER", 150, 300)],
        [_at("ToeKick- WD-09", 550, 350), _at("BIN", 600, 300)],
    )
    made = _asked(found)
    assert [q.n for q in made] == list(range(1, len(made) + 1))
    assert [q.made_by for q in made][:2] == ["tag", "supplier"]  # the likeliest first
    many = [_at(f"PANEL {k}", 150, 110 + 9 * k) for k in range(30)]
    assert len(_asked(_page(many, [_at("FIXED PANEL WD-07", 550, 350)]))) <= MAX_QUESTIONS


def test_questions_come_from_the_drawing_text_only() -> None:
    """No values are invented: every size a question quotes is printed on the page or a sister."""
    found = _page([_at("[100 1/2]", 200, 380)], [], [_at("[100]", 900, 180)])
    listing = view_questions.listing(_asked(found))
    assert listing.startswith("Q1 [A] Sizes in one view only")
    assert "Boxes: " in listing


# ---- in the reviewer -----------------------------------------------------------------------------

TWO = [
    Question(1, "D", "TOE KICK: tagged in S1, no tag in view A.", ((10, 10, 40, 20),), "tag"),
    Question(2, "E", "BIN: by GC in the front view, not in S2.", ((50, 50, 80, 60),), "supplier"),
]


@pytest.fixture
def two_questions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(view_questions, "questions", lambda *a, **k: list(TWO))


def test_the_questions_are_in_the_first_message_and_the_brief_says_how_to_settle_them(
    tmp_path: Path, two_questions: None
) -> None:
    engine, review, _ended, _ = _review(
        tmp_path,
        [[_tool("answer_question", questions=["Q1", "Q2"], why="no such parts here")]],
    )
    first = engine.script.calls[0].messages[0]["content"]
    texts = [b["text"] for b in first if b.get("type") == "text"]
    (asked,) = [t for t in texts if "Questions code made" in t]
    assert "Q1 [D] TOE KICK" in asked and "Boxes: 10,10,40,20" in asked
    assert texts[-1].startswith("The nine tiles are above")
    assert 'question "Qn"' in system_prompt() and "answer_question" in system_prompt()
    assert "question" in page_reviewer.REPORT_ISSUE["input_schema"]["properties"]
    assert review.answers == {"Q1": "ok", "Q2": "ok"}


def test_a_report_settles_its_question_and_answer_question_settles_the_rest(
    tmp_path: Path, two_questions: None
) -> None:
    _engine, review, _ended, reviews = _review(
        tmp_path,
        [
            [
                _support_issue(question="Q1"),
                _tool("answer_question", questions=["Q2"], why="the section shows no bin"),
            ],
            [_text("Done.")],
        ],
    )
    assert review.answers == {"Q1": "issue", "Q2": "ok"}
    assert review.question_nudges == 0
    reviews.pages["p2"] = review
    (row,) = reviews.block()["pages"]
    assert row["questions_made"] == 2
    assert row["questions_answered_issue"] == 1 and row["questions_answered_ok"] == 1
    assert row["questions_left"] == 0


def test_open_questions_send_it_back_at_most_three_times(
    tmp_path: Path, two_questions: None
) -> None:
    turns: list[Any] = [[_text("I am done.")]] * (MAX_QUESTION_NUDGES + 1)
    engine, review, ended, reviews = _review(tmp_path, turns)
    sent = [n for n in _notes(engine.script.calls[-1].messages) if "questions are not settled" in n]
    assert len(sent) == MAX_QUESTION_NUDGES
    assert "Q1: TOE KICK" in sent[0] and "Q2: BIN" in sent[0]
    assert len(engine.script.calls) == MAX_QUESTION_NUDGES + 1
    assert ended[2] is None
    reviews.pages["p2"] = review
    (row,) = reviews.block()["pages"]
    assert row["questions_left"] == 2 and row["questions_answered_ok"] == 0


def test_a_settled_question_is_not_sent_again_and_an_unknown_one_is_refused(
    tmp_path: Path, two_questions: None
) -> None:
    engine, review, _ended, _ = _review(
        tmp_path,
        [
            [_tool("answer_question", questions=["Q9"], why="x")],
            [_tool("answer_question", questions=["Q1"], why="no toe kick in view A")],
            [_text("Done.")],
            [_tool("answer_question", questions=["Q2"], why="fine")],
            [_text("Done.")],
        ],
    )
    assert _results(engine.script.calls[1].messages)[-1].startswith("Refused")
    sent = [n for n in _notes(engine.script.calls[-1].messages) if "questions are not settled" in n]
    assert len(sent) == 1 and "Q2: BIN" in sent[0] and "Q1:" not in sent[0]
    assert review.answers == {"Q1": "ok", "Q2": "ok"}


def test_no_questions_change_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(view_questions, "questions", lambda *a, **k: [])
    engine, review, _ended, reviews = _review(tmp_path, [[_text("Done.")]])
    first = engine.script.calls[0].messages[0]["content"]
    assert not any("Questions code made" in b.get("text", "") for b in first)
    assert len(engine.script.calls) == 1
    reviews.pages["p2"] = review
    (row,) = reviews.block()["pages"]
    assert row["questions_made"] == 0 and row["questions_left"] == 0


def test_a_failure_making_questions_never_stops_the_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*a: Any, **k: Any) -> Any:
        raise RuntimeError("made-up failure")

    monkeypatch.setattr(view_questions, "ledger_of", broken)
    _engine, review, ended, _ = _review(tmp_path, [[_text("Done.")]])
    assert review.questions == [] and ended[1] is True
