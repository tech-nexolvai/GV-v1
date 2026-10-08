"""Small adapter for Anthropic's Messages API used by the opt-in slot-reader route.

The slot reader consumes a Converse-shaped client. This adapter keeps that boundary stable while
sending images directly to the Messages API; it never logs request bodies, credentials, or answers.
"""

from __future__ import annotations

import base64
import json
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

MESSAGES_URL = "https://api.anthropic.com/v1/messages"
COUNT_TOKENS_URL = "https://api.anthropic.com/v1/messages/count_tokens"
ANTHROPIC_VERSION = "2023-06-01"
MODEL_PREFIX = "anthropic."
# A deliberately fixed, preflight input estimate for the bounded two-image span request. It avoids
# an extra provider round-trip per reading; the batch lock reserves this estimate and the full
# output allowance before any generation call begins.
ESTIMATED_INPUT_TOKENS_PER_CALL = 2048


class AnthropicRequestError(RuntimeError):
    """Sanitized API error carrying only the status class needed for bounded retries."""

    def __init__(self, status_code: int, error_type: str) -> None:
        self.status_code = status_code
        self.response = {"Error": {"Code": error_type}}
        super().__init__(f"Anthropic Messages API returned HTTP {status_code} ({error_type})")


class SpendCapExceeded(RuntimeError):
    """A request was refused before generation because its worst-case charge would exceed cap."""


@dataclass(frozen=True, slots=True)
class _Reservation:
    reserved_usd: Decimal
    model_id: str
    input_tokens: int
    output_limit: int


class BatchSpendGuard:
    """Thread-safe batch cap using a conservative fixed input estimate.

    Output is reserved at its full configured maximum. Input is reserved at twice the per-call
    estimate to allow for image-tokenization variance. Failed calls retain their reservation: a
    lost response must not make budget appear available again.
    """

    def __init__(self, maximum_usd: Decimal, rates: object) -> None:
        if not maximum_usd.is_finite() or maximum_usd < 0:
            raise ValueError("Claude reader spend cap must be finite and non-negative")
        self._maximum_usd = maximum_usd
        self._rates = rates
        self._reserved_usd = Decimal(0)
        self._lock = threading.Lock()

    def reserve(self, model_id: str, input_tokens: int, output_limit: int) -> _Reservation:
        if input_tokens < 0 or output_limit < 1:
            raise ValueError("token counts must be non-negative and output limit positive")
        rate_for = getattr(self._rates, "rate_for", None)
        if not callable(rate_for):
            raise TypeError("Claude reader spend cap requires a rate lookup")
        rate = rate_for(model_id)
        input_rate = getattr(rate, "input_per_1k_tokens", None)
        output_rate = getattr(rate, "output_per_1k_tokens", None)
        if not isinstance(input_rate, Decimal) or not isinstance(output_rate, Decimal):
            raise TypeError(f"Claude reader has no usable stated price for {model_id}")
        # Double the bounded input estimate and reserve the entire output limit. This intentionally
        # prefers a false refusal (more human review) over overspending.
        amount = (
            Decimal(input_tokens * 2) * input_rate + Decimal(output_limit) * output_rate
        ) / 1000
        with self._lock:
            if self._reserved_usd + amount > self._maximum_usd:
                raise SpendCapExceeded("Claude reader batch spend cap would be exceeded")
            self._reserved_usd += amount
        return _Reservation(amount, model_id, input_tokens, output_limit)

    def settle(self, reservation: _Reservation, response: Mapping[str, Any]) -> None:
        usage = response.get("usage")
        if not isinstance(usage, Mapping):
            return
        input_tokens = usage.get("inputTokens")
        output_tokens = usage.get("outputTokens")
        if (
            isinstance(input_tokens, bool)
            or not isinstance(input_tokens, int)
            or isinstance(output_tokens, bool)
            or not isinstance(output_tokens, int)
        ):
            return
        rate_for = getattr(self._rates, "rate_for", None)
        if not callable(rate_for):
            return
        rate = rate_for(reservation.model_id)
        input_rate = getattr(rate, "input_per_1k_tokens", None)
        output_rate = getattr(rate, "output_per_1k_tokens", None)
        if not isinstance(input_rate, Decimal) or not isinstance(output_rate, Decimal):
            return
        actual = (Decimal(input_tokens) * input_rate + Decimal(output_tokens) * output_rate) / 1000
        with self._lock:
            # If actual exceeds the reserve, keep the higher amount accounted for and fail closed.
            self._reserved_usd += actual - reservation.reserved_usd
            if self._reserved_usd > self._maximum_usd:
                raise SpendCapExceeded("provider usage exceeded the reserved Claude reader budget")


class SpendLimitedClient:
    """Reserve worst-case spend before each paid request, shared across parallel reader threads."""

    def __init__(self, client: Any, guard: BatchSpendGuard) -> None:
        self._client = client
        self._guard = guard

    def converse(self, **kwargs: Any) -> Mapping[str, Any]:
        output_config = kwargs.get("inferenceConfig")
        output_limit = (
            output_config.get("maxTokens") if isinstance(output_config, Mapping) else None
        )
        model_id = kwargs.get("modelId")
        if (
            not isinstance(model_id, str)
            or isinstance(output_limit, bool)
            or not isinstance(output_limit, int)
        ):
            raise TypeError("Claude request is missing its model id or output-token bound")
        reservation = self._guard.reserve(model_id, ESTIMATED_INPUT_TOKENS_PER_CALL, output_limit)
        response = self._client.converse(**kwargs)
        if not isinstance(response, Mapping):
            raise TypeError("Claude client returned a malformed response")
        self._guard.settle(reservation, response)
        return response


def _text_blocks(value: object) -> str:
    if not isinstance(value, list):
        raise TypeError("Anthropic system prompt must be a list of text blocks")
    blocks: list[str] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise TypeError("Anthropic system prompt blocks must be objects")
        text = item.get("text")
        if not isinstance(text, str):
            raise TypeError("Anthropic system prompt blocks must contain text")
        blocks.append(text)
    if not blocks:
        raise ValueError("Anthropic system prompt must contain at least one text block")
    return "\n".join(blocks)


def _message_content(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise TypeError("Anthropic user message content must be a list")
    content: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise TypeError("Anthropic message blocks must be objects")
        if "text" in item:
            text = item["text"]
            if not isinstance(text, str):
                raise ValueError("Anthropic text block must contain text")
            content.append({"type": "text", "text": text})
            continue
        image = item.get("image")
        if not isinstance(image, Mapping):
            raise TypeError("Anthropic message blocks must be text or images")
        source = image.get("source")
        image_format = image.get("format")
        if not isinstance(source, Mapping) or not isinstance(image_format, str):
            raise TypeError("Anthropic image block is incomplete")
        raw = source.get("bytes")
        if not isinstance(raw, bytes) or image_format not in {"png", "jpeg", "webp", "gif"}:
            raise ValueError("Anthropic image must have supported bytes and a stated format")
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": f"image/{'jpeg' if image_format == 'jpg' else image_format}",
                    "data": base64.b64encode(raw).decode("ascii"),
                },
            }
        )
    return content


def anthropic_messages_request(**kwargs: Any) -> dict[str, object]:
    """Translate the narrow Converse request used by the slot reader to Messages API JSON."""
    model_id = kwargs.get("modelId")
    if not isinstance(model_id, str) or not model_id.startswith(MODEL_PREFIX):
        raise ValueError("Claude slot-reader model ids must start with 'anthropic.'")
    model = model_id.removeprefix(MODEL_PREFIX)
    system_value = kwargs.get("system")
    messages = kwargs.get("messages")
    if not isinstance(messages, list) or len(messages) != 1:
        raise ValueError("Claude slot-reader request must contain one user message")
    message = messages[0]
    if not isinstance(message, Mapping) or message.get("role") != "user":
        raise ValueError("Claude slot-reader request must contain a user message")
    config = kwargs.get("inferenceConfig")
    max_tokens = config.get("maxTokens") if isinstance(config, Mapping) else None
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1:
        raise ValueError("Claude slot-reader request requires a positive max token count")
    request: dict[str, object] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": _message_content(message.get("content"))}],
    }
    # The Messages API makes `system` optional. The slot-reader prompts are self-contained and
    # its existing Bedrock-shaped request builders intentionally omit this field.
    if system_value is not None:
        request["system"] = _text_blocks(system_value)
    return request


class AnthropicMessagesClient:
    """One thread-owned Messages API client. The key is never included in repr or errors."""

    def __init__(self, api_key: str, *, timeout_seconds: int = 180) -> None:
        if not api_key.strip():
            raise ValueError("Claude slot-reader is enabled but ANTHROPIC_API_KEY is not set")
        if timeout_seconds <= 0:
            raise ValueError("Anthropic request timeout must be positive")
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds

    def converse(self, **kwargs: Any) -> Mapping[str, Any]:
        body = anthropic_messages_request(**kwargs)
        payload = self._post_json(MESSAGES_URL, body)
        content = payload.get("content")
        usage = payload.get("usage")
        if not isinstance(content, list) or not isinstance(usage, Mapping):
            raise AnthropicRequestError(502, "ProviderResponseMalformed")
        return {
            "output": {"message": {"content": content}},
            "usage": {
                "inputTokens": usage.get("input_tokens"),
                "outputTokens": usage.get("output_tokens"),
            },
        }

    def count_input_tokens(self, **kwargs: Any) -> int:
        body = anthropic_messages_request(**kwargs)
        body.pop("max_tokens", None)
        payload = self._post_json(COUNT_TOKENS_URL, body)
        count = payload.get("input_tokens")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise AnthropicRequestError(502, "ProviderTokenCountMalformed")
        return count

    def _post_json(self, url: str, body: Mapping[str, object]) -> Mapping[str, Any]:
        request = Request(
            url,
            data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
            headers={
                "content-type": "application/json",
                "x-api-key": self._api_key,
                "anthropic-version": ANTHROPIC_VERSION,
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                payload = json.loads(response.read())
        except HTTPError as error:
            status = int(error.code)
            error.close()
            error_type = (
                "TooManyRequestsException"
                if status == 429
                else (
                    "ModelOverloadedException"
                    if status == 529
                    else (
                        "ServiceUnavailableException"
                        if status >= 500
                        else "ProviderRequestRejected"
                    )
                )
            )
            raise AnthropicRequestError(status, error_type) from None
        except (URLError, TimeoutError, OSError):
            raise AnthropicRequestError(503, "ProviderTransportError") from None
        if not isinstance(payload, Mapping):
            raise AnthropicRequestError(502, "ProviderResponseMalformed")
        return payload


class ThreadLocalAnthropicClients:
    """Create one client instance per reader thread, matching the existing worker contract."""

    def __init__(self, api_key: str, *, timeout_seconds: int = 180) -> None:
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._local = threading.local()

    def for_current_thread(self) -> AnthropicMessagesClient:
        client = getattr(self._local, "client", None)
        if client is None:
            client = AnthropicMessagesClient(self._api_key, timeout_seconds=self._timeout_seconds)
            self._local.client = client
        return client
