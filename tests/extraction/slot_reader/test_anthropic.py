from __future__ import annotations

import base64
import json
import threading
from decimal import Decimal
from io import BytesIO
from typing import Self
from unittest.mock import patch
from urllib.error import HTTPError

import pytest

from extraction.form_reader.runner import _is_throttle
from extraction.slot_reader.anthropic import (
    AnthropicMessagesClient,
    AnthropicRequestError,
    BatchSpendGuard,
    SpendCapExceeded,
    SpendLimitedClient,
    ThreadLocalAnthropicClients,
    anthropic_messages_request,
)

MODEL = "anthropic.claude-opus-5-5"
REQUEST: dict[str, object] = {
    "modelId": MODEL,
    "system": [{"text": "Read only the marked span."}],
    "messages": [
        {
            "role": "user",
            "content": [
                {"image": {"format": "png", "source": {"bytes": b"full"}}},
                {"image": {"format": "png", "source": {"bytes": b"crop"}}},
                {"text": "Copy the printed label."},
            ],
        }
    ],
    "inferenceConfig": {"maxTokens": 3000},
}


def test_messages_request_keeps_both_images_and_maps_the_exact_token_bound() -> None:
    request = anthropic_messages_request(**REQUEST)

    assert request["model"] == "claude-opus-5-5"
    assert request["max_tokens"] == 3000
    messages = request["messages"]
    assert isinstance(messages, list)
    content = messages[0]["content"]
    assert isinstance(content, list)
    assert [item["type"] for item in content] == ["image", "image", "text"]
    assert content[0]["source"]["data"] == base64.b64encode(b"full").decode("ascii")
    assert content[1]["source"]["data"] == base64.b64encode(b"crop").decode("ascii")


def test_request_refuses_a_non_anthropic_model() -> None:
    with pytest.raises(ValueError, match="anthropic"):
        anthropic_messages_request(**{**REQUEST, "modelId": "qwen.qwen3-vl-235b-a22b"})


def test_response_is_adapted_to_the_existing_reader_shape_without_losing_usage() -> None:
    response_bytes = json.dumps(
        {
            "content": [{"type": "text", "text": '{"label":"13 1/8\\""}'}],
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
        return self.input_tokens

    def converse(self, **_kwargs: object) -> dict[str, object]:
        self.generated += 1
        return {"usage": {"inputTokens": self.input_tokens, "outputTokens": self.output_tokens}}


def test_spend_guard_refuses_before_generation_when_parallel_reservation_exceeds_cap() -> None:
    guard = BatchSpendGuard(Decimal("0.01"), FakeRates())
    client = FakeCountedClient(input_tokens=100, output_tokens=5)
    limited = SpendLimitedClient(client, guard)

    with pytest.raises(SpendCapExceeded, match="would be exceeded"):
        limited.converse(**REQUEST)

    assert client.generated == 0
