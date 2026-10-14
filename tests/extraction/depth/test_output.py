"""The depth reader's prompts, strict answers and the shared route (#1265). No model, no network:
the route is driven with a fake client, as `tests/extraction/slot_reader/test_bedrock.py` does."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Any

import pytest

from extraction.depth.output import (
    SpanRoleAnswer,
    ViewKindAnswer,
    depth_number_answer,
    span_role_answer,
    view_kind_answer,
)
from extraction.depth.prompts import (
    DEPTH_PROMPT_IDS,
    DEPTH_SPAN_PROMPT_ID,
    DEPTH_SPAN_SCHEMA,
    SPAN_ROLE_PROMPT_ID,
    VIEW_KIND_PROMPT,
    VIEW_KIND_PROMPT_ID,
    VIEW_KIND_SCHEMA,
    DepthQuestion,
    depth_span_prompt,
    span_role_prompt,
    span_role_schema,
)
from extraction.depth.read import CLAUDE_PAIR, build_depth_request
from extraction.form_reader.bedrock import AttemptUsage, MalformedFormAnswer
from extraction.slot_reader.bedrock import (
    CropJob,
    job_prompt_id,
    read_crops_parallel,
    replay_stored_answer,
)
from extraction.slot_reader.claude_output import OUTPUT_CONFIG_KEY
from extraction.slot_reader.seal import Belongs
from tests.extraction.slot_reader.test_bedrock import PNG

OPUS, SONNET = CLAUDE_PAIR

ROLES = {
    "depth": "1",
    "cutout_width": "2",
    "cutout_depth": "3",
    "front_offset": "4",
    "front_offset_start": "cabinet_face",
    "hole_outline": "A",
    "why": "made up",
}


# --- prompts ------------------------------------------------------------------------------


def test_three_pinned_ids() -> None:
    assert DEPTH_PROMPT_IDS == {"view-kind-v1", "depth-span-role-v1", "claude-depth-span-v1"}


@pytest.mark.parametrize(
    "prompt",
    [
        VIEW_KIND_PROMPT,
        span_role_prompt(spans=4, outlines=2, plan=True),
        span_role_prompt(spans=1, outlines=0, plan=False),
        depth_span_prompt(direction="down_the_plan", turned=True),
        depth_span_prompt(direction="across_the_section", turned=False),
    ],
)
def test_every_question_allows_unsure_and_ignores_gvs_marks(prompt: str) -> None:
    assert "unsure" in prompt
    assert "Ignore GV's yellow, red, green and blue marks" in prompt


def test_span_role_choices_are_only_what_the_picture_shows() -> None:
    schema = span_role_schema(3, 2)
    properties = schema["properties"]
    assert isinstance(properties, dict)
    assert properties["depth"]["enum"] == ["1", "2", "3", "none", "unsure"]
    assert properties["hole_outline"]["enum"] == ["A", "B", "none", "unsure"]
    assert span_role_schema(2, 0)["properties"]["hole_outline"]["enum"] == [  # type: ignore[index]
        "none",
        "unsure",
    ]


def test_a_question_must_be_one_of_the_pinned_three() -> None:
    with pytest.raises(ValueError):
        DepthQuestion("claude-slot-span-v5", (PNG,), "q", DEPTH_SPAN_SCHEMA)
    with pytest.raises(ValueError):
        DepthQuestion(SPAN_ROLE_PROMPT_ID, (PNG, PNG), "q", span_role_schema(2, 0), spans=2)


def test_only_claude_readers_are_asked() -> None:
    question = DepthQuestion(VIEW_KIND_PROMPT_ID, (PNG,), VIEW_KIND_PROMPT, VIEW_KIND_SCHEMA)
    request = build_depth_request(question, model_id=OPUS, max_tokens=100, claude_effort="high")
    assert request[OUTPUT_CONFIG_KEY]["format"]["schema"] == VIEW_KIND_SCHEMA
    with pytest.raises(ValueError):
        build_depth_request(question, model_id="qwen.qwen3", max_tokens=100, claude_effort="high")


# --- strict answers -----------------------------------------------------------------------


def test_view_kind_unsure_is_its_own_answer() -> None:
    assert view_kind_answer({"kind": "plan", "why": ""}, model_id=OPUS).kind == "plan"
    assert view_kind_answer({"kind": "unsure", "why": ""}, model_id=OPUS).kind is None
    for bad in ({"kind": "top", "why": ""}, {"kind": "plan"}, {"kind": "plan", "why": "", "x": 1}):
        with pytest.raises(MalformedFormAnswer):
            view_kind_answer(bad, model_id=OPUS)


def test_span_roles_are_checked_against_the_picture() -> None:
    answer = span_role_answer(ROLES, model_id=OPUS, spans=4, outlines=1)
    assert (answer.depth, answer.front_offset, answer.hole_outline) == (1, 4, "A")
    malformed = [
        {**ROLES, "depth": "5"},  # not drawn
        {**ROLES, "depth": "2"},  # one number for two things
        {**ROLES, "hole_outline": "B"},  # not lettered
        {**ROLES, "front_offset": "none"},  # an edge with no strip
        {**ROLES, "front_offset_start": "none"},  # a strip with no edge
        {**ROLES, "depth": "01"},
    ]
    for bad in malformed:
        with pytest.raises(MalformedFormAnswer):
            span_role_answer(bad, model_id=OPUS, spans=4, outlines=1)


def test_a_number_copied_for_a_dimension_the_reader_says_it_lacks_is_malformed() -> None:
    good = {
        "belongs": "yes",
        "text": '24"',
        "stacked": False,
        "combined": False,
        "readable": True,
        "no_dimension": False,
    }
    assert depth_number_answer(good, model_id=OPUS).belongs is Belongs.YES
    with pytest.raises(MalformedFormAnswer):
        depth_number_answer({**good, "belongs": "unsure"}, model_id=OPUS)
    with pytest.raises(MalformedFormAnswer):
        depth_number_answer({**good, "stacked": "no"}, model_id=OPUS)


# --- the shared route -----------------------------------------------------------------------


class FakeClients:
    def __init__(self, answer: Callable[[dict[str, Any]], str]) -> None:
        self.answer = answer
        self.requests: list[dict[str, Any]] = []
        self.lock = threading.Lock()

    def for_current_thread(self) -> FakeClients:
        return self

    def converse(self, **request: Any) -> Mapping[str, Any]:
        with self.lock:
            self.requests.append(request)
        return {
            "output": {
                "message": {"role": "assistant", "content": [{"text": self.answer(request)}]}
            },
            "stopReason": "end_turn",
            "usage": {"inputTokens": 10, "outputTokens": 5},
        }


class Rates:
    def rate_for(self, model_id: str) -> object:
        return type(
            "Rate",
            (),
            {"input_per_1k_tokens": Decimal("0.004"), "output_per_1k_tokens": Decimal("0.02")},
        )()


def _route(clients: FakeClients, jobs: list[CropJob]) -> tuple[dict[Any, Any], list[AttemptUsage]]:
    attempts: list[AttemptUsage] = []
    answers = read_crops_parallel(
        jobs,
        clients=clients,
        rates=Rates(),
        calls_per_minute={OPUS: 6000, SONNET: 6000},
        max_concurrent_calls=2,
        max_tokens=400,
        max_throttle_retries=0,
        retry_backoff_seconds=0.001,
        record_attempt=attempts.append,
    )
    return answers, attempts


def test_a_depth_question_goes_through_the_shared_route_under_its_pinned_id() -> None:
    question = DepthQuestion(VIEW_KIND_PROMPT_ID, (PNG,), VIEW_KIND_PROMPT, VIEW_KIND_SCHEMA)
    jobs = [CropJob("v1:kind", model, 0, PNG, depth=question) for model in CLAUDE_PAIR]
    assert {job_prompt_id(job, None) for job in jobs} == {VIEW_KIND_PROMPT_ID}
    clients = FakeClients(lambda _request: json.dumps({"kind": "plan", "why": "a sink seen"}))
    answers, attempts = _route(clients, jobs)
    assert answers[("v1:kind", OPUS)] == ViewKindAnswer(OPUS, "plan", "a sink seen")
    assert {attempt.prompt_id for attempt in attempts} == {VIEW_KIND_PROMPT_ID}
    assert all(not attempt.malformed for attempt in attempts)


def test_malformed_twice_abstains_and_both_attempts_are_recorded() -> None:
    schema = span_role_schema(2, 0)
    question = DepthQuestion(
        SPAN_ROLE_PROMPT_ID, (PNG,), span_role_prompt(spans=2, outlines=0, plan=True), schema, 2
    )
    bad = {**ROLES, "depth": "1", "cutout_width": "1", "cutout_depth": "none"}
    bad.update(front_offset="none", front_offset_start="none", hole_outline="none")
    clients = FakeClients(lambda _request: json.dumps(bad))
    answers, attempts = _route(clients, [CropJob("v1:roles", OPUS, 0, PNG, depth=question)])
    assert answers[("v1:roles", OPUS)] is None
    assert [attempt.malformed for attempt in attempts] == [True, True]


def test_a_stored_answer_is_replayed_through_the_same_reading_code() -> None:
    schema = span_role_schema(2, 0)
    question = DepthQuestion(
        SPAN_ROLE_PROMPT_ID, (PNG,), span_role_prompt(spans=2, outlines=0, plan=True), schema, 2
    )
    raw = json.dumps(
        {
            **ROLES,
            "depth": "2",
            "cutout_width": "none",
            "cutout_depth": "none",
            "front_offset": "none",
            "front_offset_start": "none",
            "hole_outline": "none",
        }
    )
    job = CropJob("v1:roles", SONNET, 0, PNG, depth=question)
    replayed = replay_stored_answer(job, raw, reused_from="call-1", max_tokens=400)
    assert replayed is not None
    answer, attempt = replayed
    assert isinstance(answer, SpanRoleAnswer) and answer.depth == 2
    assert attempt.reused_from == "call-1" and attempt.prompt_id == SPAN_ROLE_PROMPT_ID
    assert replay_stored_answer(job, "{}", reused_from="call-1", max_tokens=400) is None


def test_a_number_question_shows_both_turns_for_a_sideways_label() -> None:
    question = DepthQuestion(
        DEPTH_SPAN_PROMPT_ID,
        (PNG, PNG, PNG, PNG),
        depth_span_prompt(direction="down_the_plan", turned=True),
        DEPTH_SPAN_SCHEMA,
    )
    request = build_depth_request(question, model_id=SONNET, max_tokens=100, claude_effort="high")
    content = request["messages"][0]["content"]
    assert sum("image" in part for part in content) == 4
    assert "Picture 4" in content[-1]["text"]


# --- review #1274: code's marks never in GV's colours; a changed question never replays -------


def _hue(colour: tuple[int, int, int]) -> float:
    import colorsys

    hue, _light, _saturation = colorsys.rgb_to_hls(*(channel / 255 for channel in colour))
    return hue * 360


def test_code_never_marks_in_a_colour_the_readers_are_told_to_ignore() -> None:
    from extraction.depth.pictures import SPAN_COLOURS
    from extraction.depth.prompts import GV_MARK_COLOURS

    names = {name for name, _colour in SPAN_COLOURS}
    assert not names & set(GV_MARK_COLOURS)
    assert names == {"magenta", "teal", "violet"}
    gv_hues = {"red": 0.0, "yellow": 60.0, "green": 120.0, "blue": 240.0}
    for name, colour in SPAN_COLOURS:
        for gv, hue in gv_hues.items():
            distance = abs(_hue(colour) - hue) % 360
            assert min(distance, 360 - distance) >= 40, (name, gv)
    for prompt in (
        VIEW_KIND_PROMPT,
        span_role_prompt(spans=2, outlines=1, plan=True),
        depth_span_prompt(direction="down_the_plan", turned=True),
    ):
        lowered = prompt.lower()
        for word in ("purple", "orange", "brown", "olive"):
            assert word not in lowered, word
        for name in names:
            # Code's own colours are named only as code's marks, never as ones to ignore.
            ignore = lowered.split("ignore", 1)[1].split(".", 1)[0]
            assert name not in ignore


def test_a_changed_wording_or_answer_shape_never_replays_an_old_answer() -> None:
    from extraction.depth.read import PreparedPage, question_packet
    from workflow.reader_reuse import question_identity

    page = PreparedPage(0, "c" * 64, "doc", None, lambda box: (0, 0, 0, 0), (), None)  # type: ignore[arg-type]
    base = DepthQuestion(VIEW_KIND_PROMPT_ID, (PNG,), VIEW_KIND_PROMPT, VIEW_KIND_SCHEMA)
    reworded = DepthQuestion(VIEW_KIND_PROMPT_ID, (PNG,), VIEW_KIND_PROMPT + " ", VIEW_KIND_SCHEMA)
    reshaped = DepthQuestion(
        VIEW_KIND_PROMPT_ID,
        (PNG,),
        VIEW_KIND_PROMPT,
        {**VIEW_KIND_SCHEMA, "required": ["kind"]},
    )
    identities = {
        question_identity(question_packet(page, "k", question, "high"))
        for question in (base, reworded, reshaped)
    }
    assert len(identities) == 3 and None not in identities
