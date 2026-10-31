"""The learned list reads GV's reviewer's decisions straight from the stored chats
(`learned.load_cards`): each please-check card at its newest correction, with its latest decision
(an undo takes it back) and its drawing set's project name. Read only. Made-up data."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from alembic import command
from tests.app.postgres_fixture import alembic_config

pytest_plugins = ("tests.app.postgres_fixture",)

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


def _please_check(fid: str, item: str, why: str) -> dict[str, Any]:
    return {
        "id": fid,
        "item": item,
        "status": "check",
        "verified": False,
        "created_by": "page_reviewer",
        "whole_look": {
            "by": "page_reviewer",
            "proof": "likely_only",
            "letter": "D",
            "kind": "finish",
            "claim": "please_check",
            "why": why,
            "words": f"Please check and confirm: {item}: {why}",
        },
    }


def test_load_cards_reads_latest_decisions_with_the_project(postgres_engine: Engine) -> None:
    from app.models import AssistantChat, AssistantChatMessage, Package, Project
    from app.review.assistant import learned
    from app.review.assistant.chat_decisions import record_decision

    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")
    with Session(postgres_engine) as session:
        project = Project(id=uuid4(), name="Job Two")
        session.add(project)
        session.flush()
        package = Package(project_id=project.id, vendor="Acme", product_type="cabinet")
        session.add(package)
        session.flush()
        chat = AssistantChat(package_id=package.id, created_by="reviewer-one")
        session.add(chat)
        session.flush()
        blocks = [
            {"type": "finding", "finding": _please_check("F1", "Toe kick", "no finish tag")},
            {"type": "finding", "finding": _please_check("F2", "End panel", "no finish tag")},
            {"type": "finding", "finding": _please_check("F3", "Metal trim", "no MT-1 tag")},
        ]
        answer = AssistantChatMessage(
            id=uuid4(),
            chat_id=chat.id,
            seq=1,
            role="assistant",
            created_by="reviewer-one",
            text="Done.",
            blocks=blocks,
            api_content=[],
            cost_micros=0,
            created_at=T0,
        )
        session.add(answer)
        session.flush()
        for finding_id, decision in (("F1", "agree"), ("F2", "not_a_problem"), ("F3", "agree")):
            record_decision(
                session,
                chat_id=chat.id,
                message_id=answer.id,
                finding_id=finding_id,
                decision=decision,
                note=None,
                decided_by="Gina",
            )
        # The reviewer takes F3 back: it is undecided again (a newer row; the table is append-only).
        record_decision(
            session,
            chat_id=chat.id,
            message_id=answer.id,
            finding_id="F3",
            decision="undo",
            note=None,
            decided_by="Gina",
        )

        cards = sorted(learned.load_cards(session), key=lambda c: c.type)
        session.rollback()

    assert [(c.type, c.decision, c.project) for c in cards] == [
        ("D: end panel finish tag missing", "not_a_problem", "Job Two"),
        ("D: toe kick finish tag missing", "agree", "Job Two"),
    ]
