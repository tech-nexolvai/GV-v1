"""An empty reply never breaks a chat (harness audit, 2026-10-19), through the app on PostgreSQL.

Before the fix, a reply with no content was stored in the answer's API messages, and every later
question of the chat was refused by the AI service (an empty message in the history is a 400).
Now the empty reply is asked once more, then the answer says plainly that Claude wrote nothing;
the next question's history holds no empty message, and a chat whose stored history already holds
one replays without it.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

from app.review.assistant.chat_engine import NO_ANSWER_ERROR
from tests.api.test_assistant_chats import _named, _world
from tests.review.assistant.chat_fakes import ScriptedProvider, reply, text

pytest_plugins = ("tests.app.postgres_fixture",)


def _empty_messages(raw: str) -> list[int]:
    messages: list[dict[str, Any]] = json.loads(raw)["messages"]
    return [n for n, m in enumerate(messages) if m.get("content") in ([], "", None)]


def test_twice_empty_says_so_and_the_next_question_still_works(postgres_engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    provider = ScriptedProvider(lead=[reply(), reply(), reply(text("Next answer."))])
    with _world(postgres_engine, tmp_path, provider=provider) as world:
        chat_id = world.chat()

        _status, events = world.ask(chat_id, "What is on page 4?")
        first = _named(events, "message_end")[0]["message"]
        status, events = world.ask(chat_id, "And page 5?")
        second = _named(events, "message_end")[0]["message"]

    assert first["stopped"] == "error" and first["error"] == NO_ANSWER_ERROR
    assert status == 200 and second["stopped"] is None and second["text"] == "Next answer."
    assert _empty_messages(provider.lead_calls[2]) == []


def test_a_stored_empty_message_is_left_out_of_the_replay(postgres_engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from app.review.assistant.chat_store import add_assistant_message, add_user_message

    provider = ScriptedProvider(lead=[reply(text("Fine again."))])
    with _world(postgres_engine, tmp_path, provider=provider) as world:
        chat_id = world.chat()
        # An answer stored before the fix: its API messages end with an empty assistant message.
        add_user_message(world.session, chat_id=UUID(chat_id), created_by="r", text="Q1")
        add_assistant_message(
            world.session,
            message_id=uuid4(),
            chat_id=UUID(chat_id),
            created_by="r",
            model="claude-opus-5-5",
            effort="high",
            thinking=True,
            text="",
            blocks=[],
            api_content=[
                {"role": "user", "content": [{"type": "text", "text": "Q1"}]},
                {"role": "assistant", "content": []},
            ],
            cost_micros=0,
            stopped=None,
            error=None,
        )
        world.session.commit()

        status, events = world.ask(chat_id, "Q2")

    assert status == 200
    assert _named(events, "message_end")[0]["message"]["text"] == "Fine again."
    assert _empty_messages(provider.lead_calls[0]) == []
