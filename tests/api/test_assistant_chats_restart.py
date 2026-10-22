"""Chat answers survive a server restart (#1514), through the app, on a real PostgreSQL.

- A running answer saves its work to the chat folder's checkpoint; a question left with no reply by
  a restart gets a reply stored as `stopped: interrupted` with that work, under the id the screen
  was following, and what its calls cost.
- A reply that is still running (or already stored) is never doubled.
- SIGTERM: no new message, the running answer is stored as interrupted at once and its screens get
  it; its own answer is then never stored.
- Continue: a full check keeps the findings and covered pages, and gets the pages left as its work
  list; a chat answer gets its question and its work so far in words.

No paid call is possible: the provider, the sandbox and the skill builder are fakes.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.review.assistant import chat_engine, chat_trust
from tests.api.test_assistant_chats import (
    _events,
    _held,
    _named,
    _running,
    _stored,
    _until_saved,
    _world,
)
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


def _wait(condition: Any, what: str) -> None:
    # A full check first waits for the set's pages to be read (#1872): up to a minute here.
    for _ in range(2400):
        if condition():
            return
        threading.Event().wait(0.025)
    raise AssertionError(f"never happened: {what}")


def _checkpoint(root: Path, chat_id: UUID) -> dict[str, Any] | None:
    path = root / str(chat_id) / "checkpoint.json"
    try:
        loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded


def _texts(blocks: list[dict[str, Any]]) -> str:
    return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")


def _restarted(world: Any) -> Any:
    """A new process: an empty registry of running messages."""
    from app.api.assistant_chats import _REGISTRY_STATE
    from app.review.assistant.chat_service import ChatRegistry

    registry = ChatRegistry()
    setattr(world.app.state, _REGISTRY_STATE, registry)
    return registry


def test_a_question_a_restart_left_without_a_reply_is_stored_as_interrupted_with_its_work(postgres_engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from sqlalchemy import select
    from sqlalchemy.orm import sessionmaker

    from app.models import ModelInvocation
    from app.review.assistant.chat_service import run_message

    gate = threading.Event()
    provider = ScriptedProvider(lead=[_held("Two items differ on page 4, and", gate)])
    with _world(postgres_engine, tmp_path, provider=provider) as world:
        chat_id, registry, message = _running(world, provider)
        run = message.run
        threading.Thread(target=run_message, args=(message.job, run), daemon=True).start()
        _wait(
            lambda: _texts((_checkpoint(world.root, chat_id) or {}).get("blocks") or []), "a save"
        )
        saved = _checkpoint(world.root, chat_id)
        assert saved is not None
        assert saved["message_id"] == str(run.message_id) and saved["mode"] == "chat"

        # The process dies here: its thread never stores anything again.
        run.abandoned = True
        gate.set()
        _until_saved(registry, chat_id)
        assert [m.role for m in _stored(world, chat_id)] == ["user"]

        # The call it made, as the Usage page's row (priced, as a deployment's rates price it).
        with sessionmaker(bind=world.session.get_bind())() as session:
            row = session.scalars(
                select(ModelInvocation).where(ModelInvocation.template_id == f"chat:{chat_id}")
            ).one()
            columns = {
                c.key: getattr(row, c.key)
                for c in ModelInvocation.__mapper__.column_attrs
                if c.key not in ("id", "created_at")
            }
            session.add(ModelInvocation(**{**columns, "cost_micros": 1_230_000}))
            session.commit()

        _restarted(world)
        chat = world.client.get(f"/api/v1/chats/{chat_id}").json()
        assert chat["running"] is False
        assert chat["messages"][1]["cost_usd"] == "1.23"
        assert [m["role"] for m in chat["messages"]] == ["user", "assistant"]
        answer = chat["messages"][1]
        assert answer["stopped"] == "interrupted" and answer["id"] == str(run.message_id)
        # The words saved before the pause (the save waits at most 2 s after them).
        kept = _texts(answer["blocks"])
        assert kept and "Two items differ on page 4, and".startswith(kept)
        assert answer["model"] == "claude-opus-5-5" and answer["effort"] == "high"
        assert answer["resume"]["mode"] == "chat"
        assert "only the step that was running" in answer["resume"]["redo"]
        assert _checkpoint(world.root, chat_id) is None

        # A screen that was following it gets the stored answer; reading again adds nothing.
        events = _events(
            world.client.get(f"/api/v1/chats/{chat_id}/messages/{run.message_id}/events").text
        )
        assert _named(events, "message_end")[0]["message"]["stopped"] == "interrupted"
        again = world.client.get(f"/api/v1/chats/{chat_id}").json()
        assert len(again["messages"]) == 2


def test_a_reply_that_is_still_running_or_already_stored_is_never_doubled(postgres_engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from sqlalchemy.orm import sessionmaker

    from app.review.assistant.chat_service import ChatRegistry, recover_interrupted, run_message
    from app.review.assistant.chat_store import add_user_message

    gate = threading.Event()
    provider = ScriptedProvider(lead=[_held("All good.", gate)])
    with _world(postgres_engine, tmp_path, provider=provider) as world:
        factory = sessionmaker(bind=world.session.get_bind(), expire_on_commit=False)
        chat_id, registry, message = _running(world, provider)
        threading.Thread(target=run_message, args=(message.job, message.run), daemon=True).start()
        _wait(lambda: len(_stored(world, chat_id)) == 1, "the question stored")

        # Alive in this process: a read and a recovery leave it alone.
        assert world.client.get(f"/api/v1/chats/{chat_id}").json()["running"] is True
        assert recover_interrupted(factory, registry, world.root) == 0
        gate.set()
        _until_saved(registry, chat_id)
        stored = _stored(world, chat_id)
        assert [(m.role, m.stopped) for m in stored] == [("user", None), ("assistant", None)]

        # Its reply is stored: a recovery that did not see it running finds nothing to do.
        assert recover_interrupted(factory, ChatRegistry(), world.root, [chat_id]) == 0

        # Two questions in a row (an old failure): only the last one gets a reply, right after it.
        with factory() as session:
            add_user_message(session, chat_id=chat_id, created_by="reviewer-one", text="one")
            add_user_message(session, chat_id=chat_id, created_by="reviewer-one", text="two")
            session.commit()
        assert recover_interrupted(factory, ChatRegistry(), world.root, [chat_id]) == 1
        rows = _stored(world, chat_id)
        assert [(m.seq, m.role, m.stopped) for m in rows][-3:] == [
            (3, "user", None),
            (4, "user", None),
            (5, "assistant", "interrupted"),
        ]
        # Its place is taken now: a second recovery (another reader at the same time) adds nothing.
        assert recover_interrupted(factory, ChatRegistry(), world.root, [chat_id]) == 0


def test_sigterm_stores_the_running_full_check_as_interrupted_and_continue_checks_the_rest(postgres_engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from app.api.assistant_chats import _registry_of
    from app.review.assistant.chat_service import CONTINUE_TEXT, shutdown_chats

    gate = threading.Event()
    held = reply(
        tool("c1", "set_coverage", {"pages": [{"page": 1, "status": "same"}]}), stop="tool_use"
    )
    held.insert(1, {"type": "__wait__", "until": gate})
    provider = ScriptedProvider(
        lead=[
            reply(tool("r1", "report_finding", finding_input()), stop="tool_use"),
            reply(
                tool("c0", "set_coverage", {"pages": [{"page": 2, "status": "different"}]}),
                stop="tool_use",
            ),
            held,
            reply(
                tool(
                    "c2",
                    "set_coverage",
                    {"pages": [{"page": 1, "status": "same"}, {"page": 3, "status": "same"}]},
                ),
                stop="tool_use",
            ),
            reply(text("Page 1 and 3 checked.")),
        ]
    )
    with _world(postgres_engine, tmp_path, provider=provider, pdf=PDF3, pages=3) as world:
        chat_id = UUID(world.chat())
        result: dict[str, Any] = {}

        def send() -> None:
            response = world.client.post(
                f"/api/v1/chats/{chat_id}/messages",
                json={**ASK, "mode": "full_check", "products": ["countertop"]},
            )
            result["events"] = _events(response.text)

        sender = threading.Thread(target=send, daemon=True)
        sender.start()
        registry = _registry_of(world.app)
        _wait(lambda: len(provider.lead_calls) == 3, "the third call held")

        # SIGTERM: the running full check is saved and stored as interrupted at once.
        assert shutdown_chats(registry, wait_seconds=0.2) == 1
        sender.join(10)
        events = result["events"]
        end = _named(events, "message_end")[0]["message"]
        assert end["stopped"] == "interrupted"
        finding = next(b["finding"] for b in end["blocks"] if b["type"] == "finding")
        assert finding["item"] == "Vanity top length"
        assert end["resume"]["mode"] == "full_check" and end["resume"]["products"] == ["countertop"]
        assert end["resume"]["findings"] == 1 and end["resume"]["pages_done"] == 1
        assert end["resume"]["redo"].startswith(
            "Continue runs the full check again from where it stopped. The 1 finding already "
            "made is kept; the page already done (page 2) is not opened again, and pages 1, 3 "
            "are checked now."
        )
        # No new message while stopping.
        refused = world.client.post(
            f"/api/v1/chats/{chat_id}/messages", json={**ASK, "text": "anything?"}
        )
        assert refused.status_code == 503
        gate.set()  # the stopped thread ends; its own answer is never stored
        _wait(lambda: registry.get(chat_id) is None, "the thread ended")
        stored = _stored(world, chat_id)
        assert [(m.role, m.stopped) for m in stored] == [
            ("user", None),
            ("assistant", "interrupted"),
        ]

        # The next start: Continue runs the full check again from where it stopped.
        _restarted(world)
        response = world.client.post(
            f"/api/v1/chats/{chat_id}/messages",
            json={**ASK, "text": CONTINUE_TEXT, "continue_from": end["id"]},
        )
        assert response.status_code == 200, response.text
        answer = _named(_events(response.text), "message_end")[0]["message"]
        assert answer["stopped"] is None, answer["error"]
        carried = [b["finding"] for b in answer["blocks"] if b["type"] == "finding"]
        assert [f["item"] for f in carried] == ["Vanity top length"]
        sent = json.loads(provider.lead_calls[3])["messages"]
        words = json.dumps(sent)
        assert "Full check of this drawing set" in words
        assert "a server restart interrupted the last full check" in words
        assert "Work list: pages 1, 3 were not done yet" in words
        assert "do not open them again): 2." in words
        chat = world.client.get(f"/api/v1/chats/{chat_id}").json()
        assert chat["messages"][2]["text"] == "Continue the full check · Countertops · High effort"


def test_continue_a_chat_answer_hands_over_the_question_and_its_work(postgres_engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from sqlalchemy.orm import sessionmaker

    from app.review.assistant.chat_service import CONTINUE_TEXT, ChatRegistry, recover_interrupted
    from app.review.assistant.chat_store import add_user_message

    provider = ScriptedProvider(lead=[reply(text("Here is the rest."))])
    with _world(postgres_engine, tmp_path, provider=provider) as world:
        factory = sessionmaker(bind=world.session.get_bind(), expire_on_commit=False)
        chat_id = UUID(world.chat())
        with factory() as session:
            question = add_user_message(
                session, chat_id=chat_id, created_by="reviewer-one", text="Which sinks differ?"
            )
            session.commit()
            question_id = question.id
        folder = world.root / str(chat_id)
        folder.mkdir(parents=True)
        step = {"id": "s1", "tool": "bash_tool", "label": "Listing the sinks", "ok": None}
        (folder / "checkpoint.json").write_text(
            json.dumps(
                {
                    "model": "claude-opus-5-5",
                    "effort": "high",
                    "thinking": True,
                    "mode": "chat",
                    "products": None,
                    "message_id": str(uuid4()),
                    "question_id": str(question_id),
                    "blocks": [
                        {"type": "step", "step": step},
                        {"type": "text", "text": "Sink S1 differs on page 3."},
                    ],
                    "cost_micros": 420_000,
                }
            ),
            encoding="utf-8",
        )
        assert recover_interrupted(factory, ChatRegistry(), world.root) >= 1
        chat = world.client.get(f"/api/v1/chats/{chat_id}").json()
        interrupted = chat["messages"][1]
        assert interrupted["cost_usd"] == "0.42"
        # The step that was running when the server stopped shows as not finished.
        assert interrupted["blocks"][0]["step"]["ok"] is False

        # Only an interrupted answer of this chat can be continued.
        wrong = world.client.post(
            f"/api/v1/chats/{chat_id}/messages",
            json={**ASK, "text": CONTINUE_TEXT, "continue_from": str(uuid4())},
        )
        assert wrong.status_code == 422

        status, events = world.ask(str(chat_id), CONTINUE_TEXT, continue_from=interrupted["id"])
        assert status == 200
        assert _named(events, "message_end")[0]["message"]["text"] == "Here is the rest."
        sent = json.dumps(json.loads(provider.lead_calls[0])["messages"])
        assert "The question was: Which sinks differ?" in sent
        assert "Listing the sinks" in sent and "Sink S1 differs on page 3." in sent
        assert CONTINUE_TEXT in sent


def test_sigterm_with_nothing_running_goes_straight_to_the_servers_own_shutdown() -> None:
    import signal
    from types import SimpleNamespace

    from app.api.assistant_chats import _graceful_sigterm, _registry_of

    seen: list[int] = []
    before = signal.signal(signal.SIGTERM, lambda signum, _frame: seen.append(signum))
    try:
        app = SimpleNamespace(state=SimpleNamespace())
        undo = _graceful_sigterm(app)
        assert undo is not None
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)
        # No message is taken any more, and the server's own handler ran at once.
        assert seen == [signal.SIGTERM] and _registry_of(app).closing
        undo()
        assert signal.getsignal(signal.SIGTERM) is not handler
    finally:
        signal.signal(signal.SIGTERM, before)
