"""The two Claude readers through OpenRouter (#1094): the same models, the same question.

OpenRouter takes Anthropic's Messages API format (`POST /api/v1/messages`), answer schema and
effort included, so the request is the one `anthropic.py` builds, sent with a bearer key (no
`anthropic-version` header), with three differences:

- **The model by OpenRouter's name.** Only Opus 5.5 and Sonnet 5.5 are mapped; any other id is
  refused before a call.
- **Where it runs.** Only Google Vertex's global endpoint. On OpenRouter, the hosts that keep no
  data (zero data retention) for these two models are Google Vertex and Amazon Bedrock, and
  Bedrock cannot hold Claude to a strict answer schema (decision log 2026-10-06, "Models via
  OpenRouter"). No other host is tried, and none that would ignore a parameter of the request.
  Its prices are the ones in `deploy/model_rates.us-east-1.json`.
- **No image `transformations`.** It is not in OpenRouter's schema. The local size check still
  refuses a picture the model would resize, before any call.

**No model fallback, ever.** `models` and `fallbacks` are never sent, and a reply that does not
name the model asked passes on no text: a reading must stay attributable to the model that made
it, because the two-reader rule seals a value only when two *named* models print the identical
text. Everything else (only a finished turn's text, sanitized errors, the spend cap) is shared
with the direct route.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Final

from extraction.slot_reader.anthropic import messages_reply, messages_request, post_json

OPENROUTER_MESSAGES_URL: Final = "https://openrouter.ai/api/v1/messages"
_API: Final = "OpenRouter Messages API"

#: The slot reader's model ids, by OpenRouter's names for the same models.
OPENROUTER_MODELS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "anthropic.claude-opus-5-5": "anthropic/claude-opus-5.5",
        "anthropic.claude-sonnet-5-5": "anthropic/claude-sonnet-5.5",
    }
)


#: The only hosts the two models may run on: Google Vertex's global endpoint.
OPENROUTER_HOSTS: Final = ("google-vertex/global",)
#: The route in a run's identity, so a reading says where it was made.
OPENROUTER_ROUTE: Final = f"openrouter:{'+'.join(OPENROUTER_HOSTS)}"
#: A dated snapshot of a model: `-20260921` (OpenRouter, Anthropic) or `@20260921` (Vertex).
_SNAPSHOT: Final = re.compile(r"(?:[-@]\d{8})?")


def _routing() -> dict[str, object]:
    """Google Vertex's global endpoint only, keeping no data, honouring every parameter."""
    return {
        "only": list(OPENROUTER_HOSTS),
        "allow_fallbacks": False,
        "require_parameters": True,
        "data_collection": "deny",
        "zdr": True,
    }


def openrouter_messages_request(**kwargs: Any) -> dict[str, object]:
    """The slot reader's Converse-shaped request as OpenRouter's Messages API body."""
    model_id = kwargs.get("modelId")
    model = OPENROUTER_MODELS.get(model_id) if isinstance(model_id, str) else None
    if model is None:
        raise ValueError("Claude through OpenRouter reads only with Opus 5.5 and Sonnet 5.5")
    body = messages_request(kwargs, model=model, refuse_resize=False)
    body["provider"] = _routing()
    return body


def _answered_by(reported: object, model_id: str) -> bool:
    """Whether the reply names the model asked: by OpenRouter's name or Anthropic's own, exactly
    or as a dated snapshot (`anthropic/claude-opus-5.5-20260921`). Nothing else: not another
    model, and not a variant of this one (`-fast`, `-mini`)."""
    if not isinstance(reported, str):
        return False
    names = (OPENROUTER_MODELS[model_id], model_id.removeprefix("anthropic."))
    return any(
        reported.startswith(name) and _SNAPSHOT.fullmatch(reported, len(name)) is not None
        for name in names
    )


class OpenRouterMessagesClient:
    """One thread-owned OpenRouter client. The key is never included in repr or errors."""

    def __init__(self, api_key: str, *, timeout_seconds: int = 180) -> None:
        if not api_key.strip():
            raise ValueError(
                "Claude through OpenRouter is enabled but OPENROUTER_API_KEY is not set"
            )
        if timeout_seconds <= 0:
            raise ValueError("OpenRouter request timeout must be positive")
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds

    def converse(self, **kwargs: Any) -> Mapping[str, Any]:
        body = openrouter_messages_request(**kwargs)
        payload = post_json(
            OPENROUTER_MESSAGES_URL,
            body,
            headers={"authorization": f"Bearer {self._api_key}"},
            timeout_seconds=self._timeout_seconds,
            api=_API,
        )
        model_id = str(kwargs["modelId"])
        return messages_reply(
            payload, answered=_answered_by(payload.get("model"), model_id), api=_API
        )


class ThreadLocalOpenRouterClients:
    """Create one client instance per reader thread, matching the existing worker contract."""

    def __init__(self, api_key: str, *, timeout_seconds: int = 180) -> None:
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._local = threading.local()

    def for_current_thread(self) -> OpenRouterMessagesClient:
        client = getattr(self._local, "client", None)
        if client is None:
            client = OpenRouterMessagesClient(self._api_key, timeout_seconds=self._timeout_seconds)
            self._local.client = client
        return client


__all__ = [
    "OPENROUTER_MESSAGES_URL",
    "OPENROUTER_MODELS",
    "OPENROUTER_ROUTE",
    "OpenRouterMessagesClient",
    "ThreadLocalOpenRouterClients",
    "openrouter_messages_request",
]
