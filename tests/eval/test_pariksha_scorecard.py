"""Storing Pariksha scorecards and comparing two of them (#1219).

Verification for: `eval/pariksha/scorecard.py` and `eval/pariksha/compare.py`. Synthetic scorecards
only; no model call, no client value.
"""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path

import pytest

from eval.metrics import MetricResult
from eval.pariksha.compare import compare_scorecards
from eval.pariksha.score import (
    AGREED_RIGHT,
    AGREED_WRONG,
    HELD,
    NOT_ASKED,
    CaseScore,
    ReaderScore,
    Scorecard,
)
from eval.pariksha.scorecard import (
    SCORECARD_FORMAT,
    ScorecardError,
    read_scorecard,
    write_scorecard,
)
from eval.regression import IncomparableRuns


def _case(case_id: str, outcome: str) -> CaseScore:
    return CaseScore(
        case_id=case_id,
        question=f"claude-slot-span/{case_id}",
        expected='3/4"',
        outcome=outcome,
        readers=(
            ReaderScore(model_id="reader-one", answer='3/4"', verdict="right"),
            ReaderScore(model_id="reader-two", answer="(unsure)", verdict="unsure"),
        ),
        cost_micros=1500,
        note="",
    )


def _card(
    outcomes: dict[str, str],
    *,
    agent: str = "akshara",
    code: str = "v1",
    key_sha: str = "f" * 64,
    document: str | None = None,
) -> Scorecard:
    cases = tuple(_case(case_id, outcome) for case_id, outcome in outcomes.items())
    asked = sum(1 for case in cases if case.outcome != NOT_ASKED)

    def rate(outcome: str) -> MetricResult:
        count = sum(1 for case in cases if case.outcome == outcome)
        value = Fraction(count, asked) if asked else None
        return MetricResult(
            key=f"pair_{outcome}", value=value, numerator=count if asked else 0, denominator=asked
        )

    return Scorecard(
        agent=agent,
        key_name="synthetic",
        key_version="1",
        key_sha256=key_sha,
        code_version=code,
        scorer_version="scorer-test",
        extractor_versions={"agent": agent, "prompt_ids": f"prompt-{code}", "model_ids": "a,b"},
        source={
            "database": "synthetic",
            "extraction_run_id": f"run-{code}",
            **({} if document is None else {"document_sha256": document}),
        },
        metrics={
            "asked": MetricResult(
                key="asked",
                value=Fraction(asked, len(cases)),
                numerator=asked,
                denominator=len(cases),
            ),
            "pair_agreed_right": rate(AGREED_RIGHT),
            "pair_agreed_wrong": rate(AGREED_WRONG),
            "pair_held": rate(HELD),
            "cost_micros_per_question": MetricResult(
                key="cost_micros_per_question",
                value=Fraction(1000, 3),
                numerator=1000,
                denominator=3,
            ),
        },
        cases=cases,
        created_at="2026-10-14T00:00:00+00:00",
    )


def test_a_scorecard_round_trips_exactly_through_its_versioned_file(tmp_path: Path) -> None:
    card = _card({"c1": AGREED_RIGHT, "c2": HELD, "c3": NOT_ASKED})
    path = tmp_path / "card.json"

    write_scorecard(card, path)
    stored = json.loads(path.read_text())
    again = read_scorecard(path)

    assert stored["format"] == SCORECARD_FORMAT
    assert len(stored["sha256"]) == 64
    assert again == card
    # Exact: a repeating fraction is stored as its numerator and denominator, never as a decimal.
    assert again.metrics["cost_micros_per_question"].value == Fraction(1000, 3)


def test_a_stored_scorecard_is_never_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "card.json"
    write_scorecard(_card({"c1": AGREED_RIGHT}), path)

    with pytest.raises(FileExistsError):
        write_scorecard(_card({"c1": HELD}), path)


def test_an_edited_scorecard_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "card.json"
    write_scorecard(_card({"c1": AGREED_WRONG}), path)
    stored = json.loads(path.read_text())
    stored["cases"][0]["outcome"] = AGREED_RIGHT
    path.write_text(json.dumps(stored))

    with pytest.raises(ScorecardError, match="changed"):
        read_scorecard(path)


def test_compare_accepts_a_change_that_is_better_with_no_new_wrong_answer() -> None:
    before = _card({"c1": AGREED_RIGHT, "c2": HELD, "c3": AGREED_WRONG, "c4": HELD})
    after = _card({"c1": AGREED_RIGHT, "c2": AGREED_RIGHT, "c3": HELD, "c4": HELD}, code="v2")

    comparison = compare_scorecards(before, after)

    assert comparison.accepted
    assert comparison.verdict == "ACCEPT (better, 0 new wrong)"
    assert [change.case_id for change in comparison.fixed] == ["c2"]
    assert comparison.broke == ()
    assert comparison.new_wrong == ()
    assert comparison.unchanged == 2  # c1 and c4; c3 moved from wrong to held
    right = next(delta for delta in comparison.metrics if delta.metric == "pair_agreed_right")
    assert (right.before, right.after) == (Fraction(1, 4), Fraction(2, 4))
    assert right.improved


def test_compare_rejects_one_new_wrong_answer_even_when_the_rest_improved() -> None:
    before = _card({"c1": AGREED_RIGHT, "c2": HELD, "c3": HELD, "c4": HELD})
    after = _card(
        {"c1": AGREED_RIGHT, "c2": AGREED_RIGHT, "c3": AGREED_RIGHT, "c4": AGREED_WRONG},
        code="v2",
    )

    comparison = compare_scorecards(before, after)

    assert not comparison.accepted
    assert comparison.verdict == "REJECT (1 new wrong: c4)"
    assert [change.case_id for change in comparison.new_wrong] == ["c4"]


def test_a_question_asked_for_the_first_time_and_answered_wrong_is_a_new_wrong() -> None:
    """A case the old run never asked is not exempt: its first answer can be the new wrong one."""
    before = _card({"c1": HELD, "c2": NOT_ASKED})
    after = _card({"c1": AGREED_RIGHT, "c2": AGREED_WRONG}, code="v2")

    comparison = compare_scorecards(before, after)

    assert comparison.verdict == "REJECT (1 new wrong: c2)"


def test_compare_rejects_a_change_that_is_not_better() -> None:
    before = _card({"c1": AGREED_RIGHT, "c2": HELD})
    after = _card({"c1": HELD, "c2": AGREED_RIGHT}, code="v2")

    comparison = compare_scorecards(before, after)

    assert not comparison.accepted
    assert comparison.verdict.startswith("REJECT (not better")
    assert [change.case_id for change in comparison.broke] == ["c1"]
    assert [change.case_id for change in comparison.fixed] == ["c2"]


def test_scorecards_of_different_agents_are_not_compared() -> None:
    with pytest.raises(IncomparableRuns):
        compare_scorecards(_card({"c1": HELD}), _card({"c1": HELD}, agent="rekha"))


def test_the_comparison_reads_as_a_per_metric_and_per_case_report() -> None:
    before = _card({"c1": AGREED_RIGHT, "c2": HELD})
    after = _card({"c1": AGREED_WRONG, "c2": AGREED_RIGHT}, code="v2")

    text = compare_scorecards(before, after).render()

    assert "pair_agreed_right: 1/2 -> 1/2" in text
    assert "c1: agreed_right -> agreed_wrong (NEW WRONG)" in text
    assert "c2: held -> agreed_right (fixed)" in text
    assert text.rstrip().endswith("REJECT (1 new wrong: c1)")


def test_a_wrong_answer_cannot_be_hidden_by_no_longer_asking_its_question() -> None:
    before = _card({"c1": AGREED_RIGHT, "c2": AGREED_WRONG, "c3": AGREED_RIGHT})
    after = _card({"c1": AGREED_RIGHT, "c2": NOT_ASKED, "c3": AGREED_RIGHT}, code="v2")

    comparison = compare_scorecards(before, after)

    assert not comparison.accepted
    assert "1 no longer asked: c2" in comparison.verdict


def test_asking_fewer_questions_is_rejected_even_when_a_case_got_right() -> None:
    before = _card({"c1": HELD, "c2": HELD, "c3": HELD})
    after = _card({"c1": AGREED_RIGHT, "c2": NOT_ASKED, "c3": NOT_ASKED}, code="v2")

    comparison = compare_scorecards(before, after)

    assert not comparison.accepted
    assert "asked 3 -> 1" in comparison.verdict


def test_scorecards_made_with_different_keys_are_not_compared() -> None:
    """A bigger key brings more right cases; that is not the agent getting better."""
    with pytest.raises(IncomparableRuns, match="key"):
        compare_scorecards(_card({"c1": HELD}), _card({"c1": HELD}, key_sha="e" * 64))


def test_architect_scorecards_of_different_documents_are_not_compared() -> None:
    with pytest.raises(IncomparableRuns, match="document"):
        compare_scorecards(
            _card({"c1": HELD}, agent="vastu", document="1" * 64),
            _card({"c1": HELD}, agent="vastu", document="2" * 64),
        )
