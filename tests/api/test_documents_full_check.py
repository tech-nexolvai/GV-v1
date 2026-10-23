"""The Documents list and the sidebar show each set's newest full check (the chat's one-click check).

The results live in the chat now (`assistant_chats` / `assistant_chat_messages`, mode `full_check`),
not in the old check runs. Both `GET .../packages-summary` (the Documents table) and `GET
.../packages` (the sidebar) say, per set: the newest full check's review result, its counts as its
result screen shows them, when it ran and its chat; whether one runs now; where its drawings are.

Made-up vendors and values only. Expectations are worked out from the rows the test wrote.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event
from sqlalchemy.orm import Session

from alembic import command
from app.api.dependencies import get_session
from app.auth import Principal, Role, authenticate
from app.config import Settings
from app.db.session import session_factory
from app.main import API_PREFIX, create_app
from app.models import (
    AssistantChat,
    AssistantChatMessage,
    AssistantFindingDecision,
    Package,
    PackageRevision,
    Project,
)
from app.review.assistant import chat_store
from app.review.assistant.chat_service import PLAN_CHECK_FOLDER, set_pack_state
from app.review.assistant.review_action import CheckState, review_action
from app.review.assistant.set_pack import pack_folder
from tests.app.postgres_fixture import alembic_config
from tests.review.assistant.test_proof_drawing import _ai_only, _proved, _right
from tests.review.assistant.test_rule_notes import _filler

pytest_plugins = ("tests.app.postgres_fixture",)

BASE_TIME = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


def _app(session: Session, project_id: UUID, chat_root: Path) -> Any:
    app = create_app(
        Settings(
            database_url="postgresql+psycopg://gv:gv@localhost:5433/gv",
            assistant_chat_root=chat_root,
        )
    )
    app.dependency_overrides[authenticate] = lambda: Principal(
        id="reviewer", roles=frozenset({Role.REVIEWER}), projects=frozenset({project_id})
    )
    app.dependency_overrides[get_session] = lambda: session
    return app


def _project(session: Session) -> Project:
    project = Project(name="documents full check tests")
    session.add(project)
    session.flush()
    return project


def _package(session: Session, project: Project, vendor: str, minutes: int) -> Package:
    package = Package(project_id=project.id, vendor=vendor, product_type="cabinet")
    session.add(package)
    session.flush()
    # The old pipeline's state: never moved on, as on a set read only by the chat.
    session.add(
        PackageRevision(
            package_id=package.id,
            revision_number=1,
            state="UPLOADING",
            created_at=BASE_TIME + timedelta(minutes=minutes),
        )
    )
    session.flush()
    return package


def _chat(session: Session, package: Package) -> AssistantChat:
    chat = AssistantChat(package_id=package.id, created_by="reviewer")
    session.add(chat)
    session.flush()
    return chat


def _message(
    session: Session,
    chat: AssistantChat,
    seq: int,
    *,
    role: str = "assistant",
    mode: str | None = "full_check",
    blocks: list[dict[str, Any]] | None = None,
    minutes: int,
    stopped: str | None = None,
) -> AssistantChatMessage:
    message = AssistantChatMessage(
        chat_id=chat.id,
        seq=seq,
        role=role,
        created_by="reviewer",
        text="Full check" if role == "user" else "Done.",
        blocks=blocks,
        mode=mode,
        stopped=stopped,
        created_at=BASE_TIME + timedelta(minutes=minutes),
    )
    session.add(message)
    session.flush()
    return message


STATE = CheckState(still_open=0)


def _findings() -> list[dict[str, Any]]:
    """One of each: Wrong (proved), Right, Ask the architect (a GV rule), Not compared (AI only),
    a Wrong the reviewer calls not a problem, and a Not compared a later answer drops."""
    return [
        _proved(id="F1"),
        _right(id="F4"),
        _filler(id="F9"),
        _ai_only(id="F2"),
        _proved(id="F5", item="Toe kick depth"),
        _ai_only(id="F6", item="Shelf depth"),
    ]


def _blocks(findings: list[dict[str, Any]], *, package: bool) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [{"type": "finding", "finding": f} for f in findings]
    if package:
        blocks.append({"type": "package", "package": {"state": STATE.as_dict(), "numbers": {}}})
    return blocks


def _seed(session: Session) -> dict[str, Any]:
    project = _project(session)
    checked = _package(session, project, "Made-up Stone Co", minutes=1)
    unchecked = _package(session, project, "Example Millwork", minutes=5)
    # An older full check in another chat, stopped before its package.
    old_chat = _chat(session, checked)
    _message(session, old_chat, 1, role="user", minutes=10)
    _message(
        session,
        old_chat,
        2,
        blocks=_blocks([_proved(id="F1")], package=False),
        minutes=12,
        stopped="cost_cap",
    )
    # The newest full check, its package made.
    chat = _chat(session, checked)
    _message(session, chat, 1, role="user", minutes=20)
    answer = _message(session, chat, 2, blocks=_blocks(_findings(), package=True), minutes=30)
    # The reviewer: F5 is not a problem.
    session.add(
        AssistantFindingDecision(
            chat_id=chat.id,
            message_id=answer.id,
            finding_id="F5",
            decision="not_a_problem",
            decided_by="reviewer",
        )
    )
    # A later question's answer drops F6 (#1548).
    _message(session, chat, 3, role="user", mode="chat", minutes=31)
    _message(
        session,
        chat,
        4,
        mode="chat",
        minutes=32,
        blocks=[
            {
                "type": "finding_update",
                "update": {"message_id": str(answer.id), "finding_id": "F6", "dropped": "no item"},
            }
        ],
    )
    session.flush()
    return {
        "project": project,
        "checked": checked,
        "unchecked": unchecked,
        "chat": chat,
        "answer": answer,
    }


def _expected_review() -> dict[str, str]:
    shown = [
        {**f, "decision": {"decision": "not_a_problem", "by": "reviewer"}} if f["id"] == "F5" else f
        for f in _findings()
        if f["id"] != "F6"
    ]
    found = review_action(shown, STATE).as_dict()
    return {"action": found["action"], "words": found["words"]}


def _get(client: TestClient, path: str) -> dict[str, Any]:
    response = client.get(f"{API_PREFIX}{path}")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def test_both_lists_show_the_newest_full_check(session: Session, tmp_path: Path) -> None:
    seeded = _seed(session)
    project, chat, answer = seeded["project"], seeded["chat"], seeded["answer"]
    client = TestClient(_app(session, project.id, tmp_path))

    summary = {
        row["package_id"]: row
        for row in _get(client, f"/projects/{project.id}/packages-summary")["items"]
    }
    listed = {row["id"]: row for row in _get(client, f"/projects/{project.id}/packages")["items"]}

    for rows, key in ((summary, "package_id"), (listed, "id")):
        found = rows[str(seeded["checked"].id)]
        assert found["full_check"] == {
            "chat_id": str(chat.id),
            "message_id": str(answer.id),
            "ran_at": found["full_check"]["ran_at"],
            "stopped": None,
            "review_result": _expected_review(),
            # F5 (not a problem) and F6 (dropped later) are counted in none.
            "counts": {
                "wrong": 1,
                "right": 1,
                "ask_architect": 1,
                "not_compared": 1,
                "likely_problem": 0,
            },
        }, key
        assert datetime.fromisoformat(found["full_check"]["ran_at"]) == answer.created_at
        assert found["checking"] is False
        # No copy of the PDFs was made in this chat root: nothing was started.
        assert found["drawings"] is None
        none = rows[str(seeded["unchecked"].id)]
        assert (none["full_check"], none["checking"], none["drawings"]) == (None, False, None)


def test_a_full_check_is_the_sets_newest_change(session: Session, tmp_path: Path) -> None:
    seeded = _seed(session)
    client = TestClient(_app(session, seeded["project"].id, tmp_path))
    items = _get(client, f"/projects/{seeded['project'].id}/packages-summary")["items"]
    # The checked set was made first, but its full check (minute 30) is newer than the other's
    # revision (minute 5).
    assert [row["package_id"] for row in items] == [
        str(seeded["checked"].id),
        str(seeded["unchecked"].id),
    ]
    assert datetime.fromisoformat(items[0]["updated_at"]) == seeded["answer"].created_at


def test_a_newest_check_stopped_before_its_package_has_no_result(
    session: Session, tmp_path: Path
) -> None:
    project = _project(session)
    package = _package(session, project, "Sample Joinery", minutes=1)
    chat = _chat(session, package)
    _message(session, chat, 1, role="user", minutes=2)
    _message(
        session,
        chat,
        2,
        blocks=_blocks([_proved(id="F1"), _right(id="F4")], package=False),
        minutes=3,
        stopped="cancelled",
    )
    client = TestClient(_app(session, project.id, tmp_path))
    found = _get(client, f"/projects/{project.id}/packages-summary")["items"][0]["full_check"]
    assert found["review_result"] is None
    assert found["stopped"] == "cancelled"
    assert found["counts"] == {
        "wrong": 1,
        "right": 1,
        "ask_architect": 0,
        "not_compared": 0,
        "likely_problem": 0,
    }


def test_the_summary_asks_the_same_number_of_queries_for_any_number_of_sets(
    session: Session, tmp_path: Path, postgres_engine: Engine
) -> None:
    """One query finds every set's newest full check: never a query per set, even with nothing
    worked out before."""
    project = _project(session)

    def add_checked(n: int) -> None:
        package = _package(session, project, f"Made-up vendor {n}", minutes=n)
        chat = _chat(session, package)
        _message(session, chat, 1, role="user", minutes=n + 1)
        _message(session, chat, 2, blocks=_blocks(_findings(), package=True), minutes=n + 2)

    client = TestClient(_app(session, project.id, tmp_path))
    counted: list[str] = []

    def count(*args: Any) -> None:
        counted.append(str(args[2]))

    def queries() -> int:
        chat_store._SUMMARIES.clear()  # nothing worked out before: every set is read
        counted.clear()
        event.listen(postgres_engine, "before_cursor_execute", count)
        try:
            _get(client, f"/projects/{project.id}/packages-summary")
            _get(client, f"/projects/{project.id}/packages")
        finally:
            event.remove(postgres_engine, "before_cursor_execute", count)
        return len(counted)

    add_checked(1)
    queries()  # anything a first request reads once
    one = queries()
    for n in range(2, 7):
        add_checked(n * 10)
    assert queries() == one


def test_a_running_full_check_is_said(session: Session, tmp_path: Path) -> None:
    seeded = _seed(session)
    app = _app(session, seeded["project"].id, tmp_path)
    from app.api.assistant_chats import _registry_of

    run = _registry_of(app).claim(uuid4(), seeded["unchecked"].id, uuid4())
    assert run is not None
    run.live = object()  # type: ignore[assignment] - only "a full check's live card exists"
    rows = _get(TestClient(app), f"/projects/{seeded['project'].id}/packages")["items"]
    checking = {row["id"]: row["checking"] for row in rows}
    assert checking == {str(seeded["unchecked"].id): True, str(seeded["checked"].id): False}


# ---- where the drawings are (the set pack), read-only ------------------------------------------


def _listing(root: Path) -> list[dict[str, Any]]:
    listing = [{"name": "made-up-set.pdf", "sha256": "ab" * 32, "kind": "the vendor's drawings"}]
    root.mkdir(parents=True, exist_ok=True)
    (root / "uploads.json").write_text(json.dumps(listing), encoding="utf-8")
    return listing


def test_the_set_packs_state_is_read_without_starting_anything(tmp_path: Path) -> None:
    package_id = uuid4()
    assert set_pack_state(tmp_path, package_id) is None
    listing = _listing(tmp_path / PLAN_CHECK_FOLDER / str(package_id))
    # The PDFs were copied but no pack was started: still nothing.
    assert set_pack_state(tmp_path, package_id) is None
    assert not (tmp_path / "set-packs").exists()
    folder = pack_folder(tmp_path, listing)
    folder.mkdir(parents=True)
    (folder / "status.json").write_text('{"state": "building"}', encoding="utf-8")
    assert set_pack_state(tmp_path, package_id) == "building"
    (folder / "status.json").write_text('{"state": "failed"}', encoding="utf-8")
    assert set_pack_state(tmp_path, package_id) == "failed"
    (folder / "page_map.json").write_text('{"pages": []}', encoding="utf-8")
    assert set_pack_state(tmp_path, package_id) == "ready"


def test_a_chats_own_copy_is_read_when_the_plan_check_made_none(tmp_path: Path) -> None:
    package_id, chat_id = uuid4(), uuid4()
    listing = _listing(tmp_path / str(chat_id))
    folder = pack_folder(tmp_path, listing)
    folder.mkdir(parents=True)
    assert set_pack_state(tmp_path, package_id) is None
    assert set_pack_state(tmp_path, package_id, (chat_id,)) == "building"


def test_the_lists_say_reading_then_read(session: Session, tmp_path: Path) -> None:
    seeded = _seed(session)
    package_id = seeded["unchecked"].id
    listing = _listing(tmp_path / PLAN_CHECK_FOLDER / str(package_id))
    folder = pack_folder(tmp_path, listing)
    folder.mkdir(parents=True)
    client = TestClient(_app(session, seeded["project"].id, tmp_path))

    def drawings() -> Any:
        rows = _get(client, f"/projects/{seeded['project'].id}/packages-summary")["items"]
        return {row["package_id"]: row["drawings"] for row in rows}[str(package_id)]

    assert drawings() == "reading"
    (folder / "page_map.json").write_text('{"pages": []}', encoding="utf-8")
    assert drawings() == "read"


def test_a_new_decision_or_correction_is_seen_at_once(session: Session, tmp_path: Path) -> None:
    """Summaries are kept between requests by what they depend on; a new decision row or a later
    answer is a new key, so the lists never show a stale count."""
    seeded = _seed(session)
    chat, answer = seeded["chat"], seeded["answer"]
    client = TestClient(_app(session, seeded["project"].id, tmp_path))

    def counts() -> dict[str, int]:
        rows = _get(client, f"/projects/{seeded['project'].id}/packages-summary")["items"]
        found: dict[str, int] = rows[0]["full_check"]["counts"]
        return found

    assert counts() == {
        "wrong": 1,
        "right": 1,
        "ask_architect": 1,
        "not_compared": 1,
        "likely_problem": 0,
    }
    # The reviewer takes "Not a problem" back on F5: Wrong again.
    session.add(
        AssistantFindingDecision(
            chat_id=chat.id,
            message_id=answer.id,
            finding_id="F5",
            decision="undo",
            decided_by="reviewer",
        )
    )
    session.flush()
    assert counts() == {
        "wrong": 2,
        "right": 1,
        "ask_architect": 1,
        "not_compared": 1,
        "likely_problem": 0,
    }
    # A later answer drops F1.
    _message(session, chat, 5, role="user", mode="chat", minutes=40)
    _message(
        session,
        chat,
        6,
        mode="chat",
        minutes=41,
        blocks=[
            {
                "type": "finding_update",
                "update": {"message_id": str(answer.id), "finding_id": "F1", "dropped": "no item"},
            }
        ],
    )
    assert counts() == {
        "wrong": 1,
        "right": 1,
        "ask_architect": 1,
        "not_compared": 1,
        "likely_problem": 0,
    }
