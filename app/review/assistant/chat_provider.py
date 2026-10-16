"""Where the chat's model calls go (#1463): Anthropic's own API, or OpenRouter, with the official SDK.

`GV_ASSISTANT_PROVIDER=auto` (the default) uses Anthropic's own API when `ANTHROPIC_API_KEY` is set,
else OpenRouter when `OPENROUTER_API_KEY` is set; `anthropic` or `openrouter` forces one. Both use
the `anthropic` SDK and stream:

- **Anthropic:** `client.beta.messages.create(stream=True)` with the server-side refusal fallback for
  Opus 5.5 and Sonnet 5.5 (`chat_request.call_params`), else `client.messages.create(stream=True)`.
  The client is given the key explicitly, so no other credential (a profile on disk, another
  environment variable) is ever used.
- **OpenRouter:** the same SDK pointed at `https://openrouter.ai/api` with the key as a bearer token
  (`auth_token`, so `ANTHROPIC_API_KEY` is never sent there), the readers' keep-no-data routing
  block in the body, no betas and no fallbacks.

The SDK's own retries on 429, 5xx and lost connections stay on (before a stream starts).

**Errors never carry the key or the request.** A failure becomes a `ProviderFailure` with a plain
sentence and the HTTP status (when there is one), never the provider's words. The key lives only in
the SDK client; it never reaches the sandbox, the browser, a log or an event.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol

from app.review.assistant.chat_request import OPENROUTER_BASE_URL, Provider

__all__ = [
    "AnthropicSdkProvider",
    "ChatProvider",
    "EventStream",
    "ProviderChoice",
    "ProviderFailure",
    "choose_provider",
    "failure_from",
]

_log = logging.getLogger(__name__)

#: The raw stream events the engine reads; everything else (pings, helper events) is skipped.
_RAW_EVENTS: Final = frozenset(
    {
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    }
)


class EventStream(Protocol):
    """One streamed call: its raw events as plain dicts, in the API's own shapes."""

    def __iter__(self) -> Iterator[dict[str, Any]]: ...

    def close(self) -> None: ...


class ChatProvider(Protocol):
    """Opens a streamed model call. Tests pass a scripted fake; production the SDK one."""

    name: Provider

    def open_stream(self, params: Mapping[str, Any]) -> EventStream: ...


@dataclass(frozen=True, slots=True)
class ProviderChoice:
    """Which provider a deployment uses, or why none (`reason`, in plain words)."""

    name: Provider
    key: str | None
    reason: str | None = None

    @property
    def available(self) -> bool:
        return self.key is not None

    def __repr__(self) -> str:
        return f"ProviderChoice(name={self.name!r}, available={self.available})"


def choose_provider(
    setting: Literal["auto", "anthropic", "openrouter"],
    anthropic_key: str | None,
    openrouter_key: str | None,
) -> ProviderChoice:
    """`auto`: Anthropic's own API when its key is set, else OpenRouter; a forced one needs its key."""
    anthropic_key = anthropic_key.strip() if anthropic_key and anthropic_key.strip() else None
    openrouter_key = openrouter_key.strip() if openrouter_key and openrouter_key.strip() else None
    if setting == "anthropic" or (setting == "auto" and anthropic_key):
        if anthropic_key:
            return ProviderChoice("anthropic", anthropic_key)
        return ProviderChoice(
            "anthropic",
            None,
            "The assistant is set to use Anthropic's API, but ANTHROPIC_API_KEY is not set on "
            "this server.",
        )
    if openrouter_key:
        return ProviderChoice("openrouter", openrouter_key)
    if setting == "openrouter":
        reason = (
            "The assistant is set to use OpenRouter, but OPENROUTER_API_KEY is not set on this "
            "server."
        )
    else:
        reason = (
            "No AI provider key is set on this server (ANTHROPIC_API_KEY or OPENROUTER_API_KEY), "
            "so the assistant cannot answer."
        )
    return ProviderChoice("openrouter", None, reason)


class ProviderFailure(Exception):
    """A model call that failed, in plain words. `retryable`: a 5xx, 520/529 or a lost
    connection, worth asking once more; `status`: the HTTP status when there was one."""

    def __init__(self, message: str, *, status: int | None, retryable: bool) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable


def failure_from(error: BaseException) -> ProviderFailure:
    """A provider error as a plain sentence: never the provider's words, the request or the key."""
    if isinstance(error, ProviderFailure):
        return error
    import anthropic

    status = getattr(error, "status_code", None)
    status = status if isinstance(status, int) else None
    if isinstance(error, anthropic.APITimeoutError):
        return ProviderFailure(
            "Claude did not answer in time. Please ask again.", status=None, retryable=True
        )
    if isinstance(error, anthropic.APIConnectionError):
        return ProviderFailure(
            "Claude could not be reached (the connection was lost). Please ask again.",
            status=None,
            retryable=True,
        )
    if isinstance(error, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
        return ProviderFailure(
            "The AI service refused this server's key, so Claude did not answer. Tell your admin.",
            status=status,
            retryable=False,
        )
    if isinstance(error, anthropic.RateLimitError):
        return ProviderFailure(
            "Claude is busy right now (too many requests). Please ask again in a minute.",
            status=status,
            retryable=False,
        )
    if status == 402:
        return ProviderFailure(
            "The AI service account needs attention (payment), so Claude did not answer. Tell "
            "your admin.",
            status=status,
            retryable=False,
        )
    if isinstance(error, anthropic.APIStatusError):
        if status is not None and status < 400:
            # An `error` event in the middle of a stream (overloaded, api_error): the HTTP
            # status is the stream's own 200.
            return ProviderFailure(
                "The AI service stopped in the middle of the answer. Please ask again.",
                status=None,
                retryable=True,
            )
        if status is not None and status >= 500:
            return ProviderFailure(
                f"The AI service had a problem (error {status}). Please ask again.",
                status=status,
                retryable=True,
            )
        return ProviderFailure(
            f"The AI service refused the request (error {status}).", status=status, retryable=False
        )
    if isinstance(error, anthropic.APIError):
        # An error event in the middle of a stream (overloaded, api_error) carries no status.
        return ProviderFailure(
            "The AI service stopped in the middle of the answer. Please ask again.",
            status=None,
            retryable=True,
        )
    return ProviderFailure(
        "Claude's answer could not be read. Please ask again.", status=None, retryable=False
    )


class _SdkStream:
    """The SDK's raw stream, as plain dicts."""

    def __init__(self, raw: Any) -> None:
        self._raw = raw

    def __iter__(self) -> Iterator[dict[str, Any]]:
        try:
            for event in self._raw:
                kind = getattr(event, "type", None)
                if kind in _RAW_EVENTS:
                    yield event.to_dict(mode="json")
        except Exception as error:  # noqa: BLE001 - every provider failure is told plainly
            raise failure_from(error) from None

    def close(self) -> None:
        try:
            self._raw.close()
        except Exception:  # noqa: BLE001 - closing a finished or broken stream is harmless
            _log.debug("assistant chat stream did not close cleanly")


class AnthropicSdkProvider:
    """The official SDK client for one provider. Holds the key; never shows it."""

    def __init__(self, choice: ProviderChoice, *, timeout_seconds: float) -> None:
        if choice.key is None:
            raise ValueError("the assistant has no provider key")
        import anthropic

        self.name: Provider = choice.name
        if choice.name == "anthropic":
            self._client = anthropic.Anthropic(
                api_key=choice.key, timeout=timeout_seconds, max_retries=2
            )
        else:
            self._client = anthropic.Anthropic(
                auth_token=choice.key,
                base_url=OPENROUTER_BASE_URL,
                timeout=timeout_seconds,
                max_retries=2,
            )

    def __repr__(self) -> str:
        return f"AnthropicSdkProvider(name={self.name!r})"

    def open_stream(self, params: Mapping[str, Any]) -> EventStream:
        call = dict(params)
        try:
            if "betas" in call or "fallbacks" in call:
                raw = self._client.beta.messages.create(**call, stream=True)
            else:
                raw = self._client.messages.create(**call, stream=True)
        except Exception as error:  # noqa: BLE001 - every provider failure is told plainly
            raise failure_from(error) from None
        return _SdkStream(raw)
