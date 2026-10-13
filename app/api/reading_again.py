"""A person runs the reading again after it stopped for something only a person can fix (#1206).

**Why.** Two stops wait for a person, and before #1206 neither had a way back in the product:

* **The AI provider account was out of credit** (HTTP 402). The package waits in
  `FAILED_RETRYABLE`, saying so in plain words. Nothing retries it on a timer (a retry cannot top
  the account up), and the local worker parks the work instead of redelivering it every poll. Once
  the account is topped up, "Run the reading again" enqueues the reading once more; it resumes at
  the stage that failed and reuses every answer already paid for (#1112).
* **The drawing set's AI budget was reached.** The reading handed over as usual and listed the
  pages it could not read (and held the pages whose later questions it did not ask). "Read the
  unread pages" reads them on the same revision, with a fresh page-scaled budget for them
  (`workflow/stages.DatabaseStages.read_unread_pages`), again paying only for missing questions.

One route, because the person's question is one ("read it again"); which of the two it does
follows from the revision's recorded state, never from the caller. Anything else is refused with
the reason. It is audited as a reviewer action, and only someone who may start a review may ask.

This module does no reading: it enqueues the work in the caller's transaction (the outbox), as
starting the first reading does (`app/api/documents.start_extraction`).

Verification: `tests/api/test_reading_again.py`.
"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_session
from app.api.documents import EXTRACT_PACKAGE_WORKFLOW, _package_revision
from app.audit.events import AuditCategory, emit
from app.auth import Action, Principal, require_action, require_project_access
from app.lifecycle.side_states import PLAIN_STOP_REASONS
from app.models import OutboxEntry, PackageState, PackageStateEvent
from workflow.outbox import enqueue
from workflow.slot_row_scope import READ_UNREAD_PAGES_WORKFLOW, budget_stopped_pages

router = APIRouter(tags=["packages"])

#: Where a reading has handed the revision to a person, so "Read the unread pages" may start.
_READ_STATES = frozenset({PackageState.AWAITING_REVIEW, PackageState.NEEDS_INPUT})


class ReadingAgainOut(BaseModel):
    """What was asked for: the whole reading resumed, or only the pages the budget stopped."""

    action: Literal["run_reading_again", "read_unread_pages"] = Field(
        description=(
            "run_reading_again: the reading stopped because the AI provider account was out of "
            "credit, and resumes where it stopped. read_unread_pages: the AI budget was reached, "
            "and the pages it left are read on this revision."
        )
    )
    accepted_id: UUID = Field(description="The enqueued work, not a workflow run.")
    package_revision_id: UUID
    pages: tuple[int, ...] = Field(
        default=(),
        description="For read_unread_pages, the page numbers to be read; empty otherwise.",
    )


def _latest_failure_reason(session: Session, revision_id: UUID) -> str | None:
    return session.scalar(
        select(PackageStateEvent.reason)
        .where(
            PackageStateEvent.package_revision_id == revision_id,
            PackageStateEvent.to_state == PackageState.FAILED_RETRYABLE.value,
        )
        .order_by(PackageStateEvent.sequence.desc())
        .limit(1)
    )


def _work_waiting(session: Session, revision_id: UUID, workflow: str) -> bool:
    """Whether this revision already has such work queued and not yet handed over or parked."""
    return (
        session.scalar(
            select(OutboxEntry.id)
            .where(
                OutboxEntry.workflow == workflow,
                OutboxEntry.dispatched_at.is_(None),
                OutboxEntry.abandoned_at.is_(None),
                OutboxEntry.payload["package_revision_id"].astext == str(revision_id),
            )
            .limit(1)
        )
        is not None
    )


@router.post(
    "/projects/{project_id}/packages/{package_id}/reading-again",
    response_model=ReadingAgainOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Run the reading again after it stopped for the AI account or the AI budget",
)
def read_again(
    principal: Annotated[Principal, Depends(require_project_access)],
    _: Annotated[Principal, Depends(require_action(Action.START_REVIEW))],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    package_id: UUID,
) -> ReadingAgainOut:
    """Enqueue "Run the reading again" (out of credit) or "Read the unread pages" (budget).

    Refused (409) when the revision did not stop for either, or the same work is already queued.
    """
    revision = _package_revision(session, project_id, package_id, lock=True)
    state = PackageState(revision.state)
    payload = {"package_revision_id": str(revision.id), "project_id": str(project_id)}
    if state is PackageState.FAILED_RETRYABLE:
        if _latest_failure_reason(session, revision.id) not in PLAIN_STOP_REASONS:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "This reading did not stop for the AI provider account, so it is not run "
                    "again from here."
                ),
            )
        if _work_waiting(session, revision.id, EXTRACT_PACKAGE_WORKFLOW):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="The reading has already been asked to run again.",
            )
        accepted = enqueue(session, workflow=EXTRACT_PACKAGE_WORKFLOW, payload=payload)
        action: Literal["run_reading_again", "read_unread_pages"] = "run_reading_again"
        pages: tuple[int, ...] = ()
    else:
        pages = budget_stopped_pages(session, revision.id) if state in _READ_STATES else ()
        if not pages:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "No page of this reading is waiting for the AI budget or the AI account, so "
                    "there is nothing to read again."
                ),
            )
        if _work_waiting(session, revision.id, READ_UNREAD_PAGES_WORKFLOW):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="The unread pages have already been asked to be read.",
            )
        accepted = enqueue(session, workflow=READ_UNREAD_PAGES_WORKFLOW, payload=payload)
        action = "read_unread_pages"
    emit(
        session,
        category=AuditCategory.REVIEW_ACTION,
        actor=principal.id,
        target_id=accepted,
        target_type=f"reading_again:{action}",
    )
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise
    return ReadingAgainOut(
        action=action, accepted_id=accepted, package_revision_id=revision.id, pages=pages
    )
