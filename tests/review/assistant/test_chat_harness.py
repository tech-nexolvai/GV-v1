"""A chat keeps the lead's system prompt and tools it began with (#1577, preserved thinking)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.review.assistant import chat_harness
from app.review.assistant.chat_harness import (
    HARNESS_FILE,
    current_harness,
    update_note,
)
from app.review.assistant.chat_harness import (
    chat_harness as harness_for,
)
from app.review.assistant.chat_tools import LEAD_SYSTEM_PROMPT, LEAD_TOOLS


def test_a_new_chat_keeps_todays_and_reads_it_back_the_same(tmp_path: Path) -> None:
    first = harness_for(tmp_path, replayed=False)
    assert first == current_harness()
    assert first.system == LEAD_SYSTEM_PROMPT
    assert json.dumps(list(first.tools)) == json.dumps(list(LEAD_TOOLS))
    again = harness_for(tmp_path, replayed=True)
    assert json.dumps([again.system, list(again.tools)]) == json.dumps(
        [first.system, list(first.tools)]
    )


def test_a_replayed_chat_keeps_its_own_after_a_newer_harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kept = harness_for(tmp_path, replayed=False)
    monkeypatch.setattr(chat_harness, "HARNESS_ID", "chat-v99")
    monkeypatch.setattr(chat_harness, "LEAD_SYSTEM_PROMPT", "NEWER")
    assert harness_for(tmp_path, replayed=True) == kept
    # A chat with nothing to replay yet takes the newer one.
    newer = harness_for(tmp_path, replayed=False)
    assert newer.system == "NEWER" and newer.harness_id == "chat-v99"


def test_an_unreadable_file_falls_back_to_todays(tmp_path: Path) -> None:
    (tmp_path / HARNESS_FILE).write_text("{not json", encoding="utf-8")
    assert harness_for(tmp_path, replayed=True) == current_harness()
    assert json.loads((tmp_path / HARNESS_FILE).read_text())["system"] == LEAD_SYSTEM_PROMPT


def test_the_update_note_comes_once_and_only_for_an_older_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    today = current_harness()
    assert update_note(today, []) is None
    older = chat_harness.ChatHarness(harness_id="chat-v15", system="OLD", tools=today.tools)
    note = update_note(older, [])
    assert note is not None and note.startswith(f"[GV Review update {chat_harness.HARNESS_ID}]")
    assert LEAD_SYSTEM_PROMPT in note
    history = [{"role": "user", "content": [{"type": "text", "text": note}]}]
    assert update_note(older, history) is None


def test_a_chats_deferred_tools_are_stored_with_their_flags_and_replayed_byte_for_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.review.assistant.chat_request import MODELS, call_params, thinking_config

    first = harness_for(tmp_path, replayed=False)
    stored = json.loads((tmp_path / HARNESS_FILE).read_text(encoding="utf-8"))["tools"]
    deferred = [tool["name"] for tool in stored if tool.get("defer_loading") is True]
    assert deferred and "drawing_facts" in deferred and "bash_tool" not in deferred
    # A later deploy changes today's tools: the chat still sends its own, byte for byte.
    monkeypatch.setattr(chat_harness, "LEAD_TOOLS", tuple(LEAD_TOOLS[:3]))
    again = harness_for(tmp_path, replayed=True)
    assert json.dumps(list(again.tools)) == json.dumps(list(first.tools))
    model = MODELS["claude-opus-5-5"]

    def sent(tools: tuple[dict[str, object], ...]) -> str:
        params = call_params(
            provider="anthropic",
            model=model,
            effort="high",
            thinking=thinking_config(model, True),
            system=again.system,
            tools=tools,
            messages=[{"role": "user", "content": [{"type": "text", "text": "Hi"}]}],
            final=False,
            max_tokens=64000,
        )
        return json.dumps(params["tools"])

    assert sent(again.tools) == sent(first.tools)
    assert '"defer_loading": true' in sent(again.tools)
