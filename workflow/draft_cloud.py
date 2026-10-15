"""Ask for a draft cloud PDF before sign-off, and say where the newest request stands (#1340).

A reviewer wants to see the clouded drawing before signing off: what will be clouded as wrong,
what still needs a person and what was not checked. The API may not render (it may not import
`reports/`, `tests/api/test_no_heavy_work.py`), so it queues the worker's job here and reads the
status from the records the job leaves: its outbox row and the stored draft.

**One draft per request, found by its request.** The worker stores the draft under
`draft_storage_prefix(revision, request)`, so a status read never compares the clocks of the API
and the worker, and a request delivered twice finds its own draft rather than writing a second.

**Never the signed file.** A draft is an `OutputArtifact` of kind `draft_redline`. The signed
downloads serve only the `redline` a signed bundle names and check its kind
(`app/api/approvals.py`), so a draft can never be handed out as the signed cloud PDF.

This module imports no renderer; the job itself is `workflow/draft_cloud_render.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.models import OutboxEntry
from app.models.verdicts import OutputArtifact, OutputArtifactKind
from workflow.outbox import enqueue

__all__ = [
    "DRAFT_CLOUD_WORKFLOW",
    "DraftCloud",
    "DraftCloudRefused",
    "draft_cloud_status",
    "draft_storage_prefix",
    "request_draft_cloud",
]

#: The outbox name of the worker's draft job.
DRAFT_CLOUD_WORKFLOW: Final = "render_draft_cloud"

type DraftState = Literal["not_requested", "preparing", "ready", "failed"]


class DraftCloudRefused(Exception):
    """The worker could not make the draft, with a sentence for a person (`plain_reason`).

    The outbox parks such work at once with that sentence (`plain_stop_reason`), never redelivering
    it; the status read shows it, and asking again queues a new request.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.plain_reason = reason


@dataclass(frozen=True, slots=True)
class DraftCloud:
    """What became of the newest draft request of one revision."""

    state: DraftState
    request_id: UUID | None = None
    asked_at: datetime | None = None
    reason: str | None = None
    artifact: OutputArtifact | None = None


def draft_storage_prefix(package_revision_id: UUID, request_id: UUID) -> str:
    """Where the draft made for one request is stored: its key starts with this."""
    return f"draft-redlines/{package_revision_id}/{request_id}/"


def _requests(package_revision_id: UUID) -> Select[OutboxEntry]:
    return select(OutboxEntry).where(
        OutboxEntry.workflow == DRAFT_CLOUD_WORKFLOW,
        OutboxEntry.payload["package_revision_id"].astext == str(package_revision_id),
    )


def request_draft_cloud(session: Session, package_revision_id: UUID, *, requested_by: str) -> UUID:
    """Queue the draft job, or return the request already waiting. Commits nothing.

    One waiting request at a time: a second press while the first is queued queues nothing, so a
    double click never renders twice.
    """
    if not requested_by.strip():
        raise ValueError("a draft request must name who asked for it")
    waiting = session.scalar(
        _requests(package_revision_id)
        .where(OutboxEntry.dispatched_at.is_(None), OutboxEntry.abandoned_at.is_(None))
        .order_by(OutboxEntry.created_at.desc(), OutboxEntry.id.desc())
        .limit(1)
    )
    if waiting is not None:
        return waiting.id
    return enqueue(
        session,
        workflow=DRAFT_CLOUD_WORKFLOW,
        payload={"package_revision_id": str(package_revision_id), "requested_by": requested_by},
    )


def draft_for_request(
    session: Session, package_revision_id: UUID, request_id: UUID
) -> OutputArtifact | None:
    """The draft stored for one request, if the worker has made it."""
    return session.scalars(
        select(OutputArtifact)
        .where(
            OutputArtifact.package_revision_id == package_revision_id,
            OutputArtifact.kind == OutputArtifactKind.DRAFT_REDLINE.value,
            OutputArtifact.storage_key.startswith(
                draft_storage_prefix(package_revision_id, request_id), autoescape=True
            ),
        )
        .order_by(OutputArtifact.created_at.desc(), OutputArtifact.id.desc())
        .limit(1)
    ).first()


def draft_cloud_status(session: Session, package_revision_id: UUID) -> DraftCloud:
    """The newest request's state, from its outbox row and its stored draft only."""
    entry = session.scalars(
        _requests(package_revision_id)
        .order_by(OutboxEntry.created_at.desc(), OutboxEntry.id.desc())
        .limit(1)
    ).first()
    if entry is None:
        return DraftCloud("not_requested")
    artifact = draft_for_request(session, package_revision_id, entry.id)
    if artifact is not None:
        return DraftCloud("ready", entry.id, entry.created_at, None, artifact)
    if entry.abandoned_at is not None:
        return DraftCloud(
            "failed",
            entry.id,
            entry.created_at,
            entry.abandoned_reason or "The draft was not made.",
        )
    if entry.dispatched_at is not None:
        return DraftCloud(
            "failed", entry.id, entry.created_at, "No draft was recorded for this request."
        )
    return DraftCloud("preparing", entry.id, entry.created_at)
