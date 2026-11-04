"""The coverage ledger builds the grid, fills it from findings, and shows the holes.

Mirrors `app/review/assistant/coverage_ledger.py`:

- the grid is every item crossed with the questions its kind is asked;
- a finding fills a cell when it names the question's rulebook id, or carries one of the question's
  own words and comes from a lens the question lists;
- an answered cell takes the firmest answer it has: proved beats flagged beats noted beats
  not compared;
- an unanswered cell says why: the item was never identified, nothing came back on its pages, or
  nobody asked;
- an item whose kind is unknown is still charged the questions asked about every item, so it shows
  as a hole instead of vanishing from the grid;
- `never_asked` lists the questions no item in the job got an answer to.

Made-up findings and values only.
"""

from __future__ import annotations

from typing import Any

from app.review.assistant import coverage_ledger as cov
from app.review.assistant.question_list import questions_for


def _finding(
    ident: str,
    page: int,
    *,
    item: str = "",
    why: str = "",
    group: str = "likely_problem",
    made_by: str = "page_reviewer",
    rule: str | None = None,
) -> dict[str, Any]:
    return {
        "id": ident,
        "page": page,
        "item": item,
        "why": why,
        "created_by": made_by,
        "rule_check_id": rule,
        "proof": {"group": group},
    }


def _group_of(finding: Any) -> str:
    return str((finding.get("proof") or {}).get("group") or "likely_problem")


def test_the_grid_is_every_item_crossed_with_its_own_questions() -> None:
    items = (
        cov.Item(key="a", kind="countertop_piece", label="a top", pages=(1,)),
        cov.Item(key="b", kind=None, label="an unknown sheet", pages=(2,)),
    )
    cells = cov.grid(items)
    top = [cell for cell in cells if cell.item == "a"]
    unknown = [cell for cell in cells if cell.item == "b"]
    assert len(top) == len(questions_for("countertop_piece"))
    assert len(unknown) == len(questions_for(None))
    # An item nobody identified is still charged the questions asked about every item.
    assert unknown and len(unknown) < len(top)
    assert all(not cell.answered for cell in cells)


def test_a_finding_fills_the_cells_it_answers_and_the_rest_say_why() -> None:
    items = (
        cov.Item(key="a", kind="countertop_piece", label="a top", pages=(1,)),
        cov.Item(key="b", kind="countertop_piece", label="a second top", pages=(2,)),
        cov.Item(key="c", kind=None, label="an unknown sheet", pages=(3,)),
    )
    found = [
        _finding("F1", 1, item="top", why="the parts do not add up to the printed overall"),
    ]
    made = cov.ledger_for("made up", items, found, group_of=_group_of)
    filled = [cell for cell in made.cells if cell.answered]
    assert [cell.question for cell in filled] == ["string_adds_up"]
    assert filled[0].item == "a"
    assert filled[0].findings == ("F1",)
    assert filled[0].answered_by == ("page_reviewer",)
    assert filled[0].confidence == "flagged"

    gaps = made.gaps()
    # The second top was identified but nothing came back on its page.
    assert gaps["silent"] == len(questions_for("countertop_piece"))
    assert gaps["unidentified"] == len(questions_for(None))
    assert gaps["not_asked"] == len(questions_for("countertop_piece")) - 1
    assert made.unidentified == (items[2],)


def test_a_cell_takes_the_firmest_answer_it_has() -> None:
    items = (cov.Item(key="a", kind="countertop_piece", label="a top", pages=(1,)),)
    found = [
        _finding("F1", 1, why="the parts do not add up", group="likely_problem"),
        _finding("F2", 1, why="the string adds to a different overall", group="wrong"),
    ]
    made = cov.ledger_for("made up", items, found, group_of=_group_of)
    cell = next(one for one in made.cells if one.question == "string_adds_up")
    assert cell.findings == ("F1", "F2")
    assert cell.confidence == "proved"


def test_a_rulebook_id_answers_its_question_whatever_the_words_say() -> None:
    items = (cov.Item(key="a", kind="countertop_piece", label="a top", pages=(1,)),)
    found = [_finding("F1", 1, why="nothing useful here", rule="CT-SINK-OFFSET-FRONT-001")]
    made = cov.ledger_for("made up", items, found, group_of=_group_of)
    answered = {cell.question for cell in made.cells if cell.answered}
    assert "sink_front_offset" in answered


def test_a_finding_on_another_item_does_not_fill_this_item_s_cell() -> None:
    """The coverage gap the audits named: the same check fires on one sheet and not its twin."""
    items = (
        cov.Item(key="a", kind="countertop_piece", label="a top", pages=(1,)),
        cov.Item(key="b", kind="countertop_piece", label="its twin", pages=(2,)),
    )
    found = [_finding("F1", 1, why="the parts do not add up to the printed overall")]
    made = cov.ledger_for("made up", items, found, group_of=_group_of)
    twin = next(one for one in made.cells if one.item == "b" and one.question == "string_adds_up")
    assert not twin.answered
    assert twin.gap == "silent"
    assert made.coverage < 0.5


def test_never_asked_names_the_questions_no_item_answered() -> None:
    items = (cov.Item(key="a", kind="countertop_piece", label="a top", pages=(1,)),)
    found = [_finding("F1", 1, why="the parts do not add up to the printed overall")]
    made = cov.ledger_for("made up", items, found, group_of=_group_of)
    missing = cov.never_asked(made)
    assert "string_adds_up" not in missing
    assert "ct_finished_depth" in missing
    assert len(missing) == len(questions_for("countertop_piece")) - 1


def test_a_page_with_no_findings_has_no_kind_so_its_item_was_never_identified() -> None:
    found = [_finding("F1", 1, item="countertop", why="quartz top depth")]
    items = cov.items_from_run(found, pages=[1, 2])
    assert items[0].kind == "countertop_piece"
    assert items[0].pages == (1,)
    assert items[1].kind is None
    assert cov.kind_of_page([]) is None


def test_items_from_run_makes_one_item_per_vendor_page() -> None:
    found = [
        _finding("F1", 3, item="vanity top", why="the vanity top depth"),
        _finding("F2", 3, item="vanity", why="vanity"),
        _finding("F3", 5, item="key plan", why="the room type tag on the key plan"),
    ]
    items = cov.items_from_run(found, pages=[3, 5])
    assert [one.key for one in items] == ["p3", "p5"]
    assert items[0].kind == "vanity"
    assert items[1].kind == "key_plan"
    assert "vanity" in items[0].label


def test_the_summary_and_the_written_lines_say_the_same_thing() -> None:
    items = (
        cov.Item(key="a", kind="countertop_piece", label="a top", pages=(1,)),
        cov.Item(key="b", kind=None, label="an unknown sheet", pages=(2,)),
    )
    found = [_finding("F1", 1, why="the parts do not add up to the printed overall")]
    made = cov.ledger_for("made up", items, found, group_of=_group_of)
    summary = made.summary()
    assert summary["items"] == 2
    assert summary["identified"] == 1
    assert summary["unidentified"] == 1
    assert summary["answered"] == made.answered == 1
    assert summary["cells"] == len(made.cells)
    assert 0.0 < summary["coverage"] < 1.0
    assert summary["findings"] == 1 and summary["matched"] == 1

    written = cov.lines(made, per_question=True, show_never=True)
    assert written[0] == "made up"
    assert any("coverage" in line for line in written)
    assert any("never identified" in line for line in written)
    assert any("string_adds_up" in line for line in written)


def test_a_finding_that_answers_nothing_is_counted_as_unmatched() -> None:
    items = (cov.Item(key="a", kind="countertop_piece", label="a top", pages=(1,)),)
    found = [_finding("F9", 1, item="", why="qqq zzz nothing like any question")]
    made = cov.ledger_for("made up", items, found, group_of=_group_of)
    assert made.matched == 0
    assert made.unmatched == ("F9",)
    assert made.answered == 0
    assert made.coverage == 0.0
