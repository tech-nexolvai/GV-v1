"""Where the chat's model calls go (#1463): Anthropic's own API, or OpenRouter, with the official SDK.

`GV_ASSISTANT_PROVIDER=auto` (the default) uses Anthropic's own API when `ANTHROPIC_API_KEY` is set,
else OpenRouter when `OPENROUTER_API_KEY` is set; `anthropic` or `openrouter` forces one. Both use
the `anthropic` SDK and stream:

- **Anthropic:** `client.beta.messages.create(stream=True)` with the server-side refusal fallback for
  Fable 5.1, Opus 5.5 and Sonnet 5.5, and the thinking-binding safety net on adaptive thinking
  (`chat_request.call_params`), else `client.messages.create(stream=True)`.
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
import re
import socket
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final, Literal, Protocol

from app.review.assistant.account_credit import ACCOUNT_CREDIT
from app.review.assistant.chat_request import OPENROUTER_BASE_URL, Provider

__all__ = [
    "CREDIT_NOT_STARTED",
    "CREDIT_STOP_WORDS",
    "RETENTION_WORDS",
    "STREAM_IDLE_SECONDS",
    "UNREACHABLE_WORDS",
    "AnthropicSdkProvider",
    "ChatProvider",
    "Credit",
    "EventStream",
    "ProviderChoice",
    "ProviderFailure",
    "ServiceBreaker",
    "choose_provider",
    "failure_from",
    "idle_words",
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


#: Claude Fable 5.1 on an account without 30-day data retention (a zero-data-retention account gets
#: a 400 on every request; an early draft of Anthropic's docs said 404).
RETENTION_WORDS: Final = (
    "Fable 5.1 needs 30-day data retention on this Anthropic account, and this account "
    "does not have it. Pick Opus 5.5, or ask your admin to turn on data retention."
)


def _says_retention(error: BaseException) -> bool:
    """Whether a provider error is about the account's data retention. Only matched, never shown:
    the provider's own words stay out of every message."""
    words = " ".join(
        str(part) for part in (getattr(error, "message", ""), getattr(error, "body", "")) if part
    )
    return "retention" in words.lower()


#: A helper not started because the AI account ran out of credit (its report, change 11).
CREDIT_NOT_STARTED: Final = "Not started: the AI account ran out of credit."
#: What a full check that stopped for credit says (its summary, trust problems and Continue).
CREDIT_STOP_WORDS: Final = "Stopped: the AI account ran out of credit. Top up, then Continue."


#: What a failure says about the AI service for this server: `key` (it refused the key, 401/403),
#: `payment` (402), `account` (the account cannot use the model: data retention), `unreachable`
#: (no answer: connection refused or lost, timed out). `None`: anything else.
type FailureKind = Literal["key", "payment", "account", "unreachable", "busy"]


class ProviderFailure(Exception):
    """A model call that failed, in plain words. `retryable`: a 5xx, 520/529 or a lost
    connection, worth asking once more; `status`: the HTTP status when there was one; `kind`:
    what it says about the service (`FailureKind`), for the breaker (`ServiceBreaker`)."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None,
        retryable: bool,
        kind: FailureKind | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.kind = kind


#: The service failures that will not mend within one answer: the first one stops new calls.
_DEAD_AT_ONCE: Final = frozenset({"key", "payment", "account"})
#: Calls in a row that could not reach the service (no answer between) before new calls stop.
UNREACHABLE_IN_A_ROW: Final = 3
UNREACHABLE_WORDS: Final = (
    "The AI service can't be reached from this server, so no more AI calls were started for "
    "this answer. Please try again in a few minutes; if it keeps happening, tell your admin."
)


class ServiceBreaker:
    """One answer's view of the AI service (harness audit 2026-10-19: with the service down, a full
    check retried each of its 39 calls for 6 minutes). Once the service has refused this server's
    key, asked for payment or refused the account, or could not be reached `UNREACHABLE_IN_A_ROW`
    times with no answer between, no new call is started: each fails at once, not retryable, with
    the same plain reason. Thread-safe (helpers and compare calls run at once)."""

    def __init__(self, unreachable_in_a_row: int = UNREACHABLE_IN_A_ROW) -> None:
        self._lock = threading.Lock()
        self._limit = unreachable_in_a_row
        self._misses = 0
        self._open: ProviderFailure | None = None

    @property
    def reason(self) -> str | None:
        """The plain reason no call is started any more, or `None`."""
        with self._lock:
            return None if self._open is None else str(self._open)

    @property
    def kind(self) -> FailureKind | None:
        """What stopped the calls (`payment`: the account ran out of credit), or `None`."""
        with self._lock:
            return None if self._open is None else self._open.kind

    def check(self) -> None:
        """Raises the stop's failure when calls are stopped."""
        with self._lock:
            stopped = self._open
        if stopped is not None:
            raise ProviderFailure(
                str(stopped), status=stopped.status, retryable=False, kind=stopped.kind
            )

    def failed(self, failure: ProviderFailure) -> None:
        with self._lock:
            if self._open is not None:
                return
            if failure.kind in _DEAD_AT_ONCE:
                self._open = failure
            elif failure.kind == "unreachable":
                self._misses += 1
                if self._misses >= self._limit:
                    self._open = ProviderFailure(
                        UNREACHABLE_WORDS, status=None, retryable=False, kind="unreachable"
                    )
            if self._open is not None:
                _log.warning(
                    "assistant chat: the AI service is %s for this answer; no new call starts",
                    "unreachable" if self._open.kind == "unreachable" else "refusing it",
                )

    def answered(self) -> None:
        """A call reached the service and was accepted: misses in a row start again."""
        with self._lock:
            self._misses = 0


#: A refusal that is a spending limit, not a bad key (OpenRouter: "Key limit exceeded").
_SPEND_LIMIT: Final = re.compile(r"\blimit\b|\bcredits?\b", re.IGNORECASE)
#: OpenRouter's 402 for one call whose worst case the balance cannot cover ("requires more
#: credits, or fewer max_tokens ... can only afford"): while other calls run, their worst cases
#: were in the way, so it is asked again later (`busy`), never every call of the answer stopped
#: (the La Quinta run of 2026-10-29 stopped 9 page reviewers at once with about $11 left); with
#: none running, the account is out of credit (`payment`, `account_credit`).
_AFFORD: Final = re.compile(r"can only afford|fewer max_tokens", re.IGNORECASE)


#: A 429 that is the account's spending limit, not a rate limit (Anthropic: "You have reached
#: your specified API usage limits"; #1874). Only looked at, never shown.
_SPEND_CAP: Final = re.compile(
    r"usage limits?\b|spend(?:ing)? (?:limit|cap)|credit balance|billing", re.IGNORECASE
)
_RATE_WORDS: Final = re.compile(r"rate limit|per[- ]minute|too many requests", re.IGNORECASE)


def _spend_cap(error: BaseException) -> bool:
    """Whether a 429 is the account's spending limit (its words say so and not a rate limit)."""
    words = str(getattr(error, "message", "") or error)
    return _SPEND_CAP.search(words) is not None and _RATE_WORDS.search(words) is None


def _is_refusal(error: BaseException) -> bool:
    """Whether a provider error is OpenRouter's policy refusal (`error.error_type: "refusal"`).
    Only looked at, never shown."""
    body = getattr(error, "body", None)
    inner = body.get("error") if isinstance(body, Mapping) else None
    return isinstance(inner, Mapping) and inner.get("error_type") == "refusal"


def failure_from(error: BaseException) -> ProviderFailure:
    """A provider error as a plain sentence: never the provider's words, the request or the key."""
    if isinstance(error, ProviderFailure):
        return error
    import anthropic

    status = getattr(error, "status_code", None)
    status = status if isinstance(status, int) else None
    if isinstance(error, anthropic.APITimeoutError):
        # The SDK has already asked 3 times (its own retries): asking again would only wait again.
        return ProviderFailure(
            "The AI service did not answer in time (it was asked 3 times), so this call was "
            "stopped. Please ask again.",
            status=None,
            retryable=False,
            kind="unreachable",
        )
    if isinstance(error, anthropic.APIConnectionError):
        return ProviderFailure(
            "The AI service can't be reached from this server (the connection failed). Please "
            "ask again.",
            status=None,
            retryable=True,
            kind="unreachable",
        )
    if isinstance(error, anthropic.PermissionDeniedError) and _is_refusal(error):
        # OpenRouter answers a prompt a model's provider declined with a 403 marked "refusal"
        # (its GPT-6 guide, 2026-10-25): the request, not the key; asking again gets the same.
        return ProviderFailure(
            "The AI service declined this request, so the AI did not answer it.",
            status=status,
            retryable=False,
        )
    if isinstance(error, anthropic.PermissionDeniedError) and _SPEND_LIMIT.search(
        str(getattr(error, "message", "") or error)
    ):
        # OpenRouter answers a key past its spending limit with a 403 (log forensics,
        # 2026-10-20): the key is fine, the account's limit was reached (#1811). The provider's
        # words are only looked at, never shown.
        return ProviderFailure(
            "The AI service account reached its spending limit, so the AI did not answer. Tell "
            "your admin.",
            status=status,
            retryable=False,
            kind="key",
        )
    if isinstance(error, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
        return ProviderFailure(
            "The AI service refused this server's key, so the AI did not answer. Tell your admin.",
            status=status,
            retryable=False,
            kind="key",
        )
    if isinstance(error, anthropic.RateLimitError) and _spend_cap(error):
        # A 429 that is the account's spending limit (no retry-after; "usage limits"), never a
        # busy service: asking again only waits again (#1874, production pipeline research).
        return ProviderFailure(
            "The AI service account reached its spending limit, so the AI did not answer. Tell "
            "your admin.",
            status=status,
            retryable=False,
            kind="payment",
        )
    if isinstance(error, anthropic.RateLimitError):
        # Not "retryable" (a moment's wait does not mend it), but `busy`: the engine asks again
        # a few times after a longer wait (#1866) before the reviewer sees these words.
        return ProviderFailure(
            "The AI is busy right now (too many requests). Please ask again in a minute.",
            status=status,
            retryable=False,
            kind="busy",
        )
    if status == 402:
        words = str(getattr(error, "message", "") or error)
        # The provider's words in the server log only (never shown): what it asked for.
        _log.warning("assistant chat: the AI service answered 402: %s", words[:300])
        if _AFFORD.search(words) and ACCOUNT_CREDIT.others_running():
            return ProviderFailure(
                "The AI is busy right now (too many requests). Please ask again in a minute.",
                status=status,
                retryable=False,
                kind="busy",
            )
        return ProviderFailure(
            "The AI service account needs attention (payment), so the AI did not answer. Tell "
            "your admin.",
            status=status,
            retryable=False,
            kind="payment",
        )
    if status in (400, 404) and _says_retention(error):
        return ProviderFailure(RETENTION_WORDS, status=status, retryable=False, kind="account")
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
        "The AI's answer could not be read. Please ask again.", status=None, retryable=False
    )


#: How long a call waits to connect to the AI service (the whole call may take much longer).
CONNECT_SECONDS: Final = 15.0
#: How long a call's stream may send nothing at all (no event, no `ping`) before it is stopped
#: (`GV_ASSISTANT_STREAM_IDLE_SECONDS`; owner decision 2026-10-19: about 3 minutes, was the
#: call's whole 15 minutes). Anthropic's streaming docs promise no `ping` interval ("may include
#: any number of ping events"), so the limit does not rest on pings: every call asks for
#: `display: "summarized"`, which streams the reasoning's summary as it thinks (only `omitted`
#: streams a thinking block empty), and in the live app's stored answers the longest whole
#: thinking block took 35 s (244 blocks, Opus 5.5 high). The limit is the socket's read timeout,
#: so any byte, a `ping` included, starts it again.
STREAM_IDLE_SECONDS: Final = 180.0


def idle_words(seconds: float) -> str:
    """What a call that sent nothing for `seconds` ends with, in plain words."""
    minutes = seconds / 60
    span = f"{minutes:g} minutes" if minutes >= 1 else f"{seconds:g} seconds"
    return (
        f"The AI service sent nothing for {span}, so this call was stopped (the connection "
        "looked dead). Please ask again."
    )


def _is_timeout(error: BaseException) -> bool:
    """An HTTP read, write or pool time-out raised while a stream is read (the SDK wraps time-outs
    only while it sends the request)."""
    return any(cls.__name__ == "TimeoutException" for cls in type(error).__mro__)


# How long `close` waits for a read it woke to return before closing the connection.
_WAKE_SECONDS: Final = 5.0


def _shut_down(raw: Any) -> None:
    """Shut the stream's connection down, so a read waiting on it in another thread returns at
    once. Closing the response alone does not wake that read (on macOS it waits until the call's
    read timeout, 15 minutes by default), so a cancelled message would never end."""
    try:
        network = raw.response.extensions.get("network_stream")
        sock = None if network is None else network.get_extra_info("socket")
        if sock is not None:
            sock.shutdown(socket.SHUT_RDWR)
    except Exception:  # noqa: BLE001 - best effort: an already closed connection is fine
        _log.debug("assistant chat stream connection was already shut")


class _SdkStream:
    """The SDK's raw stream, as plain dicts."""

    def __init__(self, raw: Any, idle_seconds: float = STREAM_IDLE_SECONDS) -> None:
        self._raw = raw
        self._idle_seconds = idle_seconds
        self._finished = False
        self._closed = False
        # Clear while a thread waits on the raw stream for its next event.
        self._idle = threading.Event()
        self._idle.set()

    def __iter__(self) -> Iterator[dict[str, Any]]:
        events = iter(self._raw)
        try:
            while True:
                self._idle.clear()
                try:
                    event = next(events, None)
                finally:
                    self._idle.set()
                if event is None:
                    break
                if getattr(event, "type", None) in _RAW_EVENTS:
                    yield event.to_dict(mode="json")
        except Exception as error:  # noqa: BLE001 - every provider failure is told plainly
            if self._closed:
                raise _stopped() from None
            if _is_timeout(error):
                # Nothing at all arrived for the idle limit: the stream is dead. Not asked again
                # (that could wait as long once more); what it used so far is still recorded.
                raise ProviderFailure(
                    idle_words(self._idle_seconds),
                    status=None,
                    retryable=False,
                    kind="unreachable",
                ) from None
            raise failure_from(error) from None
        if self._closed:
            # A shut connection can read as the body's clean end: still a stop, never an answer.
            raise _stopped()
        self._finished = True

    def close(self) -> None:
        """Close the stream; one not read to its end (a cancel, from another thread) has its
        connection shut down first, so the read waiting on it stops now. The connection is closed
        only once that read has returned: closed under a read just woken, the read can wait again
        on the dead connection until the call's read timeout."""
        if not self._finished:
            self._closed = True
            _shut_down(self._raw)
            self._idle.wait(_WAKE_SECONDS)
        try:
            self._raw.close()
        except Exception:  # noqa: BLE001 - closing a finished or broken stream is harmless
            _log.debug("assistant chat stream did not close cleanly")


def _stopped() -> ProviderFailure:
    """A stream closed before its end (a cancel): it stopped, it did not answer."""
    return ProviderFailure(
        "The AI's answer was stopped before its end.", status=None, retryable=False
    )


@dataclass(frozen=True, slots=True)
class Credit:
    """The AI account's credit as its provider reports it (`AnthropicSdkProvider.credit`): `ok`
    with the dollars left, `unknown` (it could not be read), or `not_offered` (Anthropic's own
    API has no balance to read)."""

    state: Literal["ok", "unknown", "not_offered"]
    usd: Decimal | None


class AnthropicSdkProvider:
    """The official SDK client for one provider. Holds the key; never shows it."""

    def __init__(
        self,
        choice: ProviderChoice,
        *,
        timeout_seconds: float,
        idle_seconds: float = STREAM_IDLE_SECONDS,
        http_client: Any = None,
    ) -> None:
        """`idle_seconds`: how long a stream may send nothing (`STREAM_IDLE_SECONDS`).
        `http_client` is for tests only (a fake transport: no network)."""
        if choice.key is None:
            raise ValueError("the assistant has no provider key")
        import anthropic

        self.name: Provider = choice.name
        #: This answer's view of the service: a provider is made for each message.
        self.breaker = ServiceBreaker()
        # Only for the free credit check before a full check (`credit`), sent only to OpenRouter.
        self._credit_key: str | None = choice.key if choice.name == "openrouter" else None
        extra: dict[str, Any] = {} if http_client is None else {"http_client": http_client}
        # A host that does not answer at all fails after `CONNECT_SECONDS`, and a stream that
        # sends nothing (no event, no ping) after `idle_seconds`, not after the whole call's time
        # (15 minutes) three times over.
        self.idle_seconds = min(idle_seconds, timeout_seconds)
        timeout = anthropic.Timeout(
            timeout_seconds,
            connect=min(CONNECT_SECONDS, timeout_seconds),
            read=self.idle_seconds,
        )
        if choice.name == "anthropic":
            self._client = anthropic.Anthropic(
                api_key=choice.key, timeout=timeout, max_retries=2, **extra
            )
        else:
            self._client = anthropic.Anthropic(
                auth_token=choice.key,
                base_url=OPENROUTER_BASE_URL,
                timeout=timeout,
                max_retries=2,
                **extra,
            )

    def __repr__(self) -> str:
        return f"AnthropicSdkProvider(name={self.name!r})"

    def credit(self) -> Credit:
        """What the AI account can still spend, before a paid full check (harness pre-check,
        change 11): OpenRouter's free, read-only credit check (`provider_usage.credit_left`).
        Anthropic's own API offers no balance to read: `not_offered`."""
        if self._credit_key is None:
            return Credit("not_offered", None)
        from app.provider_usage import credit_left

        left = credit_left(self._credit_key)
        return Credit("unknown", None) if left is None else Credit("ok", left)

    def open_stream(self, params: Mapping[str, Any]) -> EventStream:
        self.breaker.check()
        call = dict(params)
        try:
            if "betas" in call or "fallbacks" in call:
                raw = self._client.beta.messages.create(**call, stream=True)
            else:
                raw = self._client.messages.create(**call, stream=True)
        except Exception as error:  # noqa: BLE001 - every provider failure is told plainly
            failure = failure_from(error)
            self.breaker.failed(failure)
            raise failure from None
        self.breaker.answered()
        return _SdkStream(raw, self.idle_seconds)
