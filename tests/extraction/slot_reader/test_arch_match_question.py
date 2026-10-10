"""The architect-view match question to the two Claude readers (#1166).

Verification for `ARCH_MATCH_PROMPT_ID` (`arch-view-match-v1`), `arch_match_prompt`,
`build_arch_match_request`, `arch_match_answer`, `read_arch_match`, the `arch_match_question` job
kind and its stored-answer replay in `extraction/slot_reader/bedrock.py`, and `arch_match_schema`
in `extraction/slot_reader/claude_output.py`. Fake clients only: no network, no client value.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from typing import Any

import pytest

from evidence.crop import encode_png
from extraction.form_reader.bedrock import AttemptUsage, MalformedFormAnswer
from extraction.slot_reader.bedrock import (
    ARCH_MATCH_PROMPT_ID,
    ARCH_MATCH_PROMPT_IDS,
    ARCH_PAIR_PROMPT_ID,
    ArchMatchAnswer,
    CropJob,
    arch_match_answer,
    arch_match_prompt,
    build_arch_match_request,
    job_prompt_id,
    read_arch_match,
    read_crops_parallel,
    replay_stored_answer,
)
from extraction.slot_reader.claude_output import arch_match_schema, claude_answer
from tests.extraction.slot_reader.test_arch_pair_question import FakeClients, Rates, reply

OPUS = "anthropic.claude-opus-5-5"
SONNET = "anthropic.claude-sonnet-5-5"
PNG = encode_png(2, 2, bytes(12))


def said(*words: str) -> list[dict[str, object]]:
    return [{"n": n, "same": word} for n, word in enumerate(words, start=1)]


def test_the_question_has_its_own_prompt_id() -> None:
    assert ARCH_MATCH_PROMPT_ID == "arch-view-match-v1"
    assert ARCH_MATCH_PROMPT_ID in ARCH_MATCH_PROMPT_IDS
    job = CropJob("p0:arch-match", OPUS, 0, PNG, arch_match_question=True, architect_candidates=2)
    assert job_prompt_id(job, None) == ARCH_MATCH_PROMPT_ID != ARCH_PAIR_PROMPT_ID


def test_the_schema_offers_each_shown_view_none_and_unsure() -> None:
    schema = arch_match_schema(3)
    properties = schema["properties"]
    assert isinstance(properties, dict)
    assert properties["pick"]["enum"] == ["1", "2", "3", "none", "unsure"]
    with pytest.raises(ValueError):
        arch_match_schema(0)


def test_the_prompt_judges_only_what_is_drawn_and_never_a_printed_number() -> None:
    prompt = arch_match_prompt(candidates=3, common_scale=True)
    lower = prompt.lower()

    assert "only by what is drawn" in lower
    assert "do not read, compare or add up any printed number" in lower
    assert "do not match by any printed words or titles" in lower
    assert "twins" in lower and '"unsure"' in lower and '"none"' in lower
    assert "same size per real inch" in lower
    assert (
        "not drawn at the same size" in arch_match_prompt(candidates=2, common_scale=False).lower()
    )
    # Its examples are invented and carry no dimension: no feet-inch, inch or millimetre value.
    assert not re.search(r"\d+\s*'|\d+\s*(in|mm)\b|\d+/\d+", prompt)


def test_the_request_shows_one_picture_and_states_its_shape_and_effort() -> None:
    request = build_arch_match_request(
        model_id=OPUS, picture_png=PNG, candidates=2, common_scale=False, max_tokens=3000
    )

    content = request["messages"][0]["content"]
    assert content[0]["image"]["source"]["bytes"] == PNG
    assert content[1]["text"] == arch_match_prompt(candidates=2, common_scale=False)
    assert request["anthropicOutputConfig"] == {
        "format": {"type": "json_schema", "schema": arch_match_schema(2)},
        "effort": "high",
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"candidates": said("yes"), "pick": "1", "why": "only one shown, two expected"},
        {"candidates": said("yes", "no", "no"), "pick": "1", "why": "three, two shown"},
        {"candidates": said("no", "maybe"), "pick": "unsure", "why": "a word not offered"},
        {"candidates": said("no", "no"), "pick": "3", "why": "a view not shown"},
        {"candidates": said("no", "unsure"), "pick": "2", "why": "picked without a yes"},
        {"candidates": said("yes", "yes"), "pick": "1", "why": "two yes"},
        {"candidates": said("yes", "no"), "pick": "none", "why": "none beside a yes"},
        {"candidates": said("no", "no"), "pick": "02", "why": "not a number shown"},
        {"candidates": said("no", "no"), "pick": "first", "why": "not offered"},
        {"candidates": said("no", "no"), "pick": 1, "why": "not a string"},
    ],
)
def test_a_malformed_answer_is_refused(payload: dict[str, object]) -> None:
    with pytest.raises((MalformedFormAnswer, ValueError)):
        arch_match_answer(payload, model_id=OPUS, candidates=2)


@pytest.mark.parametrize(
    ("pick", "same", "expected"),
    [
        ("2", ("no", "yes"), 2),
        ("none", ("no", "no"), 0),
        ("unsure", ("unsure", "unsure"), None),
        ("unsure", ("yes", "no"), None),
    ],
)
def test_a_good_answer_is_kept_exactly(
    pick: str, same: tuple[str, str], expected: int | None
) -> None:
    answer = arch_match_answer(
        {"candidates": said(*same), "pick": pick, "why": "the sink sits between two drawer stacks"},
        model_id=SONNET,
        candidates=2,
    )

    assert answer == ArchMatchAnswer(
        SONNET, expected, same, "the sink sits between two drawer stacks"
    )


def test_a_malformed_reply_is_asked_once_more_then_abstains() -> None:
    attempts: list[AttemptUsage] = []
    clients = FakeClients(
        lambda _request: reply({"candidates": said("yes", "yes"), "pick": "1", "why": ""})
    )

    with pytest.raises(MalformedFormAnswer):
        read_arch_match(
            clients,
            model_id=OPUS,
            picture_png=PNG,
            page_index=4,
            candidates=2,
            common_scale=True,
            max_tokens=3000,
            record_attempt=attempts.append,
        )

    assert [attempt.malformed for attempt in attempts] == [True, True]
    assert {attempt.prompt_id for attempt in attempts} == {ARCH_MATCH_PROMPT_ID}
    assert "malformed" in clients.requests[1]["messages"][0]["content"][-1]["text"]


def test_a_refused_claude_reply_is_malformed_not_an_answer() -> None:
    refusal = {
        **reply({"candidates": said("yes"), "pick": "1", "why": ""}),
        "stopReason": "refusal",
    }
    with pytest.raises(MalformedFormAnswer):
        claude_answer(refusal, arch_match_schema(1))


def _batch(
    answer: Callable[[dict[str, Any]], Mapping[str, Any]], jobs: list[CropJob]
) -> tuple[dict[tuple[str, str], object], list[AttemptUsage]]:
    attempts: list[AttemptUsage] = []
    answers = read_crops_parallel(
        jobs,
        clients=FakeClients(answer),
        rates=Rates(),
        calls_per_minute={OPUS: 6000, SONNET: 6000},
        max_concurrent_calls=1,
        max_tokens=3000,
        max_throttle_retries=0,
        retry_backoff_seconds=0.001,
        record_attempt=attempts.append,
    )
    return dict(answers), attempts


def _job(model: str, **extra: Any) -> CropJob:
    return CropJob(
        "p0:arch-match",
        model,
        0,
        PNG,
        arch_match_question=True,
        architect_candidates=2,
        question_packet={"packet_sha256": "abc"},
        **extra,
    )


def test_the_job_kind_rides_the_shared_batch_and_a_malformed_reader_abstains() -> None:
    def answer(request: dict[str, Any]) -> Mapping[str, Any]:
        if request["modelId"] == OPUS:
            return reply({"candidates": said("no", "yes"), "pick": "2", "why": "same run"})
        return reply("not json")

    answers, attempts = _batch(answer, [_job(OPUS), _job(SONNET)])

    assert answers == {
        ("p0:arch-match", OPUS): ArchMatchAnswer(OPUS, 2, ("no", "yes"), "same run"),
        ("p0:arch-match", SONNET): None,
    }
    assert {attempt.prompt_id for attempt in attempts} == {ARCH_MATCH_PROMPT_ID}


def test_a_match_job_must_state_how_many_views_it_shows() -> None:
    with pytest.raises(ValueError, match="how many views"):
        _batch(
            lambda _request: reply({}),
            [CropJob("p0:arch-match", OPUS, 0, PNG, arch_match_question=True)],
        )


def test_a_stored_answer_replays_through_the_same_checks() -> None:
    job = _job(OPUS)
    good = json.dumps({"candidates": said("yes", "no"), "pick": "1", "why": "same"})
    bad = json.dumps({"candidates": said("no", "no"), "pick": "1", "why": "picked a no"})

    replayed = replay_stored_answer(job, good, reused_from="call-1", max_tokens=3000)
    assert replayed is not None
    answer, attempt = replayed
    assert answer == ArchMatchAnswer(OPUS, 1, ("yes", "no"), "same")
    assert attempt.reused_from == "call-1" and attempt.prompt_id == ARCH_MATCH_PROMPT_ID
    assert replay_stored_answer(job, bad, reused_from="call-2", max_tokens=3000) is None
