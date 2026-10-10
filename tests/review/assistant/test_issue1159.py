"""Issue #1159: no dead end when an answer is refused, cleaner wording, a visible refusal reason.

From the first real run (synthetic records here, a fake model, no paid call):
- "Which countertops look most worrying, and why?" got "I couldn't check an answer…" and nothing
  else. A ranking question is now answered by code (what needs you, failures first, largest
  difference first, then held), and any refused or failed answer falls back to that, after one
  line saying the free answer could not be checked; a question about a page falls back to that
  page's answer.
- "On page 5, Also, it needs your decision in the review queue": code puts no subject in front of
  a connector, the guard refuses a connector sentence about another record, and the queue fill is
  dropped when the answer already states that record's outcome.
- The guard's reason code is logged at WARNING (never the question or the answer).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

import pytest

from app.review.assistant.contract import AnswerEvent, AssistantRequest, Draft
from app.review.assistant.guard import GuardRejected, check
from app.review.assistant.model import SYSTEM_PROMPT
from app.review.assistant.placeholders import render
from app.review.assistant.records import ReviewSnapshot
from app.review.assistant.records_only import (
    COULD_NOT_CHECK,
    is_ranking_question,
    ranked,
)
from app.review.assistant.service import AssistantRuntime, stream_answer
from tests.review.assistant import synthetic as syn
from tests.review.assistant.synthetic import exact, snapshot

SONNET = "anthropic.claude-sonnet-5-5"
SNAPSHOT = snapshot()


class _Model:
    model_id = SONNET

    def __init__(self, text: str | None = None, error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.calls = 0

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


# ---- 1. no dead end ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        "Which countertops look most worrying, and why?",
        "What are the worst ones?",
        "What are the biggest problems?",
        "Which should I look at first?",
        "Help me prioritise",
        "Where should I start?",
    ],
)
def test_a_ranking_question_is_answered_by_code_without_a_model(question: str) -> None:
    assert is_ranking_question(question)
    model = _Model(text="The countertops on {C1.page} and {C2.page} look most worrying.")
    answer = _ask(question, model)
    assert model.calls == 0
    assert answer.mode == "records_only" and answer.checked
    assert answer.text.startswith("Sign-off is blocked: 3 findings still need your decision.")
    assert "What needs you, failures first:" in answer.text
    assert answer.evidence[0].kind == "blockers"
    assert answer.actions and answer.actions[0].kind == "open_queue_item"


def test_the_order_is_failures_by_largest_difference_then_held_then_waiting() -> None:
    from fractions import Fraction

    base = syn.countertop_results()
    small, held, passed = base.items
    big = small.model_copy(
        update={
            "row_id": uuid4(),
            "finding_id": uuid4(),
            "page_number": 6,
            "label": "Sample run F",
            "delta": exact(Fraction(-3)),
        }
    )
    review = snapshot(
        syn.Inputs(
            countertops=base.model_copy(update={"items": (small, held, passed, big)}),
            readiness=syn.Readiness(
                blocking_findings=4,
                blocking_finding_ids=(
                    syn.FINDING_FAIL,
                    syn.FINDING_HELD,
                    syn.FINDING_SINK,
                    big.finding_id,  # type: ignore[arg-type]
                ),
            ),
        )
    )
    order = [(item.page_number if hasattr(item, "page_number") else 5) for item in ranked(review)]
    assert order == [6, 4, 7, 5]  # the 3" failure, the 1/2" failure, the hold, the waiting check


def test_a_realistic_refused_answer_falls_back_to_what_needs_you() -> None:
    # A plain question the model answered by ranking two records in one sentence: refused.
    model = _Model(
        text="The countertops on {C1.page} and {C2.page} look most worrying because they need you."
    )
    answer = _ask("Tell me about this review in plain words", model)
    assert model.calls == 1
    assert answer.mode == "records_only"
    assert answer.text.startswith(COULD_NOT_CHECK)
    assert "What needs you, failures first:" in answer.text
    assert answer.evidence[0].kind == "blockers"
    assert answer.actions


def test_a_refused_answer_about_a_page_falls_back_to_that_page() -> None:
    model = _Model(text="Page {C1.page} is fine.")
    answer = _ask("What happened on page 4?", model)
    assert answer.mode == "records_only"
    assert answer.text.startswith("The countertop on page 4 needs correction")
    assert COULD_NOT_CHECK not in answer.text


def test_a_failure_after_the_call_falls_back_to_what_needs_you(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.review.assistant import service

    def broken(*_: object, **__: object) -> None:
        raise ValueError("unexpected")

    monkeypatch.setattr(service, "parse_answer", broken)
    answer = _ask(
        "Tell me about this review", _Model(text="The countertop on {C1.page} {C1.outcome}.")
    )
    assert answer.mode == "records_only"
    assert answer.text.startswith(COULD_NOT_CHECK)


def test_with_nothing_open_the_fallback_still_answers() -> None:
    ready = snapshot(
        syn.Inputs(
            readiness=syn.Readiness(can_approve=True, blocking_findings=0, blocking_finding_ids=())
        )
    )
    answer = _ask("Tell me a story", _Model(text="Yes."), ready)
    assert answer.text.startswith(COULD_NOT_CHECK)
    assert "You can sign off." in answer.text


# ---- 2. wording ----------------------------------------------------------------------------------

REAL_PAGE_ANSWER = (
    "The countertop on {C2.page} {C2.outcome}; {C2.hold_reason}. Also, {C2.needs_you}."
)


def test_the_queue_fill_is_dropped_when_the_outcome_is_stated() -> None:
    check(Draft(text=REAL_PAGE_ANSWER), SNAPSHOT)
    text = render(REAL_PAGE_ANSWER, SNAPSHOT).text
    assert "Also" not in text
    assert "review queue" not in text
    assert "On page 7" not in text
    assert text.endswith("so nothing was read [[0]].")


def test_no_subject_is_put_in_front_of_a_connector() -> None:
    template = "The countertop on {C1.page} {C1.outcome}. Also, {C1.reason}."
    check(Draft(text=template), SNAPSHOT)
    text = render(template, SNAPSHOT).text
    assert "On page 4, Also" not in text
    assert "Also, the printed overall does not match" in text


@pytest.mark.parametrize("connector", ["Also", "Then", "So", "But", "However", "Next", "Finally"])
def test_a_connector_sentence_about_another_record_is_refused(connector: str) -> None:
    template = f"The countertop on {{C1.page}} {{C1.outcome}}. {connector}, {{C2.hold_reason}}."
    with pytest.raises(GuardRejected) as raised:
        check(Draft(text=template), SNAPSHOT)
    if connector in ("Also", "Then", "So", "Next"):  # glue words: refused for the binding itself
        assert str(raised.value) == "connector-without-its-subject"
    # A code-written answer has no vocabulary check: the binding rule alone refuses it.
    with pytest.raises(GuardRejected, match="connector-without-its-subject"):
        check(Draft(text=template), SNAPSHOT, by_model=False)


def test_the_queue_fill_stays_when_no_outcome_is_stated() -> None:
    text = render("For the countertop on {C1.page}, {C1.needs_you}.", SNAPSHOT).text
    assert "review queue" in text


def test_the_prompt_says_not_to_repeat_the_outcome_with_the_queue_fill() -> None:
    assert "Do not add" in SYSTEM_PROMPT and "{C1.needs_you}" in SYSTEM_PROMPT


# ---- 3. the reason is visible ---------------------------------------------------------------------


def test_the_drop_reason_code_is_logged_at_warning_without_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "unique-question-marker"
    with caplog.at_level(logging.WARNING, logger="app.review.assistant.service"):
        _ask(
            f"Tell me about {secret}", _Model(text="Page {C1.page} is fine, unique-answer-marker.")
        )
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert any("answer dropped: word-not-allowed" in record.getMessage() for record in warnings)
    for record in caplog.records:
        assert secret not in record.getMessage()
        assert "unique-answer-marker" not in record.getMessage()


def test_a_post_call_failure_is_logged_by_type_only(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.review.assistant import service

    def broken(*_: object, **__: object) -> None:
        raise ValueError("message that could quote an answer")

    monkeypatch.setattr(service, "parse_answer", broken)
    with caplog.at_level(logging.WARNING, logger="app.review.assistant.service"):
        _ask("Tell me about it", _Model(text="The countertop on {C1.page} {C1.outcome}."))
    messages = [record.getMessage() for record in caplog.records]
    assert any("could not be checked: ValueError" in message for message in messages)
    assert not any("could quote an answer" in message for message in messages)


# ---- the four real questions, with the model's passing answers simulated ---------------------------


def test_the_four_real_questions() -> None:
    page_7 = _ask(
        "Why does page 7 need me?",
        _Model(text="The countertop on {C2.page} {C2.outcome}; {C2.hold_reason}."),
    )
    left = _ask("What is left before sign-off?", _Model(text="{signoff.status}."))
    worrying = _ask(
        "Which countertops look most worrying, and why?",
        _Model(text="The countertops on {C1.page} and {C2.page} look most worrying."),
    )
    plain = _ask("In plain words, what do I need to do on page 7?", _Model(text=REAL_PAGE_ANSWER))
    assert page_7.mode == left.mode == plain.mode == "llm"
    assert worrying.mode == "records_only"
    assert "On page 7, Also" not in plain.text and "review queue" not in plain.text
