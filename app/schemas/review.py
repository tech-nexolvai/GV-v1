"""What the review endpoints accept and return.

**The reviewer's name is never in a request body.** `ReviewSession.reviewer` and `ReviewAction.actor`
come from the authenticated principal, so a caller cannot open a session or record an action as
somebody else. An audit trail whose author is client-supplied answers "who says so?" with "whoever
was asked", which is the one question it exists to answer — and `app/api/rules.py` already refuses an
unnamed approver for the same reason.

That is why `OpenReviewSession` carries no `reviewer` and `RecordAction` carries no `actor`. The
absence is the design; adding either "for convenience" would quietly undo it.

Source: `docs/DESIGN_PRODUCT.md` §4 · Verification: `tests/api/test_review_api.py`
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.review import ReviewActionKind


class OpenReviewSession(BaseModel):
    """Start reviewing one package revision.

    Names the revision rather than the package: a package moves on, and a review that silently
    followed it would record decisions against drawings the reviewer never saw.
    """

    model_config = ConfigDict(extra="forbid")

    package_revision_id: UUID


class ReviewSessionOut(BaseModel):
    """A sitting of review work."""

    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: UUID
    package_revision_id: UUID
    reviewer: str
    created_at: datetime
    completed_at: datetime | None
    """`None` while the session is open. A completed session accepts no further actions — reopening
    is a new session, so the record of who decided what, and when, stays intact."""


class ReviewSessionPage(BaseModel):
    """Sessions, newest first."""

    model_config = ConfigDict(frozen=True)

    items: list[ReviewSessionOut]


class RecordAction(BaseModel):
    """One thing a reviewer did to one finding.

    No `actor` and no `package_revision_id`. The actor is the authenticated caller, and the revision
    is read off the finding the server loaded — a caller cannot state which revision they were
    looking at, which is what makes the trail worth keeping.
    """

    model_config = ConfigDict(extra="forbid")

    finding_id: UUID
    action: ReviewActionKind
    note: str | None = Field(default=None, max_length=2000)
    """Why. Optional for a confirmation, and the thing a later reader most wants for anything else."""


class ReviewActionOut(BaseModel):
    """A recorded action. Append-only — a changed mind is a second row, and the first one stays."""

    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: UUID
    review_session_id: UUID
    finding_id: UUID
    package_revision_id: UUID
    action: ReviewActionKind
    actor: str
    note: str | None
    created_at: datetime
