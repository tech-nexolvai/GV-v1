"""Set memory across chats and follow-up corrections (#1548), through the app on a real PostgreSQL.

- A note the AI keeps (`remember`) and a reviewer's decision become set notes (append-only); the
  next chat on the set starts with them; a follow-up in the same chat gets only the new ones.
- A later message corrects an earlier answer's finding: the earlier answer reads with the newest
  version ("updated in a later message"), its marked-up PDF and CSV use it, and a dropped one is
  shown dropped. Nothing stored earlier is changed.

No paid call is possible: the provider is scripted; the sandbox is a fake.
"""

from __future__ import annotations

import io
from typing import Any

import pytest
from pypdf import PdfReader

from app.review.assistant import chat_engine, chat_trust
from tests.api.test_assistant_chats import _named, _world
from tests.review.assistant.chat_fakes import ScriptedProvider, reply, text, tool
from tests.review.assistant.test_chat_findings import finding_input, make_pdf

pytest_plugins = ("tests.app.postgres_fixture",)

PDF3 = make_pdf(3)


@pytest.fixture(autouse=True)
def _no_checks_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chat_trust, "load_module", lambda name: None)
    monkeypatch.setattr(chat_engine, "load_module", lambda name: None)


def _first_user_texts(raw: str) -> list[str]:
    import json

    sent = json.loads(raw)
    return [b["text"] for b in sent["messages"][-1]["content"] if b.get("type") == "text"]


def _findings(message: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {b["finding"]["id"]: b["finding"] for b in message["blocks"] if b["type"] == "finding"}


def test_notes_and_decisions_carry_to_the_next_chat_on_the_set(postgres_engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from sqlalchemy import text as sql
    from sqlalchemy.exc import DBAPIError

    provider = ScriptedProvider(
        lead=[
            reply(
                tool("r1", "report_finding", finding_input()),
                tool(
                    "m1",
                    "remember",
                    {"note": "The ID drawing governs over the brand prototype", "page": 2},
                ),
                stop="tool_use",
            ),
            reply(text("One difference.")),
            reply(text("Follow-up answer.")),
            reply(text("Second chat answer.")),
        ]
    )
    with _world(postgres_engine, tmp_path, provider=provider, pdf=PDF3, pages=3) as world:
        chat_id = world.chat()
        status, events = world.ask(chat_id, "Which items are wrong?")
        assert status == 200
        answer = _named(events, "message_end")[0]["message"]
        steps = [b["step"] for b in answer["blocks"] if b["type"] == "step"]
        assert [s["tool"] for s in steps] == ["remember"] and steps[0]["ok"] is True
        decided = world.client.post(
            f"/api/v1/chats/{chat_id}/messages/{answer['id']}/findings/F1/decision",
            json={"decision": "agree"},
        )
        assert decided.status_code == 201, decided.text

        rows = world.session.execute(
            sql("SELECT source, page, note FROM assistant_set_notes ORDER BY created_at")
        ).all()
        assert [(r.source, r.page) for r in rows] == [("ai", 2), ("decision", 2)]
        assert "reviewer-one agreed it is wrong" in rows[1].note
        for statement in (
            "UPDATE assistant_set_notes SET note = 'x'",
            "DELETE FROM assistant_set_notes",
        ):
            with pytest.raises(DBAPIError):
                world.session.execute(sql(statement))
            world.session.rollback()

        # A follow-up in the same chat: only what is new since its last answer (the decision).
        assert world.ask(chat_id, "Anything else on page 2?")[0] == 200
        follow_up = "\n".join(_first_user_texts(provider.lead_calls[2]))
        assert "New notes on this drawing set" in follow_up
        assert "agreed it is wrong" in follow_up and "governs over" not in follow_up

        # A new chat on the same set starts with both notes, page 2's first.
        other = world.chat()
        assert world.ask(other, "What about page 2?")[0] == 200
        started = "\n".join(_first_user_texts(provider.lead_calls[3]))
        assert "<set_notes>" in started and "from earlier chats" in started
        assert "- p.2 [noted by the AI, " in started
        assert '"The ID drawing governs over the brand prototype"' in started
        assert "[reviewer's decision, " in started


def test_a_follow_up_corrects_an_earlier_answer_everywhere_it_is_read(postgres_engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    provider = ScriptedProvider(
        lead=[
            reply(
                tool("r1", "report_finding", finding_input()),
                tool("r2", "report_finding", finding_input(item="Sink base width", page=3)),
                stop="tool_use",
            ),
            reply(text("Two differences.")),
            reply(
                tool(
                    "u1",
                    "update_finding",
                    {"finding_id": "A1-F1", "vendor": "3'-9\"", "why": "Read again sharper"},
                ),
                tool("u2", "drop_finding", {"finding_id": "A1-F2", "reason": "Not a sink base"}),
                stop="tool_use",
            ),
            reply(text("Correction to my earlier answer: F1 reads 3'-9\"; F2 dropped.")),
        ]
    )
    with _world(postgres_engine, tmp_path, provider=provider, pdf=PDF3, pages=3) as world:
        chat_id = world.chat()
        status, events = world.ask(chat_id, "Which items are wrong?")
        first = _named(events, "message_end")[0]["message"]
        assert set(_findings(first)) == {"F1", "F2"}

        status, events = world.ask(chat_id, "Look at those two again")
        assert status == 200
        asked = "\n".join(_first_user_texts(provider.lead_calls[2]))
        assert "<earlier_findings>" in asked and "- A1-F1 p.2 Vanity top length" in asked
        live = _named(events, "finding_update")
        assert [e["update"]["ref"] for e in live] == ["A1-F1", "A1-F2"]
        second = _named(events, "message_end")[0]["message"]
        updates = [b["update"] for b in second["blocks"] if b["type"] == "finding_update"]
        assert [(u["ref"], u["version"], u["dropped"]) for u in updates] == [
            ("A1-F1", 2, None),
            ("A1-F2", 2, "Not a sink base"),
        ]

        chat = world.client.get(f"/api/v1/chats/{chat_id}").json()
        earlier = chat["messages"][1]
        found = _findings(earlier)
        assert list(found) == ["F1"]
        assert found["F1"]["vendor"] == "3'-9\"" and found["F1"]["why"] == "Read again sharper"
        assert found["F1"]["updated_in"] == {"message_id": second["id"], "answer": 2, "version": 2}
        dropped = [b for b in earlier["blocks"] if b["type"] == "finding_dropped"]
        assert dropped == [
            {
                "type": "finding_dropped",
                "finding_id": "F2",
                "reason": "Not a sink base",
                "dropped_in": {"message_id": second["id"], "answer": 2, "version": 2},
            }
        ]

        csv_text = world.client.get(
            f"/api/v1/chats/{chat_id}/messages/{earlier['id']}/findings.csv"
        ).text
        assert "3'-9" in csv_text and "Sink base width" not in csv_text
        made = world.client.post(f"/api/v1/chats/{chat_id}/messages/{earlier['id']}/markup-pdf")
        assert made.status_code == 200, made.text
        pdf = world.client.get(made.json()["url"]).content
        words = " ".join(page.extract_text() for page in PdfReader(io.BytesIO(pdf)).pages)
        assert "Sink base width" not in words and "Vanity top length" in words

        # The earlier answer's stored row is unchanged (append-only).
        from sqlalchemy import text as sql

        stored = world.session.execute(
            sql("SELECT blocks FROM assistant_chat_messages WHERE id = :id"), {"id": earlier["id"]}
        ).scalar_one()
        vendors = [b["finding"]["vendor"] for b in stored if b["type"] == "finding"]
        assert vendors == ["3'-10\"", "3'-10\""]
