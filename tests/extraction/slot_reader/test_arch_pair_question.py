"""The architect-pairing question to the two Claude readers (#1053, type 1 step T2 part B).

Verification for `ARCH_PAIR_PROMPT_ID` (`arch-pair-v3`: what every architect dimension measures,
then each pairing as `A<k>`, `none` or `unsure`, #1109), `build_arch_pair_request`,
`arch_pair_answer` (v3 and stored v2 answers), `read_arch_pair` and the `arch_pair_question` job
kind in `extraction/slot_reader/bedrock.py`, and the array support and `arch_pair_schema` in
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
    ARCH_PAIR_PROMPT_IDS,
    ArchPairAnswer,
    CropJob,
    arch_pair_answer,
    arch_pair_prompt,
    build_arch_pair_request,
    read_arch_pair,
    read_crops_parallel,
)
from extraction.slot_reader.claude_output import (
    ARCH_MEASURES,
    ARCH_PAIR_V2_SCHEMA,
    arch_pair_schema,
    claude_answer,
)

OPUS = "anthropic.claude-opus-5-5"
SONNET = "anthropic.claude-sonnet-5-5"
PNG = encode_png(2, 2, bytes(12))


def measured(*measures: str) -> list[dict[str, object]]:
    """The `architect` part of an answer: A1, A2, ... measure these."""
    return [{"a": k, "measures": measure} for k, measure in enumerate(measures, start=1)]


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


def test_the_v2_answer_shape_is_kept() -> None:
    assert ARCH_PAIR_V2_SCHEMA == {
        "type": "object",
        "properties": {
            "architect": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "a": {"type": "integer"},
                        "measures": {"type": "string", "enum": list(ARCH_MEASURES)},
                    },
                    "required": ["a", "measures"],
                    "additionalProperties": False,
                },
            },
            "overall": {"type": "integer"},
            "pieces": {"type": "array", "items": {"type": "integer"}},
            "why": {"type": "string"},
        },
        "required": ["architect", "overall", "pieces", "why"],
        "additionalProperties": False,
    }
    assert ARCH_MEASURES == (
        "countertop",
        "cabinet_run",
        "single_cabinet",
        "filler_or_end_panel",
        "wall_to_wall",
        "clearance_or_gap",
        "blocking_or_backing",
        "fixture_or_appliance_centre",
        "appliance_opening",
        "height_or_other",
        "unsure",
    )


def test_the_v3_answer_shape_offers_each_marked_a_none_and_unsure_for_every_pairing() -> None:
    pairing = {"type": "string", "enum": ["A1", "A2", "none", "unsure"]}
    assert arch_pair_schema(2) == {
        "type": "object",
        "properties": {
            "architect": ARCH_PAIR_V2_SCHEMA["properties"]["architect"],  # type: ignore[index]
            "overall": pairing,
            "pieces": {"type": "array", "items": pairing},
            "why": {"type": "string"},
        },
        "required": ["architect", "overall", "pieces", "why"],
        "additionalProperties": False,
    }
    with pytest.raises(ValueError):
        arch_pair_schema(0)


def test_v3_is_asked_and_v2_and_v1_stay_recognisable_in_stored_records() -> None:
    assert ARCH_PAIR_PROMPT_ID == "arch-pair-v3"
    # The two-panel wording for a separate architect file (#1167) is recognised beside them.
    assert ARCH_PAIR_PROMPT_IDS == frozenset(
        {"arch-pair-v3", "arch-pair-2panel-v1", "arch-pair-v2", "arch-pair-v1"}
    )


def test_an_array_answer_is_checked_item_by_item() -> None:
    good = {"architect": measured("countertop"), "overall": 0, "pieces": [1, 0], "why": "x"}
    assert claude_answer(reply(good), ARCH_PAIR_V2_SCHEMA)
    for bad in (
        {**good, "pieces": [1, "2"]},
        {**good, "pieces": [True]},
        {**good, "pieces": 1},
        {**good, "architect": [{"a": 1, "measures": "a cabinet"}]},
        {**good, "architect": [{"a": 1}]},
        {**good, "architect": [{"a": "1", "measures": "countertop"}]},
        {key: value for key, value in good.items() if key != "architect"},
    ):
        with pytest.raises(MalformedFormAnswer):
            claude_answer(reply(bad), ARCH_PAIR_V2_SCHEMA)


def test_a_v3_answer_is_checked_word_by_word() -> None:
    schema = arch_pair_schema(1)
    good = {"architect": measured("countertop"), "overall": "unsure", "pieces": ["A1", "none"]}
    assert claude_answer(reply({**good, "why": "x"}), schema)
    for bad in (
        {**good, "pieces": ["A1", 0]},
        {**good, "pieces": ["A2", "none"]},
        {**good, "pieces": ["a1", "none"]},
        {**good, "overall": 0},
        {**good, "overall": "maybe"},
    ):
        with pytest.raises(MalformedFormAnswer):
            claude_answer(reply({**bad, "why": "x"}), schema)


def test_the_request_shows_one_picture_and_states_its_shape_and_effort() -> None:
    request = build_arch_pair_request(
        model_id=OPUS, picture_png=PNG, vendor_pieces=3, architect_spans=4, max_tokens=3000
    )

    content = request["messages"][0]["content"]
    assert [part.get("image", {}).get("source", {}).get("bytes") for part in content[:1]] == [PNG]
    assert content[1]["text"] == arch_pair_prompt(vendor_pieces=3, architect_spans=4)
    assert request["anthropicOutputConfig"] == {
        "format": {"type": "json_schema", "schema": arch_pair_schema(4)},
        "effort": "high",
    }


def test_the_prompt_asks_for_the_same_physical_thing_never_a_comparison() -> None:
    prompt = arch_pair_prompt(vendor_pieces=3, architect_spans=4).lower()

    assert "v1 to v3" in prompt and "a1 to a4" in prompt
    assert "same physical thing" in prompt
    assert "every architect dimension" in prompt
    for measure in ARCH_MEASURES:
        assert f'"{measure}"' in prompt, measure
        line = next(line for line in prompt.splitlines() if line.startswith(f'- "{measure}"'))
        assert line.count(" is: ") == 1 and line.count(" is not: ") == 1, line
    assert "centre line" in prompt
    assert "blocking or backing" in prompt
    assert "between a wall and an object's edge" in prompt
    assert "never a cabinet or countertop width" in prompt
    for fixture in ("outlet", "sink", "appliance", "artwork"):
        assert fixture in prompt
    assert "do not compare" in prompt
    # #1109: "none" and "unsure" are two separate answers for every pairing.
    assert '"none" only when you are sure the architect prints no dimension of the same thing' in (
        prompt
    )
    assert '"unsure" when you cannot tell' in prompt
    assert 'never answer "none" because you are unsure' in prompt
    assert "a-number, none or unsure for v1" in prompt
    # No client value: apart from the mark numbers, the prompt holds no number.
    stripped = prompt
    for mark in ("v1", "v3", "a1", "a4"):
        stripped = stripped.replace(mark, "")
    assert not any(character.isdigit() for character in stripped)


FOUR = measured("countertop", "single_cabinet", "blocking_or_backing", "unsure")
PIECES = ["A1", "A2", "A3"]


@pytest.mark.parametrize(
    "payload",
    [
        {"architect": FOUR, "overall": "none", "pieces": ["A1", "A2"], "why": "too few pieces"},
        {"architect": FOUR, "overall": "none", "pieces": [*PIECES, "A4"], "why": "too many"},
        {"architect": FOUR, "overall": "A5", "pieces": PIECES, "why": "no A5"},
        {"architect": FOUR, "overall": "A0", "pieces": PIECES, "why": "no A0"},
        {"architect": FOUR, "overall": "none", "pieces": ["A1", "0", "A3"], "why": "a number"},
        {"architect": FOUR, "overall": 0, "pieces": PIECES, "why": "the shapes mixed"},
        {"architect": FOUR[:3], "overall": "none", "pieces": PIECES, "why": "A4 not said"},
        {"architect": [*FOUR, FOUR[0]], "overall": "none", "pieces": PIECES, "why": "A1 twice"},
        {
            "architect": [*FOUR[:3], {"a": 5, "measures": "unsure"}],
            "overall": "none",
            "pieces": PIECES,
            "why": "A5 instead of A4",
        },
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
            {
                "architect": list(reversed(FOUR)),
                "overall": "A4",
                "pieces": ["A1", "A1", "none"],
                "why": "the two pieces split A1",
            }
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

    assert answer == ArchPairAnswer(
        SONNET,
        4,
        (1, 1, 0),
        "the two pieces split A1",
        ("countertop", "single_cabinet", "blocking_or_backing", "unsure"),
    ), "the measures are kept in A order, whatever order they were given in"
    assert attempts[0].question_packet == {"packet_sha256": "abc"}
    assert attempts[0].raw_response_text is not None


def test_unsure_is_kept_apart_from_none() -> None:
    clients = FakeClients(
        lambda _request: reply(
            {
                "architect": FOUR,
                "overall": "unsure",
                "pieces": ["A2", "unsure", "none"],
                "why": "the second piece's ticks are hidden",
            }
        )
    )

    answer = read_arch_pair(
        clients,
        model_id=OPUS,
        picture_png=PNG,
        page_index=0,
        vendor_pieces=3,
        architect_spans=4,
        max_tokens=3000,
        record_attempt=lambda _attempt: None,
    )

    assert (answer.overall, answer.pieces, answer.unsure) == (0, (2, 0, 0), ("overall", "V2"))
    assert len(clients.requests) == 1


def test_a_stored_v2_answer_still_parses_and_is_never_unsure() -> None:
    stored = {
        "architect": FOUR,
        "overall": 4,
        "pieces": [1, 1, 0],
        "why": "an answer in the v2 shape",
    }

    answer = arch_pair_answer(stored, model_id=SONNET, vendor_pieces=3, architect_spans=4)

    assert answer == ArchPairAnswer(
        SONNET,
        4,
        (1, 1, 0),
        "an answer in the v2 shape",
        ("countertop", "single_cabinet", "blocking_or_backing", "unsure"),
    )
    assert answer.unsure == ()
    for bad in ({**stored, "overall": 5}, {**stored, "pieces": [1, -1, 0]}):
        with pytest.raises(MalformedFormAnswer):
            arch_pair_answer(bad, model_id=SONNET, vendor_pieces=3, architect_spans=4)


def test_the_job_kind_rides_the_shared_batch_under_its_own_prompt_id() -> None:
    attempts: list[AttemptUsage] = []
    clients = FakeClients(
        lambda _request: reply(
            {
                "architect": measured("countertop", "single_cabinet"),
                "overall": "none",
                "pieces": ["A2"],
                "why": "same",
            }
        )
    )

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
        ("p0:arch-pair", OPUS): ArchPairAnswer(
            OPUS, 0, (2,), "same", ("countertop", "single_cabinet")
        ),
        ("p0:arch-pair", SONNET): ArchPairAnswer(
            SONNET, 0, (2,), "same", ("countertop", "single_cabinet")
        ),
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
