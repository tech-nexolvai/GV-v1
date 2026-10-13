"""A reading stops cleanly at its first refusal, and an out-of-credit refusal is named (#1206).

Verification for `extraction/slot_reader/stop.py` and its use in `read_crops_parallel`
(`extraction/slot_reader/bedrock.py`). Synthetic jobs and fake readers only: no network, no model.

Before #1206 a spend-cap refusal turned into one "no answer" and the next question was asked anyway,
so refusals landed scattered over the pages; and a 402 was named `TooManyRequestsException` and
retried as throttling (when it mentioned OpenRouter's in-flight budget) or failed the package for
good. Now the first refusal stops the reading, every page left is known, and a 402 is
`ProviderAccountRefused`, sent once, never retried.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping, Sequence
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from urllib.error import HTTPError

import pytest

from evidence.crop import encode_png
from extraction.form_reader.bedrock import AttemptUsage
from extraction.form_reader.runner import _is_throttle
from extraction.slot_reader.anthropic import (
    AnthropicRequestError,
    ProviderAccountRefused,
    SpendCapExceeded,
    refused_unserved,
)
from extraction.slot_reader.bedrock import CropJob, read_crops_parallel
from extraction.slot_reader.openrouter import OPENROUTER_MESSAGES_URL, OpenRouterMessagesClient
from extraction.slot_reader.stop import ReadingStop

OPUS = "anthropic.claude-opus-5-5"
SONNET = "anthropic.claude-sonnet-5-5"
PRICE = SimpleNamespace(input_per_1k_tokens=Decimal("0.01"), output_per_1k_tokens=Decimal("0.05"))
PAGES = 6


class PricedRates:
    def rate_for(self, _model_id: str) -> object:
        return PRICE


class Readers:
    """Answers every span question `2"`; counts the calls actually sent, by page."""

    def __init__(self, refuse: BaseException | None = None) -> None:
        self.refuse = refuse
        self.sent: list[tuple[str, int]] = []
        self.lock = threading.Lock()

    def for_current_thread(self) -> Readers:
        return self

    def converse(self, **kwargs: Any) -> Mapping[str, Any]:
        with self.lock:
            self.sent.append((kwargs["modelId"], len(self.sent)))
        if self.refuse is not None:
            raise self.refuse
        payload = {
            "text": '2"',
            "stacked": False,
            "combined": False,
            "readable": True,
            "no_dimension": False,
            "belongs": "yes",
        }
        return {
            "stopReason": "end_turn",
            "output": {"message": {"content": [{"text": json.dumps(payload)}]}},
            "usage": {"inputTokens": 10, "outputTokens": 5},
        }


class RefusingGuard:
    """A spend guard with room for exactly `allowed` questions: the next one is refused."""

    def __init__(self, allowed: int) -> None:
        self.allowed = allowed
        self.reserved = 0
        self.lock = threading.Lock()

    def reserve(self, model_id: str, input_tokens: int, output_limit: int) -> object:
        with self.lock:
            if self.reserved >= self.allowed:
                raise SpendCapExceeded("Claude reader batch spend cap would be exceeded")
            self.reserved += 1
        return object()

    def settle(self, reservation: object, response: Mapping[str, Any]) -> None:
        return None

    def release(self, reservation: object) -> None:
        return None


def _jobs(pages: int = PAGES) -> list[CropJob]:
    """Two questions per page (both readers), in page order, as the reader builds a batch."""
    png = encode_png(2, 2, bytes(12))
    return [
        CropJob(f"p{page}:s0:0", model, page, png, png, grounded_claude=True)
        for page in range(pages)
        for model in (OPUS, SONNET)
    ]


def _read(
    jobs: Sequence[CropJob],
    readers: Readers,
    *,
    guard: object | None = None,
    stop: ReadingStop | None = None,
    retries: int = 0,
) -> tuple[dict[tuple[str, str], object], list[AttemptUsage]]:
    attempts: list[AttemptUsage] = []
    answers = read_crops_parallel(
        jobs,
        clients=readers,
        rates=PricedRates(),  # type: ignore[arg-type]
        calls_per_minute={OPUS: 60_000, SONNET: 60_000},
        max_concurrent_calls=1,
        max_tokens=400,
        max_throttle_retries=retries,
        retry_backoff_seconds=0.001,
        record_attempt=attempts.append,
        spend_guard=guard,  # type: ignore[arg-type]
        stop=stop,
    )
    return answers, attempts


# --- The clean stop ---------------------------------------------------------------------------


def test_the_first_budget_refusal_stops_the_reading_and_no_later_question_is_sent() -> None:
    """Room for pages 0-2 (six questions): page 3's first question is refused, and nothing after
    it is sent. Before #1206 every later question was still asked, one refusal each."""
    readers = Readers()
    stop = ReadingStop()

    answers, attempts = _read(_jobs(), readers, guard=RefusingGuard(allowed=6), stop=stop)

    assert len(readers.sent) == 6, "only the questions the budget had room for were sent"
    assert stop.stopped and stop.cause == "budget"
    assert stop.refused_count == 1
    assert stop.unasked_count == 2 * PAGES - 7
    assert stop.pages_left() == frozenset({3, 4, 5})
    # Pages 0-2 are answered; every question of pages 3-5 abstains, none is lost.
    assert len(answers) == 2 * PAGES
    assert all(
        answers[(f"p{page}:s0:0", model)] is not None
        for page in range(3)
        for model in (OPUS, SONNET)
    )
    assert all(
        answers[(f"p{page}:s0:0", model)] is None
        for page in range(3, PAGES)
        for model in (OPUS, SONNET)
    )
    # The refused question is recorded with its cause; the ones never sent are not calls.
    assert [attempt.failure_kind for attempt in attempts].count("SpendCapExceeded") == 1
    assert len(attempts) == 7


def test_a_stop_shared_by_two_batches_sends_nothing_in_the_second() -> None:
    readers = Readers()
    stop = ReadingStop()
    first, second = _jobs(3), [
        CropJob(job.key.replace("s0", "walls"), job.model_id, job.page_index, job.png, job.view_png)
        for job in _jobs(3)
    ]

    _read(first, readers, guard=RefusingGuard(allowed=2), stop=stop)
    sent = len(readers.sent)
    answers, _attempts = _read(second, readers, guard=RefusingGuard(allowed=100), stop=stop)

    assert len(readers.sent) == sent, "the second batch sent nothing"
    assert all(answer is None for answer in answers.values())
    # Page 0's first questions were answered, but its second batch's were not sent.
    assert stop.pages_left() == frozenset({0, 1, 2})


def test_with_room_for_everything_nothing_stops() -> None:
    readers = Readers()
    stop = ReadingStop()

    _read(_jobs(), readers, guard=RefusingGuard(allowed=100), stop=stop)

    assert len(readers.sent) == 2 * PAGES
    assert not stop.stopped and stop.pages_left() == frozenset()


# --- Out of credit (402) ----------------------------------------------------------------------


def test_out_of_credit_is_sent_once_never_retried_and_stops_everything_queued() -> None:
    """A 402 is the account, not a rate limit: with three throttle retries allowed, the refused
    question is still sent once, nothing queued behind it is sent, and the error ends the batch."""
    readers = Readers(refuse=ProviderAccountRefused(api="OpenRouter Messages API"))
    stop = ReadingStop()

    with pytest.raises(ProviderAccountRefused) as caught:
        _read(_jobs(), readers, stop=stop, retries=3)

    assert len(readers.sent) == 1
    assert stop.cause == "account"
    assert stop.pages_left() == frozenset(range(PAGES))
    assert not _is_throttle(caught.value)
    assert "TooManyRequests" not in str(caught.value)


@pytest.mark.parametrize(
    "body",
    [
        b'{"error":{"type":"billing_error","message":"Insufficient credits"}}',
        b'{"error":{"type":"billing_error","message":"in_flight_budget_exhausted"}}',
    ],
)
def test_every_402_from_the_provider_is_named_out_of_credit(body: bytes) -> None:
    from io import BytesIO

    error = HTTPError(OPENROUTER_MESSAGES_URL, 402, "payment required", {}, BytesIO(body))
    from tests.extraction.slot_reader.test_openrouter import REQUEST

    with (
        patch("extraction.slot_reader.anthropic.urlopen", side_effect=error),
        pytest.raises(ProviderAccountRefused) as caught,
    ):
        OpenRouterMessagesClient("private-test-key").converse(**REQUEST)

    assert isinstance(caught.value, AnthropicRequestError)
    assert caught.value.status_code == 402
    assert caught.value.response == {"Error": {"Code": "ProviderAccountRefused"}}
    assert not _is_throttle(caught.value)
    # Unserved and not charged: a spend reservation it held is given back.
    assert refused_unserved(caught.value)
