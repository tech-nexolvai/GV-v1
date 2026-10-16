"""The chat-first assistant's routes (#1463), through the app, on a real PostgreSQL.

- Access: a package or chat outside the caller's projects is a 404 on every route.
- Chats per drawing set: start, list (newest first, titled by the first message), read.
- A message streams the contract's events and ends with `message_end` carrying the stored message;
  the first message lists the uploaded files; the next turn replays the stored history byte for
  byte, pictures included, read back from the database.
- 409 while a message runs, 422 for a choice the API would refuse, 503 with no provider key.
- Files the agent handed over download as attachments; pictures it viewed are served.
- The three tables are append-only in the database.
- Each call is recorded where the Usage page reads it, labelled by chat; the set's money cap stops
  a message before any call; the browser going away cancels the message, which is still stored.

No paid call is possible: the provider, the sandbox and the skill builder are fakes.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from PIL import Image

from tests.review.assistant.chat_fakes import (
    FakeSandbox,
    ScriptedProvider,
    reply,
    server_error,
    strip_cache_control,
    text,
    thinking,
    tool,
)

pytest_plugins = ("tests.app.postgres_fixture",)

PDF = b"%PDF-1.7\n% a made-up drawing set for a test\n%%EOF\n"
DEFAULT = {"model": "claude-opus-5-5", "effort": "high", "thinking": True}


def _events(body: str) -> list[tuple[str, Any]]:
    parsed: list[tuple[str, Any]] = []
    for block in body.split("\n\n"):
        event, data = "message", None
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line.removeprefix("event:").strip()
            elif line.startswith("data:"):
                data = json.loads(line.removeprefix("data:").strip())
        if data is not None:
            parsed.append((event, data))
    return parsed


def _named(events: list[tuple[str, Any]], name: str) -> list[Any]:
    return [data for event, data in events if event == name]


def _png(path: Path) -> None:
    out = io.BytesIO()
    Image.new("RGB", (30, 20), (1, 2, 3)).save(out, format="PNG")
    path.write_bytes(out.getvalue())


@dataclass
class World:
    client: Any
    app: Any
    session: Any
    project_id: UUID
    package_id: UUID
    revision_id: UUID
    root: Path
    provider: ScriptedProvider
    sandboxes: list[FakeSandbox] = field(default_factory=list)
    sandbox_ready: dict[str, bool] = field(default_factory=lambda: {"on": True})

    def chat(self) -> str:
        response = self.client.post(f"/api/v1/packages/{self.package_id}/chats")
        assert response.status_code == 201, response.text
        return str(response.json()["id"])

    def ask(self, chat_id: str, words: str, **choice: Any) -> tuple[int, list[tuple[str, Any]]]:
        response = self.client.post(
            f"/api/v1/chats/{chat_id}/messages", json={"text": words, **DEFAULT, **choice}
        )
        return response.status_code, _events(response.text)


@contextmanager
def _world(  # type: ignore[no-untyped-def]
    postgres_engine,
    tmp_path: Path,
    *,
    provider: ScriptedProvider | None = None,
    member: bool = True,
    available: bool = True,
    handler: Callable[[str, Any], tuple[str, int]] | None = None,
    settings: Mapping[str, Any] | None = None,
) -> Iterator[World]:
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import Session

    from alembic import command
    from app.api.assistant_chats import ChatRuntime, chat_runtime
    from app.api.dependencies import ARTIFACT_STORE_STATE, get_session
    from app.api.documents import storage_key
    from app.auth import Principal, Role, authenticate
    from app.config import Settings
    from app.db.session import session_factory
    from app.main import create_app
    from app.models import Package, PackageRevision, PackageState, Project
    from app.models.document import (
        Document,
        DocumentKind,
        DocumentVersion,
        PackageRevisionDocument,
        SourceArtifact,
    )
    from app.review.assistant.chat_provider import ProviderChoice
    from app.runs.rates import load_model_rates
    from storage.local import LocalStore
    from tests.app.postgres_fixture import alembic_config

    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")

    scripted = provider or ScriptedProvider()
    store = LocalStore(tmp_path / "store")
    project_id = uuid4()
    session: Session = session_factory(postgres_engine)()
    try:
        session.add(Project(id=project_id, name="chat test"))
        session.flush()
        package = Package(project_id=project_id, vendor="Sample Vendor")
        session.add(package)
        session.flush()
        revision = PackageRevision(
            package_id=package.id, revision_number=1, state=PackageState.UPLOADING
        )
        session.add(revision)
        session.flush()
        digest = hashlib.sha256(PDF).hexdigest()
        document = Document(package_id=package.id, kind=DocumentKind.SHOP)
        session.add(document)
        session.flush()
        artifact = SourceArtifact(
            storage_key=f"originals/{uuid4().hex}.pdf",
            sha256=digest,
            size=len(PDF),
            backend_version_id=None,
        )
        session.add(artifact)
        session.flush()
        version = DocumentVersion(
            document_id=document.id,
            source_artifact_id=artifact.id,
            sha256=digest,
            page_count=62,
            file_name="Vendor set.pdf",
        )
        session.add(version)
        session.flush()
        session.add(
            PackageRevisionDocument(
                package_revision_id=revision.id,
                package_id=package.id,
                document_id=document.id,
                document_version_id=version.id,
            )
        )
        session.commit()
        store.put(storage_key(document.id, digest), io.BytesIO(PDF), content_type="application/pdf")

        app = create_app(
            Settings(
                database_url="postgresql+psycopg://unused@localhost/unused",
                environment="test",
                assistant_chat_root=tmp_path / "chats",
                **(settings or {}),
            )
        )
        setattr(app.state, ARTIFACT_STORE_STATE, store)
        app.dependency_overrides[get_session] = lambda: session
        app.dependency_overrides[authenticate] = lambda: Principal(
            id="reviewer-one",
            roles=frozenset({Role.REVIEWER}),
            projects=frozenset({project_id} if member else set()),
        )
        sandboxes: list[FakeSandbox] = []
        ready = {"on": True}

        def open_sandbox(workspace: Any) -> FakeSandbox | None:
            if not ready["on"]:
                return None
            sandbox = FakeSandbox(workspace, handler or (lambda c, _w: ("ok", 0)))
            sandboxes.append(sandbox)
            return sandbox

        def skill(session: Session, revision_id: UUID, dest: Path) -> None:
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "SKILL.md").write_text(f"---\nname: gv-review\n---\nrevision {revision_id}\n")

        rates = load_model_rates(
            Path(__file__).resolve().parents[2] / "deploy" / "model_rates.us-east-1.json"
        )
        app.dependency_overrides[chat_runtime] = lambda: ChatRuntime(
            choice=(
                ProviderChoice("anthropic", "fake-key-not-used")
                if available
                else ProviderChoice("openrouter", None, "No AI provider key is set on this server.")
            ),
            provider=lambda: scripted,
            open_sandbox=open_sandbox,
            skill_builder=lambda: skill,
            rates=lambda _key: rates,
        )
        yield World(
            client=TestClient(app, raise_server_exceptions=False),
            app=app,
            session=session,
            project_id=project_id,
            package_id=package.id,
            revision_id=revision.id,
            root=tmp_path / "chats",
            provider=scripted,
            sandboxes=sandboxes,
            sandbox_ready=ready,
        )
    finally:
        session.close()


# ---- access ------------------------------------------------------------------------------------


def test_every_route_is_a_404_outside_the_callers_projects(postgres_engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    with _world(postgres_engine, tmp_path) as world:
        chat_id = world.chat()
    with _world(postgres_engine, tmp_path / "b", member=False) as outsider:
        client = outsider.client
        package = outsider.package_id
        # A chat of a package the caller cannot see, and a made-up one.
        from app.models import AssistantChat

        foreign = AssistantChat(package_id=package, created_by="someone")
        outsider.session.add(foreign)
        outsider.session.commit()
        for chat in (str(foreign.id), chat_id, str(uuid4())):
            assert client.get(f"/api/v1/chats/{chat}").status_code == 404
            assert client.post(f"/api/v1/chats/{chat}/cancel").status_code == 404
            assert (
                client.post(f"/api/v1/chats/{chat}/messages", json={"text": "hi", **DEFAULT})
            ).status_code == 404
            assert client.get(f"/api/v1/chats/{chat}/files/{uuid4()}").status_code == 404
            assert client.get(f"/api/v1/chats/{chat}/pictures/{uuid4()}").status_code == 404
        assert client.get(f"/api/v1/packages/{package}/chats").status_code == 404
        assert client.post(f"/api/v1/packages/{package}/chats").status_code == 404
        assert client.get(f"/api/v1/assistant/models?package_id={package}").status_code == 404
        assert outsider.provider.calls == []


# ---- chats and messages ------------------------------------------------------------------------


def test_a_message_streams_and_ends_with_the_stored_message(postgres_engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    provider = ScriptedProvider(
        lead=[
            reply(
                thinking("Read the skill first."),
                tool(
                    "t1",
                    "view",
                    {"path": "/mnt/skills/gv-review/SKILL.md", "description": "Read the skill"},
                ),
                stop="tool_use",
            ),
            reply(text("| Page | Item |\n|---|---|\n| 4 | sink |")),
        ]
    )
    with _world(postgres_engine, tmp_path, provider=provider) as world:
        chat_id = world.chat()
        listed = world.client.get(f"/api/v1/packages/{world.package_id}/chats").json()
        assert listed["chats"][0] == {
            "id": chat_id,
            "title": None,
            "created_at": listed["chats"][0]["created_at"],
            "last_message_at": None,
            "turns": 0,
        }

        status, events = world.ask(
            chat_id, "Which items are wrong comparing the vendor and architect drawings?"
        )

        assert status == 200
        names = [name for name, _ in events]
        assert names[0] == "message_start" and names[-1] == "message_end"
        start = events[0][1]
        assert start["model"] == "claude-opus-5-5" and start["effort"] == "high"
        assert start["thinking"] is True
        for expected in (
            "thinking_start",
            "thinking_delta",
            "thinking_end",
            "step_start",
            "step_end",
            "text_delta",
            "usage",
        ):
            assert expected in names
        stored = events[-1][1]["message"]
        assert stored["id"] == start["message_id"]
        assert stored["text"] == "| Page | Item |\n|---|---|\n| 4 | sink |"
        assert [b["type"] for b in stored["blocks"]] == ["thinking", "step", "text"]
        assert stored["blocks"][1]["step"]["output"].startswith("/mnt/skills/gv-review/SKILL.md")
        assert stored["cost_usd"] is not None and stored["stopped"] is None

        chat = world.client.get(f"/api/v1/chats/{chat_id}").json()
        assert chat["package_name"] == "Sample Vendor"
        assert chat["title"] == "Which items are wrong comparing the vendor and architect"
        assert chat["running"] is False
        assert [m["role"] for m in chat["messages"]] == ["user", "assistant"]
        assert chat["messages"][1] == stored

        # The first message told the model what was uploaded.
        first = json.loads(provider.lead_calls[0])
        listing = first["messages"][0]["content"][0]["text"]
        assert listing.startswith("<uploaded_files>")
        assert 'path="/mnt/user-data/uploads/Vendor set.pdf"' in listing and 'pages="62"' in listing
        assert first["messages"][0]["content"][1]["text"].startswith("Which items")
        # The set's PDF and the skill are in the chat's folder.
        folder = world.root / chat_id
        assert (folder / "uploads" / "Vendor set.pdf").read_bytes() == PDF
        assert "name: gv-review" in (folder / "skills" / "gv-review" / "SKILL.md").read_text()

        listed = world.client.get(f"/api/v1/packages/{world.package_id}/chats").json()["chats"]
        assert listed[0]["turns"] == 1 and listed[0]["title"] == chat["title"]


def test_the_chat_is_listed_with_its_title_when_its_first_message_starts(postgres_engine, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The screen reads the chat list when it sees `message_start`: the question is stored by then,
    so a chat whose first message is still being answered is listed, titled by it."""
    from app.api import assistant_chats
    from app.review.assistant.chat_store import chat_summaries

    seen: list[tuple[str | None, int]] = []
    real = assistant_chats.run_message

    def spy(job, run, emit):  # type: ignore[no-untyped-def]
        def tap(event: str, data: dict[str, Any]) -> None:
            if event == "message_start":
                with job.factory() as session:
                    listed = chat_summaries(session, job.package_id)[0]
                    seen.append((listed.title, listed.turns))
            emit(event, data)

        real(job, run, tap)

    monkeypatch.setattr(assistant_chats, "run_message", spy)
    with _world(
        postgres_engine, tmp_path, provider=ScriptedProvider(lead=[reply(text("Hi."))])
    ) as world:
        chat_id = world.chat()

        status, events = world.ask(chat_id, "How many pages are in this set?")

    assert status == 200 and events[0][0] == "message_start"
    assert seen == [("How many pages are in this set?", 1)]


def test_the_next_turn_replays_the_stored_history_byte_for_byte(postgres_engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    def render(command: str, workspace: Any) -> tuple[str, int]:
        _png(workspace.home / "p4.png")
        return "", 0

    provider = ScriptedProvider(
        lead=[
            reply(
                thinking("Render it."),
                tool(
                    "a", "bash_tool", {"command": "pdftoppm -f 4", "description": "Render page 4"}
                ),
                stop="tool_use",
            ),
            reply(
                tool("b", "view", {"path": "/home/claude/p4.png", "description": "Look"}),
                stop="tool_use",
            ),
            reply(thinking("Seen."), text("Page 4 has a sink.")),
            reply(text("Yes, the same sink.")),
        ]
    )
    with _world(postgres_engine, tmp_path, provider=provider, handler=render) as world:
        chat_id = world.chat()
        world.ask(chat_id, "What is on page 4?")
        status, _events = world.ask(chat_id, "Is it the same sink?", model="claude-opus-5-5")

        assert status == 200
        last_of_first = json.loads(provider.lead_calls[2])["messages"]
        second = json.loads(provider.lead_calls[3])["messages"]
        assert json.dumps(strip_cache_control(second[: len(last_of_first)])) == json.dumps(
            strip_cache_control(last_of_first)
        )
        assert second[len(last_of_first)]["content"][-1] == {
            "type": "text",
            "text": "Page 4 has a sink.",
        }
        # The second message does not list the files again.
        assert strip_cache_control(second[-1]) == {
            "role": "user",
            "content": [{"type": "text", "text": "Is it the same sink?"}],
        }
        # The picture: stored as a reference, served for the screen, re-sent as the same bytes.
        stored = world.client.get(f"/api/v1/chats/{chat_id}").json()["messages"][1]
        picture = next(
            b["step"]["picture_id"]
            for b in stored["blocks"]
            if b["type"] == "step" and b["step"]["picture_id"]
        )
        served = world.client.get(f"/api/v1/chats/{chat_id}/pictures/{picture}")
        assert served.status_code == 200 and served.headers["content-type"] == "image/png"
        from sqlalchemy import text as sql

        raw = world.session.execute(
            sql(
                "SELECT api_content::text FROM assistant_chat_messages WHERE role = 'assistant' ORDER BY seq LIMIT 1"
            )
        ).scalar_one()
        assert picture in raw and '"data"' not in raw


def test_a_handed_over_file_downloads_as_an_attachment(postgres_engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    provider = ScriptedProvider(
        lead=[
            reply(
                tool(
                    "c",
                    "create_file",
                    {
                        "path": "/mnt/user-data/outputs/issues.csv",
                        "file_text": "page,item\n4,sink\n",
                        "description": "CSV",
                    },
                ),
                tool("p", "present_files", {"filepaths": ["/mnt/user-data/outputs/issues.csv"]}),
                stop="tool_use",
            ),
            reply(text("Here it is.")),
        ]
    )
    with _world(postgres_engine, tmp_path, provider=provider) as world:
        chat_id = world.chat()
        _, events = world.ask(chat_id, "Make me a CSV")

        shown = _named(events, "file")[0]["file"]
        assert (
            shown["name"] == "issues.csv"
            and shown["url"] == f"/api/v1/chats/{chat_id}/files/{shown['id']}"
        )
        stored = events[-1][1]["message"]
        assert [f["name"] for f in stored["files"]] == ["issues.csv"]

        download = world.client.get(shown["url"])
        assert download.status_code == 200 and download.text == "page,item\n4,sink\n"
        assert download.headers["content-disposition"].startswith("attachment;")
        assert 'filename="issues.csv"' in download.headers["content-disposition"]

        # A copy that no longer matches its recorded hash is never served.
        (world.root / chat_id / "files" / shown["id"]).write_text("changed")
        assert world.client.get(shown["url"]).status_code == 404


def test_a_second_message_while_one_runs_is_a_409(postgres_engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from app.api.assistant_chats import chat_registry

    with _world(postgres_engine, tmp_path) as world:
        chat_id = world.chat()

        class _Request:
            app = world.app

        registry = chat_registry(_Request())  # type: ignore[arg-type]
        run = registry.claim(UUID(chat_id), world.package_id, uuid4())
        assert run is not None
        run.started = True

        response = world.client.post(
            f"/api/v1/chats/{chat_id}/messages", json={"text": "Anything?", **DEFAULT}
        )
        assert response.status_code == 409 and response.json()["error"] == "message_running"
        assert world.client.get(f"/api/v1/chats/{chat_id}").json()["running"] is True
        assert world.client.post(f"/api/v1/chats/{chat_id}/cancel").json() == {"cancelled": True}
        assert run.cancel.is_set()
        registry.release(run)
        assert world.client.post(f"/api/v1/chats/{chat_id}/cancel").json() == {"cancelled": False}


@pytest.mark.parametrize(
    ("choice", "words"),
    [
        ({"model": "claude-opus-5-5", "effort": "high", "thinking": False}, "always on"),
        ({"model": "claude-sonnet-5-5", "effort": "max", "thinking": False}, "high or below"),
        ({"model": "gpt-5", "effort": "high", "thinking": True}, "not one of the models"),
    ],
)
def test_a_choice_the_api_would_refuse_is_a_422(postgres_engine, tmp_path, choice, words) -> None:  # type: ignore[no-untyped-def]
    with _world(postgres_engine, tmp_path) as world:
        chat_id = world.chat()
        response = world.client.post(
            f"/api/v1/chats/{chat_id}/messages", json={"text": "hi", **choice}
        )
        assert response.status_code == 422 and words in response.json()["message"]
        assert response.json()["error"] == "choice_refused"
        assert world.provider.calls == []


def test_the_request_shape_is_refused_at_the_door(postgres_engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    with _world(postgres_engine, tmp_path) as world:
        chat_id = world.chat()
        url = f"/api/v1/chats/{chat_id}/messages"
        assert world.client.post(url, json={"text": "", **DEFAULT}).status_code == 422
        assert world.client.post(url, json={"text": "x" * 8001, **DEFAULT}).status_code == 422
        assert (
            world.client.post(url, json={"text": "hi", **DEFAULT, "temperature": 1}).status_code
            == 422
        )
        assert (
            world.client.post(url, json={"text": "hi", **DEFAULT, "effort": "huge"}).status_code
            == 422
        )


def test_with_no_provider_key_the_models_say_why_and_a_message_is_a_503(postgres_engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    with _world(postgres_engine, tmp_path, available=False) as world:
        models = world.client.get("/api/v1/assistant/models").json()
        assert models["available"] is False and "No AI provider key" in models["reason"]
        chat_id = world.chat()
        response = world.client.post(
            f"/api/v1/chats/{chat_id}/messages", json={"text": "hi", **DEFAULT}
        )
        assert response.status_code == 503
        assert world.provider.calls == []


def test_the_models_offered_and_the_caps(postgres_engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    with _world(postgres_engine, tmp_path) as world:
        models = world.client.get(f"/api/v1/assistant/models?package_id={world.package_id}").json()
    assert models["available"] is True and models["provider"] == "anthropic"
    assert models["message_cap_usd"] == "5.00" and models["package_cap_usd"] == "40.00"
    assert models["package_spent_usd"] == "0.00"
    assert models["models"][0] == {
        "id": "claude-opus-5-5",
        "label": "Claude Opus 5.5",
        "default": True,
        "efforts": ["low", "medium", "high", "xhigh", "max"],
        "default_effort": "high",
        "thinking": "always",
        "thinking_off_max_effort": None,
    }
    assert [m["id"] for m in models["models"]] == [
        "claude-opus-5-5",
        "claude-sonnet-5-5",
        "claude-haiku-5-5",
    ]
    assert models["models"][1]["thinking_off_max_effort"] == "high"


def test_the_real_runtime_never_shows_a_key(postgres_engine, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.api.assistant_chats import chat_runtime

    secret = "sk-or-v1-TEST-NEVER-SHOWN"
    monkeypatch.setenv("OPENROUTER_API_KEY", secret)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with _world(postgres_engine, tmp_path) as world:
        del world.app.dependency_overrides[chat_runtime]
        response = world.client.get("/api/v1/assistant/models")
    assert response.status_code == 200 and response.json()["provider"] == "openrouter"
    assert secret not in response.text


# ---- records -----------------------------------------------------------------------------------


def test_each_call_is_recorded_for_the_usage_page_labelled_by_chat(postgres_engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from sqlalchemy import select

    from app.models import ModelInvocation

    provider = ScriptedProvider(lead=[server_error(), reply(text("Done."))])
    with _world(postgres_engine, tmp_path, provider=provider) as world:
        chat_id = world.chat()
        world.ask(chat_id, "Hi")
        rows = world.session.scalars(
            select(ModelInvocation).order_by(ModelInvocation.created_at)
        ).all()
        assert len(rows) == 2
        assert {row.template_id for row in rows} == {f"chat:{chat_id}"}
        assert {row.prompt_id for row in rows} == {"review-assistant-chat-anthropic-chat-v2"}
        assert all(row.package_revision_id == world.revision_id for row in rows)
        assert rows[0].outcome != "ok" and rows[1].outcome == "ok"
        models = world.client.get(f"/api/v1/assistant/models?package_id={world.package_id}").json()
        assert Decimal(models["package_spent_usd"]) > 0


def test_a_failed_message_sends_an_error_then_the_stored_message(postgres_engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    provider = ScriptedProvider(lead=[server_error(), server_error()])
    with _world(postgres_engine, tmp_path, provider=provider) as world:
        chat_id = world.chat()
        _, events = world.ask(chat_id, "Hi")
        names = [name for name, _ in events]
        assert names.index("error") < names.index("message_end")
        assert _named(events, "error")[0] == {
            "message": "The AI service had a problem (error 529). Please ask again."
        }
        stored = events[-1][1]["message"]
        assert stored["stopped"] == "error" and stored["error"].startswith("The AI service")


def test_the_drawing_sets_budget_stops_a_message_before_any_call(postgres_engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    provider = ScriptedProvider(lead=[reply(text("First answer."))])
    with _world(
        postgres_engine,
        tmp_path,
        provider=provider,
        settings={"assistant_package_cap_usd": Decimal("0.01")},
    ) as world:
        chat_id = world.chat()
        world.ask(chat_id, "First")  # costs more than a cent
        calls = len(provider.calls)
        _, events = world.ask(chat_id, "Second")
        assert len(provider.calls) == calls
        stored = events[-1][1]["message"]
        assert stored["stopped"] == "cost_cap" and "assistant budget" in stored["text"]


def test_the_three_tables_are_append_only(postgres_engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from sqlalchemy import text as sql
    from sqlalchemy.exc import DBAPIError

    provider = ScriptedProvider(
        lead=[
            reply(
                tool(
                    "c",
                    "create_file",
                    {"path": "/mnt/user-data/outputs/a.txt", "file_text": "a", "description": "d"},
                ),
                tool("p", "present_files", {"filepaths": ["/mnt/user-data/outputs/a.txt"]}),
                stop="tool_use",
            ),
            reply(text("Done.")),
        ]
    )
    with _world(postgres_engine, tmp_path, provider=provider) as world:
        chat_id = world.chat()
        world.ask(chat_id, "Hi")
        for statement in (
            "UPDATE assistant_chats SET title = 'x'",
            "DELETE FROM assistant_chats",
            "UPDATE assistant_chat_messages SET text = 'x'",
            "DELETE FROM assistant_chat_messages",
            "UPDATE assistant_chat_files SET name = 'x'",
            "DELETE FROM assistant_chat_files",
        ):
            with pytest.raises(DBAPIError):
                world.session.execute(sql(statement))
            world.session.rollback()
        count = world.session.execute(sql("SELECT count(*) FROM assistant_chat_files")).scalar_one()
        assert count == 1


def test_the_browser_going_away_cancels_the_message_which_is_still_stored(postgres_engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from sqlalchemy.orm import sessionmaker

    from app.api.assistant_chats import _Message, send_message
    from app.review.assistant.chat_request import MODELS
    from app.review.assistant.chat_service import ChatRegistry, MessageJob
    from app.review.assistant.chat_store import messages_of
    from app.runs.rates import load_model_rates

    held = reply(text("Partial"))
    held.insert(3, {"type": "__wait_for_cancel__"})
    provider = ScriptedProvider(lead=[held])
    with _world(postgres_engine, tmp_path, provider=provider) as world:
        chat_id = UUID(world.chat())
        registry = ChatRegistry()
        run = registry.claim(chat_id, world.package_id, uuid4())
        assert run is not None
        provider.cancel = run.cancel
        sandboxes: list[FakeSandbox] = []
        rates = load_model_rates(
            Path(__file__).resolve().parents[2] / "deploy" / "model_rates.us-east-1.json"
        )
        job = MessageJob(
            chat_id=chat_id,
            package_id=world.package_id,
            revision_id=world.revision_id,
            created_by="reviewer-one",
            text="A long question",
            model=MODELS["claude-opus-5-5"],
            effort="high",
            thinking=True,
            chat_root=world.root,
            factory=sessionmaker(bind=world.session.get_bind(), expire_on_commit=False),
            provider=provider,  # type: ignore[arg-type]
            registry=registry,
            lead_max_turns=40,
            helper_max_turns=20,
            message_cap_micros=5_000_000,
            package_cap_micros=40_000_000,
            rates=lambda _key: rates,
            uploads=list,
            results=None,
            open_sandbox=lambda w: sandboxes.append(FakeSandbox(w)) or sandboxes[-1],
            skill_builder=None,
        )

        async def go() -> list[str]:
            stream = send_message(_Message(job=job, run=run))
            seen = []
            async for event in stream:
                seen.append(event.event)
                if event.event == "text_delta":
                    break
            await stream.aclose()  # the browser went away
            return seen

        seen = asyncio.run(go())
        assert seen[0] == "message_start" and "text_delta" in seen
        deadline = threading.Event()
        for _ in range(100):
            if registry.get(chat_id) is None:
                break
            deadline.wait(0.05)
        assert registry.get(chat_id) is None
        with sessionmaker(bind=world.session.get_bind())() as session:
            stored = messages_of(session, chat_id)
        assert [m.role for m in stored] == ["user", "assistant"]
        assert stored[1].stopped == "cancelled" and stored[1].text == "Par"
        assert sandboxes and sandboxes[0].killed >= 1


def test_the_real_skill_builder_writes_the_chats_skill(postgres_engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from app.api.assistant_chats import ChatRuntime, chat_runtime
    from app.review.assistant.chat_service import default_skill_builder

    provider = ScriptedProvider(lead=[reply(text("Read it."))])
    with _world(postgres_engine, tmp_path, provider=provider) as world:
        faked = world.app.dependency_overrides[chat_runtime]()
        world.app.dependency_overrides[chat_runtime] = lambda: ChatRuntime(
            choice=faked.choice,
            provider=faked.provider,
            open_sandbox=faked.open_sandbox,
            skill_builder=default_skill_builder,
            rates=faked.rates,
        )
        chat_id = world.chat()
        world.ask(chat_id, "Hi")
        skill = world.root / chat_id / "skills" / "gv-review"
        assert "name: gv-review" in (skill / "SKILL.md").read_text()
        assert "could not be prepared" not in (skill / "SKILL.md").read_text()
        assert (skill / "rules.md").is_file()


def test_with_no_sandbox_the_message_says_so_and_the_next_one_lists_the_files(postgres_engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    provider = ScriptedProvider(lead=[reply(text("Now I can look."))])
    with _world(postgres_engine, tmp_path, provider=provider) as world:
        chat_id = world.chat()
        world.sandbox_ready["on"] = False
        _, events = world.ask(chat_id, "First try")
        stored = events[-1][1]["message"]
        assert stored["stopped"] == "error" and "sandbox" in stored["error"]
        assert provider.calls == []

        world.sandbox_ready["on"] = True
        world.ask(chat_id, "Second try")
        first = json.loads(provider.lead_calls[0])["messages"]
        # The unanswered first message is not in the history; the files are listed now.
        assert len(first) == 1
        assert first[0]["content"][0]["text"].startswith("<uploaded_files>")
        assert first[0]["content"][1]["text"] == "Second try"
