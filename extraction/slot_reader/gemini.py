"""The Gemini reader pair through OpenRouter's chat-completions API (#1211).

Gemini 3.8 Flash and Gemini 3.1 Pro are asked the slot reader's own questions: the same pictures
(PNG, base64), the same question text and the same answer schemas (`claude_output.py`). OpenRouter
takes Gemini only in the OpenAI chat-completions shape (`POST /api/v1/chat/completions`), so this
module translates the slot reader's Converse-shaped request into that shape and the reply back. Every
rule of the Claude route holds here too:

- **Where it runs.** Google Vertex's global endpoint only (`only: ["google-vertex/global"]`, the
  standard tier: a base slug matches no flex or priority tier), keeping no data (`zdr: true`,
  `data_collection: "deny"`), no other host (`allow_fallbacks: false`), and never a host that would
  ignore a parameter (`require_parameters: true`). On OpenRouter's listing (2026-10-13) both models'
  Vertex endpoints are on the zero-data-retention list; Google AI Studio's are not.
- **No model fallback, ever.** `models`, `fallbacks` and `route` are never sent, and a reply that
  does not name the model asked (exactly, or as a dated snapshot), or that says it came from a host
  other than Vertex ("Google"), passes on no text: a reading must stay attributable to the model
  that made it.
- **The answer schema, strict.** `response_format: {type: json_schema, strict: true}` with the
  existing schema, unchanged: every object closes (`additionalProperties: false`, every property
  required), word lists are string `enum`s, and no pattern, bound or format is used. The reply is
  still checked strictly here (`claude_answer`), so a provider that enforced less only ever makes
  a question abstain, never a value. If the bake-off shows Vertex refusing a construct, the schema
  is adapted then, and said so here.
- **Effort, stated.** `reasoning: {effort, exclude: true}`, from the request's own stated effort,
  which must be the model's (`GEMINI_EFFORTS`): `low` for both. It is the lowest each model offers
  (Gemini 3.8 Flash: low, medium, high; Gemini 3.1 Pro: low, medium, high, and Pro cannot turn
  thinking off), the effort the public single-value reading scores were measured at (Flash scored
  lower at high effort and cost about 8 times more), and the cheapest: thinking is billed as output.
  `exclude` keeps the thinking text out of the reply; it is never read.
- **No temperature, no `media_resolution`.** Vertex's Gemini 3.8 Flash endpoint does not list
  `temperature` and neither model's endpoint lists `media_resolution`, so with
  `require_parameters` either would leave no host to route to. Gemini 3's documented default picture
  budget is the high one (1,120 tokens a picture), which is what `media_resolution: high` would ask
  for; the bake-off confirms it from the input tokens each call reports.
- **Pictures.** Checked against the model's limits before any call (`require_picture_fits`), as
  the Claude route's are.
- **Errors.** The same sanitized classes as the Claude route (`anthropic.post_json`,
  `status_error`): 429 and 5xx retried as throttling, a lost connection or a timeout as a transport
  error, 402 out of credit never retried, OpenRouter's in-flight 402 retried a bounded number of
  times one call at a time; an error OpenRouter reports inside a 200 reply is classified by the
  status it names. A refusal (`content_filter`, a `refusal` message), a token-limit stop (`length`)
  or any other finish passes on no text, so the reader re-asks once and then abstains.
- **Cost.** The reply's own token counts (`prompt_tokens`, `completion_tokens`, which include the
  thinking) are recorded and priced from `deploy/model_rates.us-east-1.json`; the spend guard
  reserves before each call exactly as for Claude.

Source: issue #1211; https://openrouter.ai/docs/guides/routing/provider-selection ,
https://ai.google.dev/gemini-api/docs/thinking , .../media-resolution (read 2026-10-13).
Verification: `tests/extraction/slot_reader/test_gemini.py`.
"""

from __future__ import annotations

import base64
import json
import threading
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Final

from extraction.slot_reader.anthropic import AnthropicRequestError, post_json, status_error
from extraction.slot_reader.claude_output import OUTPUT_CONFIG_KEY, require_picture_fits
from extraction.slot_reader.openrouter import _SNAPSHOT, OPENROUTER_HOSTS
from extraction.slot_reader.reader_pairs import GEMINI_FLASH, GEMINI_PRO

__all__ = [
    "ANSWER_SCHEMA_NAME",
    "GEMINI_EFFORTS",
    "GEMINI_MODELS",
    "GEMINI_ROUTE",
    "OPENROUTER_CHAT_URL",
    "OpenRouterGeminiClient",
    "ThreadLocalGeminiClients",
    "gemini_chat_request",
    "gemini_reply",
]

OPENROUTER_CHAT_URL: Final = "https://openrouter.ai/api/v1/chat/completions"
_API: Final = "OpenRouter Chat Completions API"

#: The slot reader's Gemini ids, by OpenRouter's names for the same models.
GEMINI_MODELS: Final[Mapping[str, str]] = MappingProxyType(
    {
        GEMINI_FLASH: "google/gemini-3.8-flash",
        GEMINI_PRO: "google/gemini-3.1-pro-preview",
    }
)
#: The effort each Gemini model is asked at, and the only one it is asked at (see the module note).
GEMINI_EFFORTS: Final[Mapping[str, str]] = MappingProxyType(
    {GEMINI_FLASH: "low", GEMINI_PRO: "low"}
)
#: The route in a run's identity, so a reading says where and how it was made.
GEMINI_ROUTE: Final = f"openrouter-chat:{'+'.join(OPENROUTER_HOSTS)}"
#: The name the answer schema is sent under; the schema itself is the question's own.
ANSWER_SCHEMA_NAME: Final = "slot_reader_answer"
#: OpenRouter's name for Google Vertex in a reply's `provider` field (AI Studio is another name).
_VERTEX_PROVIDER_NAME: Final = "Google"
#: How a chat completion's finish maps to the reader's stop reasons; only `end_turn` is an answer.
_FINISH: Final[Mapping[str, str]] = MappingProxyType(
    {"stop": "end_turn", "length": "max_tokens", "content_filter": "refusal"}
)


def _routing() -> dict[str, object]:
    """Google Vertex's global endpoint only, keeping no data, honouring every parameter."""
    return {
        "only": list(OPENROUTER_HOSTS),
        "allow_fallbacks": False,
        "require_parameters": True,
        "data_collection": "deny",
        "zdr": True,
    }


def _system_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, list) or not value:
        raise TypeError("a slot-reader system prompt must be a list of text blocks")
    texts: list[str] = []
    for block in value:
        text = block.get("text") if isinstance(block, Mapping) else None
        if not isinstance(text, str):
            raise TypeError("a slot-reader system prompt block must contain text")
        texts.append(text)
    return "\n".join(texts)


def _user_content(value: object, model_id: str) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise TypeError("a slot-reader user message content must be a list")
    content: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise TypeError("a slot-reader message block must be an object")
        if "text" in item:
            text = item["text"]
            if not isinstance(text, str):
                raise ValueError("a slot-reader text block must contain text")
            content.append({"type": "text", "text": text})
            continue
        image = item.get("image")
        source = image.get("source") if isinstance(image, Mapping) else None
        raw = source.get("bytes") if isinstance(source, Mapping) else None
        if (
            not isinstance(image, Mapping)
            or image.get("format") != "png"
            or not isinstance(raw, bytes)
        ):
            raise ValueError("a Gemini reader is shown PNG pictures, whose size can be checked")
        # Refused here, before any call, past the model's limits: both pairs see the same pictures.
        require_picture_fits((raw,), model_id)
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64," + base64.b64encode(raw).decode()},
            }
        )
    return content


def _answer_format(value: object, model_id: str) -> tuple[dict[str, object], str]:
    """The request's answer schema as a strict `response_format`, and its stated effort, which
    must be the one this model is asked at."""
    if value is None:
        raise ValueError("a Gemini slot-reader request must state its answer schema and effort")
    if not isinstance(value, Mapping):
        raise TypeError("a Gemini slot-reader answer schema and effort must be an object")
    answer_format = value.get("format")
    schema = answer_format.get("schema") if isinstance(answer_format, Mapping) else None
    if (
        not isinstance(answer_format, Mapping)
        or answer_format.get("type") != "json_schema"
        or not isinstance(schema, Mapping)
    ):
        raise ValueError("a Gemini slot-reader request must carry a JSON-schema answer format")
    effort = value.get("effort")
    if effort != GEMINI_EFFORTS[model_id]:
        raise ValueError(
            f"{model_id} is asked at effort {GEMINI_EFFORTS[model_id]!r} only, not {effort!r}"
        )
    return (
        {
            "type": "json_schema",
            "json_schema": {"name": ANSWER_SCHEMA_NAME, "strict": True, "schema": dict(schema)},
        },
        effort,
    )


def gemini_chat_request(**kwargs: Any) -> dict[str, object]:
    """The slot reader's Converse-shaped request as OpenRouter's chat-completions body."""
    model_id = kwargs.get("modelId")
    model = GEMINI_MODELS.get(model_id) if isinstance(model_id, str) else None
    if model is None or not isinstance(model_id, str):
        raise ValueError(
            "Gemini through OpenRouter reads only with Gemini 3.8 Flash and Gemini 3.1 Pro"
        )
    messages = kwargs.get("messages")
    if not isinstance(messages, list) or len(messages) != 1:
        raise ValueError("a Gemini slot-reader request must contain one user message")
    message = messages[0]
    if not isinstance(message, Mapping) or message.get("role") != "user":
        raise ValueError("a Gemini slot-reader request must contain a user message")
    config = kwargs.get("inferenceConfig")
    max_tokens = config.get("maxTokens") if isinstance(config, Mapping) else None
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1:
        raise ValueError("a Gemini slot-reader request requires a positive max token count")
    response_format, effort = _answer_format(kwargs.get(OUTPUT_CONFIG_KEY), model_id)
    system = _system_text(kwargs.get("system"))
    chat: list[dict[str, object]] = (
        [] if system is None else [{"role": "system", "content": system}]
    )
    chat.append({"role": "user", "content": _user_content(message.get("content"), model_id)})
    return {
        "model": model,
        "messages": chat,
        # Caps the whole output, thinking included: the spend guard reserves exactly this.
        "max_tokens": max_tokens,
        "response_format": response_format,
        "reasoning": {"effort": effort, "exclude": True},
        "provider": _routing(),
    }


def _answered_by(reported: object, model_id: str) -> bool:
    """Whether the reply names the model asked, by OpenRouter's name, exactly or as a dated
    snapshot (`google/gemini-3.8-flash-20260902`): never another model or a variant (`-lite`)."""
    name = GEMINI_MODELS[model_id]
    return (
        isinstance(reported, str)
        and reported.startswith(name)
        and _SNAPSHOT.fullmatch(reported, len(name)) is not None
    )


def _raise_reported_error(payload: Mapping[str, Any]) -> None:
    """An error OpenRouter reports inside a 200 reply, classified by the status it names; its
    words never leave this function."""
    error = payload.get("error")
    if error is None:
        return
    code = error.get("code") if isinstance(error, Mapping) else None
    status = code if isinstance(code, int) and not isinstance(code, bool) else 502
    in_flight = status == 402 and "in_flight_budget" in json.dumps(error, default=str)
    raise status_error(status, api=_API, in_flight=in_flight)


def gemini_reply(payload: Mapping[str, Any], *, model_id: str) -> dict[str, Any]:
    """A chat-completions reply in the reader's Converse shape. Only one finished choice from the
    model asked, on Vertex, with no refusal, passes on its text; anything else passes on none."""
    _raise_reported_error(payload)
    choices = payload.get("choices")
    usage = payload.get("usage")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(usage, Mapping):
        raise AnthropicRequestError(502, "ProviderResponseMalformed", api=_API)
    choice = choices[0]
    message = choice.get("message") if isinstance(choice, Mapping) else None
    if not isinstance(choice, Mapping) or not isinstance(message, Mapping):
        raise AnthropicRequestError(502, "ProviderResponseMalformed", api=_API)
    finish = choice.get("finish_reason")
    stop_reason: str | None = _FINISH.get(finish, finish) if isinstance(finish, str) else None
    if message.get("refusal"):
        stop_reason = "refusal"
    provider = payload.get("provider")
    if not _answered_by(payload.get("model"), model_id) or (
        provider is not None and provider != _VERTEX_PROVIDER_NAME
    ):
        # Not the model asked, or not where it was pinned: never passed on as its reading.
        stop_reason = "model_mismatch"
    text = message.get("content")
    texts = (
        [{"type": "text", "text": text}]
        if stop_reason == "end_turn" and isinstance(text, str)
        else []
    )
    return {
        "stopReason": stop_reason,
        "output": {"message": {"content": texts}},
        "usage": {
            "inputTokens": usage.get("prompt_tokens"),
            "outputTokens": usage.get("completion_tokens"),
        },
    }


class OpenRouterGeminiClient:
    """One thread-owned OpenRouter chat client. The key is never included in repr or errors."""

    def __init__(self, api_key: str, *, timeout_seconds: int = 180) -> None:
        if not api_key.strip():
            raise ValueError(
                "Gemini through OpenRouter is enabled but OPENROUTER_API_KEY is not set"
            )
        if timeout_seconds <= 0:
            raise ValueError("OpenRouter request timeout must be positive")
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds

    def __repr__(self) -> str:
        return f"OpenRouterGeminiClient(timeout_seconds={self._timeout_seconds})"

    def converse(self, **kwargs: Any) -> Mapping[str, Any]:
        body = gemini_chat_request(**kwargs)
        payload = post_json(
            OPENROUTER_CHAT_URL,
            body,
            headers={"authorization": f"Bearer {self._api_key}"},
            timeout_seconds=self._timeout_seconds,
            api=_API,
        )
        return gemini_reply(payload, model_id=str(kwargs["modelId"]))


class ThreadLocalGeminiClients:
    """Create one client instance per reader thread, matching the existing worker contract."""

    def __init__(self, api_key: str, *, timeout_seconds: int = 180) -> None:
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._local = threading.local()

    def __repr__(self) -> str:
        return f"ThreadLocalGeminiClients(timeout_seconds={self._timeout_seconds})"

    def for_current_thread(self) -> OpenRouterGeminiClient:
        client = getattr(self._local, "client", None)
        if client is None:
            client = OpenRouterGeminiClient(self._api_key, timeout_seconds=self._timeout_seconds)
            self._local.client = client
        return client
