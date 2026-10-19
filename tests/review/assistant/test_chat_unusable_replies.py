"""Replies the chat cannot use as an answer (harness audit, 2026-10-19).

Anthropic's API sometimes ends a turn with no content at all (`end_turn`, mostly right after tool
results), a reply can hold only thinking, and a stream can end without its `message_delta` (the
connection closed cleanly in the middle). Before this fix each was stored as a finished answer:
the reviewer saw a blank or half answer with no word of why, and the empty or unsigned assistant
message went into the chat's history, so every later question in that chat failed with a 400.

- An empty or thinking-only reply is not sent back; the model is asked once more for its answer.
- Twice empty: a plain error, and nothing empty enters the history.
- A stream without its stop is a failure (asked once more, like a 5xx), its partial words dropped.
- No assistant message without words or a tool call is ever stored in `api_content`, and a chat
  whose stored history already holds one replays without it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.review.assistant.chat_engine import EMPTY_REPLY_NOTE, NO_ANSWER_ERROR, _Agent, _Share
from app.review.assistant.chat_request import MODELS
from app.review.assistant.chat_store import replayable_messages
from tests.review.assistant.chat_fakes import SONNET, ScriptedProvider, reply, text, thinking, tool
from tests.review.assistant.test_chat_engine import DOLLARS, Harness


def _sent(raw: str) -> dict[str, Any]:
    result: dict[str, Any] = json.loads(raw)
    return result


def _no_unusable_message(messages: list[dict[str, Any]]) -> bool:
    """What the API refuses in a history (a message with no content, an empty text block, a
    thinking block without its signature), and an assistant message with neither words nor a tool
    call (an empty or thinking-only reply: nothing to send back)."""
    for message in messages:
        content = message.get("content")
        if content in ([], "", None):
            return False
        if (
            message.get("role") == "assistant"
            and isinstance(content, list)
            and not any(
                (b.get("type") == "text" and str(b.get("text") or "").strip())
                or b.get("type") in ("tool_use", "server_tool_use")
                for b in content
            )
        ):
            return False
        for block in content if isinstance(content, list) else []:
            if block.get("type") == "text" and not block.get("text"):
                return False
            if block.get("type") == "thinking" and not block.get("signature"):
                return False
    return True


def _cut(*blocks: dict[str, Any]) -> list[dict[str, Any]]:
    """A reply whose stream ended cleanly in the middle: no `message_delta`, no `message_stop`."""
    events = reply(*blocks)
    return [e for e in events if e["type"] not in ("message_delta", "message_stop")]


def test_an_empty_reply_is_asked_once_more_and_never_enters_the_history(tmp_path: Path) -> None:
    provider = ScriptedProvider(lead=[reply(), reply(text("Nothing is different there."))])
    harness = Harness(tmp_path, provider, question="How wide is the sink on page 4?")

    outcome = harness.run()

    assert outcome.stopped is None and outcome.error is None
    assert outcome.text == "Nothing is different there."
    second = _sent(provider.lead_calls[1])["messages"]
    assert second[-1]["content"][-1]["text"] == EMPTY_REPLY_NOTE
    assert _no_unusable_message(second)
    assert _no_unusable_message(outcome.api_content)


def test_a_reply_with_only_thinking_is_asked_once_more(tmp_path: Path) -> None:
    provider = ScriptedProvider(lead=[reply(thinking("Hmm.")), reply(text("Answer."))])
    harness = Harness(tmp_path, provider)

    outcome = harness.run()

    assert outcome.text == "Answer." and outcome.stopped is None
    assert _no_unusable_message(_sent(provider.lead_calls[1])["messages"])
    assert _no_unusable_message(outcome.api_content)


def test_an_empty_reply_after_tool_results_is_asked_once_more(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        lead=[
            reply(tool("a", "bash_tool", {"command": "ls", "description": "x"}), stop="tool_use"),
            reply(),
            reply(text("Done.")),
        ]
    )
    harness = Harness(tmp_path, provider)

    outcome = harness.run()

    assert outcome.text == "Done." and outcome.stopped is None
    assert _no_unusable_message(_sent(provider.lead_calls[2])["messages"])
    assert _no_unusable_message(outcome.api_content)


def test_twice_empty_ends_with_a_plain_error_and_nothing_empty_is_stored(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        lead=[
            reply(tool("a", "bash_tool", {"command": "ls", "description": "x"}), stop="tool_use"),
            reply(),
            reply(thinking("Still nothing.")),
        ]
    )
    harness = Harness(tmp_path, provider)

    outcome = harness.run()

    assert outcome.stopped == "error" and outcome.error == NO_ANSWER_ERROR
    assert len(provider.lead_calls) == 3  # asked once more, not again and again
    assert outcome.api_content and _no_unusable_message(outcome.api_content)


def test_a_first_reply_that_is_empty_twice_leaves_the_history_untouched(tmp_path: Path) -> None:
    provider = ScriptedProvider(lead=[reply(), reply()])
    harness = Harness(tmp_path, provider)

    outcome = harness.run()

    assert outcome.stopped == "error" and outcome.error == NO_ANSWER_ERROR
    # An unanswered question never enters the history (as after a refusal).
    assert outcome.api_content == []


def test_a_forced_final_reply_with_only_thinking_stores_no_unusable_message(
    tmp_path: Path,
) -> None:
    provider = ScriptedProvider(
        lead=[
            reply(tool("a", "bash_tool", {"command": "ls", "description": "x"}), stop="tool_use"),
            reply(thinking("Hmm.")),
        ]
    )
    harness = Harness(tmp_path, provider, lead_turns=1)

    outcome = harness.run()

    assert outcome.stopped == "turn_limit" and "tool turns" in outcome.text
    assert _no_unusable_message(outcome.api_content)


def test_a_stream_that_ends_without_its_stop_is_asked_once_more(tmp_path: Path) -> None:
    cut = _cut(thinking("Half a thought", signature=""), text("Partial answer that was cut"))
    provider = ScriptedProvider(lead=[cut, reply(text("Whole answer."))])
    harness = Harness(tmp_path, provider)

    outcome = harness.run()

    assert outcome.stopped is None and outcome.text == "Whole answer."
    assert [b["text"] for b in outcome.blocks if b["type"] == "text"] == ["Whole answer."]
    assert _no_unusable_message(outcome.api_content)
    # The cut call is recorded as failed, with what it used so far charged.
    first = harness.ledger.rows[0]
    assert first["error"] is not None and first["input_tokens"] > 0


def test_a_stream_cut_twice_stops_with_a_plain_error(tmp_path: Path) -> None:
    provider = ScriptedProvider(lead=[_cut(text("Part")), _cut(text("Part again"))])
    harness = Harness(tmp_path, provider)

    outcome = harness.run()

    assert outcome.stopped == "error"
    assert outcome.error == "The AI service stopped in the middle of the answer. Please ask again."
    assert outcome.api_content == []


def test_a_roster_helper_never_sends_an_empty_reply_back(tmp_path: Path) -> None:
    brief = "PAGES: compare pages 3 and 4."
    words = "Report: pages 3 and 4 compared, nothing different."
    provider = ScriptedProvider(
        helpers={brief: [reply(model=SONNET)] + [reply(text(words), model=SONNET)] * 3}
    )
    harness = Harness(tmp_path, provider, message_cap=5 * DOLLARS)
    agent = _Agent(
        call_id="h1",
        description="pages 3-4",
        prompt=brief,
        model=MODELS["claude-sonnet-5-5"],
        number=1,
        pages=(3, 4),
        soft_cap=True,
        cap_micros=1 * DOLLARS,
    )

    report, ok, _stopped = harness.engine._helper(
        agent, 0, {"steps": []}, _Share(limit=5 * DOLLARS)
    )

    assert ok and report == words
    calls = provider.helper_calls[brief]
    assert len(calls) >= 2
    assert all(_no_unusable_message(_sent(call)["messages"]) for call in calls)


def test_a_stored_history_with_an_unusable_message_replays_without_it() -> None:
    stored = [
        {"role": "user", "content": [{"type": "text", "text": "Q1"}]},
        {"role": "assistant", "content": []},
        {"role": "user", "content": [{"type": "text", "text": "Q2"}]},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "cut", "signature": ""},
                {"type": "text", "text": "Partial"},
            ],
        },
        {"role": "user", "content": [{"type": "text", "text": "Q3"}]},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "fine", "signature": "sig"},
                {"type": "text", "text": ""},
                {"type": "text", "text": "Answer 3"},
            ],
        },
    ]

    replayed = replayable_messages(stored)

    assert _no_unusable_message(replayed)
    assert replayed[0] == stored[0] and replayed[1] == stored[2]
    assert replayed[2]["content"] == [{"type": "text", "text": "Partial"}]
    assert replayed[-1]["content"] == [
        {"type": "thinking", "thinking": "fine", "signature": "sig"},
        {"type": "text", "text": "Answer 3"},
    ]
    # A history with nothing unusable is replayed exactly as stored (the same objects' values).
    clean = [
        stored[0],
        stored[2],
        stored[4],
        {"role": "assistant", "content": [{"type": "text", "text": "A"}]},
    ]
    assert replayable_messages(clean) == clean
