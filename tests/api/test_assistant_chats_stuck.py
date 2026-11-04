"""`GET /chats/{chat_id}/stuck-report` (2026-11-04) on a real PostgreSQL: a full check stores where
it got stuck with its trust report, and the route serves it; an older check (stored without one)
gets it worked out from its stored findings; a chat with no full check is a 404.

No paid call is possible: the provider is scripted; the sandbox is a fake.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from app.review.assistant import chat_engine, chat_trust, stuck_report
from tests.api.test_assistant_chats import _world
from tests.review.assistant.chat_fakes import ScriptedProvider, reply, text, tool
from tests.review.assistant.test_chat_findings import finding_input, make_pdf

pytest_plugins = ("tests.app.postgres_fixture",)

PDF3 = make_pdf(3)
ASK = {"model": "claude-opus-5-5", "effort": "high", "thinking": True}


@pytest.fixture(autouse=True)
def _no_checks_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    """The checks builder's modules absent: their checks are recorded as not run."""
    monkeypatch.setattr(chat_trust, "load_module", lambda name: None)
    monkeypatch.setattr(chat_engine, "load_module", lambda name: None)


def _provider() -> ScriptedProvider:
    return ScriptedProvider(
        lead=[
            reply(tool("r1", "report_finding", finding_input()), stop="tool_use"),
            reply(text("One item to look at.")),
        ]
    )


def _full_check(world: Any, chat_id: str) -> None:
    response = world.client.post(
        f"/api/v1/chats/{chat_id}/messages",
        json={**ASK, "mode": "full_check", "products": ["countertop"]},
    )
    assert response.status_code == 200, response.text


def test_a_full_check_stores_where_it_got_stuck_and_the_route_serves_it(postgres_engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    with _world(postgres_engine, tmp_path, provider=_provider(), pdf=PDF3, pages=3) as world:
        chat_id = world.chat()
        # No full check yet: nothing to report.
        assert world.client.get(f"/api/v1/chats/{chat_id}/stuck-report").status_code == 404
        _full_check(world, chat_id)
        chat = world.client.get(f"/api/v1/chats/{chat_id}").json()
        answer = chat["messages"][-1]
        trust = next(b["trust"] for b in answer["blocks"] if b["type"] == "trust")
        findings = [b for b in answer["blocks"] if b["type"] == "finding"]
        # The chat's payload does not carry it (the read model leaves the stored key out).
        assert stuck_report.FIELD not in trust
        response = world.client.get(f"/api/v1/chats/{chat_id}/stuck-report")
        assert response.status_code == 200, response.text
        report = response.json()
        assert report["stored"] is True and report["message_id"] == answer["id"]
        assert report["answers"] == len(findings) == 1
        assert sum(report["groups"].values()) == 1
        assert report["stuck"] == len(report["findings"])
        assert sum(row["answers"] for row in report["stages"]) == report["stuck"]
        for row in report["findings"]:
            assert row["stage"] in stuck_report.STAGES and row["reason"]
        # Someone else's chat says nothing.
        assert world.client.get(f"/api/v1/chats/{uuid4()}/stuck-report").status_code == 404


def test_an_older_full_check_gets_its_report_worked_out_on_read(postgres_engine, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # A check stored before the report existed: its trust block has none.
    monkeypatch.setattr(stuck_report, "attach", lambda engine: None)
    with _world(postgres_engine, tmp_path, provider=_provider(), pdf=PDF3, pages=3) as world:
        chat_id = world.chat()
        _full_check(world, chat_id)
        response = world.client.get(f"/api/v1/chats/{chat_id}/stuck-report")
        assert response.status_code == 200, response.text
        report = response.json()
        assert report["stored"] is False and report["answers"] == 1
        assert report["stuck"] == len(report["findings"])
