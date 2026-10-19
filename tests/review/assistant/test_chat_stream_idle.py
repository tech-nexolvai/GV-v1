"""A stream that sends nothing is stopped after about 3 minutes (owner decision 2026-10-19).

Before: a call's stream could stay silent for the call's whole time (15 minutes) and the answer
stayed "running"; when it did end, it said "Claude's answer could not be read". Now a stream that
sends nothing at all (no event, no `ping`: the socket's read time-out, so any byte starts it again)
for `GV_ASSISTANT_STREAM_IDLE_SECONDS` (180 s) ends the call with a plain reason, and what the call
used so far is recorded. A request the SDK already asked 3 times without an answer is not asked a
fourth time by the chat.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx2
import pytest

from app.review.assistant.chat_provider import (
    STREAM_IDLE_SECONDS,
    AnthropicSdkProvider,
    ProviderChoice,
    ProviderFailure,
    failure_from,
    idle_words,
)
from tests.review.assistant.chat_fakes import ScriptedProvider, reply, text
from tests.review.assistant.test_chat_engine import Harness

START = {
    "type": "message_start",
    "message": {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": [],
        "stop_reason": None,
        "usage": {"input_tokens": 40_000, "output_tokens": 1},
    },
}


class _Silent(httpx2.SyncByteStream):
    """`message_start`, then nothing until the socket's read time-out."""

    def __iter__(self) -> Iterator[bytes]:
        yield f"event: message_start\ndata: {json.dumps(START)}\n\n".encode()
        raise httpx2.ReadTimeout("no data")


def _provider(idle: float | None = None) -> AnthropicSdkProvider:
    import anthropic

    def answer(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200, headers={"content-type": "text/event-stream"}, stream=_Silent(), request=request
        )

    extra: dict[str, Any] = {} if idle is None else {"idle_seconds": idle}
    return AnthropicSdkProvider(
        ProviderChoice("anthropic", "sk-ant-test"),
        timeout_seconds=900,
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(answer)),
        **extra,
    )


def _params() -> dict[str, Any]:
    return {
        "model": "claude-opus-5-5",
        "max_tokens": 1000,
        "messages": [{"role": "user", "content": [{"type": "text", "text": "Hi"}]}],
    }


def test_the_stream_waits_about_three_minutes_for_any_byte_not_fifteen() -> None:
    import anthropic

    provider = _provider()

    timeout = provider._client.timeout
    assert isinstance(timeout, anthropic.Timeout)
    assert STREAM_IDLE_SECONDS == 180 and timeout.read == 180 and timeout.connect == 15
    assert _provider(120)._client.timeout.read == 120


def test_a_silent_stream_ends_with_a_plain_reason() -> None:
    stream = _provider(180).open_stream(_params())

    seen: list[str] = []
    with pytest.raises(ProviderFailure) as raised:
        for event in stream:
            seen += [event["type"]]

    assert seen == ["message_start"]
    failure = raised.value
    assert str(failure) == idle_words(180)
    assert "3 minutes" in str(failure) and failure.retryable is False
    assert failure.kind == "unreachable"


def test_a_silent_call_ends_the_answer_and_its_cost_is_recorded(tmp_path: Path) -> None:
    silent = reply(text("Thinking out loud"), usage={"input_tokens": 40_000})
    failure = ProviderFailure(idle_words(180), status=None, retryable=False, kind="unreachable")
    silent.insert(1, {"type": "x", "__raise__": failure})
    harness = Harness(
        tmp_path,
        ScriptedProvider(lead=[silent, reply(text("never asked"))]),
        question="How wide is the sink on page 4?",
    )

    outcome = harness.run()

    assert outcome.stopped == "error" and outcome.error == idle_words(180)
    (row,) = harness.ledger.rows
    assert row["input_tokens"] == 40_000 and row["error"] is not None
    assert outcome.cost_micros > 0


def test_a_request_the_sdk_already_asked_three_times_is_not_asked_again() -> None:
    import anthropic

    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")

    failure = failure_from(anthropic.APITimeoutError(request=request))

    assert failure.retryable is False and failure.kind == "unreachable"
    assert "did not answer in time" in str(failure)


def test_the_setting_reaches_the_provider() -> None:
    from app.config import Settings

    settings = Settings(
        database_url="postgresql+psycopg://unused@localhost/unused",
        environment="test",
        assistant_stream_idle_seconds=240,
    )

    assert settings.assistant_stream_idle_seconds == 240
    assert (
        Settings(database_url="postgresql+psycopg://u@localhost/u").assistant_stream_idle_seconds
        == 180
    )
