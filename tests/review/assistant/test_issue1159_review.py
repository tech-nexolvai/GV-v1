"""Issue #1159, independent review: every finding as a test (synthetic records, fake model).

1. a connector cannot lean on a sentence code drops; no double space where one was dropped;
2. a ranking question about deciding says whose decision it is;
3. a page-less other check can be the start item (no dead end);
4. the architect check ranks and explains a countertop when only it is open;
5. the queue fill is dropped only when the outcome wording already says "needs your decision";
6. ranking questions are narrow and never name a page;
7. sign-off possible with only unchecked countertops says they do not hold it up;
8. failures rank by severity, then difference (documented tie rule); a cap keeps the ranked ones;
9. provider refusals and failures and a records-only guard failure log codes at WARNING.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from fractions import Fraction
from typing import Any
from uuid import uuid4

import pytest

from app.review.assistant.contract import AnswerEvent, AssistantRequest, Draft
from app.review.assistant.guard import GuardRejected, check
from app.review.assistant.model import ModelRefused, ModelUnavailable
from app.review.assistant.placeholders import render
from app.review.assistant.records import ReviewSnapshot
from app.review.assistant.records_only import (
    COULD_NOT_CHECK,
    UNCHECKED_DO_NOT_BLOCK,
    YOUR_DECISION,
    is_ranking_question,
    needs_you_answer,
    ranked,
)
from app.review.assistant.service import AssistantRuntime, stream_answer
from app.schemas.visual_ui import ArchitectResultOut, ReviewerDecisionOut
from tests.review.assistant import synthetic as syn
from tests.review.assistant.synthetic import exact, snapshot
from verdict.outcomes import Outcome

SNAPSHOT = snapshot()
SONNET = "anthropic.claude-sonnet-5-5"


class _Model:
    model_id = SONNET

    def __init__(self, text: str | None = None, error: Exception | None = None) -> None:
        self.text, self.error, self.calls = text, error, 0

    def answer(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        body = json.dumps({"text": self.text, "evidence": [], "actions": []})
        return {
            "stopReason": "end_turn",
            "output": {"message": {"content": [{"type": "text", "text": body}]}},
            "usage": {"inputTokens": 1000, "outputTokens": 50},
        }


class _Ledger:
    def before_call(self) -> None: ...

    def record(self, **_: object) -> None: ...


def _ask(question: str, model: _Model | None, review: ReviewSnapshot = SNAPSHOT) -> AnswerEvent:
    events = list(
        stream_answer(
            AssistantRequest(question=question),
            load_snapshot=lambda: review,
            runtime=AssistantRuntime(model=model, max_history_turns=6),  # type: ignore[arg-type]
            ledger=_Ledger(),
        )
    )
    answer = events[-1][1]
    assert isinstance(answer, AnswerEvent), events
    return answer


def _decision(action: str, *, carried: bool = False) -> ReviewerDecisionOut:
    return ReviewerDecisionOut(
        action=action,
        note=None,
        actor="reviewer-one",
        time=datetime(2026, 10, 1, tzinfo=UTC),
        carried_over=carried,
        carried_from_finding_id=uuid4() if carried else None,
    )


# ---- 1 -------------------------------------------------------------------------------------------

DROPPED_ANCHOR = (
    "The countertop on {C2.page} {C2.outcome}. The countertop on {C1.page} {C1.outcome}. "
    "{C2.needs_you}. Also, {C2.hold_reason}."
)


def test_a_connector_cannot_lean_on_a_dropped_sentence() -> None:
    for by_model in (True, False):
        with pytest.raises(GuardRejected, match="connector-without-its-subject"):
            check(Draft(text=DROPPED_ANCHOR), SNAPSHOT, by_model=by_model)


def test_a_dropped_sentence_leaves_no_double_space() -> None:
    template = "The countertop on {C2.page} {C2.outcome}. {C2.needs_you}. Open it in the queue."
    check(Draft(text=template), SNAPSHOT)
    text = render(template, SNAPSHOT).text
    assert "  " not in text
    assert text == "The countertop on page 7 needs your decision [[0]]. Open it in the queue."


# ---- 2 -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        "What should I approve first?",
        "Which one should I accept as an exception first?",
        "Is it ok to approve the worst one?",
        "Should I approve the worst one first?",
        "Which should I dismiss first?",
    ],
)
def test_a_ranking_question_about_deciding_says_whose_decision_it_is(question: str) -> None:
    model = _Model(text="{signoff.status}.")
    answer = _ask(question, model)
    assert model.calls == 0
    assert answer.mode == "records_only"
    assert "Start with the countertop on page 4" in answer.text
    assert answer.text.endswith(YOUR_DECISION)


def test_a_plain_ranking_question_has_no_decision_sentence() -> None:
    assert YOUR_DECISION not in _ask("Which countertops look most worrying?", None).text


# ---- 3 -------------------------------------------------------------------------------------------


def _pageless_only() -> ReviewSnapshot:
    finding = uuid4()
    base = syn.countertop_results()
    return snapshot(
        syn.Inputs(
            countertops=base.model_copy(update={"items": ()}),
            findings=(
                syn.composer(
                    finding, "PKG-001", "Package drawing list", "FAIL", "Two sheets are missing."
                ),
            ),
            readiness=syn.Readiness(blocking_findings=1, blocking_finding_ids=(finding,)),
        )
    )


def test_a_pageless_check_can_be_where_to_start() -> None:
    review = _pageless_only()
    draft = needs_you_answer(review)
    check(draft, review, by_model=False)
    answer = _ask("Where should I start?", None, review)
    assert answer.text == (
        "Sign-off is blocked: 1 finding still needs your decision. Start with the Package drawing "
        "list check: it needs correction; two sheets are missing [[0]]."
    )


def test_a_refused_answer_with_a_pageless_start_is_no_dead_end() -> None:
    review = _pageless_only()
    answer = _ask("Tell me about this review", _Model(text="It is fine."), review)
    assert answer.text.startswith(COULD_NOT_CHECK)
    assert "Start with the Package drawing list check" in answer.text


def test_the_check_name_counts_as_naming_the_subject() -> None:
    check(Draft(text="Start with the {F1.check} check: it {F1.outcome}."), SNAPSHOT)


# ---- 4 and 5 -------------------------------------------------------------------------------------


def _architect_open(case: str) -> ReviewSnapshot:
    base = syn.countertop_results()
    fail, _held, passed = base.items
    architect_id = uuid4()
    if case == "looks-right":
        item = passed.model_copy(
            update={
                "architect": ArchitectResultOut(
                    outcome=Outcome.FAIL,
                    finding_id=architect_id,
                    needs_decision=True,
                    reason="Architect overall differs",
                ),
                "reviewer_decision": None,
            }
        )
        items = (item,)
    else:  # a carried-over confirmed width failure with the architect check still pending
        item = fail.model_copy(
            update={
                "reviewer_decision": _decision("confirm", carried=True),
                "needs_decision": False,
                "architect": ArchitectResultOut(
                    outcome=Outcome.REVIEW_REQUIRED,
                    finding_id=architect_id,
                    needs_decision=True,
                    reason="Architect pairing needs you",
                ),
            }
        )
        items = (item,)
    return snapshot(
        syn.Inputs(
            countertops=base.model_copy(update={"items": items}),
            findings=(),
            readiness=syn.Readiness(blocking_findings=1, blocking_finding_ids=(architect_id,)),
        )
    )


@pytest.mark.parametrize(
    ("case", "architect"),
    [
        ("looks-right", "the architect check needs correction (Architect overall differs)"),
        ("decided-width", "the architect check needs your decision (Architect pairing needs you)"),
    ],
)
def test_the_architect_check_explains_a_countertop_when_only_it_is_open(
    case: str, architect: str
) -> None:
    review = _architect_open(case)
    text = render(needs_you_answer(review).text, review).text
    assert f"; {architect} [[0]]." in text


def test_the_architect_failure_ranks_as_a_failure() -> None:
    base = syn.countertop_results()
    _fail, held, passed = base.items
    architect_id = uuid4()
    passed = passed.model_copy(
        update={
            "architect": ArchitectResultOut(
                outcome=Outcome.FAIL, finding_id=architect_id, needs_decision=True
            )
        }
    )
    review = snapshot(
        syn.Inputs(
            countertops=base.model_copy(update={"items": (held, passed)}),
            findings=syn.findings()[1:3],
            readiness=syn.Readiness(
                blocking_findings=2, blocking_finding_ids=(syn.FINDING_HELD, architect_id)
            ),
        )
    )
    assert [item.page_number for item in ranked(review)] == [9, 7]  # type: ignore[union-attr]


@pytest.mark.parametrize("case", ["looks-right", "decided-width"])
def test_the_queue_fill_is_kept_when_the_outcome_does_not_say_it_needs_you(case: str) -> None:
    review = _architect_open(case)
    text = render("The countertop on {C1.page} {C1.outcome}. {C1.needs_you}.", review).text
    assert "it needs your decision in the review queue" in text


def test_the_queue_fill_is_dropped_only_for_an_undecided_hold() -> None:
    text = render("The countertop on {C2.page} {C2.outcome}. {C2.needs_you}.", SNAPSHOT).text
    assert "review queue" not in text
    text = render("The countertop on {C1.page} {C1.outcome}. {C1.needs_you}.", SNAPSHOT).text
    assert "review queue" in text  # "needs correction" does not say it needs you


# ---- 6 -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        "Which page is first?",
        "What was read first on page 4?",
        "What's the first piece on page 4?",
        "worst-case tolerance?",
        "What is next to the sink on page 4?",
        "Which wall is the countertop next to?",
        "Does page 4 start with a filler?",
        "What is the priority of the sink check?",
        "What happens next after sign-off?",
        "What did I decide first?",
        "What is the most important dimension on page 4?",
        "Which countertop is the worst on page 4?",
    ],
)
def test_not_a_ranking_question(question: str) -> None:
    assert is_ranking_question(question) is False


@pytest.mark.parametrize(
    "question",
    [
        "Which countertops look most worrying?",
        "Where should I start?",
        "What should I fix first?",
        "Prioritise the problems",
    ],
)
def test_still_a_ranking_question(question: str) -> None:
    assert is_ranking_question(question) is True


# ---- 7 -------------------------------------------------------------------------------------------


def test_sign_off_possible_with_only_unchecked_countertops_says_they_do_not_hold_it_up() -> None:
    base = syn.countertop_results()
    unchecked = base.items[2].model_copy(
        update={
            "finding_id": None,
            "row_id": uuid4(),
            "page_number": 11,
            "outcome": None,
            "needs_decision": True,
            "reviewer_decision": None,
            "label": "Sample run D",
        }
    )
    review = snapshot(
        syn.Inputs(
            countertops=base.model_copy(update={"items": (unchecked,)}),
            findings=(),
            readiness=syn.Readiness(
                can_approve=True, blocking_findings=0, blocking_finding_ids=(), reason=None
            ),
        )
    )
    text = render(needs_you_answer(review).text, review).text
    assert text.startswith("You can sign off. Start with the countertop on page 11")
    assert text.endswith(UNCHECKED_DO_NOT_BLOCK)


# ---- 8 -------------------------------------------------------------------------------------------


def test_same_severity_a_countertop_with_a_difference_ranks_before_a_check_without() -> None:
    finding = uuid4()
    base = syn.countertop_results()
    review = snapshot(
        syn.Inputs(
            countertops=base.model_copy(update={"items": (base.items[0],)}),
            findings=(
                syn.composer(
                    syn.FINDING_FAIL, "CT-WIDTH-001", "Countertop width", "FAIL", "Width."
                ),
                syn.composer(
                    finding, "SINK-001", "Sink cutout", "FAIL", "Too narrow.", pages=("5",)
                ),
            ),
            readiness=syn.Readiness(
                blocking_findings=2, blocking_finding_ids=(syn.FINDING_FAIL, finding)
            ),
        )
    )
    assert [item.id for item in ranked(review)] == ["C1", "F1"]


def test_severity_decides_before_the_difference() -> None:
    review = SNAPSHOT
    countertop = review.countertops[0].model_copy(update={"severity": "minor"})
    finding = review.other_checks[0].model_copy(update={"outcome": "FAIL", "severity": "critical"})
    changed = review.model_copy(
        update={"countertops": (countertop, *review.countertops[1:]), "other_checks": (finding,)}
    )
    assert [item.id for item in ranked(changed)][:2] == ["F1", "C1"]


def test_a_cap_keeps_the_countertop_the_answer_starts_with(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.review.assistant import records as records_module

    base = syn.countertop_results()
    small = base.items[0]
    big = small.model_copy(
        update={
            "row_id": uuid4(),
            "finding_id": uuid4(),
            "page_number": 6,
            "label": "Sample run F",
            "delta": exact(Fraction(-3)),
        }
    )
    monkeypatch.setattr(records_module, "MAX_COUNTERTOPS", 1)
    review = snapshot(
        syn.Inputs(countertops=base.model_copy(update={"items": (small, big)}), findings=())
    )
    assert [item.page_number for item in review.countertops] == [6]


# ---- 9 -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (ModelRefused(429), "call refused unserved: 429"),
        (ModelRefused(402), "call refused unserved: 402"),
        (ModelUnavailable("503"), "call failed: ModelUnavailable"),
    ],
)
def test_provider_refusals_and_failures_log_codes_at_warning(
    caplog: pytest.LogCaptureFixture, error: Exception, message: str
) -> None:
    with caplog.at_level(logging.WARNING, logger="app.review.assistant.service"):
        events = list(
            stream_answer(
                AssistantRequest(question="Tell me about unique-question-marker"),
                load_snapshot=lambda: SNAPSHOT,
                runtime=AssistantRuntime(model=_Model(error=error), max_history_turns=6),  # type: ignore[arg-type]
                ledger=_Ledger(),
            )
        )
    assert events[-1][0] == "error"
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(message in text for text in warnings), warnings
    assert all("unique-question-marker" not in r.getMessage() for r in caplog.records)


def test_a_records_only_guard_failure_logs_its_code_at_warning(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.review.assistant import service

    monkeypatch.setattr(service, "needs_you_answer", lambda *_, **__: Draft(text="{C9.outcome}."))
    with caplog.at_level(logging.WARNING, logger="app.review.assistant.service"):
        _ask("Which countertops look most worrying?", None)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert "review assistant records-only answer failed the guard: unknown-placeholder" in warnings
