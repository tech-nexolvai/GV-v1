"""The crop readers, against a fake Bedrock client (#987). Verification for
`extraction/slot_reader/bedrock.py`. No network, no credentials, no client values."""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Any

import pytest

from evidence.crop import encode_png
from extraction.form_reader.bedrock import AttemptUsage, MalformedFormAnswer
from extraction.form_reader.runner import ModelPacer
from extraction.slot_reader.anthropic import BatchSpendGuard
from extraction.slot_reader.bedrock import (
    CLAUDE_SPAN_PROMPT,
    CLAUDE_SPAN_PROMPT_ID,
    CLAUDE_UPRIGHT_NOTE,
    COUNTER_BREAK_PROMPT,
    COUNTER_BREAK_PROMPT_ID,
    CROP_PROMPT,
    ROW_PROMPT,
    CounterBreakAnswer,
    CropJob,
    RowChoiceAnswer,
    build_counter_break_request,
    build_crop_request,
    build_row_request,
    build_wall_request,
    read_crops_parallel,
    read_row_choice,
)
from extraction.slot_reader.claude_output import (
    COUNTER_BREAK_SCHEMA,
    CROP_SCHEMA,
    ROW_CHOICE_SCHEMA,
    SPAN_SCHEMA,
    WALL_SCHEMA,
)
from extraction.slot_reader.seal import ReaderAnswer
from extraction.slot_reader.walls import WALL_PROMPT, Side, WallAnswer

QWEN = "qwen.qwen3-vl-235b-a22b"
KIMI = "us.moonshotai.kimi-k3"
OPUS = "anthropic.claude-opus-5-5"
PNG = encode_png(2, 2, bytes(12))


def reply(payload: object) -> dict[str, Any]:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return {
        "stopReason": "end_turn",
        "output": {"message": {"content": [{"text": text}]}},
        "usage": {"inputTokens": 10, "outputTokens": 5},
    }


def good(text: str) -> dict[str, object]:
    return {
        "text": text,
        "stacked": False,
        "combined": False,
        "readable": True,
        "no_dimension": False,
    }


class FakeClients:
    def __init__(self, answer: Callable[[dict[str, Any]], Mapping[str, Any]]) -> None:
        self.answer = answer
        self.requests: list[dict[str, Any]] = []
        self.lock = threading.Lock()

    def for_current_thread(self) -> FakeClients:
        return self

    def converse(self, **kwargs: Any) -> Mapping[str, Any]:
        with self.lock:
            self.requests.append(kwargs)
        return self.answer(kwargs)

    def count_input_tokens(self, **_kwargs: Any) -> int:
        return 10


class Rates:
    def rate_for(self, model_id: str) -> object | None:
        return object()


class AnthropicRates:
    def rate_for(self, model_id: str) -> object | None:
        if model_id not in {
            "anthropic.claude-opus-5-5",
            "anthropic.claude-sonnet-5-5",
        }:
            return None
        return type(
            "Rate",
            (),
            {
                "input_per_1k_tokens": Decimal("0.004"),
                "output_per_1k_tokens": Decimal("0.020"),
            },
        )()


def run(clients: FakeClients, jobs: list[CropJob], **overrides: Any) -> dict[tuple[str, str], Any]:
    attempts: list[AttemptUsage] = []
    options: dict[str, Any] = {
        "clients": clients,
        "rates": Rates(),
        "calls_per_minute": {QWEN: 6000, KIMI: 6000},
        "max_concurrent_calls": 4,
        "max_tokens": 400,
        "max_throttle_retries": 1,
        "retry_backoff_seconds": 0.001,
        "record_attempt": attempts.append,
    }
    options.update(overrides)
    return read_crops_parallel(jobs, **options)


def test_kimi_is_asked_at_low_effort_and_qwen_at_temperature_zero() -> None:
    kimi = build_crop_request(model_id=KIMI, crop_png=PNG, max_tokens=400)
    qwen = build_crop_request(model_id=QWEN, crop_png=PNG, max_tokens=400)
    assert kimi["outputConfig"] == {"effort": "low"}
    assert "temperature" not in kimi["inferenceConfig"]
    assert qwen["inferenceConfig"]["temperature"] == 0 and "outputConfig" not in qwen
    content = qwen["messages"][0]["content"]
    assert "image" in content[0] and content[1]["text"] == CROP_PROMPT


def test_claude_spend_cap_holds_before_parallel_generation() -> None:
    model = "anthropic.claude-opus-5-5"
    clients = FakeClients(lambda _request: reply(good('12"')))
    answers = run(
        clients,
        [CropJob("slot", model, 0, PNG)],
        rates=AnthropicRates(),
        calls_per_minute={model: 6000},
        spend_cap_usd=Decimal("0.001"),
    )

    assert answers[("slot", model)] is None
    assert clients.requests == []


def test_claude_spend_guard_is_shared_across_separate_row_and_value_batches() -> None:
    model = OPUS
    clients = FakeClients(
        lambda _request: {
            "stopReason": "end_turn",
            "output": {"message": {"content": [{"text": json.dumps(good('2"'))}]}},
            "usage": {"inputTokens": 10, "outputTokens": 3000},
        }
    )
    guard = BatchSpendGuard(Decimal("0.10"), AnthropicRates())
    pacer = ModelPacer({model: 6000})
    options = {
        "rates": AnthropicRates(),
        "calls_per_minute": {model: 6000},
        "max_concurrent_calls": 1,
        "max_tokens": 3000,
        "max_throttle_retries": 0,
        "retry_backoff_seconds": 0.001,
        "record_attempt": lambda _attempt: None,
        "spend_guard": guard,
        "pacer": pacer,
    }

    first = read_crops_parallel([CropJob("row", model, 0, PNG)], clients=clients, **options)
    second = read_crops_parallel([CropJob("label", model, 0, PNG)], clients=clients, **options)

    assert isinstance(first[("row", model)], ReaderAnswer)
    assert second[("label", model)] is None
    assert len(clients.requests) == 1, "the second batch must not reset the first batch's reserve"


def test_slot_question_sends_marked_full_view_before_close_up() -> None:
    full_view = encode_png(4, 2, bytes(24))
    request = build_crop_request(
        model_id=QWEN,
        full_view_png=full_view,
        crop_png=PNG,
        max_tokens=400,
    )
    content = request["messages"][0]["content"]

    assert [part["image"]["source"]["bytes"] for part in content if "image" in part] == [
        full_view,
        PNG,
    ]
    assert content[-1]["text"] == CROP_PROMPT


def test_claude_span_prompt_asks_ownership_separately_from_exact_copy() -> None:
    full_view = encode_png(4, 2, bytes(24))
    request = build_crop_request(
        model_id=OPUS,
        full_view_png=full_view,
        crop_png=PNG,
        max_tokens=2000,
        grounded_claude=True,
    )
    content = request["messages"][0]["content"]

    assert [part["image"]["source"]["bytes"] for part in content if "image" in part] == [
        full_view,
        PNG,
    ]
    assert "belongs" in content[-1]["text"]
    assert "copy its characters exactly" in content[-1]["text"]
    assert request["inferenceConfig"]["maxTokens"] == 2000


def test_claude_span_answer_requires_ownership_and_reasks_only_when_malformed() -> None:
    calls = 0

    def response(_request: dict[str, Any]) -> Mapping[str, Any]:
        nonlocal calls
        calls += 1
        payload = good('2"') if calls == 1 else good('2"') | {"belongs": True}
        return reply(payload)

    model = OPUS
    answers = run(
        FakeClients(response),
        [CropJob("slot", model, 0, PNG, grounded_claude=True)],
        rates=AnthropicRates(),
        calls_per_minute={model: 6000},
        max_tokens=2000,
        max_throttle_retries=0,
    )

    assert calls == 2
    assert answers[("slot", model)] is not None
    assert answers[("slot", model)].belongs is True


def test_row_request_shows_one_numbered_vendor_view_and_the_approved_prompt() -> None:
    request = build_row_request(model_id=OPUS, page_png=PNG, max_tokens=3000)

    content = request["messages"][0]["content"]
    assert len(content) == 2
    assert content[0]["image"]["source"]["bytes"] == PNG
    assert content[1]["text"] == ROW_PROMPT
    assert request["inferenceConfig"]["maxTokens"] == 3000


ROW_TWO = {"row": 2, "kind": "row", "also": [], "why": "front elevation"}


def _row_answer(
    payload: object, *, model_id: str = OPUS, candidate_count: int = 4
) -> tuple[RowChoiceAnswer | None, list[AttemptUsage]]:
    """One row question answered `payload` every time; `None` when it stayed malformed."""
    attempts: list[AttemptUsage] = []
    try:
        answer = read_row_choice(
            FakeClients(lambda _request: reply(payload)),
            model_id=model_id,
            page_png=PNG,
            page_index=0,
            candidate_count=candidate_count,
            max_tokens=3000,
            record_attempt=attempts.append,
        )
    except MalformedFormAnswer:
        return None, attempts
    return answer, attempts


@pytest.mark.parametrize(
    ("payload", "kind"),
    [
        ({"row": 3, "kind": "row", "also": [], "why": "synthetic: box 3"}, "row"),
        ({"row": 0, "kind": "no_countertop", "also": [], "why": "synthetic: tall unit"}, None),
        ({"row": 0, "kind": "not_among_boxes", "also": [], "why": "synthetic: unboxed"}, None),
        ({"row": 0, "kind": "unsure", "also": [], "why": "synthetic: cannot tell"}, None),
    ],
)
def test_row_reader_keeps_each_kind_of_answer(payload: dict[str, object], kind: str | None) -> None:
    """v3 (#1108): "no countertop" and "a countertop no box measures" are two answers, not one 0."""
    answer, attempts = _row_answer(payload)

    assert answer is not None
    assert answer.kind == payload["kind"]
    assert answer.row == payload["row"]
    assert answer.also == ()
    assert [attempt.malformed for attempt in attempts] == [False]
    assert attempts[0].prompt_id == "slot-row-choice-v3"


def test_row_reader_keeps_a_second_countertop_row_but_never_the_row_itself() -> None:
    answer, _ = _row_answer({"row": 1, "kind": "row", "also": [4, 1, 3, 4], "why": "two tops"})

    assert answer is not None and answer.row == 1 and answer.also == (3, 4)


@pytest.mark.parametrize(
    "payload",
    [
        {"row": 2, "kind": "no_countertop", "also": [], "why": "contradiction"},
        {"row": 0, "kind": "row", "also": [], "why": "contradiction"},
        {"row": 1, "kind": "row", "also": [5], "why": "box 5 does not exist"},
        {"row": 1, "kind": "row", "also": [0], "why": "box 0 does not exist"},
        {"row": 1, "kind": "maybe", "also": [], "why": "not a kind"},
    ],
)
def test_a_self_contradicting_or_out_of_range_row_answer_is_malformed(
    payload: dict[str, object],
) -> None:
    """Re-asked once, then the reader abstains, which sends the page to the reviewer."""
    answer, attempts = _row_answer(payload)

    assert answer is None
    assert [attempt.malformed for attempt in attempts] == [True, True]


def test_a_row_answer_of_the_older_shape_still_parses_with_no_kind() -> None:
    """A non-Claude reader's (or a stored v2) answer has no kind and names no second row."""
    answer, _ = _row_answer({"row": 0, "why": "synthetic: none"}, model_id=QWEN)

    assert answer == RowChoiceAnswer(QWEN, 0, "synthetic: none", None, ())


def test_row_prompt_names_its_box_colours_and_none_is_the_reviewers() -> None:
    """Box 1 was crimson while the same question said red numbers are the reviewer's (#1108)."""
    from extraction.slot_reader.bedrock import ROW_BOX_COLOURS, ROW_PROMPT_IDS

    assert {"slot-row-choice-v1", "slot-row-choice-v2", "slot-row-choice-v3"} == ROW_PROMPT_IDS
    for name, _rgb in ROW_BOX_COLOURS:
        assert name in ROW_PROMPT
    assert "numbers in red or blue, and yellow boxes, are the reviewer's markup" in ROW_PROMPT
    for name in ("red", "crimson", "blue", "yellow", "orange"):
        assert name not in {colour for colour, _ in ROW_BOX_COLOURS}
    for kind in ("no_countertop", "not_among_boxes", "unsure", '"also"'):
        assert kind in ROW_PROMPT


def test_row_reader_reasks_only_a_malformed_answer_and_records_both_attempts() -> None:
    calls = 0
    attempts: list[AttemptUsage] = []

    def answer(_request: dict[str, Any]) -> Mapping[str, Any]:
        nonlocal calls
        calls += 1
        return reply("not json" if calls == 1 else ROW_TWO)

    result = read_row_choice(
        FakeClients(answer),
        model_id=OPUS,
        page_png=PNG,
        page_index=3,
        candidate_count=4,
        max_tokens=3000,
        record_attempt=attempts.append,
    )

    assert result == RowChoiceAnswer(OPUS, 2, "front elevation", "row", ())
    assert calls == 2
    assert [attempt.malformed for attempt in attempts] == [True, False]
    assert [attempt.raw_response_text for attempt in attempts] == ["not json", json.dumps(ROW_TWO)]


def test_row_reader_zero_is_a_valid_reviewer_choice_not_a_retry() -> None:
    attempts: list[AttemptUsage] = []
    result = read_row_choice(
        FakeClients(
            lambda _request: reply(
                {"row": 0, "kind": "no_countertop", "also": [], "why": "no candidate fits"}
            )
        ),
        model_id=OPUS,
        page_png=PNG,
        page_index=0,
        candidate_count=3,
        max_tokens=3000,
        record_attempt=attempts.append,
    )

    assert result.row == 0
    assert len(attempts) == 1 and not attempts[0].malformed


def test_row_question_uses_the_same_read_pool_result_shape() -> None:
    model = OPUS
    answers = run(
        FakeClients(
            lambda _request: reply({"row": 1, "kind": "row", "also": [2], "why": "countertop row"})
        ),
        [CropJob("p0:row-choice", model, 0, PNG, row_question=True, candidate_count=2)],
        rates=AnthropicRates(),
        calls_per_minute={model: 6000},
        max_tokens=3000,
    )

    assert answers == {
        ("p0:row-choice", model): RowChoiceAnswer(model, 1, "countertop row", "row", (2,))
    }


def test_counter_break_question_is_a_hold_only_two_picture_question() -> None:
    model = "anthropic.claude-sonnet-5-5"
    view = encode_png(4, 2, bytes(24))
    request = build_counter_break_request(
        model_id=model, row_png=PNG, view_png=view, max_tokens=100
    )
    content = request["messages"][0]["content"]
    assert content[0]["image"]["source"]["bytes"] == view
    assert content[1]["image"]["source"]["bytes"] == PNG
    assert content[2]["text"] == COUNTER_BREAK_PROMPT
    assert "does not approve the row" in COUNTER_BREAK_PROMPT

    attempts: list[AttemptUsage] = []
    answers = read_crops_parallel(
        [CropJob("p0:counter-break", model, 0, PNG, view, counter_break_question=True)],
        clients=FakeClients(
            lambda _request: reply(
                {
                    "contains_tall_appliance": True,
                    "stone_ends": "unsure",
                    "why": "a tall outlined bay is present",
                }
            )
        ),
        rates=AnthropicRates(),
        calls_per_minute={model: 6000},
        max_concurrent_calls=1,
        max_tokens=100,
        max_throttle_retries=0,
        retry_backoff_seconds=0.001,
        record_attempt=attempts.append,
    )
    assert answers == {
        ("p0:counter-break", model): CounterBreakAnswer(
            model, True, "a tall outlined bay is present"
        )
    }
    assert len(attempts) == 1
    assert attempts[0].prompt_id == COUNTER_BREAK_PROMPT_ID
    assert attempts[0].raw_response_text is not None


def test_each_reader_attempt_retains_its_exact_question_packet() -> None:
    packet = {
        "question_id": "p0:slot0:0",
        "candidate_ids": ["p0:slot0:0"],
        "images": {
            "full_view": {"sha256": "a" * 64, "storage_key": "full.png"},
            "close_up": {"sha256": "b" * 64, "storage_key": "close.png"},
        },
    }
    attempts: list[AttemptUsage] = []
    full_view = encode_png(4, 2, bytes(24))
    read_crops_parallel(
        [CropJob("p0:slot0:0", QWEN, 0, PNG, full_view, question_packet=packet)],
        clients=FakeClients(lambda _request: reply(good('2"'))),
        rates=Rates(),
        calls_per_minute={QWEN: 6000},
        max_concurrent_calls=1,
        max_tokens=400,
        max_throttle_retries=0,
        retry_backoff_seconds=0.001,
        record_attempt=attempts.append,
    )

    assert len(attempts) == 1
    assert attempts[0].question_packet == packet


def test_the_prompt_never_asks_for_the_parts_of_a_number() -> None:
    """Whole, numerator and denominator are never used (E2 guard 2), so they are not asked for."""
    for word in ("whole", "numerator", "denominator"):
        assert f'"{word}"' not in CROP_PROMPT


def test_the_prompts_example_numbers_are_invented() -> None:
    """The prompt is public; its only numbers are the invented examples written here."""
    assert set(re.findall(r"\d+", CROP_PROMPT)) <= {"250", "9", "7", "8", "4", "1", "6"}


def test_every_job_is_answered_under_its_own_key() -> None:
    clients = FakeClients(lambda request: reply(good(request["modelId"][:4])))
    jobs = [CropJob(f"k{i}", model, 0, PNG) for i in range(3) for model in (QWEN, KIMI)]

    answers = run(clients, jobs)

    assert set(answers) == {(job.key, job.model_id) for job in jobs}
    assert answers[("k1", QWEN)].text == "qwen"
    assert answers[("k2", KIMI)].text == "us.m"


def test_a_malformed_answer_is_asked_once_more_then_abstains() -> None:
    clients = FakeClients(lambda _request: reply("not json"))
    answers = run(clients, [CropJob("k", QWEN, 0, PNG)])

    assert answers == {("k", QWEN): None}
    assert len(clients.requests) == 2


def test_an_answer_with_a_wrong_type_is_malformed_not_coerced() -> None:
    payload = good('2"') | {"readable": "yes"}
    clients = FakeClients(lambda _request: reply(payload))
    assert run(clients, [CropJob("k", QWEN, 0, PNG)]) == {("k", QWEN): None}


def test_a_throttled_call_is_retried() -> None:
    calls = {"n": 0}

    class ThrottlingException(Exception):
        pass

    def answer(_request: dict[str, Any]) -> Mapping[str, Any]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ThrottlingException("slow down")
        return reply(good('2"'))

    answers = run(FakeClients(answer), [CropJob("k", QWEN, 0, PNG)])
    answered = answers[("k", QWEN)]
    assert answered is not None and answered.text == '2"'


def test_an_unpriced_or_unpaced_reader_is_refused_before_any_call() -> None:
    clients = FakeClients(lambda _request: reply(good('2"')))

    class NoRates:
        def rate_for(self, model_id: str) -> object | None:
            return None

    with pytest.raises(ValueError, match="no stated price"):
        run(clients, [CropJob("k", QWEN, 0, PNG)], rates=NoRates())
    with pytest.raises(ValueError, match="pacing"):
        run(clients, [CropJob("k", QWEN, 0, PNG)], calls_per_minute={KIMI: 60})
    assert clients.requests == []


def test_a_crop_is_read_once_by_each_reader() -> None:
    clients = FakeClients(lambda _request: reply(good('2"')))
    with pytest.raises(ValueError):
        run(clients, [CropJob("k", QWEN, 0, PNG), CropJob("k", QWEN, 0, PNG)])


# ---------------------------------------------------------------------------------------------
# The wall question (#992)
# ---------------------------------------------------------------------------------------------

VIEW = encode_png(3, 2, bytes(18))


def walls(
    left: str = "yes", right: str = "yes", behind: str = "unsure", view: str = "elevation"
) -> dict[str, str]:
    return {
        "left": left,
        "right": right,
        "behind": behind,
        "view": view,
        "left_evidence": "hatched wall",
        "right_evidence": "hatched wall",
        "behind_evidence": "",
    }


def test_the_wall_question_shows_the_row_then_the_view_and_asks_kimi_at_low_effort() -> None:
    kimi = build_wall_request(model_id=KIMI, row_png=PNG, view_png=VIEW, max_tokens=400)
    qwen = build_wall_request(model_id=QWEN, row_png=PNG, view_png=VIEW, max_tokens=400)
    assert kimi["outputConfig"] == {"effort": "low"}
    assert "temperature" not in kimi["inferenceConfig"]
    assert qwen["inferenceConfig"]["temperature"] == 0
    content = qwen["messages"][0]["content"]
    assert content[0]["image"]["source"]["bytes"] == PNG
    assert content[1]["image"]["source"]["bytes"] == VIEW
    assert content[2]["text"] == WALL_PROMPT


def test_label_crops_and_wall_questions_share_one_batch_and_each_gets_its_own_answer() -> None:
    def answer(request: dict[str, Any]) -> Mapping[str, Any]:
        pictures = [part for part in request["messages"][0]["content"] if "image" in part]
        return reply(walls(view="Plan") if len(pictures) == 2 else good('2"'))

    attempts: list[AttemptUsage] = []
    answers = run(
        FakeClients(answer),
        [CropJob("label", QWEN, 0, PNG), CropJob("walls", QWEN, 0, PNG, VIEW)],
        record_attempt=attempts.append,
    )

    label, wall = answers[("label", QWEN)], answers[("walls", QWEN)]
    assert isinstance(label, ReaderAnswer) and label.text == '2"'
    assert isinstance(wall, WallAnswer)
    assert (wall.left, wall.right, wall.behind, wall.view) == (
        Side.YES,
        Side.YES,
        Side.UNSURE,
        "plan",
    )
    assert {attempt.prompt_id for attempt in attempts} == {"slot-crop-v1", "slot-walls-v1"}
    assert all(attempt.raw_response_text for attempt in attempts), "raw answers are kept (#985)"


def test_a_wall_side_outside_yes_no_unsure_is_malformed_and_abstains() -> None:
    clients = FakeClients(lambda _request: reply(walls(left="probably")))
    assert run(clients, [CropJob("walls", KIMI, 0, PNG, VIEW)]) == {("walls", KIMI): None}
    assert len(clients.requests) == 2, "asked once more, then abstains"


def test_an_unclear_view_is_never_a_plan() -> None:
    clients = FakeClients(lambda _request: reply(walls(view="section")))
    answer = run(clients, [CropJob("walls", KIMI, 0, PNG, VIEW)])[("walls", KIMI)]
    assert isinstance(answer, WallAnswer) and answer.view == "other"


def test_counter_break_answer_carries_where_the_stone_ends() -> None:
    model = "anthropic.claude-opus-5-5"
    view = encode_png(4, 2, bytes(24))
    answers = read_crops_parallel(
        [CropJob("p0:counter-break", model, 0, PNG, view, counter_break_question=True)],
        clients=FakeClients(
            lambda _request: reply(
                {
                    "contains_tall_appliance": False,
                    "stone_ends": "into_walls",
                    "why": "stone runs into pockets",
                }
            )
        ),
        rates=AnthropicRates(),
        calls_per_minute={model: 6000},
        max_concurrent_calls=1,
        max_tokens=100,
        max_throttle_retries=0,
        retry_backoff_seconds=0.001,
        record_attempt=lambda _attempt: None,
    )
    answer = answers[("p0:counter-break", model)]
    assert isinstance(answer, CounterBreakAnswer) and answer.stone_ends == "into_walls"
    assert '"stone_ends"' in COUNTER_BREAK_PROMPT


# ---------------------------------------------------------------------------------------------
# The Claude readers' fixed answer shape, effort and picture limits (#1051)
# ---------------------------------------------------------------------------------------------

SONNET = "anthropic.claude-sonnet-5-5"
CLAUDE_PACE = {OPUS: 6000, SONNET: 6000}


def span(text: str = '2"', **changes: object) -> dict[str, object]:
    return good(text) | {"belongs": True} | changes


def claude_requests() -> dict[str, tuple[dict[str, Any], dict[str, object]]]:
    return {
        "span": (
            build_crop_request(
                model_id=OPUS,
                crop_png=PNG,
                full_view_png=VIEW,
                max_tokens=3000,
                grounded_claude=True,
            ),
            SPAN_SCHEMA,
        ),
        "crop": (build_crop_request(model_id=OPUS, crop_png=PNG, max_tokens=3000), CROP_SCHEMA),
        "row": (build_row_request(model_id=OPUS, page_png=PNG, max_tokens=3000), ROW_CHOICE_SCHEMA),
        "counter_break": (
            build_counter_break_request(model_id=OPUS, row_png=PNG, view_png=VIEW, max_tokens=3000),
            COUNTER_BREAK_SCHEMA,
        ),
        "walls": (
            build_wall_request(model_id=OPUS, row_png=PNG, view_png=VIEW, max_tokens=3000),
            WALL_SCHEMA,
        ),
    }


@pytest.mark.parametrize("question", ["span", "crop", "row", "counter_break", "walls"])
def test_every_claude_question_carries_its_answer_shape_and_a_high_effort(question: str) -> None:
    request, schema = claude_requests()[question]

    assert request["anthropicOutputConfig"] == {
        "format": {"type": "json_schema", "schema": schema},
        "effort": "high",
    }


def test_bedrock_readers_requests_are_unchanged() -> None:
    for model in (QWEN, KIMI):
        for request in (
            build_crop_request(model_id=model, crop_png=PNG, max_tokens=400),
            build_row_request(model_id=model, page_png=PNG, max_tokens=400),
            build_wall_request(model_id=model, row_png=PNG, view_png=VIEW, max_tokens=400),
            build_counter_break_request(model_id=model, row_png=PNG, view_png=VIEW, max_tokens=400),
        ):
            assert "anthropicOutputConfig" not in request
            assert set(request) <= {"modelId", "messages", "inferenceConfig", "outputConfig"}


def test_the_stated_effort_reaches_every_claude_call() -> None:
    clients = FakeClients(lambda _request: reply(span()))
    run(
        clients,
        [CropJob("slot", OPUS, 0, PNG, VIEW, grounded_claude=True)],
        rates=AnthropicRates(),
        calls_per_minute=CLAUDE_PACE,
        claude_effort="max",
    )

    assert [request["anthropicOutputConfig"]["effort"] for request in clients.requests] == ["max"]


@pytest.mark.parametrize(
    "response",
    [
        # A refusal: the adapter passes on no text.
        {"stopReason": "refusal", "output": {"message": {"content": []}}, "usage": {}},
        # Cut off at the token limit, partial text and all.
        {
            "stopReason": "max_tokens",
            "output": {"message": {"content": [{"text": '{"belongs": true, "text": "1'}]}},
        },
        # Finished, but a key short of its shape.
        reply(good('2"')),
        # Finished, with a key the shape does not have.
        reply(span() | {"why": "extra"}),
        # Finished, but fenced: a Claude answer must be exactly the JSON object.
        reply("```json\n" + json.dumps(span()) + "\n```"),
    ],
)
def test_a_refused_cut_off_or_misshapen_claude_answer_abstains(response: dict[str, Any]) -> None:
    attempts: list[AttemptUsage] = []
    clients = FakeClients(lambda _request: response)

    answers = run(
        clients,
        [CropJob("slot", OPUS, 0, PNG, VIEW, grounded_claude=True)],
        rates=AnthropicRates(),
        calls_per_minute=CLAUDE_PACE,
        record_attempt=attempts.append,
    )

    assert answers == {("slot", OPUS): None}
    assert len(clients.requests) == 2, "asked once more, then abstains"
    assert [attempt.malformed for attempt in attempts] == [True, True]


def test_an_oversized_claude_picture_is_refused_before_sending_and_records_why() -> None:
    attempts: list[AttemptUsage] = []
    clients = FakeClients(lambda _request: reply(span()))
    wide = encode_png(2600, 2, bytes(2600 * 2 * 3))

    answers = run(
        clients,
        [
            CropJob("wide", OPUS, 4, wide, VIEW, grounded_claude=True),
            CropJob("fine", OPUS, 4, PNG, VIEW, grounded_claude=True),
        ],
        rates=AnthropicRates(),
        calls_per_minute=CLAUDE_PACE,
        record_attempt=attempts.append,
    )

    assert answers[("wide", OPUS)] is None
    assert isinstance(answers[("fine", OPUS)], ReaderAnswer)
    assert len(clients.requests) == 1
    refused = [attempt for attempt in attempts if attempt.failure_kind == "PictureWouldBeResized"]
    assert len(refused) == 1
    assert refused[0].prompt_id == CLAUDE_SPAN_PROMPT_ID
    assert refused[0].page_index == 4
    assert refused[0].input_tokens is None and refused[0].output_tokens is None


def test_an_oversized_picture_never_blocks_a_bedrock_reader() -> None:
    wide = encode_png(2600, 2, bytes(2600 * 2 * 3))
    answers = run(FakeClients(lambda _request: reply(good('2"'))), [CropJob("k", QWEN, 0, wide)])
    assert isinstance(answers[("k", QWEN)], ReaderAnswer)


def test_a_sideways_span_is_shown_its_upright_copy_third_and_told_which_it_is() -> None:
    upright = encode_png(2, 3, bytes(18))
    clients = FakeClients(lambda _request: reply(span()))

    answers = run(
        clients,
        [CropJob("slot", OPUS, 0, PNG, VIEW, grounded_claude=True, upright_png=upright)],
        rates=AnthropicRates(),
        calls_per_minute=CLAUDE_PACE,
    )

    assert isinstance(answers[("slot", OPUS)], ReaderAnswer)
    content = clients.requests[0]["messages"][0]["content"]
    assert [part["image"]["source"]["bytes"] for part in content if "image" in part] == [
        VIEW,
        PNG,
        upright,
    ]
    texts = [part["text"] for part in content if "text" in part]
    assert texts == [CLAUDE_UPRIGHT_NOTE, CLAUDE_SPAN_PROMPT]
    assert "Picture 3 is Picture 2 turned a quarter turn clockwise" in CLAUDE_UPRIGHT_NOTE


def test_a_span_without_a_sideways_label_keeps_its_two_pictures_and_wording() -> None:
    request, _ = claude_requests()["span"]
    content = request["messages"][0]["content"]

    assert [part["image"]["source"]["bytes"] for part in content if "image" in part] == [VIEW, PNG]
    assert [part["text"] for part in content if "text" in part] == [CLAUDE_SPAN_PROMPT]
    assert CLAUDE_SPAN_PROMPT_ID == "claude-slot-span-v3"


def test_an_upright_copy_belongs_only_to_a_grounded_claude_span() -> None:
    with pytest.raises(ValueError, match="upright"):
        build_crop_request(model_id=QWEN, crop_png=PNG, max_tokens=400, upright_png=PNG)


def test_the_claude_label_question_defines_every_answer_field() -> None:
    """#1104: v2 named the fields without saying what they mean, so each reader guessed; one called a
    plain piece label "combined" because its neighbours' labels showed in the close-up. Each field is
    now defined for the one label that belongs to the span, and neighbours never make it combined.
    Earlier wordings stay recognised for stored runs."""
    from extraction.slot_reader.bedrock import CLAUDE_SPAN_PROMPT_IDS

    for field in ('"stacked":', '"combined":', '"readable":', '"no_dimension":'):
        assert f"- {field}" in CLAUDE_SPAN_PROMPT, field
    assert "Labels of neighbouring spans that also show in Picture 2 never make it combined" in (
        CLAUDE_SPAN_PROMPT
    )
    assert "A single dimension is not combined, even with a stacked fraction" in CLAUDE_SPAN_PROMPT
    assert "inches in brackets" in CLAUDE_SPAN_PROMPT and "is not combined" in CLAUDE_SPAN_PROMPT
    assert CLAUDE_SPAN_PROMPT_IDS == {
        "claude-slot-span-v3",
        "claude-slot-span-v2",
        "claude-slot-span-v1",
    }
