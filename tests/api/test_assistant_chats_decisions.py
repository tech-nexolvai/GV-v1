"""The reviewer's decision on each finding (#1521) through the app on a real PostgreSQL: ✓ Agree /
✗ Not a problem kept append-only (the latest per finding wins), returned on the finding, and the
cloud drawing and check report remade by code from the decisions (only agreed items clouded).

No paid call is possible: the provider is scripted; the sandbox is a fake.
"""

from __future__ import annotations

import csv
import io
from typing import Any
from uuid import uuid4

import pytest
from pypdf import PdfReader

from app.review.assistant import chat_engine, chat_trust
from tests.api.test_assistant_chats import _events, _named, _running, _world
from tests.api.test_assistant_chats_findings import _red_near
from tests.review.assistant.chat_fakes import ScriptedProvider, reply, text, tool
from tests.review.assistant.test_chat_findings import VENDOR_BOX, finding_input, make_pdf

pytest_plugins = ("tests.app.postgres_fixture",)

PDF3 = make_pdf(3)
PAGE3_BOX = {**VENDOR_BOX, "page": 3}


@pytest.fixture(autouse=True)
def _no_checks_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chat_trust, "load_module", lambda name: None)
    monkeypatch.setattr(chat_engine, "load_module", lambda name: None)


def _three_findings() -> ScriptedProvider:
    """F1 wrong on page 2, F2 wrong on page 3, F3 right."""
    return ScriptedProvider(
        lead=[
            reply(
                tool("r1", "report_finding", finding_input()),
                tool(
                    "r2",
                    "report_finding",
                    finding_input(item="Sink base width", vendor_box=dict(PAGE3_BOX), page=3),
                ),
                tool(
                    "r3",
                    "report_finding",
                    finding_input(item="Backsplash height", status="same", vendor='37"'),
                ),
                stop="tool_use",
            ),
            reply(text("Two differences.")),
        ]
    )


def _answer(world: Any) -> tuple[str, dict[str, Any]]:
    chat_id = world.chat()
    status, events = world.ask(chat_id, "Which items are wrong?")
    assert status == 200
    answer = _named(events, "message_end")[0]["message"]
    ids = [b["finding"]["id"] for b in answer["blocks"] if b["type"] == "finding"]
    assert ids == ["F1", "F2", "F3"]
    return chat_id, answer


def _decide(world: Any, chat_id: str, message_id: str, finding_id: str, **body: Any) -> Any:
    return world.client.post(
        f"/api/v1/chats/{chat_id}/messages/{message_id}/findings/{finding_id}/decision", json=body
    )


def _findings(message: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {b["finding"]["id"]: b["finding"] for b in message["blocks"] if b["type"] == "finding"}


def _text(pdf: bytes) -> list[str]:
    return [page.extract_text() for page in PdfReader(io.BytesIO(pdf)).pages]


def test_decisions_are_append_only_and_the_latest_one_wins(postgres_engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from sqlalchemy import text as sql
    from sqlalchemy.exc import DBAPIError

    with _world(postgres_engine, tmp_path, provider=_three_findings(), pdf=PDF3, pages=3) as world:
        chat_id, answer = _answer(world)
        first = _decide(world, chat_id, answer["id"], "F1", decision="not_a_problem")
        assert first.status_code == 201, first.text
        changed = _decide(world, chat_id, answer["id"], "F1", decision="agree", note=" Tell them ")
        assert changed.status_code == 201
        body = changed.json()
        assert body["decision"] == "agree" and body["note"] == "Tell them"
        assert body["by"] == "reviewer-one" and body["at"]

        stored = world.client.get(f"/api/v1/chats/{chat_id}").json()["messages"][-1]
        found = _findings(stored)
        assert found["F1"]["decision"]["decision"] == "agree"
        assert found["F1"]["decision"]["by"] == "reviewer-one"
        assert found["F2"]["decision"] is None
        # The follow feed of the finished answer carries the same decisions.
        followed = world.client.get(f"/api/v1/chats/{chat_id}/messages/{answer['id']}/events")
        assert _events(followed.text) == [("message_end", {"message": stored})]

        rows = world.session.execute(
            sql("SELECT decision FROM assistant_finding_decisions ORDER BY decided_at")
        ).scalars()
        assert list(rows) == ["not_a_problem", "agree"]
        for statement in (
            "UPDATE assistant_finding_decisions SET decision = 'agree'",
            "DELETE FROM assistant_finding_decisions",
        ):
            with pytest.raises(DBAPIError):
                world.session.execute(sql(statement))
            world.session.rollback()


def test_decisions_are_refused_for_unknown_findings_bad_values_and_outsiders(postgres_engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    with _world(postgres_engine, tmp_path, provider=_three_findings(), pdf=PDF3, pages=3) as world:
        chat_id, answer = _answer(world)
        assert _decide(world, chat_id, answer["id"], "F9", decision="agree").status_code == 404
        assert _decide(world, chat_id, str(uuid4()), "F1", decision="agree").status_code == 404
        question = world.client.get(f"/api/v1/chats/{chat_id}").json()["messages"][0]["id"]
        assert _decide(world, chat_id, question, "F1", decision="agree").status_code == 404
        assert _decide(world, chat_id, answer["id"], "F1", decision="maybe").status_code == 422
        drawing = f"/api/v1/chats/{chat_id}/messages/{answer['id']}/cloud-drawing"
        assert world.client.post(drawing.replace(answer["id"], str(uuid4()))).status_code == 404

    with _world(postgres_engine, tmp_path / "b", member=False) as outsider:
        assert _decide(outsider, chat_id, answer["id"], "F1", decision="agree").status_code == 404
        assert outsider.client.post(drawing).status_code == 404


def test_a_running_answer_cannot_be_decided_or_drawn(postgres_engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    provider = ScriptedProvider()
    with _world(postgres_engine, tmp_path, provider=provider) as world:
        chat_id, registry, message = _running(world, provider)
        try:
            message_id = message.run.message_id
            refused = _decide(world, str(chat_id), str(message_id), "F1", decision="agree")
            assert refused.status_code == 409 and refused.json()["error"] == "message_running"
            drawn = world.client.post(
                f"/api/v1/chats/{chat_id}/messages/{message_id}/cloud-drawing"
            )
            assert drawn.status_code == 409 and drawn.json()["error"] == "message_running"
        finally:
            registry.release(message.run)


def test_the_cloud_drawing_follows_the_decisions_and_keeps_older_files(postgres_engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    with _world(postgres_engine, tmp_path, provider=_three_findings(), pdf=PDF3, pages=3) as world:
        chat_id, answer = _answer(world)
        url = f"/api/v1/chats/{chat_id}/messages/{answer['id']}/cloud-drawing"
        _decide(world, chat_id, answer["id"], "F1", decision="agree")

        made = world.client.post(url)
        assert made.status_code == 200, made.text
        first = made.json()
        assert first["name"] == "Vendor set - GV cloud drawing.pdf"
        pdf = world.client.get(first["url"]).content
        pages = _text(pdf)
        # The zero-click drawing (#1581): cover, the 3 sheets, one proof page, notes.
        assert list(PdfReader(io.BytesIO(pdf)).page_labels) == [
            "Cover",
            "1",
            "2",
            "3",
            "Proof 1",
            "Notes 1",
        ]
        cover = pages[0]
        assert "Checked automatically by GV Review" in cover
        assert "and reviewed by reviewer-one" in cover and "draft" not in cover.lower()
        # F1 agreed: clouded, an override (only AI readings back it here). F2 is not decided and
        # only AI readings back it, F3 was not seen on both sides: Not compared (#1619), never
        # clouded, not on the drawing: the separate Not-compared list.
        assert "1\nWrong" in cover and "2\nNot compared" in cover
        sheet = " ".join(pages[2].split())
        assert '3\'-10" should be 37"' in sheet  # the sheet note (#1804)
        assert _red_near(pdf, 3, VENDOR_BOX) > 5
        assert _red_near(pdf, 4, PAGE3_BOX) == 0
        # The proof card names the reviewer's decision, not their login (#1804).
        assert "Decided by GV's reviewer" in pages[4] and "reviewer-one" not in pages[4]
        notes = " ".join(pages[5].split())
        assert "Not compared (2)" in notes and "Sink base width" not in notes

        stored = world.client.get(f"/api/v1/chats/{chat_id}").json()["messages"][-1]
        found = _findings(stored)
        assert found["F1"]["proof"]["group"] == "clouded"
        assert found["F2"]["proof"]["group"] == "not_compared"
        assert found["F2"]["proof"]["not_compared"] == "unreadable"
        assert found["F3"]["proof"]["group"] == "not_compared"
        assert found["F3"]["proof"]["not_compared"] == "not_seen"
        report = next(f for f in stored["files"] if f["name"].endswith("AI check report.pdf"))
        report_text = "".join(_text(world.client.get(report["url"]).content))
        assert "1 wrong clouded (proved by code), 0 right, 2 not compared" in " ".join(
            report_text.split()
        )

        # F2 dismissed: not a problem, still never clouded, listed apart with the note.
        _decide(world, chat_id, answer["id"], "F2", decision="not_a_problem", note="Field fit")
        again = world.client.post(url)
        assert again.status_code == 200
        second = again.json()
        assert second["id"] != first["id"]
        pdf2 = world.client.get(second["url"]).content
        pages2 = _text(pdf2)
        assert "and reviewed by reviewer-one" in pages2[0]
        assert _red_near(pdf2, 3, VENDOR_BOX) > 5
        assert _red_near(pdf2, 4, PAGE3_BOX) == 0
        assert "Right (reviewer): Field fit" in " ".join(pages2[-1].split())

        # The older pair is kept and still downloads unchanged.
        files = world.client.get(f"/api/v1/chats/{chat_id}").json()["messages"][-1]["files"]
        assert [f["id"] for f in files].count(first["id"]) == 1 and len(files) == 4
        assert world.client.get(first["url"]).content == pdf


def test_the_csv_carries_the_decisions(postgres_engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    with _world(postgres_engine, tmp_path, provider=_three_findings(), pdf=PDF3, pages=3) as world:
        chat_id, answer = _answer(world)
        _decide(world, chat_id, answer["id"], "F1", decision="agree", note="=SUM(A1)")
        got = world.client.get(f"/api/v1/chats/{chat_id}/messages/{answer['id']}/findings.csv")
        assert got.status_code == 200
        rows = list(csv.reader(io.StringIO(got.content.decode("utf-8-sig"))))
        header = rows[0]
        assert header[-6:] == [
            "Decision",
            "Decision note",
            "Decided by",
            "Decided at",
            "Should be",
            "Proof",
        ]
        by_id = {row[1]: dict(zip(header, row, strict=True)) for row in rows[1:]}
        assert by_id["F1"]["Decision"] == "Agreed" and by_id["F1"]["Decided by"] == "reviewer-one"
        assert by_id["F1"]["Decision note"] == "'=SUM(A1)" and by_id["F1"]["Decided at"]
        assert by_id["F2"]["Decision"] == "Not yet decided"
        assert by_id["F3"]["Decision"] == ""
        # Where the zero-click cloud drawing puts each item (#1581).
        assert by_id["F1"]["Should be"] == '37" (architect, page 1)'
        assert by_id["F1"]["Proof"].startswith("Wrong, proved by code (clouded): Agreed by")
        assert by_id["F2"]["Proof"].startswith(
            "Not compared: could not read the vendor's value on page 3. Needed:"
        )
        assert by_id["F3"]["Proof"].startswith("Not compared")
