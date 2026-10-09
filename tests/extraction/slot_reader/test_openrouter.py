"""Claude through OpenRouter (#1094): the same request and reply rules as Anthropic's own API.

Synthetic pictures and replies only; nothing here reaches the network.
"""

from __future__ import annotations

import base64
import json
import threading
from decimal import Decimal
from http.client import IncompleteRead
from io import BytesIO
from typing import Self
from unittest.mock import patch
from urllib.error import HTTPError

import pytest

from evidence.crop import encode_png
from extraction.form_reader.runner import _is_throttle
from extraction.slot_reader.anthropic import (
    AnthropicRequestError,
    BatchSpendGuard,
    SpendCapExceeded,
    SpendLimitedClient,
    anthropic_messages_request,
)
from extraction.slot_reader.claude_output import SPAN_SCHEMA, PictureWouldBeResized, output_config
from extraction.slot_reader.openrouter import (
    OPENROUTER_MESSAGES_URL,
    OpenRouterMessagesClient,
    ThreadLocalOpenRouterClients,
    openrouter_messages_request,
)

OPUS = "anthropic.claude-opus-5-5"
SONNET = "anthropic.claude-sonnet-5-5"
FULL = encode_png(4, 2, bytes(24))
CROP = encode_png(2, 2, bytes(12))
REQUEST: dict[str, object] = {
    "modelId": OPUS,
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
    "inferenceConfig": {"maxTokens": 3000},
    "anthropicOutputConfig": output_config(SPAN_SCHEMA, "high"),
}
SPAN_TEXT = json.dumps({"belongs": True, "text": '13 1/8"', "readable": True})


class Response:
    def __init__(self, payload: object) -> None:
        self.payload = json.dumps(payload).encode()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


def provider_reply(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "model": "anthropic/claude-opus-5.5",
        "provider": "Google",
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": SPAN_TEXT}],
        "usage": {"input_tokens": 45, "output_tokens": 12, "cost": 0.0004},
    }
    payload.update(overrides)
    return payload


def converse_with(
    payload: object, request: dict[str, object] = REQUEST
) -> tuple[dict[str, object], list[object]]:
    sent: list[object] = []

    def fake_urlopen(sent_request: object, *, timeout: float) -> Response:
        sent.append(sent_request)
        return Response(payload)

    with patch("extraction.slot_reader.anthropic.urlopen", side_effect=fake_urlopen):
        result = OpenRouterMessagesClient("private-test-key").converse(**request)
    return dict(result), sent


# --- The request ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model_id", "openrouter_id"),
    [(OPUS, "anthropic/claude-opus-5.5"), (SONNET, "anthropic/claude-sonnet-5.5")],
)
def test_the_same_two_models_are_asked_by_their_openrouter_names(
    model_id: str, openrouter_id: str
) -> None:
    body = openrouter_messages_request(**{**REQUEST, "modelId": model_id})

    assert body["model"] == openrouter_id


@pytest.mark.parametrize(
    "model_id", ["anthropic.claude-haiku-5-5", "anthropic.claude-opus-5", "qwen.qwen3-vl-235b-a22b"]
)
def test_any_other_model_is_refused_before_a_call(model_id: str) -> None:
    with (
        patch("extraction.slot_reader.anthropic.urlopen") as urlopen,
        pytest.raises(ValueError, match="Opus 5.5 and Sonnet 5.5"),
    ):
        OpenRouterMessagesClient("private-test-key").converse(**{**REQUEST, "modelId": model_id})
    urlopen.assert_not_called()


def test_the_question_itself_is_the_one_anthropics_api_is_asked() -> None:
    """Same pictures, prompt, token bound, answer schema and effort: only the route differs."""
    body = openrouter_messages_request(**REQUEST)
    direct = anthropic_messages_request(**REQUEST)

    assert body["max_tokens"] == direct["max_tokens"] == 3000
    assert body["system"] == direct["system"]
    assert (
        body["output_config"]
        == direct["output_config"]
        == {
            "format": {"type": "json_schema", "schema": SPAN_SCHEMA},
            "effort": "high",
        }
    )
    content = body["messages"][0]["content"]
    assert [block["type"] for block in content] == ["image", "image", "text"]
    assert content[0]["source"] == {
        "type": "base64",
        "media_type": "image/png",
        "data": base64.b64encode(FULL).decode("ascii"),
    }
    assert content[1]["source"]["data"] == base64.b64encode(CROP).decode("ascii")
    assert content[2] == {"type": "text", "text": "Copy the printed label."}


def test_pictures_carry_no_transformations_field_which_openrouter_does_not_take() -> None:
    body = openrouter_messages_request(**REQUEST)
    images = [block for block in body["messages"][0]["content"] if block["type"] == "image"]

    assert len(images) == 2
    assert all("transformations" not in block for block in images)


def test_an_oversized_picture_is_still_refused_before_anything_is_sent() -> None:
    wide = encode_png(2600, 2, bytes(2600 * 2 * 3))
    request = {
        **REQUEST,
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
        OpenRouterMessagesClient("private-test-key").converse(**request)
    urlopen.assert_not_called()


def test_routing_is_google_vertex_only_keeping_no_data_with_every_parameter_honoured() -> None:
    body = openrouter_messages_request(**REQUEST)

    assert body["provider"] == {
        "only": ["google-vertex/global"],
        "allow_fallbacks": False,
        "require_parameters": True,
        "data_collection": "deny",
        "zdr": True,
    }


def test_no_model_fallback_is_ever_requested() -> None:
    _, sent = converse_with(provider_reply())
    body = json.loads(sent[0].data)

    assert "models" not in body
    assert "fallbacks" not in body
    assert "route" not in body
    assert sent[0].get_header("Anthropic-beta") is None


def test_the_call_goes_to_openrouter_with_a_bearer_key_and_nothing_else_secret() -> None:
    result, sent = converse_with(provider_reply())

    assert len(sent) == 1
    request = sent[0]
    assert request.full_url == OPENROUTER_MESSAGES_URL == "https://openrouter.ai/api/v1/messages"
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == "Bearer private-test-key"
    assert request.get_header("X-api-key") is None
    assert request.get_header("Content-type") == "application/json"
    assert result["stopReason"] == "end_turn"
    assert result["output"]["message"]["content"] == [{"type": "text", "text": SPAN_TEXT}]
    assert result["usage"] == {"inputTokens": 45, "outputTokens": 12}


# --- The reply ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reported",
    [
        "anthropic/claude-opus-5.5",
        "anthropic/claude-opus-5.5-20260921",
        "claude-opus-5-5",
        "claude-opus-5-5-20260921",
        "claude-opus-5-5@20260921",
    ],
)
def test_a_reply_naming_the_asked_model_is_passed_on(reported: str) -> None:
    result, _ = converse_with(provider_reply(model=reported))

    assert result["stopReason"] == "end_turn"
    assert result["output"]["message"]["content"] == [{"type": "text", "text": SPAN_TEXT}]


@pytest.mark.parametrize(
    "reported",
    [
        "anthropic/claude-sonnet-5.5",
        "claude-sonnet-5-5-20260928",
        "anthropic/claude-opus-5",
        "openai/gpt-6-sol",
        # A variant of the asked model is not the asked model.
        "anthropic/claude-opus-5.5-fast",
        "claude-opus-5-5-mini",
        "anthropic/claude-opus-5.5-2026092",
        "anthropic/claude-opus-5.5-20260921x",
        "xanthropic/claude-opus-5.5",
        "",
        None,
    ],
)
def test_a_reply_from_another_model_is_never_passed_on_as_the_asked_models(
    reported: object,
) -> None:
    result, _ = converse_with(provider_reply(model=reported))

    assert result["stopReason"] == "model_mismatch"
    assert result["output"]["message"]["content"] == []
    assert result["usage"] == {"inputTokens": 45, "outputTokens": 12}


def test_sonnet_is_checked_against_sonnet() -> None:
    result, _ = converse_with(
        provider_reply(model="anthropic/claude-sonnet-5.5-20260928"),
        {**REQUEST, "modelId": SONNET},
    )
    assert result["stopReason"] == "end_turn"

    result, _ = converse_with(
        provider_reply(model="anthropic/claude-opus-5.5"), {**REQUEST, "modelId": SONNET}
    )
    assert result["stopReason"] == "model_mismatch"


@pytest.mark.parametrize("stop", ["refusal", "max_tokens", "pause_turn", None])
def test_a_refused_or_cut_off_reply_passes_on_no_text(stop: str | None) -> None:
    result, _ = converse_with(
        provider_reply(stop_reason=stop, content=[{"type": "text", "text": SPAN_TEXT[:20]}])
    )

    assert result["stopReason"] == stop
    assert result["output"]["message"]["content"] == []


def test_thinking_blocks_are_never_passed_on() -> None:
    result, _ = converse_with(
        provider_reply(
            content=[
                {"type": "thinking", "thinking": "private reasoning", "signature": "x"},
                {"type": "text", "text": SPAN_TEXT},
            ]
        )
    )

    assert result["output"]["message"]["content"] == [{"type": "text", "text": SPAN_TEXT}]


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "error", "error": {"type": "api_error", "message": "x"}},
        {"model": "anthropic/claude-opus-5.5", "content": "text", "usage": {}},
        ["not", "an", "object"],
    ],
)
def test_a_malformed_reply_is_an_error_not_an_answer(payload: object) -> None:
    with pytest.raises(AnthropicRequestError, match="OpenRouter") as caught:
        converse_with(payload)

    assert caught.value.status_code == 502


# --- Errors ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "body", "expected_code", "throttle"),
    [
        (429, b'{"error":{"type":"rate_limit_error"}}', "TooManyRequestsException", True),
        # A new or low-balance account's "in-flight budget" (decision log 2026-10-06): retried.
        (
            402,
            b'{"error":{"type":"billing_error","message":"in_flight_budget_exhausted"}}',
            "TooManyRequestsException",
            True,
        ),
        # No credit: refused without a retry, so the run stops with this error, as any other
        # refused request does.
        (
            402,
            b'{"error":{"type":"billing_error","message":"Insufficient credits"}}',
            "ProviderRequestRejected",
            False,
        ),
        (529, b"{}", "ModelOverloadedException", True),
        (503, b"{}", "ServiceUnavailableException", True),
        (404, b'{"error":{"message":"No endpoints found"}}', "ProviderRequestRejected", False),
        (400, b"secret", "ProviderRequestRejected", False),
    ],
)
def test_provider_errors_are_sanitized_and_categorized_for_retry(
    status: int, body: bytes, expected_code: str, throttle: bool
) -> None:
    error = HTTPError(OPENROUTER_MESSAGES_URL, status, "private", {}, BytesIO(body))
    with (
        patch("extraction.slot_reader.anthropic.urlopen", side_effect=error),
        pytest.raises(AnthropicRequestError) as caught,
    ):
        OpenRouterMessagesClient("private-test-key").converse(**REQUEST)

    assert "OpenRouter" in str(caught.value)
    assert caught.value.status_code == status
    assert caught.value.response == {"Error": {"Code": expected_code}}
    assert _is_throttle(caught.value) is throttle
    for secret in ("private-test-key", "secret", "Insufficient", "in_flight", "endpoints"):
        assert secret not in repr(caught.value)
        assert secret not in str(caught.value)


def test_an_error_reply_cut_off_mid_read_never_leaks_its_words() -> None:
    """A 402 whose body ends early raises `IncompleteRead` carrying the bytes it got: they must
    not reach the error, and the reply is still closed and classified."""

    class CutOff(BytesIO):
        closed_by_us = False

        def read(self, *_args: object) -> bytes:
            raise IncompleteRead(b"secret words from the provider", 100)

        def close(self) -> None:
            CutOff.closed_by_us = True
            super().close()

    error = HTTPError(OPENROUTER_MESSAGES_URL, 402, "private", {}, CutOff())
    with (
        patch("extraction.slot_reader.anthropic.urlopen", side_effect=error),
        pytest.raises(AnthropicRequestError) as caught,
    ):
        OpenRouterMessagesClient("private-test-key").converse(**REQUEST)

    assert caught.value.response == {"Error": {"Code": "ProviderRequestRejected"}}
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True
    assert "secret" not in repr(caught.value)
    assert "secret" not in str(caught.value)
    assert CutOff.closed_by_us is True


def test_a_lost_connection_is_a_retryable_transport_error() -> None:
    with (
        patch("extraction.slot_reader.anthropic.urlopen", side_effect=TimeoutError()),
        pytest.raises(AnthropicRequestError) as caught,
    ):
        OpenRouterMessagesClient("private-test-key").converse(**REQUEST)

    assert caught.value.response == {"Error": {"Code": "ProviderTransportError"}}
    assert _is_throttle(caught.value) is True


# --- The client -----------------------------------------------------------------------------------


def test_the_key_is_never_part_of_the_client_repr() -> None:
    assert "private-test-key" not in repr(OpenRouterMessagesClient("private-test-key"))
    assert "private-test-key" not in repr(ThreadLocalOpenRouterClients("private-test-key"))


@pytest.mark.parametrize("key", ["", "   "])
def test_an_empty_key_is_refused(key: str) -> None:
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        OpenRouterMessagesClient(key)


def test_thread_local_provider_keeps_clients_separate_by_thread() -> None:
    provider = ThreadLocalOpenRouterClients("private-test-key", timeout_seconds=90)
    first = provider.for_current_thread()
    seen: list[object] = []
    thread = threading.Thread(target=lambda: seen.append(provider.for_current_thread()))
    thread.start()
    thread.join()

    assert isinstance(first, OpenRouterMessagesClient)
    assert provider.for_current_thread() is first
    assert seen and seen[0] is not first


class Rates:
    def rate_for(self, _model_id: str) -> object:
        return type(
            "Rate",
            (),
            {
                "input_per_1k_tokens": Decimal("0.004"),
                "output_per_1k_tokens": Decimal("0.020"),
            },
        )()


def test_the_spend_cap_refuses_before_an_openrouter_call_it_cannot_afford() -> None:
    guard = BatchSpendGuard(Decimal("0.01"), Rates())
    with (
        patch("extraction.slot_reader.anthropic.urlopen") as urlopen,
        pytest.raises(SpendCapExceeded, match="would be exceeded"),
    ):
        SpendLimitedClient(OpenRouterMessagesClient("private-test-key"), guard).converse(**REQUEST)
    urlopen.assert_not_called()


def test_the_spend_cap_settles_an_openrouter_reply_by_its_reported_tokens() -> None:
    """The reply's token counts reach the guard: a reply that used far more than was reserved
    stops the batch, so OpenRouter's usage is really what the cap is settled on."""
    guard = BatchSpendGuard(Decimal(1), Rates())
    limited = SpendLimitedClient(OpenRouterMessagesClient("private-test-key"), guard)
    with patch(
        "extraction.slot_reader.anthropic.urlopen",
        side_effect=lambda *_a, **_k: Response(provider_reply()),
    ):
        result = limited.converse(**REQUEST)
    assert result["usage"] == {"inputTokens": 45, "outputTokens": 12}

    huge = provider_reply(usage={"input_tokens": 45, "output_tokens": 1_000_000})
    with (
        patch(
            "extraction.slot_reader.anthropic.urlopen",
            side_effect=lambda *_a, **_k: Response(huge),
        ),
        pytest.raises(SpendCapExceeded, match="exceeded the reserved"),
    ):
        limited.converse(**REQUEST)


@pytest.mark.parametrize("status", [429, 402])
def test_a_call_refused_outright_gives_its_reservation_back(status: int) -> None:
    """OpenRouter limits a new account to 20 calls a minute per model. A refused call is not
    charged, so it must not hold budget: with room for one call, the retry still goes through."""
    guard = BatchSpendGuard(Decimal("0.08"), Rates())
    limited = SpendLimitedClient(OpenRouterMessagesClient("private-test-key"), guard)
    refusal = HTTPError(OPENROUTER_MESSAGES_URL, status, "limited", {}, BytesIO(b"{}"))
    with patch(
        "extraction.slot_reader.anthropic.urlopen",
        side_effect=[refusal, Response(provider_reply())],
    ) as urlopen:
        with pytest.raises(AnthropicRequestError):
            limited.converse(**REQUEST)
        result = limited.converse(**REQUEST)

    assert urlopen.call_count == 2
    assert result["stopReason"] == "end_turn"


@pytest.mark.parametrize(
    "failure",
    [
        HTTPError(OPENROUTER_MESSAGES_URL, 503, "unavailable", {}, BytesIO(b"{}")),
        HTTPError(OPENROUTER_MESSAGES_URL, 529, "overloaded", {}, BytesIO(b"{}")),
        TimeoutError(),
    ],
)
def test_a_call_that_may_have_been_charged_keeps_its_reservation(failure: Exception) -> None:
    guard = BatchSpendGuard(Decimal("0.08"), Rates())
    limited = SpendLimitedClient(OpenRouterMessagesClient("private-test-key"), guard)
    with patch("extraction.slot_reader.anthropic.urlopen", side_effect=failure) as urlopen:
        with pytest.raises(AnthropicRequestError):
            limited.converse(**REQUEST)
        with pytest.raises(SpendCapExceeded, match="would be exceeded"):
            limited.converse(**REQUEST)

    assert urlopen.call_count == 1
