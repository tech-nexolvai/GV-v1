"""The sharpen step through the chat API (#1551), against Postgres with scripted models ($0).

- "Is it ok?" streams an `understood` event and one question back, runs no model, and keeps the
  exchange in the chat's history: the next message is read with it, files listing included.
- The reviewer's corrected spec (Edit, `task` in the body) is used as given, with no model call.
- With the model path on, the sharpen call is Haiku at low effort, recorded with the chat's calls,
  and the stored message carries `understood`.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping
from typing import Any

from app.api.assistant_chats import chat_runtime
from app.review.assistant.sharpen import DEFAULT_QUESTION_BACK, SHARPEN_SYSTEM
from tests.api.test_assistant_chats import DEFAULT, _events, _named, _world
from tests.review.assistant.chat_fakes import ScriptedProvider, reply, strip_cache_control, text

pytest_plugins = ("tests.app.postgres_fixture",)


def _first_user_texts(raw: str) -> list[list[str]]:
    sent = strip_cache_control(json.loads(raw))
    return [
        [block["text"] for block in message["content"] if block.get("type") == "text"]
        for message in sent["messages"]
        if message["role"] == "user"
    ]


def test_is_it_ok_asks_back_and_the_next_message_keeps_the_exchange(postgres_engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    provider = ScriptedProvider(lead=[reply(text("The vendor prints 24 in."))])
    with _world(postgres_engine, tmp_path, provider=provider) as world:
        chat_id = world.chat()

        status, events = world.ask(chat_id, "Is it ok?")

        assert status == 200 and provider.calls == []
        names = [name for name, _ in events]
        assert names[:3] == ["message_start", "understood", "text_delta"]
        assert names[-1] == "message_end"
        understood = _named(events, "understood")[0]["understood"]
        assert understood["intent"] == "clarify" and understood["path"] == "code"
        stored = events[-1][1]["message"]
        assert stored["text"] == DEFAULT_QUESTION_BACK
        assert stored["understood"]["question_back"] == DEFAULT_QUESTION_BACK
        assert stored["stopped"] is None and stored["cost_usd"] == "0.00"

        status, events = world.ask(chat_id, "What's the countertop depth on page 7?")

        assert status == 200
        turns = _first_user_texts(provider.lead_calls[0])
        # The earlier exchange (with the files listed in it) is replayed before the new question.
        assert any(t.startswith("<uploaded_files>") for t in turns[0])
        assert turns[0][-1] == "Is it ok?"
        # The note just before the reviewer's own words (the app's scope note follows them).
        asked = turns[1].index("What's the countertop depth on page 7?")
        assert turns[1][asked - 1].startswith("How the app read this question")

        chat = world.client.get(f"/api/v1/chats/{chat_id}").json()
        answers = [m for m in chat["messages"] if m["role"] == "assistant"]
        assert answers[0]["understood"]["intent"] == "clarify"
        assert answers[1]["understood"]["what_to_compare"] == ["countertop depth"]
        assert answers[1]["understood"]["scope"]["pages"] == [7]


def test_an_edited_spec_is_used_as_given(postgres_engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    provider = ScriptedProvider(lead=[reply(text("Drawers match."))])
    with _world(postgres_engine, tmp_path, provider=provider) as world:
        chat_id = world.chat()

        response = world.client.post(
            f"/api/v1/chats/{chat_id}/messages",
            json={
                "text": "is it ok?",
                **DEFAULT,
                "task": {
                    "task": "Compare drawer fronts with doors",
                    "what_to_compare": ["drawer vs door"],
                    "pages": [12],
                    "output_shape": "short",
                },
            },
        )

        assert response.status_code == 200
        events = _events(response.text)
        understood = _named(events, "understood")[0]["understood"]
        assert understood["path"] == "edited"
        assert understood["label"] == "Compare drawer fronts with doors · page 12 · short answer"
        assert len(provider.calls) == 1  # the lead only
        texts = _first_user_texts(provider.lead_calls[0])[0]
        assert "Task: Compare drawer fronts with doors." in texts[-2]
        assert texts[-1] == "is it ok?"


def test_a_bad_edit_is_refused_at_the_door(postgres_engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    with _world(postgres_engine, tmp_path) as world:
        chat_id = world.chat()
        response = world.client.post(
            f"/api/v1/chats/{chat_id}/messages",
            json={"text": "x", **DEFAULT, "task": {"task": "", "pages": [0]}},
        )
        assert response.status_code == 422


def test_the_model_path_is_haiku_low_and_recorded(postgres_engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    spec: dict[str, Any] = {
        "task": "Compare outlet heights (architect vs vendor)",
        "intent": "compare",
        "scope": {"kind": "whole_set", "pages": [], "area": None},
        "products": [],
        "what_to_compare": ["outlet heights"],
        "checks_to_run": ["read_value", "verify_value"],
        "output_shape": "short",
        "assumptions": [],
        "question_back": None,
    }

    def sharpen_reply(params: Mapping[str, Any]) -> Any:
        assert params["system"][0]["text"] == SHARPEN_SYSTEM
        return reply(
            text(json.dumps(spec)),
            model="claude-haiku-5-5",
            usage={"input_tokens": 1100, "output_tokens": 160},
        )

    provider = ScriptedProvider(lead=[reply(text("They match."))], helper_answer=sharpen_reply)
    with _world(postgres_engine, tmp_path, provider=provider) as world:
        given = world.app.dependency_overrides[chat_runtime]
        world.app.dependency_overrides[chat_runtime] = lambda: dataclasses.replace(
            given(), sharpen=True
        )
        chat_id = world.chat()

        status, events = world.ask(chat_id, "is the outlet height ok on page 4?")

        assert status == 200
        sharpen_call = json.loads(provider.calls[0])
        assert sharpen_call["model"] == "claude-haiku-5-5"
        assert sharpen_call["output_config"] == {"effort": "low"}
        stored = events[-1][1]["message"]
        assert stored["understood"]["path"] == "model"
        assert stored["understood"]["scope"]["pages"] == [4]  # the reviewer's page wins
        assert stored["understood"]["cost_usd"] == "0.0002"
