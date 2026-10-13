"""The Gemini reader pair through OpenRouter's chat-completions API (#1211).

Gemini 3.8 Flash and Gemini 3.1 Pro are asked the slot reader's own questions, with the same
pictures and the same answer schemas, on Google Vertex with no data kept; every reply rule and
guard of the Claude route applies. Synthetic pictures and replies only: nothing here reaches the
network and no model is called.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable, Mapping
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import patch
from urllib.error import HTTPError

import pytest

from evidence.crop import encode_png
from extraction.form_reader.bedrock import AttemptUsage
from extraction.form_reader.runner import _is_throttle
from extraction.slot_reader.anthropic import (
    AnthropicRequestError,
    BatchSpendGuard,
    ProviderAccountRefused,
    ProviderInFlightLimited,
    SpendCapExceeded,
    SpendLimitedClient,
    input_token_upper_bound,
)
from extraction.slot_reader.bedrock import (
    ArchMatchAnswer,
    ArchPairAnswer,
    CounterBreakAnswer,
    CropJob,
    RowChoiceAnswer,
    read_crops_parallel,
)
from extraction.slot_reader.claude_output import (
    COUNTER_BREAK_SCHEMA,
    CROP_SCHEMA,
    ROW_CHOICE_SCHEMA,
    SPAN_SCHEMA,
    WALL_SCHEMA,
    PictureWouldBeResized,
    arch_match_schema,
    arch_pair_schema,
    output_config,
    picture_fits,
)
from extraction.slot_reader.gemini import (
    GEMINI_EFFORTS,
    GEMINI_MODELS,
    OPENROUTER_CHAT_URL,
    OpenRouterGeminiClient,
    ThreadLocalGeminiClients,
    gemini_chat_request,
)
from extraction.slot_reader.reader_pairs import GEMINI_FLASH, GEMINI_PRO
from extraction.slot_reader.seal import ReaderAnswer
from extraction.slot_reader.walls import WallAnswer

FLASH = GEMINI_FLASH
PRO = GEMINI_PRO
FULL = encode_png(4, 2, bytes(24))
CROP = encode_png(2, 2, bytes(12))
SPAN = {
    "belongs": "yes",
    "text": '17 5/8"',
    "stacked": False,
    "combined": False,
    "readable": True,
    "no_dimension": False,
}
SPAN_TEXT = json.dumps(SPAN)
REQUEST: dict[str, object] = {
    "modelId": FLASH,
    "system": [{"text": "Read only the marked span."}],
    "messages": [
        {
            "role": "user",
            "content": [
                {"image": {"format": "png", "source": {"bytes": FULL}}},
                {"image": {"format": "png", "source": {"bytes": CROP}}},
                {"text": "Copy the printed label."},
            ],
        }
    ],
    "inferenceConfig": {"maxTokens": 3000, "temperature": 0},
    "anthropicOutputConfig": output_config(SPAN_SCHEMA, "low"),
}
SHIPPED_RATES = Path(__file__).resolve().parents[3] / "deploy" / "model_rates.us-east-1.json"


class Response:
    def __init__(self, payload: object) -> None:
        self.payload = json.dumps(payload).encode()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


def chat_reply(
    text: str = SPAN_TEXT, *, model: object = "google/gemini-3.8-flash", **overrides: object
) -> dict[str, object]:
    """A chat-completions reply in OpenRouter's shape (synthetic)."""
    choice: dict[str, object] = {
        "index": 0,
        "finish_reason": overrides.pop("finish_reason", "stop"),
        "native_finish_reason": "STOP",
        "message": {
            "role": "assistant",
            "content": text,
            "refusal": overrides.pop("refusal", None),
        },
    }
    payload: dict[str, object] = {
        "id": "gen-synthetic",
        "object": "chat.completion",
        "model": model,
        "provider": "Google",
        "choices": [choice],
        "usage": {"prompt_tokens": 1200, "completion_tokens": 40, "total_tokens": 1240},
    }
    payload.update(overrides)
    return payload


def converse_with(
    payload: object, request: Mapping[str, object] = REQUEST
) -> tuple[dict[str, object], list[Any]]:
    sent: list[Any] = []

    def fake_urlopen(sent_request: object, *, timeout: float) -> Response:
        sent.append(sent_request)
        return Response(payload)

    with patch("extraction.slot_reader.anthropic.urlopen", side_effect=fake_urlopen):
        result = OpenRouterGeminiClient("private-test-key").converse(**request)
    return dict(result), sent


# --- The request: same question, same pictures, same schema ----------------------------------------


@pytest.mark.parametrize(
    ("model_id", "openrouter_id"),
    [(FLASH, "google/gemini-3.8-flash"), (PRO, "google/gemini-3.1-pro-preview")],
)
def test_the_two_gemini_models_are_asked_by_their_openrouter_names(
    model_id: str, openrouter_id: str
) -> None:
    body = gemini_chat_request(**{**REQUEST, "modelId": model_id})

    assert body["model"] == openrouter_id
    assert dict(GEMINI_MODELS) == {
        FLASH: "google/gemini-3.8-flash",
        PRO: "google/gemini-3.1-pro-preview",
    }


def test_the_request_is_the_same_question_as_chat_completions() -> None:
    body = gemini_chat_request(**REQUEST)

    assert body["max_tokens"] == 3000
    system, user = body["messages"]
    assert system == {"role": "system", "content": "Read only the marked span."}
    assert user["role"] == "user"
    assert user["content"] == [
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64," + base64.b64encode(FULL).decode()},
        },
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64," + base64.b64encode(CROP).decode()},
        },
        {"type": "text", "text": "Copy the printed label."},
    ]


@pytest.mark.parametrize(
    "schema",
    [
        SPAN_SCHEMA,
        CROP_SCHEMA,
        ROW_CHOICE_SCHEMA,
        COUNTER_BREAK_SCHEMA,
        WALL_SCHEMA,
        arch_pair_schema(3),
        arch_match_schema(2),
    ],
)
def test_the_answer_schema_is_sent_strict_and_unchanged(schema: Mapping[str, object]) -> None:
    body = gemini_chat_request(**{**REQUEST, "anthropicOutputConfig": output_config(schema, "low")})

    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "slot_reader_answer", "strict": True, "schema": dict(schema)},
    }


def test_routing_is_google_vertex_only_keeping_no_data_with_every_parameter_honoured() -> None:
    body = gemini_chat_request(**REQUEST)

    assert body["provider"] == {
        "only": ["google-vertex/global"],
        "allow_fallbacks": False,
        "require_parameters": True,
        "data_collection": "deny",
        "zdr": True,
    }


def test_no_fallback_temperature_or_unlisted_parameter_is_ever_sent() -> None:
    """`require_parameters` routes only to an endpoint that takes every parameter sent. Vertex's
    Gemini 3.8 Flash endpoint lists no `temperature` and neither lists `media_resolution`, so
    neither is sent; Gemini 3's default picture budget is the high one (1,120 tokens)."""
    body = gemini_chat_request(**REQUEST)

    assert set(body) == {
        "model",
        "messages",
        "max_tokens",
        "response_format",
        "reasoning",
        "provider",
    }


@pytest.mark.parametrize(("model_id", "effort"), [(FLASH, "low"), (PRO, "low")])
def test_the_reasoning_effort_is_stated_explicitly_per_model(model_id: str, effort: str) -> None:
    body = gemini_chat_request(**{**REQUEST, "modelId": model_id})

    assert GEMINI_EFFORTS[model_id] == effort
    assert body["reasoning"] == {"effort": effort, "exclude": True}


@pytest.mark.parametrize("effort", ["medium", "high", "xhigh", "max"])
def test_a_question_at_another_effort_is_refused_before_a_call(effort: str) -> None:
    request = {**REQUEST, "anthropicOutputConfig": output_config(SPAN_SCHEMA, effort)}
    with (
        patch("extraction.slot_reader.anthropic.urlopen") as urlopen,
        pytest.raises(ValueError, match="effort"),
    ):
        OpenRouterGeminiClient("private-test-key").converse(**request)
    urlopen.assert_not_called()


@pytest.mark.parametrize(
    "model_id",
    [
        "anthropic.claude-opus-5-5",
        "google.gemini-3-8-flash-lite",
        "google.gemini-3-1-pro",
        "qwen.qwen3-vl-235b-a22b",
    ],
)
def test_any_other_model_is_refused_before_a_call(model_id: str) -> None:
    with (
        patch("extraction.slot_reader.anthropic.urlopen") as urlopen,
        pytest.raises(ValueError, match="Gemini 3.8 Flash and Gemini 3.1 Pro"),
    ):
        OpenRouterGeminiClient("private-test-key").converse(**{**REQUEST, "modelId": model_id})
    urlopen.assert_not_called()


def test_a_request_without_its_answer_schema_is_refused_before_a_call() -> None:
    request = {key: value for key, value in REQUEST.items() if key != "anthropicOutputConfig"}
    with (
        patch("extraction.slot_reader.anthropic.urlopen") as urlopen,
        pytest.raises(ValueError, match="schema"),
    ):
        OpenRouterGeminiClient("private-test-key").converse(**request)
    urlopen.assert_not_called()


@pytest.mark.parametrize("model_id", [FLASH, PRO])
def test_an_oversized_picture_is_refused_before_anything_is_sent(model_id: str) -> None:
    """Each Gemini model has its own picture limit: the same as the Claude pair's, so both pairs
    are shown exactly the same pictures."""
    wide = encode_png(2600, 2, bytes(2600 * 2 * 3))
    assert picture_fits(2576, 1400, model_id) is True
    assert picture_fits(2600, 2, model_id) is False
    request = {
        **REQUEST,
        "modelId": model_id,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"image": {"format": "png", "source": {"bytes": wide}}},
                    {"text": "Copy the printed label."},
                ],
            }
        ],
    }
    with (
        patch("extraction.slot_reader.anthropic.urlopen") as urlopen,
        pytest.raises(PictureWouldBeResized, match="2600x2"),
    ):
        OpenRouterGeminiClient("private-test-key").converse(**request)
    urlopen.assert_not_called()


def test_the_call_goes_to_openrouter_chat_completions_with_a_bearer_key_only() -> None:
    result, sent = converse_with(chat_reply())

    assert len(sent) == 1
    request = sent[0]
    assert request.full_url == OPENROUTER_CHAT_URL
    assert OPENROUTER_CHAT_URL == "https://openrouter.ai/api/v1/chat/completions"
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == "Bearer private-test-key"
    assert request.get_header("X-api-key") is None
    body = json.loads(request.data)
    assert "models" not in body and "fallbacks" not in body and "route" not in body
    assert result["stopReason"] == "end_turn"
    assert result["output"]["message"]["content"] == [{"type": "text", "text": SPAN_TEXT}]
    assert result["usage"] == {"inputTokens": 1200, "outputTokens": 40}


def test_the_key_is_never_in_the_clients_repr() -> None:
    client = OpenRouterGeminiClient("private-test-key")
    assert "private-test-key" not in repr(client)
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        OpenRouterGeminiClient("  ")


# --- The reply ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model_id", "reported"),
    [
        (FLASH, "google/gemini-3.8-flash"),
        (FLASH, "google/gemini-3.8-flash-20260902"),
        (PRO, "google/gemini-3.1-pro-preview"),
        (PRO, "google/gemini-3.1-pro-preview-20260219"),
    ],
)
def test_a_reply_naming_the_asked_model_is_passed_on(model_id: str, reported: str) -> None:
    result, _ = converse_with(chat_reply(model=reported), {**REQUEST, "modelId": model_id})

    assert result["stopReason"] == "end_turn"
    assert result["output"]["message"]["content"] == [{"type": "text", "text": SPAN_TEXT}]


@pytest.mark.parametrize(
    ("model_id", "reported"),
    [
        (FLASH, "google/gemini-3.1-pro-preview"),
        (FLASH, "google/gemini-3.8-flash-lite"),
        (FLASH, "google/gemini-3.7-flash"),
        (FLASH, "google/gemini-3.8-flash-2026090"),
        (FLASH, "anthropic/claude-opus-5.5"),
        (FLASH, "xgoogle/gemini-3.8-flash"),
        (PRO, "google/gemini-3.8-flash"),
        (PRO, "google/gemini-3.1-pro"),
        (FLASH, ""),
        (FLASH, None),
    ],
)
def test_a_reply_from_another_model_is_never_passed_on(model_id: str, reported: object) -> None:
    result, _ = converse_with(chat_reply(model=reported), {**REQUEST, "modelId": model_id})

    assert result["stopReason"] == "model_mismatch"
    assert result["output"]["message"]["content"] == []
    assert result["usage"] == {"inputTokens": 1200, "outputTokens": 40}, "still paid for"


def test_a_reply_from_a_host_other_than_vertex_is_never_passed_on() -> None:
    result, _ = converse_with(chat_reply(provider="Google AI Studio"))

    assert result["stopReason"] == "model_mismatch"
    assert result["output"]["message"]["content"] == []


@pytest.mark.parametrize(
    ("overrides", "stop"),
    [
        ({"finish_reason": "length"}, "max_tokens"),
        ({"finish_reason": "content_filter"}, "refusal"),
        ({"refusal": "I can't help with that."}, "refusal"),
        ({"finish_reason": "error"}, "error"),
        ({"finish_reason": "tool_calls"}, "tool_calls"),
        ({"finish_reason": None}, None),
    ],
)
def test_a_refused_or_cut_off_reply_passes_on_no_text(
    overrides: dict[str, object], stop: str | None
) -> None:
    result, _ = converse_with(chat_reply(SPAN_TEXT[:20], **overrides))

    assert result["stopReason"] == stop
    assert result["output"]["message"]["content"] == []


@pytest.mark.parametrize(
    "payload",
    [
        {"model": "google/gemini-3.8-flash", "choices": [], "usage": {}},
        {"model": "google/gemini-3.8-flash", "choices": "text", "usage": {}},
        {"model": "google/gemini-3.8-flash", "choices": [{"message": {}}, {"message": {}}]},
        {"model": "google/gemini-3.8-flash", "choices": [{"finish_reason": "stop"}], "usage": {}},
        ["not", "an", "object"],
    ],
)
def test_a_malformed_reply_is_an_error_not_an_answer(payload: object) -> None:
    with pytest.raises(AnthropicRequestError, match="OpenRouter") as caught:
        converse_with(payload)

    assert caught.value.status_code == 502


# --- Errors: the same failure classes as the Claude route --------------------------------------


@pytest.mark.parametrize(
    ("status", "body", "expected_code", "throttle"),
    [
        (429, b'{"error":{"message":"rate limited"}}', "TooManyRequestsException", True),
        (
            402,
            b'{"error":{"code":402,"message":"in_flight_budget_exhausted"}}',
            "ProviderInFlightLimited",
            False,
        ),
        (402, b'{"error":{"message":"Insufficient credits"}}', "ProviderAccountRefused", False),
        (503, b"{}", "ServiceUnavailableException", True),
        (404, b'{"error":{"message":"No endpoints found"}}', "ProviderRequestRejected", False),
        (400, b"secret", "ProviderRequestRejected", False),
    ],
)
def test_provider_errors_are_sanitized_and_categorized_for_retry(
    status: int, body: bytes, expected_code: str, throttle: bool
) -> None:
    error = HTTPError(OPENROUTER_CHAT_URL, status, "private", {}, BytesIO(body))
    with (
        patch("extraction.slot_reader.anthropic.urlopen", side_effect=error),
        pytest.raises(AnthropicRequestError) as caught,
    ):
        OpenRouterGeminiClient("private-test-key").converse(**REQUEST)

    assert "OpenRouter" in str(caught.value)
    assert caught.value.status_code == status
    assert caught.value.response == {"Error": {"Code": expected_code}}
    assert _is_throttle(caught.value) is throttle
    for secret in ("private-test-key", "secret", "Insufficient", "in_flight", "endpoints"):
        assert secret not in str(caught.value)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ({"code": 402, "message": "Insufficient credits"}, ProviderAccountRefused),
        ({"code": 402, "message": "in_flight_budget_exhausted"}, ProviderInFlightLimited),
        ({"code": 429, "message": "rate limited"}, AnthropicRequestError),
        ({"code": 502, "message": "upstream failed"}, AnthropicRequestError),
        ({"message": "no code"}, AnthropicRequestError),
    ],
)
def test_an_error_inside_a_200_reply_is_classified_like_the_status_it_names(
    error: dict[str, object], expected: type[AnthropicRequestError]
) -> None:
    with pytest.raises(expected) as caught:
        converse_with({"error": error})

    assert "Insufficient" not in str(caught.value) and "upstream" not in str(caught.value)
    code = error.get("code")
    assert caught.value.status_code == (code if isinstance(code, int) else 502)


def test_a_timeout_is_a_retryable_transport_error() -> None:
    with (
        patch("extraction.slot_reader.anthropic.urlopen", side_effect=TimeoutError()),
        pytest.raises(AnthropicRequestError) as caught,
    ):
        OpenRouterGeminiClient("private-test-key").converse(**REQUEST)

    assert caught.value.status_code == 503
    assert caught.value.response == {"Error": {"Code": "ProviderTransportError"}}
    assert _is_throttle(caught.value) is True


# --- Every answer type parses from a Gemini reply -------------------------------------------------

ANSWERS: dict[str, dict[str, object]] = {
    "span": SPAN,
    "row": {"row": 1, "kind": "row", "also": [], "why": "box 1 follows the countertop"},
    "counter_break": {
        "contains_tall_appliance": False,
        "stone_ends": "to_walls",
        "why": "no tall unit in the span",
    },
    "walls": {
        "left": "yes",
        "right": "no",
        "behind": "unsure",
        "left_evidence": "hatching at the left end",
        "right_evidence": "",
        "behind_evidence": "",
        "view": "elevation",
    },
    "arch_pair": {
        "architect": [{"a": 1, "measures": "countertop"}],
        "overall": "A1",
        "pieces": ["none", "unsure"],
        "why": "A1 spans the same run",
    },
    "arch_match": {
        "candidates": [{"n": 1, "same": "yes"}, {"n": 2, "same": "no"}],
        "pick": "1",
        "why": "the same sink bay",
    },
}


def _answer_for(body: Mapping[str, Any]) -> dict[str, object]:
    properties = set(body["response_format"]["json_schema"]["schema"]["properties"])
    for answer in ANSWERS.values():
        if set(answer) == properties:
            return answer
    raise AssertionError(f"no synthetic answer for {sorted(properties)}")


def _gemini_http(
    *, refuse_first: int = 0, error: Callable[[], HTTPError] | None = None
) -> tuple[Callable[..., Response], list[dict[str, Any]]]:
    """A fake OpenRouter: answers each chat request with the synthetic answer its schema asks
    for, naming the model it was asked; the first `refuse_first` calls raise `error()`."""
    bodies: list[dict[str, Any]] = []

    def fake_urlopen(request: Any, *, timeout: float) -> Response:
        body = json.loads(request.data)
        bodies.append(body)
        if len(bodies) <= refuse_first and error is not None:
            raise error()
        return Response(chat_reply(json.dumps(_answer_for(body)), model=body["model"]))

    return fake_urlopen, bodies


def _jobs(model_id: str) -> list[CropJob]:
    png = encode_png(2, 2, bytes(12))
    return [
        CropJob("span", model_id, 0, png, png, grounded_claude=True),
        CropJob("row", model_id, 0, png, row_question=True, candidate_count=2),
        CropJob("counter", model_id, 0, png, png, counter_break_question=True),
        CropJob("walls", model_id, 0, png, png, wall_question=True),
        CropJob(
            "pair", model_id, 0, png, arch_pair_question=True, vendor_pieces=2, architect_spans=1
        ),
        CropJob("match", model_id, 0, png, arch_match_question=True, architect_candidates=2),
    ]


def _read(
    jobs: list[CropJob],
    *,
    guard: BatchSpendGuard | None = None,
    rates: object | None = None,
) -> tuple[dict[tuple[str, str], object], list[AttemptUsage]]:
    attempts: list[AttemptUsage] = []
    priced = rates or SimpleNamespace(
        rate_for=lambda _model: SimpleNamespace(
            input_per_1k_tokens=Decimal("0.00075"), output_per_1k_tokens=Decimal("0.00375")
        )
    )
    answers = read_crops_parallel(
        jobs,
        clients=ThreadLocalGeminiClients("private-test-key", timeout_seconds=30),
        rates=priced,  # type: ignore[arg-type]
        calls_per_minute={FLASH: 60_000, PRO: 60_000},
        max_concurrent_calls=1,
        max_tokens=3000,
        max_throttle_retries=0,
        retry_backoff_seconds=0.001,
        record_attempt=attempts.append,
        spend_guard=guard,
        claude_effort="low",
    )
    return answers, attempts


@pytest.mark.parametrize("model_id", [FLASH, PRO])
def test_every_answer_type_parses_from_a_gemini_reply(model_id: str) -> None:
    fake, bodies = _gemini_http()
    with patch("extraction.slot_reader.anthropic.urlopen", side_effect=fake):
        answers, attempts = _read(_jobs(model_id))

    assert len(bodies) == 6
    assert all(body["reasoning"] == {"effort": "low", "exclude": True} for body in bodies)
    span = answers[("span", model_id)]
    assert isinstance(span, ReaderAnswer) and span.text == '17 5/8"' and span.model_id == model_id
    row = answers[("row", model_id)]
    assert isinstance(row, RowChoiceAnswer) and (row.row, row.kind) == (1, "row")
    counter = answers[("counter", model_id)]
    assert isinstance(counter, CounterBreakAnswer) and counter.stone_ends == "to_walls"
    walls = answers[("walls", model_id)]
    assert isinstance(walls, WallAnswer) and walls.view == "elevation"
    pair = answers[("pair", model_id)]
    assert isinstance(pair, ArchPairAnswer) and (pair.overall, pair.pieces) == (1, (0, 0))
    assert pair.unsure == ("V2",)
    match = answers[("match", model_id)]
    assert isinstance(match, ArchMatchAnswer) and match.pick == 1
    assert all(attempt.model_id == model_id for attempt in attempts)
    assert all(not attempt.malformed and attempt.failure_kind is None for attempt in attempts)
    assert all(attempt.raw_response_text for attempt in attempts), "every reply kept privately"


def test_a_reply_off_its_schema_is_malformed_reasked_once_then_abstains() -> None:
    """The provider's own schema enforcement is not trusted: the reply is checked strictly here,
    as a Claude reply is, and a miss is never a value."""
    loose = {**SPAN, "confidence": 0.9}

    def fake_urlopen(request: Any, *, timeout: float) -> Response:
        return Response(chat_reply(json.dumps(loose)))

    with patch("extraction.slot_reader.anthropic.urlopen", side_effect=fake_urlopen):
        answers, attempts = _read(_jobs(FLASH)[:1])

    assert answers == {("span", FLASH): None}
    assert [attempt.malformed for attempt in attempts] == [True, True]


def test_a_model_mismatch_abstains_and_is_never_a_reading() -> None:
    def fake_urlopen(request: Any, *, timeout: float) -> Response:
        return Response(chat_reply(SPAN_TEXT, model="google/gemini-3.1-pro-preview"))

    with patch("extraction.slot_reader.anthropic.urlopen", side_effect=fake_urlopen):
        answers, attempts = _read(_jobs(FLASH)[:1])

    assert answers == {("span", FLASH): None}
    assert len(attempts) == 2 and all(attempt.malformed for attempt in attempts)


# --- Cost, the spend guard and the in-flight retry ------------------------------------------------


def test_the_shipped_price_file_prices_both_gemini_models_at_vertex_global() -> None:
    from app.runs.rates import call_cost_micros, load_model_rates

    shipped = load_model_rates(SHIPPED_RATES)
    flash = shipped.rate_for(FLASH)
    pro = shipped.rate_for(PRO)
    assert flash is not None and pro is not None
    assert (flash.input_per_1k_tokens, flash.output_per_1k_tokens) == (
        Decimal("0.00075"),
        Decimal("0.00375"),
    )
    assert (pro.input_per_1k_tokens, pro.output_per_1k_tokens) == (
        Decimal("0.002"),
        Decimal("0.012"),
    )
    # 1,200 in and 40 out on Flash: 900 + 150 = 1,050 millionths of a dollar.
    assert call_cost_micros(shipped, FLASH, 1200, 40) == 1050
    # The same on Pro: 2,400 + 480 = 2,880.
    assert call_cost_micros(shipped, PRO, 1200, 40) == 2880


def test_the_recorded_tokens_are_the_ones_the_reply_reports() -> None:
    fake, _ = _gemini_http()
    with patch("extraction.slot_reader.anthropic.urlopen", side_effect=fake):
        _answers, attempts = _read(_jobs(FLASH)[:1])

    (attempt,) = attempts
    assert (attempt.input_tokens, attempt.output_tokens) == (1200, 40)


def test_the_spend_guard_reserves_before_each_gemini_call_and_settles_at_its_rate() -> None:
    from app.runs.rates import load_model_rates

    rates = load_model_rates(SHIPPED_RATES)
    guard = BatchSpendGuard(Decimal("1.00"), rates)
    fake, _ = _gemini_http()
    with patch("extraction.slot_reader.anthropic.urlopen", side_effect=fake):
        client = SpendLimitedClient(OpenRouterGeminiClient("private-test-key"), guard)
        client.converse(**REQUEST)

    # Settled at the real use: 1,200 in and 40 out on Flash.
    assert guard._reserved_usd == Decimal("0.00105")


def test_a_gemini_picture_is_reserved_at_no_less_than_its_fixed_token_budget() -> None:
    """Gemini 3 bills a picture at a fixed budget (1,120 tokens at the high resolution, its
    default), however small: the reservation's upper bound never counts it at less."""
    flash = input_token_upper_bound(REQUEST)
    claude = input_token_upper_bound({**REQUEST, "modelId": "anthropic.claude-opus-5-5"})

    assert flash - claude == 2 * 1120 - 2  # two pictures, each one 28-px tile for Claude


def test_the_spend_cap_refuses_a_gemini_call_before_it_is_sent() -> None:
    from app.runs.rates import load_model_rates

    guard = BatchSpendGuard(Decimal("0.001"), load_model_rates(SHIPPED_RATES))
    with (
        patch("extraction.slot_reader.anthropic.urlopen") as urlopen,
        pytest.raises(SpendCapExceeded),
    ):
        SpendLimitedClient(OpenRouterGeminiClient("private-test-key"), guard).converse(**REQUEST)
    urlopen.assert_not_called()


def _in_flight_402() -> HTTPError:
    body = b'{"error":{"code":402,"message":"in_flight_budget_exhausted"}}'
    return HTTPError(OPENROUTER_CHAT_URL, 402, "private", {"Retry-After": "0"}, BytesIO(body))


def test_an_in_flight_402_is_retried_one_call_at_a_time_and_then_answers() -> None:
    fake, bodies = _gemini_http(refuse_first=2, error=_in_flight_402)
    with patch("extraction.slot_reader.anthropic.urlopen", side_effect=fake):
        answers, attempts = _read(_jobs(FLASH)[:2])

    assert all(answer is not None for answer in answers.values())
    assert len(bodies) == 2 + 2
    assert [attempt.failure_kind for attempt in attempts].count("ProviderInFlightLimited") == 2


def test_out_of_credit_is_raised_once_and_never_retried() -> None:
    def no_credit() -> HTTPError:
        body = b'{"error":{"code":402,"message":"Insufficient credits"}}'
        return HTTPError(OPENROUTER_CHAT_URL, 402, "private", {}, BytesIO(body))

    fake, bodies = _gemini_http(refuse_first=99, error=no_credit)
    with (
        patch("extraction.slot_reader.anthropic.urlopen", side_effect=fake),
        pytest.raises(ProviderAccountRefused),
    ):
        _read(_jobs(FLASH)[:2])

    assert len(bodies) == 1
