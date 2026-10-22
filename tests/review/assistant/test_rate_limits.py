"""One limiter per AI model (#1874): calls at once, the provider's minute, busy answers.

Plain code with a fake clock: no network, no paid call.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import anthropic
import httpx

from app.review.assistant.chat_provider import failure_from
from app.review.assistant.rate_limits import (
    GROW_AFTER,
    Limits,
    ModelLimiter,
    limiter_for,
    reset_limiters,
)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _limiter(at_once: int = 2, rpm: int = 1000, itpm: int = 10**9, otpm: int = 10**9) -> Any:
    clock = _Clock()
    return ModelLimiter("m", Limits(at_once, rpm, itpm, otpm), clock=clock), clock


def test_at_most_the_limit_runs_at_once_and_a_release_lets_the_next_in() -> None:
    limiter, _clock = _limiter(at_once=2)
    assert limiter.acquire() and limiter.acquire()
    cancel = threading.Event()
    got: list[bool] = []
    waiter = threading.Thread(target=lambda: got.append(limiter.acquire(cancel)))
    waiter.start()
    time.sleep(0.05)
    assert not got  # the third waits for a place
    limiter.release(fresh_input=10, output=5)
    waiter.join(2)
    assert got == [True]
    assert limiter.most_in_flight == 2


def test_a_busy_answer_halves_the_limit_and_answered_calls_grow_it_back() -> None:
    limiter, _clock = _limiter(at_once=8)
    assert limiter.acquire()
    limiter.release(busy=True)
    assert limiter.limit == 4 and limiter.busy == 1
    for _ in range(GROW_AFTER):
        assert limiter.acquire()
        limiter.release(fresh_input=1)
    assert limiter.limit == 5  # one more after GROW_AFTER answered calls in a row
    for _ in range(GROW_AFTER * 10):
        assert limiter.acquire()
        limiter.release(fresh_input=1)
    assert limiter.limit == 8  # never past the most


def test_the_minute_limits_hold_a_call_until_the_oldest_count_leaves() -> None:
    limiter, clock = _limiter(at_once=10, rpm=2)
    assert limiter.acquire() and limiter.acquire()
    limiter.release()
    limiter.release()
    cancel = threading.Event()
    got: list[bool] = []
    waiter = threading.Thread(target=lambda: got.append(limiter.acquire(cancel)))
    waiter.start()
    time.sleep(0.05)
    assert not got  # two requests in the last minute: the third waits
    clock.now += 61
    waiter.join(3)
    assert got == [True]


def test_fresh_input_counts_for_the_minute_and_cancel_ends_a_wait() -> None:
    limiter, _clock = _limiter(at_once=10, itpm=100)
    assert limiter.acquire()
    limiter.release(fresh_input=150, output=10)
    cancel = threading.Event()
    got: list[bool] = []
    waiter = threading.Thread(target=lambda: got.append(limiter.acquire(cancel)))
    waiter.start()
    time.sleep(0.05)
    cancel.set()
    waiter.join(3)
    assert got == [False]
    assert limiter.in_flight == 0


def test_a_specialist_holds_one_place_and_its_own_calls_take_none() -> None:
    limiter, _clock = _limiter(at_once=1)
    with limiter.agent_slot() as placed:
        assert placed and limiter.in_flight == 1
        # Its own call (any model's limiter) takes no place: never a wait on itself.
        assert not limiter.call_slot()
        other, _ = _limiter(at_once=1)
        assert not other.call_slot()
        assert limiter.acquire(slot=limiter.call_slot())
        limiter.release(slot=False, fresh_input=1)
        assert limiter.in_flight == 1
    assert limiter.in_flight == 0 and limiter.call_slot()


def test_the_server_keeps_one_limiter_per_model_and_new_settings_apply() -> None:
    reset_limiters()
    first = limiter_for("claude-sonnet-5-5", Limits.of("start", 32))
    assert limiter_for("claude-sonnet-5-5", Limits.of("start", 32)) is first
    assert limiter_for("claude-opus-5-5", Limits.of("start", 32)) is not first
    limiter_for("claude-sonnet-5-5", Limits.of("build", 64))
    assert first.limit == 64 and first.limits.requests_per_minute == 5_000
    reset_limiters()


def _rate_error(message: str) -> anthropic.RateLimitError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(429, request=request, json={"error": {"message": message}})
    return anthropic.RateLimitError(message, response=response, body=None)


def test_a_spending_limit_429_is_never_asked_again_but_a_rate_limit_is() -> None:
    spend = failure_from(_rate_error("You have reached your specified API usage limits."))
    assert spend.kind == "payment" and not spend.retryable
    busy = failure_from(
        _rate_error("This request would exceed the rate limit for your organization.")
    )
    assert busy.kind == "busy"
