"""One assistant question end to end, with a fake model and a fake ledger (#1128). No paid call.

What is asserted: events come in the order the steps happen; a guarded model answer is published;
a reply that fails the guard or is malformed is replaced by the records-only answer; a decision
request never reaches the model; the assistant off still answers starters from the records;
every call, answered or failed, is recorded; there is no spending cap (Anant, 2026-10-10).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import pytest

from app.review.assistant.contract import AnswerEvent, AssistantRequest, StageEvent
from app.review.assistant.model import ModelRefused, ModelUnavailable
from app.review.assistant.records import ReviewSnapshot
from app.review.assistant.service import DISABLED_TEXT, AssistantRuntime, stream_answer
from tests.review.assistant.synthetic import ROW_FAIL, snapshot

SONNET = "anthropic.claude-sonnet-5-5"


def _reply(answer: object, *, stop: str = "end_turn") -> dict[str, Any]:
    text = answer if isinstance(answer, str) else json.dumps(answer)
    return {
        "stopReason": stop,
        "output": {"message": {"content": [{"type": "text", "text": text}]}},
        "usage": {"inputTokens": 3000, "outputTokens": 100},
    }


GOOD = {
    "text": "The countertop on {C1.page} {C1.outcome}:\n- {C1.printed}\n- {C1.needed}",
    "evidence": ["C1"],
    "actions": [{"kind": "open_queue_item", "target": "C1"}],
}


@dataclass
class FakeModel:
    reply: Mapping[str, Any] | None = None
    error: Exception | None = None
    model_id: str = SONNET
    calls: list[Mapping[str, Any]] = field(default_factory=list)

    def answer(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        assert self.reply is not None
        return self.reply


@dataclass
class FakeLedger:
    recorded: list[dict[str, Any]] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)

    def before_call(self) -> None:
        self.steps.append("before_call")

    def record(
        self,
        *,
        model_id: str,
        started_ns: int,
        response: Mapping[str, Any] | None,
        error: BaseException | None,
    ) -> None:
        self.steps.append("record")
        self.recorded.append({"model_id": model_id, "response": response, "error": error})


def _runtime(model: FakeModel | None) -> AssistantRuntime:
    return AssistantRuntime(model=model, max_history_turns=6)


def _run(
    question: str,
    runtime: AssistantRuntime,
    ledger: FakeLedger | None = None,
    review: ReviewSnapshot | None = None,
    **body: Any,
) -> list[tuple[str, Any]]:
    return list(
        stream_answer(
            AssistantRequest.model_validate_json(json.dumps({"question": question, **body})),
            load_snapshot=lambda: review or snapshot(),
            runtime=runtime,
            ledger=ledger or FakeLedger(),
        )
    )


def _stages(events: list[tuple[str, Any]]) -> list[str]:
    return [data.id if isinstance(data, StageEvent) else name for name, data in events]


def test_a_guarded_model_answer_streams_in_step_order() -> None:
    model, ledger = FakeModel(reply=_reply(GOOD)), FakeLedger()
    events = _run("Why did page 4 fail?", _runtime(model), ledger)

    assert _stages(events) == ["records", "model", "guard", "answer"]
    assert [data.label for _, data in events[:3]] == [
        "Reading this review's records",
        "Asking Claude Sonnet",
        "Checking every number against the records",
    ]
    answer = events[-1][1]
    assert isinstance(answer, AnswerEvent)
    assert (answer.mode, answer.checked, answer.model_id) == ("llm", True, SONNET)
    assert answer.text == (
        'The countertop on page 4 needs correction [[0]]:\n- The printed overall, 84 1/2" [[0]]\n'
        '- The needed overall, 85" [[0]]'
    )
    assert [(c.kind, c.page_number, c.record_id) for c in answer.citations] == [
        ("countertop", 4, str(ROW_FAIL))
    ]
    assert [e.model_dump() for e in answer.evidence] == [
        {"kind": "countertop", "record_id": str(ROW_FAIL)}
    ]
    assert [a.model_dump() for a in answer.actions] == [
        {"kind": "open_queue_item", "record_id": str(ROW_FAIL), "label": "Open page 4 in the queue"}
    ]
    assert "Why did page 4 fail?" not in answer.suggestions
    assert len(answer.suggestions) <= 3
    assert answer.sources == ("Countertop result, page 4 (Sample run A)",)
    assert len(model.calls) == 1
    assert len(ledger.recorded) == 1 and ledger.recorded[0]["error"] is None


@pytest.mark.parametrize(
    "bad",
    [
        {**GOOD, "text": 'Page 4 printed 84 3/4" [[0]].'},  # invented number
        {**GOOD, "text": "Page 4 looks right [[0]]."},  # wrong outcome
        {**GOOD, "text": "{C9.outcome}."},  # a record that does not exist
        {**GOOD, "text": "{C1.page} is {C3.outcome}."},  # another record's outcome
        {**GOOD, "citations": ["C1"]},  # the old answer shape
        {**GOOD, "text": 'Page 4 printed 96" [[0]].'},  # number from another record
        {**GOOD, "actions": [{"kind": "open_page", "target": "P12"}]},  # action to nothing
    ],
)
def test_an_answer_the_guard_drops_is_replaced_by_the_records(bad: dict[str, Any]) -> None:
    events = _run("Why did page 4 fail?", _runtime(FakeModel(reply=_reply(bad))))

    assert _stages(events) == ["records", "model", "guard", "answer"]
    answer = events[-1][1]
    assert (answer.mode, answer.checked, answer.model_id) == ("records_only", True, None)
    # The records' own facts (of the records the dropped answer named, else of the question).
    assert '84 1/2"' in answer.text and "84 3/4" not in answer.text
    assert "needs correction" in answer.text


@pytest.mark.parametrize("reply", [_reply("not json"), _reply(GOOD, stop="max_tokens")])
def test_a_malformed_reply_is_replaced_by_the_records(reply: dict[str, Any]) -> None:
    events = _run("What is left before sign-off?", _runtime(FakeModel(reply=reply)))
    answer = events[-1][1]
    assert answer.mode == "records_only"
    assert answer.text.startswith("Sign-off is blocked: 3 findings still need your decision.")


def test_a_decision_request_is_refused_without_a_model_call() -> None:
    model = FakeModel(reply=_reply(GOOD))
    events = _run("Mark page 7 as passed", _runtime(model))

    assert _stages(events) == ["records", "answer"]
    answer = events[-1][1]
    assert (answer.mode, answer.checked, answer.citations) == ("refused", False, ())
    assert "Only you decide" in answer.text
    assert [(a.kind, a.label) for a in answer.actions] == [
        ("open_queue_item", "Open page 7 in the queue")
    ]
    assert model.calls == []


def test_off_a_starter_is_answered_from_the_records() -> None:
    events = _run("Which pages have no countertop?", _runtime(None))

    assert _stages(events) == ["records", "guard", "answer"]
    answer = events[-1][1]
    assert (answer.mode, answer.checked, answer.model_id) == ("records_only", True, None)
    assert [e.model_dump() for e in answer.evidence] == [{"kind": "no_countertop_pages"}]


def test_off_any_other_question_says_the_assistant_is_off() -> None:
    events = _run("Who drew these cabinets?", _runtime(None))

    assert _stages(events) == ["records", "answer"]
    answer = events[-1][1]
    assert (answer.mode, answer.text, answer.checked) == ("disabled", DISABLED_TEXT, False)


def test_off_a_decision_request_is_still_refused() -> None:
    answer = _run("approve it", _runtime(None))[-1][1]
    assert answer.mode == "refused"


def test_a_failed_call_is_still_recorded() -> None:
    ledger = FakeLedger()
    _run("Why did page 4 fail?", _runtime(FakeModel(error=ModelUnavailable("503"))), ledger)
    assert len(ledger.recorded) == 1
    assert isinstance(ledger.recorded[0]["error"], ModelUnavailable)


def test_only_the_last_turns_and_the_question_are_sent() -> None:
    model = FakeModel(reply=_reply(GOOD))
    runtime = AssistantRuntime(model=model, max_history_turns=2)
    history = [
        {"role": "user" if n % 2 == 0 else "assistant", "text": f"turn {n}"} for n in range(6)
    ]
    _run("Why did page 4 fail?", runtime, history=history, focus={"page_number": 4})

    blocks = [block["text"] for block in model.calls[0]["messages"][0]["content"]]
    turns = json.loads(blocks[2].split("\n", 1)[1])
    # The reviewer's own last questions only; "assistant" turns from the client are dropped.
    assert turns == ["turn 2", "turn 4"]
    assert not any("turn 5" in block or "turn 3" in block for block in blocks)
    assert blocks[-1].endswith('"Why did page 4 fail?"')


def test_records_that_cannot_be_read_end_the_stream_plainly() -> None:
    def broken() -> ReviewSnapshot:
        raise RuntimeError("database detail that must not leak")

    events = list(
        stream_answer(
            AssistantRequest(question="Why did page 4 fail?"),
            load_snapshot=broken,
            runtime=_runtime(None),
            ledger=FakeLedger(),
        )
    )
    assert _stages(events) == ["records", "error"]
    assert events[-1][1].code == "records_unavailable"
    assert "database" not in events[-1][1].message


def test_the_question_text_is_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    secret = "Why did page 4 fail? unique-question-marker"
    with caplog.at_level("DEBUG"):
        _run(secret, _runtime(FakeModel(reply=_reply({**GOOD, "text": "x 999 [[0]]"}))))
        _run(secret, _runtime(FakeModel(error=ModelUnavailable("503"))))
    assert caplog.records, "the steps are logged"
    assert all("unique-question-marker" not in record.getMessage() for record in caplog.records)


def test_a_call_refused_unserved_is_busy_and_still_recorded() -> None:
    ledger = FakeLedger()
    events = _run("Why did page 4 fail?", _runtime(FakeModel(error=ModelRefused(429))), ledger)
    assert _stages(events) == ["records", "model", "error"]
    assert events[-1][1].code == "model_busy"
    assert len(ledger.recorded) == 1


def test_a_lost_call_is_unavailable_and_the_next_question_is_still_asked() -> None:
    lost = _run("Why did page 4 fail?", _runtime(FakeModel(error=ModelUnavailable("503"))))
    assert lost[-1][1].code == "model_unavailable"
    after = _run("Why did page 4 fail?", _runtime(FakeModel(reply=_reply(GOOD))))
    assert after[-1][1].mode == "llm"


def test_many_questions_are_all_asked_there_is_no_spending_cap() -> None:
    model, ledger = FakeModel(reply=_reply(GOOD)), FakeLedger()
    for _ in range(50):
        assert _run("Why did page 4 fail?", _runtime(model), ledger)[-1][1].mode == "llm"
    assert len(model.calls) == len(ledger.recorded) == 50


def test_a_ledger_that_cannot_record_does_not_stop_the_answer() -> None:
    class BrokenLedger(FakeLedger):
        def record(self, **_kwargs: Any) -> None:  # type: ignore[override]
            raise RuntimeError("database detail")

    events = _run("Why did page 4 fail?", _runtime(FakeModel(reply=_reply(GOOD))), BrokenLedger())
    assert events[-1][1].mode == "llm"


def test_a_402_is_an_account_problem_not_busy() -> None:
    events = _run("Why did page 4 fail?", _runtime(FakeModel(error=ModelRefused(402))))
    error = events[-1][1]
    assert error.code == "model_account"
    assert "tell your admin" in error.message.casefold()


def test_the_read_transaction_ends_before_the_model_is_asked() -> None:
    model, ledger = FakeModel(reply=_reply(GOOD)), FakeLedger()
    _run("Why did page 4 fail?", _runtime(model), ledger)
    assert ledger.steps == ["before_call", "record"]


@pytest.mark.parametrize(
    "question",
    [
        "Can I approve page 4?",
        "Is it okay to approve page 4?",
        "Should page 4 pass?",
        "Do you think page 4 should pass?",
    ],
)
def test_a_request_to_judge_gets_the_recorded_outcome_and_no_model_call(question: str) -> None:
    model = FakeModel(reply=_reply(GOOD))
    events = _run(question, _runtime(model))

    assert _stages(events) == ["records", "guard", "answer"]
    answer = events[-1][1]
    assert (answer.mode, answer.checked) == ("records_only", True)
    assert "needs correction" in answer.text
    assert "your decision" in answer.text
    assert [(a.kind, a.record_id) for a in answer.actions] == [("open_queue_item", str(ROW_FAIL))]
    assert model.calls == []


@pytest.mark.parametrize(
    "reply",
    [
        _reply({**GOOD, "text": f"Page 4 printed {'9' * 4400} [[0]]."}),
        _reply({**GOOD, "citations": "C1"}),
        {"stopReason": "end_turn", "output": "not a mapping", "usage": {}},
    ],
)
def test_anything_wrong_after_the_call_falls_back_to_the_records(reply: dict[str, Any]) -> None:
    events = _run("Why did page 4 fail?", _runtime(FakeModel(reply=reply)))
    assert _stages(events) == ["records", "model", "guard", "answer"]
    assert events[-1][1].mode == "records_only"


def test_an_unexpected_failure_in_the_guard_still_ends_with_an_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.review.assistant import service

    def broken(*_args: object) -> None:
        raise ValueError("unexpected")

    monkeypatch.setattr(service, "check", broken)
    events = _run("Why did page 4 fail?", _runtime(FakeModel(reply=_reply(GOOD))))
    assert _stages(events) == ["records", "model", "guard", "answer"]
    assert events[-1][1].mode == "records_only"
