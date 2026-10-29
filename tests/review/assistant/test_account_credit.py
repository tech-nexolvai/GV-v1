"""The AI account's credit held at each running call's worst case (`account_credit`).

The La Quinta run of 2026-10-29 started 9 page reviewers and the helpers together with about $16
of OpenRouter credit; OpenRouter refused every call (402) at $5.45 spent, because the balance could
not cover the worst cases of the calls running. A call that does not fit next to the calls running
now waits for one to end; OpenRouter's "can only afford" 402 is a busy answer, never a dead account.
"""

from __future__ import annotations

import threading
import time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx2
import pytest

from app.review.assistant import chat_engine
from app.review.assistant.account_credit import ACCOUNT_CREDIT, AccountCredit
from app.review.assistant.chat_provider import Credit, failure_from
from tests.review.assistant.chat_fakes import ScriptedProvider, reply, text
from tests.review.assistant.test_chat_engine import Harness

DOLLAR = 1_000_000


def _with_credit(usd: str) -> Any:
    return SimpleNamespace(credit=lambda: Credit("ok", Decimal(usd)))


def test_a_call_that_does_not_fit_next_to_the_running_ones_waits_for_one_to_end() -> None:
    account = AccountCredit()
    account.start(_with_credit("5"))  # $5, $1 kept spare: $4 for the calls running at once
    cancel = threading.Event()
    first = account.hold(3 * DOLLAR, cancel)
    assert first == 3 * DOLLAR
    got: list[float] = []

    def second() -> None:
        account.hold(2 * DOLLAR, cancel)
        got.append(time.monotonic())

    waiting = threading.Thread(target=second)
    waiting.start()
    time.sleep(0.3)
    assert not got  # $3 + $2 > $4: it waits
    ended = time.monotonic()
    account.done(first, int(0.5 * DOLLAR))
    waiting.join(timeout=5)
    assert got and got[0] >= ended  # it went once the first call ended


def test_a_call_too_big_even_alone_goes_ahead_and_no_balance_holds_nothing() -> None:
    account = AccountCredit()
    account.start(_with_credit("1.50"))
    cancel = threading.Event()
    assert account.hold(3 * DOLLAR, cancel) == 3 * DOLLAR  # nothing running: never held back
    account.done(3 * DOLLAR, DOLLAR)
    # A provider with no balance to read (Anthropic's own API, a fake): the old balance goes.
    account.start(SimpleNamespace())
    assert account.hold(50 * DOLLAR, cancel) == 0  # counted as running, nothing held
    unknown = AccountCredit()
    unknown.start(SimpleNamespace(credit=lambda: Credit("unknown", None)))
    assert unknown.hold(50 * DOLLAR, cancel) == 0


def test_the_balance_is_read_again_each_minute_and_at_an_answers_first_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.review.assistant import account_credit

    account = AccountCredit()
    account.start(_with_credit("10"))
    cancel = threading.Event()
    held = account.hold(4 * DOLLAR, cancel)
    account.start(_with_credit("100"))  # read under a minute ago: the count is kept
    assert account._left == 10 * DOLLAR
    account.done(held, 2 * DOLLAR)
    assert account._left == 8 * DOLLAR  # what the call cost is taken off
    # A top-up is seen while calls run: at an answer's first call, or once the read is old.
    account.hold(DOLLAR, cancel)
    account.start(_with_credit("30"), force=True)
    assert account._left == 30 * DOLLAR
    monkeypatch.setattr(account_credit, "READ_EVERY_S", 0.0)
    account.start(_with_credit("7.25"))
    assert account._left == 7_250_000
    # A read that fails holds nothing (calls run as before), never an old balance.

    def broken() -> Credit:
        raise RuntimeError("no network")

    account.start(SimpleNamespace(credit=broken))
    assert account._left is None


def test_a_cancel_never_leaves_a_call_waiting() -> None:
    account = AccountCredit()
    account.start(_with_credit("3"))
    cancel = threading.Event()
    account.hold(2 * DOLLAR, cancel)
    cancel.set()
    started = time.monotonic()
    account.hold(2 * DOLLAR, cancel)
    assert time.monotonic() - started < 1


def test_every_call_of_an_answer_is_held_and_let_go(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, int, int]] = []

    class _Spy(AccountCredit):
        def hold(self, worst: int | None, cancel: threading.Event) -> int:
            seen.append(("hold", int(worst or 0), 0))
            return int(worst or 0)

        def done(self, held: int, cost: int) -> None:
            seen.append(("done", held, cost))

    monkeypatch.setattr(chat_engine, "ACCOUNT_CREDIT", _Spy())
    provider = ScriptedProvider(lead=[reply(text("All good."))])
    Harness(tmp_path, provider).run()
    holds = [s for s in seen if s[0] == "hold"]
    dones = [s for s in seen if s[0] == "done"]
    assert holds and len(holds) == len(dones)
    assert all(h[1] > 0 for h in holds)  # each call held at its worst case
    assert [d[1] for d in dones] == [h[1] for h in holds]


def test_openrouters_can_only_afford_402_is_busy_only_while_other_calls_run(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import anthropic

    request = httpx2.Request("POST", "https://openrouter.ai/api/v1/messages")

    def afford() -> Any:
        return failure_from(
            anthropic.APIStatusError(
                "This request requires more credits, or fewer max_tokens. You requested up to "
                "64000 tokens, but can only afford 12000.",
                response=httpx2.Response(402, request=request),
                body=None,
            )
        )

    cancel = threading.Event()
    # Only the asking call runs: the account cannot pay for even one call, it is out of credit.
    mine = ACCOUNT_CREDIT.hold(0, cancel)
    try:
        assert afford().kind == "payment"
        other = ACCOUNT_CREDIT.hold(0, cancel)
        try:
            busy = afford()  # another call's worst case was in the way: ask again later
        finally:
            ACCOUNT_CREDIT.done(other, 0)
    finally:
        ACCOUNT_CREDIT.done(mine, 0)
    assert busy.kind == "busy" and busy.status == 402
    assert "can only afford" in caplog.text  # the provider's words, in the server log only
    assert "can only afford" not in str(busy)
    broke = failure_from(
        anthropic.APIStatusError(
            "Insufficient credits", response=httpx2.Response(402, request=request), body=None
        )
    )
    assert broke.kind == "payment"
