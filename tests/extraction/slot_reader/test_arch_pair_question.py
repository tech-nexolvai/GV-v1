"""The architect-pairing question to the two Claude readers (#1053, type 1 step T2 part B).

Verification for `ARCH_PAIR_PROMPT_ID`, `build_arch_pair_request`, `read_arch_pair` and the
`arch_pair_question` job kind in `extraction/slot_reader/bedrock.py`, and the array support in
`extraction/slot_reader/claude_output.py`. Fake clients only: no network, no client value.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Any

import pytest

from evidence.crop import encode_png
from extraction.form_reader.bedrock import AttemptUsage, MalformedFormAnswer
from extraction.slot_reader.bedrock import (
    ARCH_PAIR_PROMPT_ID,
    ArchPairAnswer,
    CropJob,
    arch_pair_prompt,
    build_arch_pair_request,
    read_arch_pair,
    read_crops_parallel,
)
from extraction.slot_reader.claude_output import ARCH_PAIR_SCHEMA, claude_answer

OPUS = "anthropic.claude-opus-5-5"
SONNET = "anthropic.claude-sonnet-5-5"
PNG = encode_png(2, 2, bytes(12))


def reply(payload: object) -> dict[str, Any]:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return {
        "stopReason": "end_turn",
        "output": {"message": {"content": [{"text": text}]}},
        "usage": {"inputTokens": 10, "outputTokens": 5},
    }


class FakeClients:
    def __init__(self, answer: Callable[[dict[str, Any]], Mapping[str, Any]]) -> None:
        self.answer = answer
        self.requests: list[dict[str, Any]] = []

    def for_current_thread(self) -> FakeClients:
        return self

    def converse(self, **kwargs: Any) -> Mapping[str, Any]:
        self.requests.append(kwargs)
        return self.answer(kwargs)


class Rates:
    def rate_for(self, model_id: str) -> object | None:
        rate: object = type(
            "Rate",
            (),
            {
                "input_per_1k_tokens": Decimal("0.004"),
                "output_per_1k_tokens": Decimal("0.020"),
            },
        )()
        return rate


def test_the_answer_shape_is_structure_only() -> None:
    assert ARCH_PAIR_SCHEMA == {
        "type": "object",
        "properties": {
            "overall": {"type": "integer"},
            "pieces": {"type": "array", "items": {"type": "integer"}},
            "why": {"type": "string"},
        },
        "required": ["overall", "pieces", "why"],
        "additionalProperties": False,
    }


def test_an_array_answer_is_checked_item_by_item() -> None:
    assert claude_answer(reply({"overall": 0, "pieces": [1, 0], "why": "x"}), ARCH_PAIR_SCHEMA)
    with pytest.raises(MalformedFormAnswer):
        claude_answer(reply({"overall": 0, "pieces": [1, "2"], "why": "x"}), ARCH_PAIR_SCHEMA)
    with pytest.raises(MalformedFormAnswer):
        claude_answer(reply({"overall": 0, "pieces": [True], "why": "x"}), ARCH_PAIR_SCHEMA)
    with pytest.raises(MalformedFormAnswer):
        claude_answer(reply({"overall": 0, "pieces": 1, "why": "x"}), ARCH_PAIR_SCHEMA)


def test_the_request_shows_one_picture_and_states_its_shape_and_effort() -> None:
    request = build_arch_pair_request(
        model_id=OPUS, picture_png=PNG, vendor_pieces=3, architect_spans=4, max_tokens=3000
    )

    content = request["messages"][0]["content"]
    assert [part.get("image", {}).get("source", {}).get("bytes") for part in content[:1]] == [PNG]
    assert content[1]["text"] == arch_pair_prompt(vendor_pieces=3, architect_spans=4)
    assert request["anthropicOutputConfig"] == {
        "format": {"type": "json_schema", "schema": ARCH_PAIR_SCHEMA},
        "effort": "high",
    }


def test_the_prompt_asks_for_the_same_physical_thing_never_a_comparison() -> None:
    prompt = arch_pair_prompt(vendor_pieces=3, architect_spans=4).lower()

    assert "v1 to v3" in prompt and "a1 to a4" in prompt
    assert "same physical thing" in prompt
    assert "centre line" in prompt and "never a cabinet width" in prompt
    for fixture in ("outlet", "sink", "appliance", "artwork"):
        assert fixture in prompt
    assert "do not compare" in prompt
    # No client value: apart from the mark numbers and "0 for none", the prompt holds no number.
    stripped = prompt
    for mark in ("v1", "v3", "a1", "a4", "or 0", "use 0"):
        stripped = stripped.replace(mark, "")
    assert not any(character.isdigit() for character in stripped)


@pytest.mark.parametrize(
    "payload",
    [
        {"overall": 0, "pieces": [1, 2], "why": "too few pieces"},
        {"overall": 0, "pieces": [1, 2, 3, 4], "why": "too many"},
        {"overall": 5, "pieces": [1, 2, 3], "why": "no A5"},
        {"overall": 0, "pieces": [1, -1, 3], "why": "negative"},
    ],
)
def test_an_answer_outside_the_numbered_marks_is_malformed(payload: dict[str, object]) -> None:
    attempts: list[AttemptUsage] = []
    clients = FakeClients(lambda _request: reply(payload))

    with pytest.raises(MalformedFormAnswer):
        read_arch_pair(
            clients,
            model_id=OPUS,
            picture_png=PNG,
            page_index=0,
            vendor_pieces=3,
            architect_spans=4,
            max_tokens=3000,
            record_attempt=attempts.append,
        )
    assert len(clients.requests) == 2, "asked once more, then abstains"
    assert [attempt.malformed for attempt in attempts] == [True, True]
    assert {attempt.prompt_id for attempt in attempts} == {ARCH_PAIR_PROMPT_ID}


def test_a_good_answer_is_kept_exactly_as_given() -> None:
    attempts: list[AttemptUsage] = []
    clients = FakeClients(
        lambda _request: reply(
            {"overall": 4, "pieces": [1, 1, 0], "why": "the two pieces split A1"}
        )
    )

    answer = read_arch_pair(
        clients,
        model_id=SONNET,
        picture_png=PNG,
        page_index=2,
        vendor_pieces=3,
        architect_spans=4,
        max_tokens=3000,
        record_attempt=attempts.append,
        question_packet={"packet_sha256": "abc"},
    )

    assert answer == ArchPairAnswer(SONNET, 4, (1, 1, 0), "the two pieces split A1")
    assert attempts[0].question_packet == {"packet_sha256": "abc"}
    assert attempts[0].raw_response_text is not None


def test_the_job_kind_rides_the_shared_batch_under_its_own_prompt_id() -> None:
    attempts: list[AttemptUsage] = []
    clients = FakeClients(lambda _request: reply({"overall": 0, "pieces": [2], "why": "same"}))

    answers = read_crops_parallel(
        [
            CropJob(
                "p0:arch-pair",
                model,
                0,
                PNG,
                arch_pair_question=True,
                vendor_pieces=1,
                architect_spans=2,
                question_packet={"packet_sha256": "abc"},
            )
            for model in (OPUS, SONNET)
        ],
        clients=clients,
        rates=Rates(),
        calls_per_minute={OPUS: 6000, SONNET: 6000},
        max_concurrent_calls=1,
        max_tokens=3000,
        max_throttle_retries=0,
        retry_backoff_seconds=0.001,
        record_attempt=attempts.append,
    )

    assert answers == {
        ("p0:arch-pair", OPUS): ArchPairAnswer(OPUS, 0, (2,), "same"),
        ("p0:arch-pair", SONNET): ArchPairAnswer(SONNET, 0, (2,), "same"),
    }
    assert {attempt.prompt_id for attempt in attempts} == {ARCH_PAIR_PROMPT_ID}
    assert all(
        request["messages"][0]["content"][1]["text"].startswith("This sheet")
        for request in clients.requests
    )


def test_a_pair_job_must_state_its_numbering() -> None:
    clients = FakeClients(lambda _request: reply({"overall": 0, "pieces": [0], "why": ""}))
    with pytest.raises(ValueError, match="numbering"):
        read_crops_parallel(
            [CropJob("p0:arch-pair", OPUS, 0, PNG, arch_pair_question=True)],
            clients=clients,
            rates=Rates(),
            calls_per_minute={OPUS: 6000},
            max_concurrent_calls=1,
            max_tokens=3000,
            max_throttle_retries=0,
            retry_backoff_seconds=0.001,
            record_attempt=lambda _attempt: None,
        )
