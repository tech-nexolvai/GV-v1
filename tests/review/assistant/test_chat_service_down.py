"""A dead or refusing AI service fails fast (harness audit, 2026-10-19).

With the AI service down, a full check retried each of its 39 compare calls (and the SDK retried
each try) for 6 minutes before giving up; with the key refused, it still sent all 39 calls. Now one
answer stops starting calls once the service has refused this server's key (401/403), asked for
payment (402) or refused the account, or could not be reached three times in a row with no answer
between: every later call fails at once, not retryable, with the same plain reason, which the
answer then says. A busy service (429/529) is not a dead one: it is asked again as before.
"""

from __future__ import annotations

from typing import Any

import httpx2
import pytest

from app.review.assistant.chat_engine import MessageOutcome
from app.review.assistant.chat_provider import (
    CONNECT_SECONDS,
    UNREACHABLE_WORDS,
    AnthropicSdkProvider,
    ProviderChoice,
    ProviderFailure,
    ServiceBreaker,
)
from app.review.assistant.chat_service import _say_service_stop

KEY_WORDS = "The AI service refused this server's key, so Claude did not answer. Tell your admin."


def _params() -> dict[str, Any]:
    return {
        "model": "claude-opus-5-5",
        "max_tokens": 1000,
        "messages": [{"role": "user", "content": [{"type": "text", "text": "Hi"}]}],
    }


class _Counting:
    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.calls = 0

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.calls += 1
        result = self.answer(request)
        if isinstance(result, Exception):
            raise result
        return result


def _provider(transport: _Counting) -> AnthropicSdkProvider:
    import anthropic

    provider = AnthropicSdkProvider(
        ProviderChoice("anthropic", "sk-ant-test"),
        timeout_seconds=60,
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(transport)),
    )
    # The SDK's own retries (with their waits) are not what is tested here.
    provider._client = provider._client.with_options(max_retries=0)
    return provider


def _refused_key(request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(
        401,
        json={"type": "error", "error": {"type": "authentication_error", "message": "bad key"}},
        request=request,
    )


def test_a_refused_key_stops_every_later_call_without_asking_again() -> None:
    transport = _Counting(_refused_key)
    provider = _provider(transport)

    for _ in range(5):
        with pytest.raises(ProviderFailure) as raised:
            provider.open_stream(_params())
        assert str(raised.value) == KEY_WORDS and raised.value.retryable is False

    assert transport.calls == 1


def test_a_service_that_cannot_be_reached_stops_after_three_misses_in_a_row() -> None:
    transport = _Counting(lambda request: httpx2.ConnectError("refused", request=request))
    provider = _provider(transport)

    failures = []
    for _ in range(6):
        with pytest.raises(ProviderFailure) as raised:
            provider.open_stream(_params())
        failures.append(raised.value)

    assert transport.calls == 3
    assert all(f.retryable for f in failures[:3])
    assert [str(f) for f in failures[3:]] == [UNREACHABLE_WORDS] * 3
    assert not any(f.retryable for f in failures[3:])


def test_a_busy_service_is_not_a_dead_one() -> None:
    def overloaded(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            529, json={"type": "error", "error": {"type": "overloaded_error"}}, request=request
        )

    transport = _Counting(overloaded)
    provider = _provider(transport)

    for _ in range(5):
        with pytest.raises(ProviderFailure) as raised:
            provider.open_stream(_params())
        assert raised.value.retryable is True

    assert transport.calls == 5 and provider.breaker.reason is None


def test_misses_with_an_answer_between_do_not_stop_the_calls() -> None:
    breaker = ServiceBreaker()
    unreachable = ProviderFailure("x", status=None, retryable=True, kind="unreachable")

    for _ in range(10):
        breaker.failed(unreachable)
        breaker.failed(unreachable)
        breaker.answered()

    breaker.check()
    assert breaker.reason is None


def test_a_host_that_never_answers_fails_after_the_connect_time() -> None:
    import anthropic

    provider = AnthropicSdkProvider(ProviderChoice("anthropic", "sk-ant-test"), timeout_seconds=900)

    timeout = provider._client.timeout
    assert isinstance(timeout, anthropic.Timeout)
    assert timeout.connect == CONNECT_SECONDS and timeout.read == 900


def _outcome(**changes: Any) -> MessageOutcome:
    base: dict[str, Any] = {"text": "", "blocks": [], "api_content": [], "cost_micros": 0}
    return MessageOutcome(**{**base, **changes})


class _Stopped:
    def __init__(self, reason: str | None) -> None:
        self.breaker = ServiceBreaker()
        if reason is not None:
            self.breaker.failed(ProviderFailure(reason, status=401, retryable=False, kind="key"))


def test_a_full_check_says_why_its_ai_calls_stopped() -> None:
    outcome = _outcome(text="Checked 24 of 60 pages.", blocks=[{"type": "text", "text": "x"}])

    _say_service_stop(_Stopped(KEY_WORDS), outcome)

    said = f"**AI calls stopped:** {KEY_WORDS}"
    assert outcome.text == f"Checked 24 of 60 pages.\n\n{said}"
    assert outcome.blocks[-1] == {"type": "text", "text": said}
    # Said once, even when the words also sit inside a longer line (a work item's reason).
    _say_service_stop(_Stopped(KEY_WORDS), outcome)
    assert outcome.text.count(said) == 1


def test_a_failed_answer_carries_the_reason_once() -> None:
    told = _outcome(stopped="error", error=KEY_WORDS)
    _say_service_stop(_Stopped(KEY_WORDS), told)
    assert told.error == KEY_WORDS and told.blocks == []

    silent = _outcome(stopped="error")
    _say_service_stop(_Stopped(KEY_WORDS), silent)
    assert silent.error == KEY_WORDS

    stopped = _outcome(stopped="cancelled")
    _say_service_stop(_Stopped(KEY_WORDS), stopped)
    assert stopped.text == "" and stopped.blocks == []

    fine = _outcome(text="All good.")
    _say_service_stop(_Stopped(None), fine)
    assert fine.text == "All good."
