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
from extraction.form_reader.bedrock import AttemptUsage
from extraction.slot_reader.bedrock import (
    CROP_PROMPT,
    CropJob,
    build_crop_request,
    build_wall_request,
    read_crops_parallel,
)
from extraction.slot_reader.seal import ReaderAnswer
from extraction.slot_reader.walls import WALL_PROMPT, Side, WallAnswer

QWEN = "qwen.qwen3-vl-235b-a22b"
KIMI = "us.moonshotai.kimi-k3"
PNG = encode_png(2, 2, bytes(12))


def reply(payload: object) -> dict[str, Any]:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return {
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
        if model_id != "anthropic.claude-opus-5-5":
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
