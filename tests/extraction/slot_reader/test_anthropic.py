from __future__ import annotations

import base64
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from io import BytesIO
from typing import Self
from unittest.mock import patch
from urllib.error import HTTPError

import pytest

from evidence.crop import encode_png
from extraction.form_reader.runner import _is_throttle
from extraction.slot_reader.anthropic import (
    ESTIMATED_INPUT_TOKENS_PER_CALL,
    AnthropicMessagesClient,
    AnthropicRequestError,
    BatchSpendGuard,
    SpendCapExceeded,
    SpendLimitedClient,
    ThreadLocalAnthropicClients,
    anthropic_messages_request,
    input_token_upper_bound,
)
from extraction.slot_reader.claude_output import (
    SPAN_SCHEMA,
    PictureWouldBeResized,
    output_config,
    visual_tokens,
)

MODEL = "anthropic.claude-opus-5-5"
FULL = encode_png(4, 2, bytes(24))
CROP = encode_png(2, 2, bytes(12))
REQUEST: dict[str, object] = {
    "modelId": MODEL,
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
SPAN_TEXT = json.dumps(
    {
        "belongs": True,
        "text": '17 5/8"',
        "stacked": False,
        "combined": False,
        "readable": True,
        "no_dimension": False,
    }
)


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
        "model": "claude-opus-5-5",
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": SPAN_TEXT}],
        "usage": {"input_tokens": 45, "output_tokens": 12},
    }
    payload.update(overrides)
    return payload


def converse_with(payload: object) -> tuple[dict[str, object], list[object]]:
    sent: list[object] = []

    def fake_urlopen(request: object, *, timeout: float) -> Response:
        sent.append(request)
        return Response(payload)

    with patch("extraction.slot_reader.anthropic.urlopen", side_effect=fake_urlopen):
        result = AnthropicMessagesClient("private-test-key").converse(**REQUEST)
    return dict(result), sent


def test_messages_request_keeps_both_images_and_maps_the_exact_token_bound() -> None:
    request = anthropic_messages_request(**REQUEST)

    assert request["model"] == "claude-opus-5-5"
    assert request["max_tokens"] == 3000
    messages = request["messages"]
    assert isinstance(messages, list)
    content = messages[0]["content"]
    assert isinstance(content, list)
    assert [item["type"] for item in content] == ["image", "image", "text"]
    assert content[0]["source"]["data"] == base64.b64encode(FULL).decode("ascii")
    assert content[1]["source"]["data"] == base64.b64encode(CROP).decode("ascii")


def test_messages_request_allows_self_contained_reader_prompts_without_system_field() -> None:
    request = anthropic_messages_request(
        **{key: value for key, value in REQUEST.items() if key != "system"}
    )

    assert "system" not in request
    assert request["messages"] == anthropic_messages_request(**REQUEST)["messages"]


def test_request_refuses_a_non_anthropic_model() -> None:
    with pytest.raises(ValueError, match="anthropic"):
        anthropic_messages_request(**{**REQUEST, "modelId": "qwen.qwen3-vl-235b-a22b"})


def test_response_is_adapted_to_the_existing_reader_shape_without_losing_usage() -> None:
    response_bytes = json.dumps(
        {
            "model": "claude-opus-5-5",
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": '{"label":"17 5/8\\""}'}],
            "usage": {"input_tokens": 45, "output_tokens": 12},
        }
    ).encode()

    class Response:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return response_bytes

    sent: list[object] = []

    def fake_urlopen(request: object, *, timeout: float) -> Response:
        sent.append(request)
        assert timeout == 180.0
        return Response()

    with patch("extraction.slot_reader.anthropic.urlopen", side_effect=fake_urlopen):
        result = AnthropicMessagesClient("private-test-key").converse(**REQUEST)

    assert len(sent) == 1
    request = sent[0]
    assert hasattr(request, "get_header")
    assert request.get_header("X-api-key") == "private-test-key"
    assert request.get_header("Anthropic-version") == "2023-06-01"
    assert result["output"]["message"]["content"][0]["text"].startswith('{"label"')
    assert result["usage"] == {"inputTokens": 45, "outputTokens": 12}


@pytest.mark.parametrize(
    ("status", "expected_code"),
    [
        (429, "TooManyRequestsException"),
        (529, "ModelOverloadedException"),
        (503, "ServiceUnavailableException"),
        (400, "ProviderRequestRejected"),
    ],
)
def test_provider_errors_are_sanitized_and_categorized_for_retry(
    status: int, expected_code: str
) -> None:
    error = HTTPError(
        "https://api.anthropic.com/v1/messages", status, "private", {}, BytesIO(b"secret")
    )
    with (
        patch("extraction.slot_reader.anthropic.urlopen", side_effect=error),
        pytest.raises(AnthropicRequestError) as caught,
    ):
        AnthropicMessagesClient("private-test-key").converse(**REQUEST)

    assert expected_code in str(caught.value)
    assert "secret" not in str(caught.value)
    assert "private-test-key" not in repr(caught.value)
    assert caught.value.response == {"Error": {"Code": expected_code}}
    assert _is_throttle(caught.value) is (status != 400)


def test_thread_local_provider_keeps_clients_separate_by_thread() -> None:
    provider = ThreadLocalAnthropicClients("private-test-key")
    first = provider.for_current_thread()
    seen: list[object] = []
    thread = threading.Thread(target=lambda: seen.append(provider.for_current_thread()))
    thread.start()
    thread.join()

    assert provider.for_current_thread() is first
    assert seen and seen[0] is not first


def test_api_key_is_not_part_of_client_repr() -> None:
    assert "private-test-key" not in repr(AnthropicMessagesClient("private-test-key"))


def test_count_tokens_uses_the_messages_count_endpoint_without_generation_tokens() -> None:
    response_bytes = json.dumps({"input_tokens": 321}).encode()

    class Response:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return response_bytes

    sent: list[object] = []

    def fake_urlopen(request: object, *, timeout: float) -> Response:
        sent.append(request)
        return Response()

    with patch("extraction.slot_reader.anthropic.urlopen", side_effect=fake_urlopen):
        count = AnthropicMessagesClient("private-test-key").count_input_tokens(**REQUEST)

    assert count == 321
    assert len(sent) == 1
    request = sent[0]
    assert request.full_url.endswith("/v1/messages/count_tokens")
    body = json.loads(request.data)
    assert "max_tokens" not in body
    assert body["model"] == "claude-opus-5-5"


class FakeRates:
    def rate_for(self, _model_id: str) -> object:
        return type(
            "Rate",
            (),
            {
                "input_per_1k_tokens": Decimal("0.004"),
                "output_per_1k_tokens": Decimal("0.020"),
            },
        )()


class FakeCountedClient:
    def __init__(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.generated = 0

    def count_input_tokens(self, **_kwargs: object) -> int:
        raise AssertionError("the Claude slot path must not make a count_tokens request")

    def converse(self, **_kwargs: object) -> dict[str, object]:
        self.generated += 1
        return {"usage": {"inputTokens": self.input_tokens, "outputTokens": self.output_tokens}}


def test_spend_guard_uses_a_fixed_estimate_and_refuses_before_generation_over_cap() -> None:
    guard = BatchSpendGuard(Decimal("0.01"), FakeRates())
    client = FakeCountedClient(input_tokens=100, output_tokens=5)
    limited = SpendLimitedClient(client, guard)

    with pytest.raises(SpendCapExceeded, match="would be exceeded"):
        limited.converse(**REQUEST)

    assert client.generated == 0


def test_concurrent_claude_calls_reserve_atomically_before_generation() -> None:
    entered = threading.Event()
    release = threading.Event()
    counter_lock = threading.Lock()

    class SlowClient(FakeCountedClient):
        def converse(self, **_kwargs: object) -> dict[str, object]:
            with counter_lock:
                self.generated += 1
            entered.set()
            assert release.wait(timeout=2)
            return {"usage": {"inputTokens": 10, "outputTokens": 5}}

    client = SlowClient(input_tokens=10, output_tokens=5)
    guard = BatchSpendGuard(Decimal("0.03"), FakeRates())
    limited = SpendLimitedClient(client, guard)
    request = {**REQUEST, "inferenceConfig": {"maxTokens": 128}}

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(limited.converse, **request) for _ in range(2)]
        assert entered.wait(timeout=2)
        release.set()
        results: list[object] = []
        for future in futures:
            try:
                results.append(future.result(timeout=2))
            except SpendCapExceeded as error:
                results.append(error)

    assert client.generated == 1
    assert sum(isinstance(result, SpendCapExceeded) for result in results) == 1


# --- #1051: fixed answer shape, stated effort, exact picture sizing, refusals --------------------


def test_every_request_states_its_answer_schema_and_effort() -> None:
    body = anthropic_messages_request(**REQUEST)

    assert body["output_config"] == {
        "format": {"type": "json_schema", "schema": SPAN_SCHEMA},
        "effort": "high",
    }
    assert "anthropicOutputConfig" not in body


def test_a_request_without_its_answer_schema_and_effort_is_refused() -> None:
    bare = {key: value for key, value in REQUEST.items() if key != "anthropicOutputConfig"}
    with pytest.raises(ValueError, match="answer schema and effort"):
        anthropic_messages_request(**bare)
    with pytest.raises(ValueError, match="effort"):
        anthropic_messages_request(
            **{
                **REQUEST,
                "anthropicOutputConfig": {
                    "format": output_config(SPAN_SCHEMA, "high")["format"],
                    "effort": "adaptive",
                },
            }
        )


def test_every_picture_asks_for_an_error_rather_than_a_resize() -> None:
    body = anthropic_messages_request(**REQUEST)
    images = [block for block in body["messages"][0]["content"] if block["type"] == "image"]

    assert len(images) == 2
    assert all(block["transformations"] == {"oversized_image": "error"} for block in images)


def test_an_oversized_picture_is_refused_before_anything_is_sent() -> None:
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
        AnthropicMessagesClient("private-test-key").converse(**request)
    urlopen.assert_not_called()


def test_no_server_side_fallback_is_ever_requested() -> None:
    result, sent = converse_with(provider_reply())

    body = json.loads(sent[0].data)
    assert "fallbacks" not in body
    assert sent[0].get_header("Anthropic-beta") is None
    assert result["stopReason"] == "end_turn"


def test_only_a_finished_turns_text_blocks_are_passed_on() -> None:
    result, _ = converse_with(
        provider_reply(
            content=[
                {"type": "thinking", "thinking": "private reasoning", "signature": "x"},
                {"type": "text", "text": SPAN_TEXT},
            ]
        )
    )

    assert result["output"]["message"]["content"] == [{"type": "text", "text": SPAN_TEXT}]


@pytest.mark.parametrize("stop", ["refusal", "max_tokens", "pause_turn"])
def test_a_refused_or_cut_off_reply_passes_on_no_text(stop: str) -> None:
    result, _ = converse_with(
        provider_reply(stop_reason=stop, content=[{"type": "text", "text": SPAN_TEXT[:20]}])
    )

    assert result["stopReason"] == stop
    assert result["output"]["message"]["content"] == []
    assert result["usage"] == {"inputTokens": 45, "outputTokens": 12}


def test_a_reply_from_another_model_is_never_passed_on_as_the_asked_models() -> None:
    result, _ = converse_with(provider_reply(model="claude-other-1"))

    assert result["stopReason"] == "model_mismatch"
    assert result["output"]["message"]["content"] == []


def test_a_dated_snapshot_of_the_asked_model_is_the_same_model() -> None:
    result, _ = converse_with(provider_reply(model="claude-opus-5-5-20260901"))

    assert result["stopReason"] == "end_turn"
    assert result["output"]["message"]["content"] != []


def test_the_input_upper_bound_counts_each_picture_by_its_tiles() -> None:
    view = encode_png(1650, 1275, bytes(1650 * 1275 * 3))
    bound = input_token_upper_bound(
        {
            "messages": [
                {
                    "content": [
                        {"image": {"source": {"bytes": view}}},
                        {"image": {"source": {"bytes": view}}},
                        {"image": {"source": {"bytes": view}}},
                        {"text": "abc"},
                    ]
                }
            ],
            "anthropicOutputConfig": output_config(SPAN_SCHEMA, "high"),
        }
    )

    assert bound > 3 * visual_tokens(1650, 1275) > 2 * ESTIMATED_INPUT_TOKENS_PER_CALL


def test_a_large_request_reserves_at_least_its_upper_bound() -> None:
    view = encode_png(1650, 1275, bytes(1650 * 1275 * 3))
    request = {
        **REQUEST,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"image": {"format": "png", "source": {"bytes": view}}},
                    {"image": {"format": "png", "source": {"bytes": view}}},
                    {"text": "Copy the printed label."},
                ],
            }
        ],
        "inferenceConfig": {"maxTokens": 1},
    }
    bound = input_token_upper_bound(request)
    rate = Decimal("0.004") / 1000
    # Just below the bound's price: the old fixed estimate (2 x 2048 tokens) would have fitted.
    guard = BatchSpendGuard(Decimal(bound) * rate, FakeRates())
    client = FakeCountedClient(input_tokens=10, output_tokens=1)

    with pytest.raises(SpendCapExceeded):
        SpendLimitedClient(client, guard).converse(**request)
    assert client.generated == 0
