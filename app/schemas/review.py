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

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.review import ExceptionScope, ReviewActionKind


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


class DecideEvidence(BaseModel):
    """Confirm or correct one observation behind one finding.

    **`correct` carries the corrected value; `confirm` must not.** A confirmation that accepted a
    value would be indistinguishable from a correction that happened to agree, and the correction
    rate (`D5.4`) counts the difference. The pairing is validated here rather than trusted.

    The value is **as typed**, with its unit, exactly as `MeasurementEntry` takes a reviewer's
    reading. The server parses it — `25.5"` and `25 1/2"` are the same value, and the reviewer
    should not have to know which spelling this accepts.
    """

    model_config = ConfigDict(extra="forbid")

    finding_id: UUID
    observation_id: UUID
    action: ReviewActionKind
    corrected_value: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        description="The corrected value as typed, with its unit. Required for `correct` only.",
    )

    @model_validator(mode="after")
    def _value_matches_the_action(self) -> DecideEvidence:
        if self.action is ReviewActionKind.CORRECT and self.corrected_value is None:
            raise ValueError(
                "a correction needs the corrected value. Recording that something was corrected "
                "without saying to what leaves the ledger with no correction in it."
            )
        if self.action is ReviewActionKind.CONFIRM and self.corrected_value is not None:
            raise ValueError(
                "a confirmation cannot carry a corrected value. If the value changed it is a "
                "correction, and the correction rate counts the difference."
            )
        if self.action not in (ReviewActionKind.CONFIRM, ReviewActionKind.CORRECT):
            raise ValueError(
                f"{self.action.value!r} is not an evidence decision. Confirming and correcting act "
                "on a reading; dismissing and excepting act on a finding and have their own routes."
            )
        return self


class DecidedEvidenceOut(BaseModel):
    """What was recorded: the action, and the observation the reviewer's decision produced.

    Two observation ids, because a correction never edits the original. The reading the system made
    stays exactly as it was and a new `HUMAN_CONFIRMED` one is written beside it — which is what
    makes "what did we get wrong?" answerable at all.
    """

    model_config = ConfigDict(frozen=True)

    action: ReviewActionOut
    original_observation_id: UUID
    resulting_observation_id: UUID
    original_value: str
    resulting_value: str
    """Both rendered as they are stored in the ledger: exact numerator/denominator with the unit and
    semantic type, never a float."""


class GrantException(BaseModel):
    """Accept one specific deviation, until one specific moment.

    **Every field is required, and `expires_at` most of all.** A permanent silent exception is not
    representable anywhere in this system: the column is `NOT NULL`, `ExceptionGrant` has no default
    for it, and this schema will not accept its absence. An exception with no end date is how a check
    gets switched off and nobody remembers.

    No `approved_by`. The approver is the authenticated caller — `AGENTS.md` §2.6 calls an anonymous
    exception nobody's decision, and a client-supplied approver answers "who says so?" with "whoever
    was asked".
    """

    model_config = ConfigDict(extra="forbid")

    finding_id: UUID
    scope: ExceptionScope
    scope_id: UUID
    """Which finding, item or package revision. Matching is exact at read time — an exception never
    widens to cover something similar, because that would be a rule change with no author."""

    reason: str = Field(min_length=1, max_length=1000)
    expires_at: datetime

    @model_validator(mode="after")
    def _expiry_is_aware(self) -> GrantException:
        if self.expires_at.tzinfo is None:
            raise ValueError(
                "expires_at must carry a timezone. Guessing the zone is how an exception ends up "
                "living hours longer than it was granted for, and in the worst case forever."
            )
        return self


class ReviewExceptionOut(BaseModel):
    """A granted exception, with the terms a later reader needs."""

    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: UUID
    review_action_id: UUID
    scope: ExceptionScope
    scope_id: UUID
    reason: str
    approved_by: str
    expires_at: datetime
    created_at: datetime
