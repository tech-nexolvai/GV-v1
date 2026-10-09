"""The assistant's records snapshot (#1128): selected from the screens' records, never re-derived.

Synthetic records only. What matters: outcomes and numbers are the API's own (display text and exact
value), records that need the reviewer are never the ones a cap drops, nothing is listed twice, and
the prompt carries short ids and display text but no ids, names or times the model does not need.
"""

from __future__ import annotations

import json
from fractions import Fraction
from uuid import uuid4

from app.review.assistant import records as records_module
from app.review.assistant.records import prompt_records
from app.schemas.visual_ui import CountertopResultsOut
from tests.review.assistant.synthetic import (
    FINDING_SINK,
    ROW_FAIL,
    ROW_PASS,
    Decision,
    Inputs,
    _Action,
    countertop_results,
    snapshot,
)

SNAPSHOT = snapshot()


def test_numbers_keep_the_display_text_and_the_exact_value() -> None:
    page_4 = SNAPSHOT.countertops[0]
    assert page_4.printed is not None and page_4.needed is not None
    assert (page_4.printed.display, page_4.printed.exact) == ('84 1/2"', "169/2")
    assert (page_4.needed.display, page_4.needed.exact) == ('85"', "85/1")
    assert page_4.difference is not None and page_4.difference.display == '-1/2"'
    assert [piece.number for piece in page_4.pieces] == [1, 2, 3]


def test_outcomes_are_the_screens_words_and_the_engines_value() -> None:
    assert [(c.id, c.page_number, c.outcome, c.outcome_label) for c in SNAPSHOT.countertops] == [
        ("C1", 4, "FAIL", "Needs correction"),
        ("C2", 7, "REVIEW_REQUIRED", "Needs your decision"),
        ("C3", 9, "PASS", "Looks right"),
    ]


def test_records_carry_the_screens_ids() -> None:
    assert SNAPSHOT.countertops[0].record_id == str(ROW_FAIL)
    assert SNAPSHOT.by_record_id(str(ROW_PASS)) is SNAPSHOT.countertops[2]
    assert SNAPSHOT.other_checks[0].record_id == str(FINDING_SINK)


def test_hold_wall_decision_and_rule_come_through() -> None:
    held, passed = SNAPSHOT.countertops[1], SNAPSHOT.countertops[2]
    assert held.hold_code == "row-choice-split"
    assert held.architect is not None and held.architect.not_compared_reason
    assert passed.decision is not None
    assert passed.decision.carried_over is True
    assert passed.decision.words == "confirmed by the reviewer"
    assert passed.wall == "island; no wall ends" and passed.wall_source == "reviewer"
    rule = SNAPSHOT.countertops[0].rule
    assert rule is not None and (rule.name, rule.tolerance) == ("Countertop width", '0"')


def test_findings_shown_on_a_countertop_are_not_listed_again() -> None:
    assert [finding.check_name for finding in SNAPSHOT.other_checks] == ["Sink centre line"]


def test_other_checks_carry_their_decision() -> None:
    decided = snapshot(Inputs(decisions={FINDING_SINK: Decision(_Action("dismiss", "n/a here"))}))
    decision = decided.other_checks[0].decision
    assert decision is not None and decision.words == "dismissed by the reviewer"
    assert decision.note == "n/a here"


def test_readiness_lists_what_needs_the_reviewer_in_page_order() -> None:
    readiness = SNAPSHOT.readiness
    assert readiness.can_sign_off is False
    assert readiness.blocking_findings == 3
    assert readiness.needing_you == ("C1", "C2", "F1")


def test_page_lists_come_through() -> None:
    assert [note.page_number for note in SNAPSHOT.pages_without_countertop] == [3]
    assert [note.page_number for note in SNAPSHOT.rows_not_checked] == [9]
    assert SNAPSHOT.pages() == frozenset({3, 4, 5, 7, 9})


def test_a_cap_never_drops_a_record_that_needs_the_reviewer(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(records_module, "MAX_COUNTERTOPS", 2)
    capped = snapshot()
    assert [c.page_number for c in capped.countertops] == [4, 7]
    assert dict(capped.omitted) == {"countertops": 1}


def test_long_lists_are_capped_and_counted() -> None:
    base = countertop_results()
    many = CountertopResultsOut(
        package_id=base.package_id,
        revision_id=base.revision_id,
        items=tuple(
            base.items[2].model_copy(update={"row_id": uuid4(), "page_number": page})
            for page in range(10, 10 + records_module.MAX_COUNTERTOPS + 5)
        ),
    )
    capped = snapshot(Inputs(countertops=many))
    assert len(capped.countertops) == records_module.MAX_COUNTERTOPS
    assert capped.omitted["countertops"] == 5


def test_the_prompt_has_short_ids_and_display_text_only() -> None:
    text = prompt_records(SNAPSHOT)
    document = json.loads(text)
    assert [item["id"] for item in document["countertops"]] == ["C1", "C2", "C3"]
    assert document["countertops"][0]["printed_overall"] == '84 1/2"'
    assert document["sign_off"]["records_needing_reviewer"] == ["C1", "C2", "F1"]
    for hidden in (str(ROW_FAIL), str(FINDING_SINK), "reviewer-one", "2026-10-01", "169/2"):
        assert hidden not in text


def test_the_prompt_drops_piece_detail_when_it_is_too_long(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    full = prompt_records(SNAPSHOT)
    monkeypatch.setattr(records_module, "MAX_PROMPT_CHARS", len(full) - 1)
    short = json.loads(prompt_records(SNAPSHOT))
    assert "pieces" not in short["countertops"][0]
    assert short["countertops"][0]["pieces_not_listed"] == 3


def test_the_same_records_give_the_same_snapshot() -> None:
    assert snapshot() == snapshot()
    assert prompt_records(snapshot()) == prompt_records(snapshot())


def test_the_exact_value_is_a_fraction_never_a_float() -> None:
    printed = SNAPSHOT.countertops[0].printed
    assert printed is not None and printed.exact is not None
    assert Fraction(printed.exact) == Fraction(169, 2)
