"""Small adapter for Anthropic's Messages API used by the opt-in slot-reader route.

The slot reader consumes a Converse-shaped client. This adapter keeps that boundary stable while
sending images directly to the Messages API; it never logs request bodies, credentials, or answers.

Since #1051 every request carries its answer schema and effort (`output_config`, from the slot
reader's `anthropicOutputConfig`), every picture is marked `oversized_image: "error"` and is first
checked here against the documented limits, and a reply is passed on only when it finished its turn
on the model that was asked: a refusal, a token-limit stop or another model's answer comes back
with no text, which the reader treats as malformed and abstains on.

**No server-side fallback, ever.** The API can re-run a refused request on another model when the
request opts in (`fallbacks`, beta header `server-side-fallback-2026-07-01`). This adapter never
sends either: a reading must stay attributable to the model that made it, because the two-reader
rule seals a value only when two *named* models print the identical text.

The same request goes through OpenRouter by default (`openrouter.py`, #1094), with OpenRouter's
names for the models and its routing; the reply rules and the spend cap here are shared.
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

from extraction.slot_reader.claude_output import (
    CLAUDE_EFFORTS,
    OUTPUT_CONFIG_KEY,
    png_size,
    require_picture_fits,
    visual_tokens,
)

MESSAGES_URL = "https://api.anthropic.com/v1/messages"
COUNT_TOKENS_URL = "https://api.anthropic.com/v1/messages/count_tokens"
ANTHROPIC_VERSION = "2023-06-01"
MODEL_PREFIX = "anthropic."
# A deliberately fixed, preflight input estimate for the bounded two-image span request. It avoids
# an extra provider round-trip per reading; the batch lock reserves this estimate and the full
# output allowance before any generation call begins.
ESTIMATED_INPUT_TOKENS_PER_CALL = 2048
#: Room for the API's own scaffolding around a structured-output request (its schema instructions
#: and message framing), added to the upper bound below.
_REQUEST_OVERHEAD_TOKENS = 1024


class AnthropicRequestError(RuntimeError):
    """Sanitized API error carrying only the status class needed for bounded retries."""

    def __init__(
        self, status_code: int, error_type: str, *, api: str = "Anthropic Messages API"
    ) -> None:
        self.status_code = status_code
        self.response = {"Error": {"Code": error_type}}
        super().__init__(f"{api} returned HTTP {status_code} ({error_type})")


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
    lost response must not make budget appear available again. The one exception is a call the
    provider refused outright, unserved (`refused_unserved`): nothing was generated or charged.
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

    def release(self, reservation: _Reservation) -> None:
        """Give back the reservation of a call the provider refused outright, unserved."""
        with self._lock:
            self._reserved_usd -= reservation.reserved_usd

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
        reservation = self._guard.reserve(model_id, _reserved_input_tokens(kwargs), output_limit)
        try:
            response = self._client.converse(**kwargs)
        except AnthropicRequestError as error:
            if refused_unserved(error):
                self._guard.release(reservation)
            raise
        if not isinstance(response, Mapping):
            raise TypeError("Claude client returned a malformed response")
        self._guard.settle(reservation, response)
        return response


def refused_unserved(error: AnthropicRequestError) -> bool:
    """Whether the provider answered, before generating anything, that it would not serve the call:
    a rate limit (429) or payment required (402, OpenRouter's in-flight budget or no credit). Such a
    call is not charged (#1094: OpenRouter limits a new account to 20 calls a minute per model, and
    its refusals held $1+ of a $2.50 cap). A lost connection, an overload or a server error may
    have been charged, so those keep their reservation."""
    return error.status_code in (402, 429)


def input_token_upper_bound(request: Mapping[str, Any]) -> int:
    """An upper bound on a request's input tokens, from what it carries.

    Each picture costs exactly its documented tile count (a picture whose size cannot be read is
    counted at the largest any picture may cost); each text and the answer schema at most one token
    per UTF-8 byte; plus a fixed allowance for the API's own framing.
    """
    total = _REQUEST_OVERHEAD_TOKENS
    for message in request.get("messages") or ():
        content = message.get("content") if isinstance(message, Mapping) else None
        for item in content or ():
            if not isinstance(item, Mapping):
                continue
            text = item.get("text")
            if isinstance(text, str):
                total += len(text.encode("utf-8"))
            image = item.get("image")
            source = image.get("source") if isinstance(image, Mapping) else None
            raw = source.get("bytes") if isinstance(source, Mapping) else None
            if isinstance(raw, bytes):
                try:
                    total += visual_tokens(*png_size(raw))
                except ValueError:
                    total += 4784
    for block in request.get("system") or ():
        if isinstance(block, Mapping) and isinstance(block.get("text"), str):
            total += len(block["text"].encode("utf-8"))
    config = request.get(OUTPUT_CONFIG_KEY)
    if config is not None:
        total += len(json.dumps(config, separators=(",", ":")).encode("utf-8"))
    return total


def _reserved_input_tokens(request: Mapping[str, Any]) -> int:
    """The input estimate a reservation is made with: the guard doubles it, so the reserve is
    never below the request's upper bound and never below the fixed estimate used before #1051."""
    return max(ESTIMATED_INPUT_TOKENS_PER_CALL, -(-input_token_upper_bound(request) // 2))


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


def _message_content(
    value: object, model_id: str, *, refuse_resize: bool
) -> list[dict[str, object]]:
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
        if not isinstance(raw, bytes) or image_format != "png":
            raise ValueError("Anthropic image must be PNG bytes, whose size can be checked")
        # Refused here, before any call, if the API would resize it (#1051): the pictures were
        # measured at the size they are drawn, and a silently shrunk one is not that picture.
        require_picture_fits((raw,), model_id)
        block: dict[str, object] = {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": f"image/{'jpeg' if image_format == 'jpg' else image_format}",
                "data": base64.b64encode(raw).decode("ascii"),
            },
        }
        if refuse_resize:
            # And if the local check and the API ever disagree, the API refuses rather than
            # resizes (vision-coordinates, "Turn resizing into an error").
            block["transformations"] = {"oversized_image": "error"}
        content.append(block)
    return content


def _output_config(value: object) -> dict[str, object]:
    """The request's answer schema and effort, required on every Claude call (#1051)."""
    if value is None:
        raise ValueError("Claude slot-reader request must state its answer schema and effort")
    if not isinstance(value, Mapping):
        raise TypeError("Claude slot-reader answer schema and effort must be an object")
    answer_format = value.get("format")
    effort = value.get("effort")
    if (
        not isinstance(answer_format, Mapping)
        or answer_format.get("type") != "json_schema"
        or not isinstance(answer_format.get("schema"), Mapping)
    ):
        raise ValueError("Claude slot-reader request must carry a JSON-schema answer format")
    if effort not in CLAUDE_EFFORTS:
        raise ValueError("Claude slot-reader request must state a supported effort level")
    return {
        "format": {"type": "json_schema", "schema": dict(answer_format["schema"])},
        "effort": effort,
    }


def anthropic_messages_request(**kwargs: Any) -> dict[str, object]:
    """Translate the narrow Converse request used by the slot reader to Messages API JSON."""
    model_id = kwargs.get("modelId")
    if not isinstance(model_id, str) or not model_id.startswith(MODEL_PREFIX):
        raise ValueError("Claude slot-reader model ids must start with 'anthropic.'")
    return messages_request(kwargs, model=model_id.removeprefix(MODEL_PREFIX), refuse_resize=True)


def messages_request(
    kwargs: Mapping[str, Any], *, model: str, refuse_resize: bool
) -> dict[str, object]:
    """The Messages API body for a slot-reader request, asking `model` by the route's own name.

    `refuse_resize` marks every picture `oversized_image: "error"` where the route takes that
    field; the local size check refuses an oversized picture before any call either way.
    """
    model_id = kwargs.get("modelId")
    if not isinstance(model_id, str):
        raise TypeError("Claude slot-reader request must name its model")
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
        "messages": [
            {
                "role": "user",
                "content": _message_content(
                    message.get("content"), model_id, refuse_resize=refuse_resize
                ),
            }
        ],
        "output_config": _output_config(kwargs.get(OUTPUT_CONFIG_KEY)),
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
        return messages_reply(
            payload, answered=_answered_by(payload.get("model"), str(body["model"]))
        )

    def count_input_tokens(self, **kwargs: Any) -> int:
        body = anthropic_messages_request(**kwargs)
        body.pop("max_tokens", None)
        payload = self._post_json(COUNT_TOKENS_URL, body)
        count = payload.get("input_tokens")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise AnthropicRequestError(502, "ProviderTokenCountMalformed")
        return count

    def _post_json(self, url: str, body: Mapping[str, object]) -> Mapping[str, Any]:
        return post_json(
            url,
            body,
            headers={"x-api-key": self._api_key, "anthropic-version": ANTHROPIC_VERSION},
            timeout_seconds=self._timeout_seconds,
        )


def messages_reply(
    payload: Mapping[str, Any], *, answered: bool, api: str = "Anthropic Messages API"
) -> dict[str, Any]:
    """A Messages API reply in the reader's Converse shape. `answered` says the reply names the
    model that was asked: when it does not, no text is passed on as that model's reading."""
    content = payload.get("content")
    usage = payload.get("usage")
    if not isinstance(content, list) or not isinstance(usage, Mapping):
        raise AnthropicRequestError(502, "ProviderResponseMalformed", api=api)
    stop_reason = payload.get("stop_reason")
    if not answered:
        # Not the model that was asked: never passed on as that model's reading.
        stop_reason = "model_mismatch"
    # Only a finished turn's text blocks are an answer. Thinking blocks are the model's own and
    # never parsed; a refusal or a token-limit stop may carry partial text, which the API says
    # to discard (refusals-and-fallback), so the reader sees no text and abstains.
    texts = (
        [
            {"type": "text", "text": item["text"]}
            for item in content
            if isinstance(item, Mapping)
            and item.get("type") == "text"
            and isinstance(item.get("text"), str)
        ]
        if stop_reason == "end_turn"
        else []
    )
    return {
        "stopReason": stop_reason,
        "output": {"message": {"content": texts}},
        "usage": {
            "inputTokens": usage.get("input_tokens"),
            "outputTokens": usage.get("output_tokens"),
        },
    }


def post_json(
    url: str,
    body: Mapping[str, object],
    *,
    headers: Mapping[str, str],
    timeout_seconds: int,
    api: str = "Anthropic Messages API",
) -> Mapping[str, Any]:
    """POST one JSON body. Errors carry only a status and a retry class: never the body sent,
    the reply's words or a credential."""
    request = Request(
        url,
        data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
        headers={"content-type": "application/json", **headers},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read())
    except HTTPError as error:
        status = int(error.code)
        # OpenRouter's "in-flight budget" (a new or low-balance account, too many calls at once)
        # is a 402 that clears by itself, so it is retried like a throttle (decision log
        # 2026-10-06). Any other 402 (no credit) is refused. Only that marker is looked for; the
        # reply's words never leave this function.
        in_flight = status == 402 and b"in_flight_budget" in _error_body(error)
        error.close()
        error_type = (
            "TooManyRequestsException"
            if status == 429 or in_flight
            else (
                "ModelOverloadedException"
                if status == 529
                else ("ServiceUnavailableException" if status >= 500 else "ProviderRequestRejected")
            )
        )
        raise AnthropicRequestError(status, error_type, api=api) from None
    except (URLError, TimeoutError, OSError):
        raise AnthropicRequestError(503, "ProviderTransportError", api=api) from None
    if not isinstance(payload, Mapping):
        raise AnthropicRequestError(502, "ProviderResponseMalformed", api=api)
    return payload


def _error_body(error: HTTPError) -> bytes:
    """At most 4 KB of an error reply, or nothing when it cannot be read whole; always closed.

    Any failure is swallowed: a cut-off reply (`IncompleteRead`) carries the reply's own bytes,
    which must never reach an exception."""
    try:
        return error.read(4096) or b""
    except Exception:  # noqa: BLE001 - the reply's words must never escape
        return b""
    finally:
        error.close()


def _answered_by(reported: object, requested: str) -> bool:
    """Whether the reply names the model asked (or a dated snapshot of it)."""
    return isinstance(reported, str) and (
        reported == requested or reported.startswith(f"{requested}-")
    )


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
