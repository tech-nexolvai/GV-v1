"""What the send-ready package reads from the database (#1622): who and what it is about, and the
drawing set's earlier checks for the stable comment numbers.

- `identity`: the project, the vendor, the submittal number (the drawing set's place among its
  project's sets: 1, 2, …; a resubmittal adds a letter from the round, `send_package.submittal`),
  the date the latest drawings were received, and the products.
- `set_answers`: every stored answer of the drawing set with findings (each finding at its newest
  correction), for `job_tracker.comment_numbers`, so a comment keeps its number across checks and
  rounds.

Reads only. No AI call.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import AssistantChat, AssistantChatMessage, Package, PackageRevision, Project
from app.review.assistant.chat_corrections import corrections_by_answer, with_corrections
from app.review.assistant.chat_findings import live_findings
from app.review.assistant.job_tracker import TrackerAnswer, comment_numbers
from app.review.assistant.send_package import PackageIdentity

__all__ = ["identity", "numbering", "set_answers", "stored_numbers"]


def identity(session: Session, package: Package, products: Sequence[str] = ()) -> PackageIdentity:
    """The package's identity for the letter and the register."""
    project = session.get(Project, package.project_id)
    submittal = session.scalar(
        select(func.count())
        .select_from(Package)
        .where(Package.project_id == package.project_id, Package.created_at <= package.created_at)
    )
    received = session.scalar(
        select(func.max(PackageRevision.created_at)).where(PackageRevision.package_id == package.id)
    )
    return PackageIdentity(
        project="" if project is None else str(project.name or ""),
        vendor=(package.vendor or "").strip(),
        submittal=int(submittal) if submittal else None,
        received="" if received is None else received.date().isoformat(),
        products=tuple(products),
    )


def set_answers(session: Session, package_id: UUID) -> list[TrackerAnswer]:
    """Every stored answer of the drawing set that has findings, oldest first."""
    chats = list(
        session.scalars(select(AssistantChat.id).where(AssistantChat.package_id == package_id))
    )
    if not chats:
        return []
    rows = list(
        session.scalars(
            select(AssistantChatMessage)
            .where(
                AssistantChatMessage.chat_id.in_(chats), AssistantChatMessage.role == "assistant"
            )
            .order_by(AssistantChatMessage.chat_id, AssistantChatMessage.seq)
        )
    )
    by_chat: dict[UUID, list[AssistantChatMessage]] = {}
    for row in rows:
        by_chat.setdefault(row.chat_id, []).append(row)
    out: list[TrackerAnswer] = []
    for chat_id, answers in by_chat.items():
        corrections = corrections_by_answer(answers)
        for row in answers:
            findings = live_findings(with_corrections(row.blocks, corrections.get(str(row.id))))
            if not findings:
                continue
            out.append(
                TrackerAnswer(
                    package_id=package_id,
                    chat_id=chat_id,
                    message_id=row.id,
                    at=row.created_at,
                    kind="full_check" if row.mode == "full_check" else "question",
                    title="",
                    stopped=row.stopped,
                    findings=findings,
                )
            )
    return out


def numbering(
    factory: sessionmaker[Session], package_id: UUID, chat_id: UUID, message_id: UUID
) -> Callable[[Sequence[Mapping[str, Any]]], dict[str, str]]:
    """For a full check still running: the comment numbers of its own findings (finding id ->
    "C-07"), worked out with the set's stored answers before it. When the database cannot be read,
    the findings keep no number (the package numbers them 1, 2, … in order)."""

    def numbers(findings: Sequence[Mapping[str, Any]]) -> dict[str, str]:
        with factory() as session:
            earlier = [a for a in set_answers(session, package_id) if a.message_id != message_id]
        now = TrackerAnswer(
            package_id=package_id,
            chat_id=chat_id,
            message_id=message_id,
            at=datetime.now(UTC),
            kind="full_check",
            title="",
            stopped=None,
            findings=list(findings),
        )
        found = comment_numbers([*earlier, now])
        mine = str(message_id)
        return {
            finding_id: label for (message, finding_id), label in found.items() if message == mine
        }

    return numbers


def stored_numbers(session: Session, package_id: UUID, message_id: UUID) -> dict[str, str]:
    """A stored answer's comment numbers (finding id -> "C-07")."""
    found = comment_numbers(set_answers(session, package_id))
    mine = str(message_id)
    return {finding_id: label for (message, finding_id), label in found.items() if message == mine}
