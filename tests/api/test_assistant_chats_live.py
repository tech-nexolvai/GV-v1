"""The full check's live data through the app (#1710), on a real PostgreSQL.

- A full check through the route sends the live events (versioned, each its contract shape) after
  `message_start` and ends them with `done` before `message_end`; a question sends none.
- The live state is one call away while a full check runs (`GET /chats/{chat_id}/live`, and
  `ChatOut.live`); nothing when it is not running, or when the running message is a question; a
  chat outside the caller's projects is a 404.
- The chat list says each chat's newest question's mode, whether it is running (with the drawings
  done and to do), and its newest full check's review result.
- Stop says "stopping" on the live state before anything else happens.

No paid call is possible: the provider is scripted; the sandbox is a fake.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.review.assistant import chat_engine, chat_trust
from app.review.assistant.chat_contract import ChatLiveStateOut, ChatSummaryOut
from app.review.assistant.live_check import EVENTS, LIVE_VERSION, LiveCheck
from tests.api.test_assistant_chats import _events, _named, _world
from tests.review.assistant.chat_fakes import ScriptedProvider, reply, text, tool
from tests.review.assistant.test_chat_findings import finding_input, make_pdf
from tests.review.assistant.test_live_check import _Screen, _valid

pytest_plugins = ("tests.app.postgres_fixture",)

PDF3 = make_pdf(3)
ASK = {"model": "claude-opus-5-5", "effort": "high", "thinking": True}


@pytest.fixture(autouse=True)
def _no_checks_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    """The checks builder's modules absent: their checks are recorded as not run."""
    monkeypatch.setattr(chat_trust, "load_module", lambda name: None)
    monkeypatch.setattr(chat_engine, "load_module", lambda name: None)


def _registry(world: Any) -> Any:
    from app.api.assistant_chats import chat_registry

    return chat_registry(SimpleNamespace(app=world.app))  # type: ignore[arg-type]


def _asked(world: Any, chat_id: UUID, mode: str) -> None:
    """The question stored, as `run_message` stores it before `message_start`."""
    from sqlalchemy.orm import sessionmaker

    from app.review.assistant.chat_store import add_user_message

    with sessionmaker(bind=world.session.get_bind())() as session:
        add_user_message(
            session,
            chat_id=chat_id,
            created_by="reviewer-one",
            text="Full check · All products · High effort" if mode == "full_check" else "Hi",
            mode=mode,
            products=None,
        )
        session.commit()


def test_a_full_check_sends_its_live_events_and_the_list_says_its_result(postgres_engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    provider = ScriptedProvider(
        lead=[
            reply(tool("r1", "report_finding", finding_input()), stop="tool_use"),
            reply(text("One item is wrong.")),
            reply(text("Three pages.")),
        ]
    )
    with _world(postgres_engine, tmp_path, provider=provider, pdf=PDF3, pages=3) as world:
        chat_id = world.chat()
        response = world.client.post(
            f"/api/v1/chats/{chat_id}/messages",
            json={**ASK, "mode": "full_check", "products": ["countertop"]},
        )
        assert response.status_code == 200, response.text
        events = _events(response.text)
        names = [name for name, _data in events]
        live = [(n, d) for n, d in events if n.startswith("check_")]
        assert live and all(n in EVENTS and d["v"] == LIVE_VERSION for n, d in live)
        screen = _Screen()
        screen.events = live
        _valid(screen)
        # After `message_start`, first "getting ready" with the start time; `done` before the end.
        first = names.index("check_phase")
        assert names.index("message_start") < first
        assert events[first][1]["phase"]["phase"] == "getting_ready"
        phases = [d["phase"]["phase"] for d in _named(events, "check_phase")]
        assert phases[-1] == "done" and "making_files" in phases
        assert names.index("message_end") > len(names) - 1 - names[::-1].index("check_phase")
        # The roster has no drawings: pages, and its findings decided once (at writing).
        comparing = [
            d["phase"] for d in _named(events, "check_phase") if d["phase"]["phase"] == "comparing"
        ]
        assert comparing and comparing[-1]["unit"] == "pages"
        files = [
            d["phase"]
            for d in _named(events, "check_phase")
            if d["phase"]["phase"] == "making_files"
        ]
        message = _named(events, "message_end")[0]["message"]
        assert files[-1]["done"] == len(message["files"]) == 5
        decided = _named(events, "check_finding")
        assert decided and all(d["finding"]["final"] for d in decided)
        # Nothing runs any more: no live state.
        after = world.client.get(f"/api/v1/chats/{chat_id}/live").json()
        assert after == {"running": False, "message_id": None, "live": None}
        assert world.client.get(f"/api/v1/chats/{chat_id}").json()["live"] is None
        # The chat list: a full check, not running, its review result (the same as the answer's).
        listed = world.client.get(f"/api/v1/packages/{world.package_id}/chats").json()["chats"]
        row = ChatSummaryOut.model_validate(listed[0])
        assert row.mode == "full_check" and row.running is False
        assert row.drawings_done is None and row.drawings_total is None
        assert row.review_result is not None
        assert row.review_result.words == message["review_action"]["words"]
        assert row.review_result.action == message["review_action"]["action"]
        # A question afterwards: no live events, and the list says the newest question's mode.
        status = world.client.post(
            f"/api/v1/chats/{chat_id}/messages", json={**ASK, "text": "How many pages?"}
        )
        assert status.status_code == 200
        assert not [n for n, _d in _events(status.text) if n.startswith("check_")]
        again = world.client.get(f"/api/v1/packages/{world.package_id}/chats").json()["chats"][0]
        assert again["mode"] == "chat"
        assert again["review_result"]["words"] == message["review_action"]["words"]


def test_the_live_state_is_one_call_away_while_a_full_check_runs(postgres_engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    with _world(postgres_engine, tmp_path) as world:
        registry = _registry(world)
        chat_id = UUID(world.chat())
        _asked(world, chat_id, "full_check")
        run = registry.claim(chat_id, world.package_id, uuid4())
        assert run is not None
        screen = _Screen()
        run.live = LiveCheck(screen, mode="simple")
        run.live.start()
        items = [
            SimpleNamespace(id="p2.v1", page=2, round=1, area="Area A"),
            SimpleNamespace(id="p3.v1", page=3, round=1, area="Area B"),
            SimpleNamespace(id="p4.v1", page=4, round=1, area="Area B"),
        ]
        run.live.work_list(items, {})
        run.live.drawing_started("p2.v1")
        run.live.drawing_finished("p2.v1", "answered")
        run.live.drawing_started("p3.v1")
        try:
            got = world.client.get(f"/api/v1/chats/{chat_id}/live")
            assert got.status_code == 200
            body = got.json()
            assert body["running"] is True and body["message_id"] == str(run.message_id)
            state = ChatLiveStateOut.model_validate(body["live"])
            assert [d.status for d in state.drawings] == ["answered", "comparing", "waiting"]
            assert state.drawings[0].area == "Area A"
            assert state.phase.phase == "comparing" and (state.phase.done, state.phase.total) == (
                1,
                3,
            )
            assert (state.counts.drawings_done, state.counts.drawings_total) == (1, 3)
            # The chat itself carries the same state (a reloaded page draws the card at once).
            chat = world.client.get(f"/api/v1/chats/{chat_id}").json()
            assert chat["running"] is True and chat["live"] == body["live"]
            # The sidebar: "Checking · 1 of 3".
            row = world.client.get(f"/api/v1/packages/{world.package_id}/chats").json()["chats"][0]
            assert row["running"] is True and row["mode"] == "full_check"
            assert (row["drawings_done"], row["drawings_total"]) == (1, 3)
            assert row["review_result"] is None
            # Stop: the live state says "stopping" at once (one still finishing, one never started).
            assert world.client.post(f"/api/v1/chats/{chat_id}/cancel").json() == {
                "cancelled": True
            }
            assert screen.last("check_stopping")["stopping"] == {"in_flight": 1, "not_started": 1}
            stopped = world.client.get(f"/api/v1/chats/{chat_id}/live").json()["live"]
            assert stopped["phase"]["stopping"] is True
            assert stopped["stopping"] == {"in_flight": 1, "not_started": 1}
        finally:
            registry.release(run)
        _valid(screen)


def test_a_running_question_has_no_live_card(postgres_engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    with _world(postgres_engine, tmp_path) as world:
        registry = _registry(world)
        chat_id = UUID(world.chat())
        _asked(world, chat_id, "chat")
        run = registry.claim(chat_id, world.package_id, uuid4())
        assert run is not None
        try:
            body = world.client.get(f"/api/v1/chats/{chat_id}/live").json()
            assert body == {"running": True, "message_id": str(run.message_id), "live": None}
            row = world.client.get(f"/api/v1/packages/{world.package_id}/chats").json()["chats"][0]
            assert row["running"] is True and row["mode"] == "chat"
            assert row["drawings_done"] is None and row["drawings_total"] is None
        finally:
            registry.release(run)


def test_the_live_route_is_a_404_outside_the_callers_projects(postgres_engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    with _world(postgres_engine, tmp_path) as world:
        chat_id = world.chat()
    with _world(postgres_engine, tmp_path / "b", member=False) as outsider:
        for chat in (chat_id, str(uuid4())):
            assert outsider.client.get(f"/api/v1/chats/{chat}/live").status_code == 404
