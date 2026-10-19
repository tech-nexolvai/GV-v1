"""What a call that fails in the middle cost (harness audit, 2026-10-19).

A call that breaks after its stream started (an `overloaded_error` event, a dropped connection) has
already been billed for its prompt (Anthropic reports it in `message_start`). Before this fix the
Usage row of such a call said 0 tokens and $0, and the message's runaway guard did not count it,
so a retried call could pass the cap by the size of its prompt. Now what it used so far is
recorded and charged, as for a call stopped by the reviewer.
"""

from __future__ import annotations

from pathlib import Path

from tests.review.assistant.chat_fakes import ScriptedProvider, reply, server_error, text
from tests.review.assistant.test_chat_engine import RATES, Harness


def _broken_after_start(input_tokens: int) -> list[dict[str, object]]:
    events = reply(text("Garbled start"), usage={"input_tokens": input_tokens})
    events.insert(3, {"type": "x", "__raise__": server_error()})
    return events


def test_a_call_that_fails_mid_stream_is_recorded_and_charged(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        lead=[_broken_after_start(50_000), reply(text("Fine now."), usage={"input_tokens": 1000})]
    )
    harness = Harness(tmp_path, provider, question="How wide is the sink on page 4?")

    outcome = harness.run()

    assert outcome.text == "Fine now." and outcome.stopped is None
    failed, answered = harness.ledger.rows
    assert failed["error"] is not None and failed["input_tokens"] == 50_000
    assert answered["error"] is None
    # The message's cost counts the failed call's prompt too ($4 per million on Opus 5.5).
    rate = RATES.rate_for("anthropic.claude-opus-5-5")
    assert rate is not None
    assert outcome.cost_micros >= 50_000 * 4


def test_a_call_that_fails_before_its_stream_records_nothing_billed(tmp_path: Path) -> None:
    provider = ScriptedProvider(lead=[server_error(), reply(text("Fine now."))])
    harness = Harness(tmp_path, provider, question="How wide is the sink on page 4?")

    harness.run()

    failed = harness.ledger.rows[0]
    assert failed["error"] is not None and failed["input_tokens"] == 0
