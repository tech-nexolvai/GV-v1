"""The two-panel pairing question for a separate architect file (`arch-pair-2panel-v1`, #1167).

Verification for `ARCH_PAIR_2PANEL_PROMPT_ID`, `arch_pair_two_panel_prompt`, the `two_panel` option
of `build_arch_pair_request` / `read_arch_pair`, and the `arch_pair_two_panel` job kind
(`job_prompt_id`, the shared batch, a stored answer replayed). A combined sheet's `arch-pair-v3`
question stays word for word. Fake clients only: no network, no client value.
"""

from __future__ import annotations

import hashlib

import pytest

from extraction.form_reader.bedrock import AttemptUsage
from extraction.slot_reader.bedrock import (
    ARCH_PAIR_2PANEL_PROMPT_ID,
    ARCH_PAIR_PROMPT_ID,
    ARCH_PAIR_PROMPT_IDS,
    ArchPairAnswer,
    CropJob,
    arch_pair_prompt,
    arch_pair_two_panel_prompt,
    build_arch_pair_request,
    job_prompt_id,
    read_arch_pair,
    read_crops_parallel,
    replay_stored_answer,
)
from tests.extraction.slot_reader.test_arch_pair_question import (
    OPUS,
    PNG,
    SONNET,
    FakeClients,
    Rates,
    measured,
    reply,
)

GOOD = {
    "architect": measured("single_cabinet", "single_cabinet"),
    "overall": "none",
    "pieces": ["A1", "A2", "none"],
    "why": "the same two cabinets",
}

#: `arch-pair-v3` as asked before #1167, by its SHA-256 for three numberings.
V3_PROMPT_SHA256 = {
    (1, 1): "896e55ddd839f5ef5ae8a34c24b76a32ad39f14f3f25b7df2d46086980e5cc1a",
    (3, 2): "dd3bc2e3d7b980302a22087c8991c2bbabf5a299d35ca7a0e430b7cbe3662932",
    (5, 7): "a01f7257634a73866293919f7821d20fe23a805b1c49ed559a074725bb1ddf1a",
}


@pytest.mark.parametrize(("pieces", "spans"), sorted(V3_PROMPT_SHA256))
def test_the_combined_sheet_question_is_unchanged_word_for_word(pieces: int, spans: int) -> None:
    text = arch_pair_prompt(vendor_pieces=pieces, architect_spans=spans)

    assert hashlib.sha256(text.encode()).hexdigest() == V3_PROMPT_SHA256[(pieces, spans)]


def test_the_two_panel_question_has_its_own_id_and_is_recognised_with_the_others() -> None:
    assert ARCH_PAIR_2PANEL_PROMPT_ID == "arch-pair-2panel-v1"
    assert ARCH_PAIR_2PANEL_PROMPT_ID in ARCH_PAIR_PROMPT_IDS
    assert ARCH_PAIR_PROMPT_ID == "arch-pair-v3"


@pytest.mark.parametrize("common_scale", [True, False])
def test_the_two_panel_question_says_what_the_panels_are_then_asks_v3s_steps(
    common_scale: bool,
) -> None:
    v3 = arch_pair_prompt(vendor_pieces=3, architect_spans=2)
    two = arch_pair_two_panel_prompt(vendor_pieces=3, architect_spans=2, common_scale=common_scale)
    steps = v3[v3.index("Judge everything only by") :]

    assert two.endswith(steps), "the steps, the measure words and the answer shape are v3's"
    opening = two[: -len(steps)]
    assert "LEFT panel" in opening and "RIGHT panel" in opening and "separate file" in opening
    assert "V1 to V3" in opening and "A1 to A2" in opening
    assert ("same scale" in opening) is common_scale
    assert ("different scales" in opening) is not common_scale
    assert not any(
        character.isdigit() for character in opening.replace("V1 to V3", "").replace("A1 to A2", "")
    ), "no printed number in the opening"
    with pytest.raises(ValueError):
        arch_pair_two_panel_prompt(vendor_pieces=0, architect_spans=2, common_scale=True)


def test_the_request_asks_the_two_panel_wording_only_when_told() -> None:
    def text(two_panel: bool | None) -> str:
        request = build_arch_pair_request(
            model_id=OPUS,
            picture_png=PNG,
            vendor_pieces=3,
            architect_spans=2,
            max_tokens=3000,
            two_panel=two_panel,
        )
        return str(request["messages"][0]["content"][1]["text"])

    assert text(None) == arch_pair_prompt(vendor_pieces=3, architect_spans=2)
    assert text(True) == arch_pair_two_panel_prompt(
        vendor_pieces=3, architect_spans=2, common_scale=True
    )
    assert text(False) == arch_pair_two_panel_prompt(
        vendor_pieces=3, architect_spans=2, common_scale=False
    )


def test_a_two_panel_answer_is_read_by_the_same_parser_and_recorded_under_its_own_id() -> None:
    attempts: list[AttemptUsage] = []
    clients = FakeClients(lambda _request: reply(GOOD))

    answer = read_arch_pair(
        clients,
        model_id=OPUS,
        picture_png=PNG,
        page_index=0,
        vendor_pieces=3,
        architect_spans=2,
        max_tokens=3000,
        record_attempt=attempts.append,
        two_panel=True,
    )

    assert answer == ArchPairAnswer(
        OPUS, 0, (1, 2, 0), "the same two cabinets", ("single_cabinet", "single_cabinet")
    )
    assert [(a.prompt_id, a.template_id) for a in attempts] == [
        (ARCH_PAIR_2PANEL_PROMPT_ID, ARCH_PAIR_2PANEL_PROMPT_ID)
    ]


def _job(model: str, *, two_panel: bool) -> CropJob:
    return CropJob(
        "p0:arch-pair",
        model,
        0,
        PNG,
        arch_pair_question=True,
        vendor_pieces=3,
        architect_spans=2,
        question_packet={"packet_sha256": "abc"},
        arch_pair_two_panel=two_panel,
        arch_pair_common_scale=two_panel,
    )


def test_the_job_kind_rides_the_shared_batch_under_its_own_prompt_id() -> None:
    attempts: list[AttemptUsage] = []
    clients = FakeClients(lambda _request: reply(GOOD))

    answers = read_crops_parallel(
        [_job(model, two_panel=True) for model in (OPUS, SONNET)],
        clients=clients,
        rates=Rates(),
        calls_per_minute={OPUS: 6000, SONNET: 6000},
        max_concurrent_calls=1,
        max_tokens=3000,
        max_throttle_retries=0,
        retry_backoff_seconds=0.001,
        record_attempt=attempts.append,
    )

    assert set(answers) == {("p0:arch-pair", OPUS), ("p0:arch-pair", SONNET)}
    assert {attempt.prompt_id for attempt in attempts} == {ARCH_PAIR_2PANEL_PROMPT_ID}
    assert all(
        request["messages"][0]["content"][1]["text"].startswith("This picture has two panels")
        for request in clients.requests
    )
    assert job_prompt_id(_job(OPUS, two_panel=True), None) == ARCH_PAIR_2PANEL_PROMPT_ID
    assert job_prompt_id(_job(OPUS, two_panel=False), None) == ARCH_PAIR_PROMPT_ID


def test_a_stored_two_panel_answer_replays_through_the_same_code() -> None:
    import json

    replayed = replay_stored_answer(
        _job(OPUS, two_panel=True), json.dumps(GOOD), reused_from="call-1", max_tokens=3000
    )

    assert replayed is not None
    answer, attempt = replayed
    assert isinstance(answer, ArchPairAnswer) and answer.pieces == (1, 2, 0)
    assert attempt.prompt_id == ARCH_PAIR_2PANEL_PROMPT_ID and attempt.reused_from == "call-1"
