"""The job tracker's routes (#1578) through the app on a real PostgreSQL: built from the stored chat
answers (each finding at its newest correction, with its latest decision), project-scoped, and
exported as a workbook (or CSV without openpyxl). Read-only: nothing is written, no AI call."""

from __future__ import annotations

import io
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from tests.review.assistant.test_job_tracker import finding

pytest_plugins = ("tests.app.postgres_fixture",)

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


@dataclass
class World:
    client: Any
    session: Any
    project_id: UUID
    first: UUID
    second: UUID


@contextmanager
def _world(postgres_engine: Any, *, member: bool = True) -> Iterator[World]:
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import Session

    from alembic import command
    from app.api.dependencies import get_session
    from app.auth import Principal, Role, authenticate
    from app.config import Settings
    from app.db.session import session_factory
    from app.main import create_app
    from app.models import Package, Project
    from tests.app.postgres_fixture import alembic_config

    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")
    session: Session = session_factory(postgres_engine)()
    project_id = uuid4()
    try:
        session.add(Project(id=project_id, name="tracker test"))
        session.flush()
        first = Package(project_id=project_id, vendor="Acme Millwork", product_type="cabinet")
        second = Package(project_id=project_id, vendor="ACME millwork", product_type="countertop")
        session.add_all([first, second])
        session.commit()
        app = create_app(
            Settings(
                database_url="postgresql+psycopg://unused@localhost/unused", environment="test"
            )
        )
        app.dependency_overrides[get_session] = lambda: session
        app.dependency_overrides[authenticate] = lambda: Principal(
            id="reviewer-one",
            roles=frozenset({Role.REVIEWER}),
            projects=frozenset({project_id} if member else set()),
        )
        yield World(
            client=TestClient(app, raise_server_exceptions=False),
            session=session,
            project_id=project_id,
            first=first.id,
            second=second.id,
        )
    finally:
        session.close()


def _chat(
    session: Any,
    package_id: UUID,
    question: str,
    answers: list[list[dict[str, Any]]],
    *,
    minutes: int = 0,
    mode: str | None = None,
) -> tuple[UUID, list[UUID]]:
    """A stored chat: the question, then each answer's blocks (and a follow-up question between)."""
    from app.models import AssistantChat, AssistantChatMessage

    chat = AssistantChat(package_id=package_id, created_by="reviewer-one")
    session.add(chat)
    session.flush()
    ids: list[UUID] = []
    seq = 0
    for at, blocks in enumerate(answers):
        seq += 1
        session.add(
            AssistantChatMessage(
                chat_id=chat.id,
                seq=seq,
                role="user",
                created_by="reviewer-one",
                text=question if at == 0 else "And now?",
                mode=mode if at == 0 else "chat",
                created_at=T0 + timedelta(minutes=minutes + 2 * at),
            )
        )
        seq += 1
        message = AssistantChatMessage(
            id=uuid4(),
            chat_id=chat.id,
            seq=seq,
            role="assistant",
            created_by="reviewer-one",
            text="Done.",
            blocks=[{"type": "thinking", "text": "long"}, *blocks],
            api_content=[],
            cost_micros=0,
            created_at=T0 + timedelta(minutes=minutes + 2 * at + 1),
        )
        session.add(message)
        session.flush()
        ids.append(message.id)
    session.commit()
    return chat.id, ids


def _block(item: dict[str, Any]) -> dict[str, Any]:
    return {"type": "finding", "finding": item}


def _decide(
    session: Any,
    chat_id: UUID,
    message_id: UUID,
    finding_id: str,
    decision: str,
    *,
    by: str,
    hours: int,
) -> None:
    from app.models import AssistantFindingDecision

    session.add(
        AssistantFindingDecision(
            chat_id=chat_id,
            message_id=message_id,
            finding_id=finding_id,
            decision=decision,
            decided_by=by,
            decided_at=T0 + timedelta(hours=hours),
        )
    )
    session.commit()


def test_the_tracker_follows_items_across_rounds_decisions_and_corrections(postgres_engine) -> None:  # type: ignore[no-untyped-def]
    with _world(postgres_engine) as world:
        seeded = _seed_with_correction(world)
        response = world.client.get(f"/api/v1/projects/{world.project_id}/tracker")
        assert response.status_code == 200, response.text
        body = response.json()
        items = {item["item"]: item for item in body["items"]}
        # The follow-up dropped F6: it is not an item.
        assert set(items) == {
            "Sink base width",
            "Filler at wall end",
            "Outlet height",
            "Countertop overall width",
        }
        assert (items["Sink base width"]["state"], items["Sink base width"]["fixed_round"]) == (
            "fixed",
            2,
        )
        filler = items["Filler at wall end"]
        assert (filler["state"], filler["open_kind"], filler["tags"]) == (
            "open",
            "wrong",
            ["Previous comment not addressed"],
        )
        assert filler["link"] == {
            "package_id": str(world.first),
            "chat_id": str(seeded["chat"]),
            "message_id": str(seeded["answer"]),
            "finding_id": "F5",
        }
        outlet = items["Outlet height"]
        assert (outlet["state"], outlet["decided_by"]) == ("not_a_problem", "reviewer-two")
        assert body["totals"] == {
            "sets": 2,
            "checks": 2,
            "items": 4,
            "open": 2,
            "open_wrong": 2,
            "open_decision": 0,
            "fixed": 1,
            "not_a_problem": 1,
            "right_later": 0,
        }
        kinds = {s["name"]: [c["kind"] for c in s["checks"]] for s in body["sets"]}
        assert kinds == {"Acme Millwork": ["full_check"], "ACME millwork": ["question"]}
        (vendor,) = body["vendors"]
        assert vendor["sets"] == 2 and vendor["mistakes"] == 3
        assert (vendor["fixed_next_round"], vendor["fixed_next_of"]) == (1, 2)
        # Nothing was written.
        from sqlalchemy import func, select

        from app.models import AssistantChatMessage, AssistantFindingDecision

        assert world.session.scalar(select(func.count()).select_from(AssistantChatMessage)) == 6
        assert world.session.scalar(select(func.count()).select_from(AssistantFindingDecision)) == 2


def _seed_with_correction(world: World) -> dict[str, Any]:
    """`_seed`, with the follow-up's correction pointing at the first answer (stored that way, as
    the engine stores it: the later answer is written after the earlier one exists)."""
    from app.models import AssistantChat, AssistantChatMessage

    session = world.session
    chat = AssistantChat(package_id=world.first, created_by="reviewer-one")
    session.add(chat)
    session.flush()
    answer_id = uuid4()
    rows = [
        ("user", "Full check · Cabinets · High effort", None, "full_check"),
        (
            "assistant",
            "Done.",
            [
                _block(finding("F1", "Sink base width", 2)),
                _block(finding("F2", "Filler at wall end", 2)),
                _block(finding("F3", "Outlet height", 2, "check")),
                _block(finding("F4", "Sink base width", 3, "same", round=2)),
                _block(finding("F5", "Filler at wall end", 3, round=2)),
                _block(finding("F6", "Drawer count", 3, round=2)),
            ],
            "full_check",
        ),
        ("user", "Is the drawer count really wrong?", None, "chat"),
        (
            "assistant",
            "No.",
            [
                {
                    "type": "finding_update",
                    "update": {
                        "ref": "A1-F6",
                        "message_id": str(answer_id),
                        "finding_id": "F6",
                        "answer": 1,
                        "version": 2,
                        "finding": finding("F6", "Drawer count", 3, round=2),
                        "dropped": "Not a difference: the drawer count matches.",
                    },
                }
            ],
            "chat",
        ),
    ]
    for seq, (role, text, blocks, mode) in enumerate(rows, start=1):
        session.add(
            AssistantChatMessage(
                id=answer_id if seq == 2 else uuid4(),
                chat_id=chat.id,
                seq=seq,
                role=role,
                created_by="reviewer-one",
                text=text,
                mode=mode,
                blocks=None if blocks is None else [{"type": "text", "text": "x"}, *blocks],
                api_content=None if role == "user" else [],
                cost_micros=None if role == "user" else 0,
                created_at=T0 + timedelta(minutes=seq),
            )
        )
    session.commit()
    _decide(session, chat.id, answer_id, "F3", "agree", by="reviewer-one", hours=1)
    _decide(session, chat.id, answer_id, "F3", "not_a_problem", by="reviewer-two", hours=2)
    _chat(
        session,
        world.second,
        "Does the countertop meet the width rule?",
        [
            [
                _block(
                    finding(
                        "F1",
                        "Countertop overall width",
                        7,
                        category="countertop",
                        source="rules",
                        # GV's own rule check agrees: a rule item is proved only by code's rule.
                        checks={"rule": "agrees"},
                        witnesses=["rule"],
                    )
                )
            ]
        ],
        minutes=30,
    )
    return {"chat": chat.id, "answer": answer_id}


def test_the_tracker_is_a_404_outside_the_project_and_empty_with_no_checks(postgres_engine) -> None:  # type: ignore[no-untyped-def]
    with _world(postgres_engine) as world:
        empty = world.client.get(f"/api/v1/projects/{world.project_id}/tracker").json()
        assert empty["items"] == [] and empty["sets"] == [] and empty["totals"]["checks"] == 0
        project = world.project_id
    with _world(postgres_engine, member=False) as outsider:
        for path in ("tracker", "tracker.xlsx"):
            assert outsider.client.get(f"/api/v1/projects/{project}/tracker").status_code == 404
            assert (
                outsider.client.get(f"/api/v1/projects/{outsider.project_id}/{path}").status_code
                == 404
            )


def test_the_export_is_a_filtered_text_only_workbook(postgres_engine) -> None:  # type: ignore[no-untyped-def]
    openpyxl = pytest.importorskip("openpyxl")
    with _world(postgres_engine) as world:
        _seed_with_correction(world)
        base = f"/api/v1/projects/{world.project_id}/tracker.xlsx"
        response = world.client.get(base, params={"state": "open"})
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        assert ".xlsx" in response.headers["content-disposition"]
        book = openpyxl.load_workbook(io.BytesIO(response.content))
        assert book.sheetnames == ["Items", "Vendors", "Checks"]
        items = book["Items"]
        rows = [[cell.value for cell in row] for row in items.iter_rows()]
        assert rows[0][:3] == ["State", "Item", "Drawing set"]
        assert sorted(row[1] for row in rows[1:]) == [
            "Countertop overall width",
            "Filler at wall end",
        ]
        assert {row[0] for row in rows[1:]} == {"Open · Wrong"}
        assert items.auto_filter.ref == "A1:S3" and items.freeze_panes == "A2"
        assert all(cell.number_format == "@" for row in items.iter_rows() for cell in row)
        assert all(
            isinstance(cell.value, str) for row in items.iter_rows() for cell in row if cell.value
        )
        vendors = [[c.value for c in row] for row in book["Vendors"].iter_rows(min_row=2)]
        assert vendors[0][0] == "Acme Millwork" and vendors[0][5] == "1 of 2 (50%)"
        assert len(list(book["Checks"].iter_rows(min_row=2))) == 2

        one_set = world.client.get(base, params={"package_id": str(world.second)})
        rows = list(
            openpyxl.load_workbook(io.BytesIO(one_set.content))["Items"].iter_rows(min_row=2)
        )
        assert [row[1].value for row in rows] == ["Countertop overall width"]
        by_vendor = world.client.get(base, params={"vendor": "  acme MILLWORK "})
        rows = list(
            openpyxl.load_workbook(io.BytesIO(by_vendor.content))["Items"].iter_rows(min_row=2)
        )
        assert len(rows) == 4
        assert world.client.get(base, params={"state": "maybe"}).status_code == 422


def test_the_export_falls_back_to_csv_without_openpyxl(postgres_engine, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from importlib import import_module

    real = import_module

    def without_openpyxl(name: str) -> Any:
        if name.startswith("openpyxl"):
            raise ModuleNotFoundError(name)
        return real(name)

    monkeypatch.setattr("app.review.assistant.workbook.import_module", without_openpyxl)
    with _world(postgres_engine) as world:
        _seed_with_correction(world)
        response = world.client.get(f"/api/v1/projects/{world.project_id}/tracker.xlsx")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        assert ".csv" in response.headers["content-disposition"]
        lines = response.text.strip().splitlines()
        assert lines[0].startswith("State,Item,Drawing set")
        assert len(lines) == 5
        assert any(line.startswith("Not a problem · reviewer-two,Outlet height") for line in lines)
