"""The chat's messages and set packs run by the worker, progress through PostgreSQL (#1871).

Through the app, on a real PostgreSQL, with `GV_ASSISTANT_RUN_IN_WORKER` on and the worker's own
code (`job_worker.run_job`) answering in this test's threads:

- a message is a job row; the worker answers it exactly as the API's thread did (the same events,
  the same stored answer); the screen's stream reads the events from the database; nothing about it
  is kept in the API's memory;
- one message per chat and one paid full check at a time hold across processes (the database's
  partial unique indexes, 409 as before);
- Stop is a flag the job's process reads: the answer stops and is stored as cancelled;
- a job whose process died is stored as interrupted with the work its checkpoint saved (Continue),
  and its screens get that answer; an API restart never interrupts a job that is alive;
- a set pack is a job (one per pack); the supervisor runs each job in its own process, collects a
  dead one at once, and builds a set pack whose build died once more before it fails it.

No paid call is possible: the provider, the sandbox and the skill builder are fakes.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from tests.api.test_assistant_chats import DEFAULT, World, _events, _named, _stored, _world
from tests.review.assistant.chat_fakes import ScriptedProvider, reply, text, thinking

pytest_plugins = ("tests.app.postgres_fixture",)


def _wait(condition: Any, what: str, seconds: float = 15.0) -> None:
    for _ in range(int(seconds / 0.025)):
        if condition():
            return
        threading.Event().wait(0.025)
    raise AssertionError(f"never happened: {what}")


@contextmanager
def _worker_world(postgres_engine: Any, tmp_path: Path, **kwargs: Any) -> Iterator[World]:
    settings = {"assistant_run_in_worker": True, **(kwargs.pop("settings", None) or {})}
    with _world(postgres_engine, tmp_path, settings=settings, **kwargs) as world:
        # The app's own engine (what `get_session` makes in a deployment): the test database.
        world.app.state._gv_read_engine = postgres_engine
        yield world


def _factory(world: World) -> sessionmaker[Session]:
    return sessionmaker(bind=world.session.get_bind(), expire_on_commit=False)


class _Worker:
    """The worker's supervisor, in this test's threads: claims the queued jobs and runs each with
    `job_worker.run_job` (the job's process's own code) and the test's fakes."""

    def __init__(self, world: World, provider: ScriptedProvider | None = None) -> None:
        self.world = world
        self.provider = provider
        self.stop = threading.Event()
        self.statuses: dict[UUID, str] = {}
        self.threads: list[threading.Thread] = []
        self._loop = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> _Worker:
        self._loop.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop.set()
        self._loop.join(10)
        for thread in self.threads:
            thread.join(10)

    def _run(self) -> None:
        from app.review.assistant.job_board import claim

        while not self.stop.wait(0.05):
            with _factory(self.world)() as session:
                jobs = [job.id for job in claim(session, "test", kinds=["message"], limit=5)]
                session.commit()
            for job_id in jobs:
                thread = threading.Thread(target=self._one, args=(job_id,), daemon=True)
                self.threads.append(thread)
                thread.start()

    def _one(self, job_id: UUID) -> None:
        from app.api.assistant_chats import chat_runtime
        from app.api.dependencies import ARTIFACT_STORE_STATE
        from app.review.assistant import chat_service
        from app.review.assistant.job_worker import run_job

        app = self.world.app
        provider = self.provider
        real = chat_service.run_message

        def run_message(job: Any, run: Any, emit: Any = None) -> None:
            if provider is not None:
                provider.cancel = run.cancel  # the fake stream holds until Stop
            real(job, run, emit)

        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(chat_service, "run_message", run_message)
            self.statuses[job_id] = run_job(
                job_id,
                factory=_factory(self.world),
                settings=app.state.settings,
                runtime=app.dependency_overrides[chat_runtime](),
                store=getattr(app.state, ARTIFACT_STORE_STATE),
            )


def _job(world: World, job_id: UUID | str) -> Any:
    from app.models import AssistantJob

    with _factory(world)() as session:
        return session.get(AssistantJob, UUID(str(job_id)))


def _job_events(world: World, job_id: UUID) -> list[tuple[str, Any]]:
    from app.review.assistant.job_board import events_after

    with _factory(world)() as session:
        return [(event, data) for _seq, event, data in events_after(session, job_id, 0)]


def _ask_script() -> list[Any]:
    return [reply(thinking("Count the pages."), text("The set has 62 pages."))]


def _parity(events: list[tuple[str, Any]]) -> list[tuple[str, Any]]:
    """The events as a screen ends up with them: streamed words of one block joined (the worker
    sends them a few times a second, the thread one by one), the newest usage kept; ids and times
    left out."""
    from app.review.assistant.job_board import _merge

    kept: list[tuple[str, dict[str, Any]]] = []
    for name, data in events:
        _merge(kept, name, data)
    return [(name, data.get("text") if name.endswith("_delta") else None) for name, data in kept]


# ---- a message is a job ----------------------------------------------------------------------------


def test_a_message_is_a_job_the_worker_answers_and_the_screen_streams_from_the_database(postgres_engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    # The same question answered by the API's own thread (the setting off), for comparison.
    with _world(postgres_engine, tmp_path / "thread", provider=ScriptedProvider(lead=_ask_script())) as old:
        old_status, old_events = old.ask(old.chat(), "How many pages?")
    assert old_status == 200

    provider = ScriptedProvider(lead=_ask_script())
    with _worker_world(postgres_engine, tmp_path / "worker", provider=provider) as world:
        chat_id = world.chat()
        with _Worker(world):
            status, events = world.ask(chat_id, "How many pages?")
        assert status == 200
        # The same events, in the same order, as the API's own thread sent them.
        assert _parity(events) == _parity(old_events)
        assert events[0][0] == "message_start" and events[-1][0] == "message_end"
        end = events[-1][1]["message"]
        old_end = old_events[-1][1]["message"]
        assert end["text"] == old_end["text"] == "The set has 62 pages."
        assert [b["type"] for b in end["blocks"]] == [b["type"] for b in old_end["blocks"]]
        assert end["id"] == events[0][1]["message_id"]

        # The job: done, its events kept in order, the answer stored under the job's id.
        job = _job(world, end["id"])
        assert job.kind == "message" and job.status == "done" and job.paid is False
        assert job.request["body"]["text"] == "How many pages?"
        assert job.request["principal"]["id"] == "reviewer-one"
        kept = _job_events(world, job.id)
        assert [name for name, _ in kept][0] == "message_start" and kept[-1][0] == "message_end"
        assert job.last_seq == len(kept)
        stored = _stored(world, UUID(chat_id))
        assert [m.role for m in stored] == ["user", "assistant"]
        assert str(stored[1].id) == end["id"]

        # Nothing about it was kept in the API's memory; the chat reads as not running.
        from app.api.assistant_chats import _registry_of
        from app.review.assistant.job_board import JobRegistry

        registry = _registry_of(world.app)
        assert isinstance(registry, JobRegistry) and registry._runs == {}
        chat = world.client.get(f"/api/v1/chats/{chat_id}").json()
        assert chat["running"] is False and len(chat["messages"]) == 2

        # A screen following it afterwards gets the stored answer at once.
        followed = _events(
            world.client.get(f"/api/v1/chats/{chat_id}/messages/{end['id']}/events").text
        )
        assert _named(followed, "message_end")[0]["message"]["id"] == end["id"]


def test_one_message_per_chat_and_one_paid_check_hold_across_processes(postgres_engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from app.review.assistant.job_board import ChatBusy, PaidBusy, enqueue_message

    with _worker_world(postgres_engine, tmp_path) as world:
        busy, other, third = (UUID(world.chat()) for _ in range(3))
        factory = _factory(world)
        with factory() as session:
            # Another process's jobs: a question in one chat, a paid full check in another.
            for chat_id, paid in ((busy, False), (other, True)):
                enqueue_message(
                    session,
                    job_id=uuid4(),
                    chat_id=chat_id,
                    package_id=world.package_id,
                    created_by="someone",
                    paid=paid,
                    request={},
                )
            session.commit()

        refused = world.client.post(
            f"/api/v1/chats/{busy}/messages", json={"text": "hi", **DEFAULT}
        )
        assert refused.status_code == 409 and refused.json()["error"] == "message_running"
        paid = world.client.post(
            f"/api/v1/chats/{third}/messages",
            json={"text": "", "mode": "full_check", **DEFAULT},
        )
        assert paid.status_code == 409 and paid.json()["error"] == "paid_job_running"
        chat = world.client.get(f"/api/v1/chats/{busy}").json()
        assert chat["running"] is True

        # The database refuses the race itself (two processes writing at once).
        with factory() as session:
            with pytest.raises(ChatBusy):
                enqueue_message(
                    session,
                    job_id=uuid4(),
                    chat_id=busy,
                    package_id=world.package_id,
                    created_by="x",
                    paid=False,
                    request={},
                )
            with pytest.raises(PaidBusy):
                enqueue_message(
                    session,
                    job_id=uuid4(),
                    chat_id=third,
                    package_id=world.package_id,
                    created_by="x",
                    paid=True,
                    request={},
                )


def test_stop_reaches_the_workers_answer_which_is_stored_as_cancelled(postgres_engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    held = reply(text("Partial"))
    held.insert(3, {"type": "__wait_for_cancel__"})
    provider = ScriptedProvider(lead=[held])
    with _worker_world(postgres_engine, tmp_path, provider=provider) as world:
        chat_id = world.chat()
        with _Worker(world, provider) as worker:
            answer: dict[str, Any] = {}
            asked = threading.Thread(
                target=lambda: answer.update(result=world.ask(chat_id, "A long question")),
                daemon=True,
            )
            asked.start()
            _wait(lambda: bool(worker.statuses) or worker.threads, "the job claimed")

            def streaming() -> bool:
                from app.models import AssistantJob

                with _factory(world)() as session:
                    job = session.scalars(
                        select(AssistantJob).where(AssistantJob.chat_id == UUID(chat_id))
                    ).first()
                return job is not None and any(
                    name == "text_delta" for name, _ in _job_events(world, job.id)
                )

            _wait(streaming, "words streamed")
            response = world.client.post(f"/api/v1/chats/{chat_id}/cancel")
            assert response.json() == {"cancelled": True}
            asked.join(20)
        status, events = answer["result"]
        assert status == 200
        assert events[-1][0] == "message_end"
        assert events[-1][1]["message"]["stopped"] == "cancelled"
        stored = _stored(world, UUID(chat_id))
        assert stored[1].stopped == "cancelled"
        assert list(worker.statuses.values()) == ["done"]


# ---- dead jobs and restarts ------------------------------------------------------------------------


def _running_job(world: World, chat_id: UUID, *, beat_seconds_ago: float) -> UUID:
    """A question stored and its job running in another process, its heartbeat this old, with the
    work its checkpoint saved."""
    from app.models import AssistantJob
    from app.review.assistant.chat_store import add_user_message
    from app.review.assistant.job_board import append_events, enqueue_message

    job_id = uuid4()
    with _factory(world)() as session:
        question = add_user_message(
            session, chat_id=chat_id, created_by="reviewer-one", text="A long question"
        )
        enqueue_message(
            session,
            job_id=job_id,
            chat_id=chat_id,
            package_id=world.package_id,
            created_by="reviewer-one",
            paid=False,
            request={},
        )
        session.flush()
        session.execute(
            update(AssistantJob)
            .where(AssistantJob.id == job_id)
            .values(
                status="running",
                heartbeat_at=_now(session, -beat_seconds_ago),
                live=_live(),
                spent_micros=1_500_000,
            )
        )
        append_events(
            session,
            job_id,
            1,
            [
                ("message_start", {"message_id": str(job_id)}),
                ("text_delta", {"index": 0, "text": "Two items differ"}),
            ],
        )
        session.commit()
        question_id = question.id
    folder = world.root / str(chat_id)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "checkpoint.json").write_text(
        json.dumps(
            {
                "model": "claude-opus-5-5",
                "effort": "high",
                "thinking": True,
                "mode": "chat",
                "products": None,
                "message_id": str(job_id),
                "question_id": str(question_id),
                "blocks": [{"type": "text", "text": "Two items differ"}],
                "cost_micros": 1_500_000,
            }
        )
    )
    return job_id


def _live() -> dict[str, Any]:
    """A full check's live card as the worker's heartbeat writes it."""
    from app.review.assistant.live_check import LiveCheck

    live = LiveCheck(lambda _event, _data: None, mode="roster")
    live.start()
    return json.loads(json.dumps({"snapshot": live.snapshot(), "counts": live.counts()}))


def _now(session: Session, seconds: float) -> Any:
    from datetime import timedelta

    from sqlalchemy import func

    now = session.scalar(select(func.now()))
    assert now is not None
    return now + timedelta(seconds=seconds)


def test_a_job_whose_process_died_is_stored_as_interrupted_with_its_work(postgres_engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    with _worker_world(postgres_engine, tmp_path) as world:
        chat_id = UUID(world.chat())
        job_id = _running_job(world, chat_id, beat_seconds_ago=600)

        # The next read of the chat finds it dead (no heartbeat for 10 minutes).
        chat = world.client.get(f"/api/v1/chats/{chat_id}").json()
        assert chat["running"] is False
        answer = chat["messages"][1]
        assert answer["id"] == str(job_id) and answer["stopped"] == "interrupted"
        kept = "".join(b.get("text", "") for b in answer["blocks"] if b.get("type") == "text")
        assert kept == "Two items differ" and answer["cost_usd"] == "1.50"
        assert answer["resume"]["mode"] == "chat"  # Continue
        job = _job(world, job_id)
        assert job.status == "interrupted" and job.error
        # Its screens got the stored answer as the stream's end.
        followed = _job_events(world, job_id)
        assert followed[-1][0] == "message_end"
        assert followed[-1][1]["message"]["stopped"] == "interrupted"
        # The chat takes a new message again.
        assert _stored(world, chat_id)[-1].role == "assistant"


def test_an_api_restart_never_interrupts_a_job_that_is_alive(postgres_engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from app.api.assistant_chats import _REGISTRY_STATE, _recover_at_start

    with _worker_world(postgres_engine, tmp_path) as world:
        chat_id = UUID(world.chat())
        job_id = _running_job(world, chat_id, beat_seconds_ago=1)

        # A new API process: nothing in its memory; it checks for cut-off answers at start-up.
        setattr(world.app.state, _REGISTRY_STATE, None)
        _recover_at_start(world.app)
        assert [m.role for m in _stored(world, chat_id)] == ["user"]

        # The screen reconnects: the check is running, with its live card and spend.
        response = world.client.get(f"/api/v1/chats/{chat_id}")
        assert response.status_code == 200, response.text
        chat = response.json()
        assert chat["running"] is True and chat["running_message_id"] == str(job_id)
        live = world.client.get(f"/api/v1/chats/{chat_id}/live").json()
        assert live["running"] is True and live["message_id"] == str(job_id)
        listed = world.client.get(f"/api/v1/packages/{world.package_id}/chats").json()
        assert [c["id"] for c in listed["chats"]] == [str(chat_id)]
        models = world.client.get(f"/api/v1/assistant/models?package_id={world.package_id}")
        assert models.json()["package_spent_usd"] == "1.50"

        # Following it replays its events so far, then the rest as the worker writes them.
        from app.models import AssistantJob
        from app.review.assistant.job_board import JobFeedReader, append_events, finish

        async def follow() -> list[tuple[str, Any]]:
            seen: list[tuple[str, Any]] = []
            async for item in JobFeedReader(_factory(world), job_id, every=0.02).follow():
                seen.append(item)
                if len(seen) == 1:
                    with _factory(world)() as session:
                        append_events(
                            session, job_id, 3, [("text_delta", {"index": 0, "text": " here."})]
                        )
                        finish(session, job_id, "done")
                        session.commit()
            return seen

        seen = asyncio.run(follow())
        # The first two rows are one streamed block when replayed; the new words follow.
        assert seen == [
            ("message_start", {"message_id": str(job_id)}),
            ("text_delta", {"index": 0, "text": "Two items differ"}),
            ("text_delta", {"index": 0, "text": " here."}),
        ]
        with _factory(world)() as session:
            assert session.get(AssistantJob, job_id).status == "done"  # type: ignore[union-attr]


# ---- set packs and the supervisor ------------------------------------------------------------------


def test_a_set_pack_is_one_job_per_pack_and_never_built_in_the_api(postgres_engine, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.models import AssistantJob
    from app.review.assistant import set_pack
    from app.review.assistant.job_board import pack_starter

    built: list[Any] = []
    monkeypatch.setattr(set_pack, "ensure_pack", lambda *a, **k: built.append(a))
    with _worker_world(postgres_engine, tmp_path) as world:
        start = pack_starter(_factory(world))
        folder = tmp_path / "packs" / "abc123"
        listing = [{"name": "Vendor set.pdf", "sha256": "0" * 64, "pages": 3}]
        start(folder, tmp_path / "uploads", listing)
        start(folder, tmp_path / "uploads", listing)  # asked again while it waits
        with _factory(world)() as session:
            jobs = list(session.scalars(select(AssistantJob).where(AssistantJob.kind == "set_pack")))
        assert len(jobs) == 1 and jobs[0].pack_key == "abc123" and jobs[0].status == "queued"
        assert jobs[0].request["listing"] == listing
        # The pack says "building" at once, as `ensure_pack` says it (a full check waits for it).
        assert set_pack.read_status(folder)["state"] == "building"
        assert built == []

        # A ready pack of this code's version is not built again.
        with _factory(world)() as session:
            session.execute(update(AssistantJob).values(status="done"))
            session.commit()
        set_pack._set_status(folder, "ready", [])
        start(folder, tmp_path / "uploads", listing)
        with _factory(world)() as session:
            assert session.scalar(select(AssistantJob.id).where(AssistantJob.status == "queued")) is None


class _Gone:
    """A job's process that has ended with `code`."""

    def __init__(self, code: int) -> None:
        self.code = code

    def poll(self) -> int:
        return self.code


def test_the_supervisor_stores_a_crashed_message_at_once_and_builds_a_dead_pack_once_more(postgres_engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from app.models import AssistantJob
    from app.review.assistant.job_board import enqueue_pack
    from app.review.assistant.job_worker import Supervisor

    with _worker_world(postgres_engine, tmp_path) as world:
        chat_id = UUID(world.chat())
        message_job = _running_job(world, chat_id, beat_seconds_ago=1)
        with _factory(world)() as session:
            session.execute(
                update(AssistantJob).where(AssistantJob.id == message_job).values(status="queued")
            )
            enqueue_pack(
                session,
                pack_key="p1",
                request={"folder": str(tmp_path / "p1"), "uploads": "u", "listing": []},
                created_by="t",
            )
            session.commit()
        started: list[UUID] = []

        def start(job_id: UUID) -> Any:
            started.append(job_id)
            return _Gone(-9)  # killed at once

        supervisor = Supervisor(
            _factory(world), world.root, message_slots=1, pack_slots=1, start=start
        )
        supervisor.tick()  # both claimed and started
        assert len(started) == 2 and message_job in started
        pack = next(job_id for job_id in started if job_id != message_job)
        # Next look: both found dead at once. The message is stored as interrupted (Continue);
        # the pack is queued again and started once more.
        supervisor.tick()
        assert _job(world, message_job).status == "interrupted"
        assert _stored(world, chat_id)[-1].stopped == "interrupted"
        assert started.count(pack) == 2 and _job(world, pack).attempts == 2
        supervisor.tick()  # its second build died too: it fails, and the pack says so
        failed = _job(world, pack)
        assert failed.status == "failed" and failed.error
        from app.review.assistant import set_pack

        assert set_pack.read_status(tmp_path / "p1")["state"] == "failed"
