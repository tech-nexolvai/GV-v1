"""Review sessions, what a reviewer did, corrections, approvals and exceptions.

The human half of the record. Everything here answers *who decided, on what, and why* — and it is
written so that those answers cannot later be tidied.

**The correction ledger is how we learn where automation is unreliable.** It is `Immutable` for the
same reason the findings are: the record of what we got wrong is exactly what somebody would be
tempted to edit, and a ledger that can be edited measures nothing. Nothing in `rules/` may read it —
a correction is a reviewer fixing one drawing, not a rule change, and `AGENTS.md` §2.6 keeps those
apart. `tests/test_verdict_isolation.py` asserts the import never appears.

**An exception must expire.** `expires_at` is `NOT NULL`, which is the whole of the control: a
permanent silent exception is not representable, so nobody can quietly switch a check off forever.
Its scope is a single finding, item or package — never "this rule, everywhere", which is a rule
change wearing an exception's clothes.

**An approval names server-side finding revisions.** A client-supplied value would let the thing
being approved be chosen by the caller, which is the same class of hole as trusting a client id.

Source: backend proposal §10.1 · Design: `docs/DESIGN_PRODUCT.md` §4 ·
Verification: `tests/db/test_review_models.py`
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum, StrEnum
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Immutable, TimestampedUUID, UTCDateTime


def _sql_values(enum_type: type[Enum]) -> str:
    return ", ".join(f"'{member.value}'" for member in enum_type)


class ReviewActionKind(StrEnum):
    """What a reviewer did to a finding. Four verbs, and no fifth.

    There is deliberately no `edit`. A reviewer either confirms what the system said, corrects the
    value it read, grants a bounded exception, or dismisses the finding — each of which leaves a
    different record. A general "edit" would collapse all four into one, and the ledger exists to
    keep them apart.
    """

    CONFIRM = "confirm"
    CORRECT = "correct"
    EXCEPT = "except"
    DISMISS = "dismiss"


class ExceptionScope(StrEnum):
    """How far an exception reaches. Never "this rule, everywhere".

    A rule that should not fire is a rule change, and it goes through the rulebook where somebody
    reviews it. An exception is a reviewer saying *this one* is acceptable, on this drawing, until a
    date — so the scope names one thing.
    """

    FINDING = "finding"
    ITEM = "item"
    PACKAGE = "package"


ACTION_VALUES = _sql_values(ReviewActionKind)
SCOPE_VALUES = _sql_values(ExceptionScope)


class ReviewSession(Base, TimestampedUUID):
    """One sitting: a reviewer working through one package revision.

    Not `Immutable` — a session is open while it is being worked, and `completed_at` is set when it
    ends. What must not change is what was *done* in it, and those are separate rows.
    """

    __tablename__ = "review_sessions"

    package_revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("package_revisions.id", ondelete="RESTRICT"), index=True
    )
    reviewer: Mapped[str] = mapped_column(String(200), index=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)

    __table_args__ = (
        CheckConstraint("reviewer <> ''", name="review_session_reviewer_present"),
        UniqueConstraint("id", "package_revision_id", name="uq_review_sessions_id_revision"),
    )


class ReviewAction(Base, TimestampedUUID, Immutable):
    """One thing a reviewer did to one finding.

    `finding_id` is a foreign key to a server-side row, never a value the client supplied. A caller
    that could name the finding it was acting on could name a different one.
    """

    __tablename__ = "review_actions"

    review_session_id: Mapped[UUID] = mapped_column(index=True)
    finding_id: Mapped[UUID] = mapped_column(index=True)

    package_revision_id: Mapped[UUID] = mapped_column(index=True)
    """The revision both sides must agree on.

    Two composite foreign keys below resolve it against the session *and* against the finding, so a
    session reviewing package A cannot carry an action on a finding from package B. Without this the
    row would be accepted and would misstate what was reviewed — and an approval built from it would
    misstate what was signed off.
    """
    action: Mapped[str] = mapped_column(String(32), index=True)
    actor: Mapped[str] = mapped_column(String(200))
    """Who, by name. The session has a reviewer, and this repeats it because a session may be picked
    up by somebody else and an action has to say who actually did it."""

    note: Mapped[str | None] = mapped_column(String(1000), default=None)

    __table_args__ = (
        ForeignKeyConstraint(
            ["review_session_id", "package_revision_id"],
            ["review_sessions.id", "review_sessions.package_revision_id"],
            name="fk_review_actions_session_revision",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["finding_id", "package_revision_id"],
            ["findings.id", "findings.package_revision_id"],
            name="fk_review_actions_finding_revision",
            ondelete="RESTRICT",
        ),
        # Lets the ledger and the exception table bind to the *kind* of action, below.
        UniqueConstraint("id", "action", name="uq_review_actions_id_action"),
        CheckConstraint(f"action IN ({ACTION_VALUES})", name="review_action_kind"),
        CheckConstraint("actor <> ''", name="review_action_actor_present"),
        Index("ix_review_actions_finding_action", "finding_id", "action"),
    )


class CorrectionLedgerEntry(Base, TimestampedUUID, Immutable):
    """A reviewer changed a value we read, kept forever.

    The original is stored beside the correction, always. Keeping only the corrected value would
    leave no way to ask what we got wrong — which is the entire purpose, and the reason the reviewer
    correction rate can be measured at all (`D5.4`).

    Append-only, and `AGENTS.md` §2.6 forbids anything in `rules/` reading it: a correction is a
    reviewer fixing one drawing, not a rule change. Corrections becoming rules by accumulation is
    how a system quietly starts deciding what it was told to check.
    """

    __tablename__ = "correction_ledger"

    review_action_id: Mapped[UUID] = mapped_column(unique=True, index=True)
    """One correction per action. Two would leave "what did the reviewer change?" with two answers."""

    action: Mapped[str] = mapped_column(String(32), default=ReviewActionKind.CORRECT.value)
    """Always `correct`, and the database enforces it.

    A composite foreign key resolves `(review_action_id, action)` against `review_actions`, so this
    copy cannot disagree with the action it names — and the CHECK pins it to `correct`. Without both,
    a ledger entry could hang off a `confirm` or a `dismiss`, and the correction rate would count
    events that were not corrections.
    """

    canonical_observation_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "canonical_observations.id", ondelete="RESTRICT", name="fk_correction_observation"
        ),
        index=True,
    )

    original_value: Mapped[str] = mapped_column(String(500))
    """Verbatim, as text. Not a number: a correction may be to a unit, a label or an identifier as
    readily as to a dimension, and parsing it into a typed column here would lose the ones that do
    not fit."""

    corrected_value: Mapped[str] = mapped_column(String(500))

    __table_args__ = (
        CheckConstraint("original_value <> ''", name="correction_original_present"),
        CheckConstraint("corrected_value <> ''", name="correction_corrected_present"),
        # A correction that changed nothing is a confirmation, and it belongs in `review_actions`
        # as one. Storing it here would inflate the correction rate with non-events.
        CheckConstraint(
            "original_value <> corrected_value", name="correction_actually_changes_something"
        ),
        ForeignKeyConstraint(
            ["review_action_id", "action"],
            ["review_actions.id", "review_actions.action"],
            name="fk_correction_action_kind",
            ondelete="RESTRICT",
        ),
        CheckConstraint("action = 'correct'", name="correction_action_is_a_correction"),
    )


class Approval(Base, TimestampedUUID, Immutable):
    """A reviewer signing off a package revision, and exactly what was in force when they did.

    `Immutable`: an approval is the record that a human accepted responsibility, and it has to keep
    meaning what it meant.
    """

    __tablename__ = "approvals"

    package_revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("package_revisions.id", ondelete="RESTRICT"), index=True
    )
    approved_by: Mapped[str] = mapped_column(String(200))
    """Which findings this covered lives in `ApprovedFinding`, not in a column here. A list of ids
    on this row would be the client-supplied value the acceptance forbids, with nothing checking
    that any of them exist."""

    __table_args__ = (
        CheckConstraint("approved_by <> ''", name="approval_approved_by_present"),
        UniqueConstraint("id", "package_revision_id", name="uq_approvals_id_revision"),
    )


class ReviewException(Base, TimestampedUUID, Immutable):
    """A reviewer accepting a specific deviation, until a date.

    Named `ReviewException` rather than `Exception` — shadowing the builtin in a module every model
    imports is a trap, and the issue's sketch called it `Exception_`, which is the same trap with an
    underscore.

    **`expires_at` is `NOT NULL`, and that is the control.** A permanent silent exception is not
    representable: somebody has to look again. An exception with no end date is how a check gets
    switched off and nobody remembers.
    """

    __tablename__ = "review_exceptions"

    review_action_id: Mapped[UUID] = mapped_column(unique=True, index=True)
    action: Mapped[str] = mapped_column(String(32), default=ReviewActionKind.EXCEPT.value)
    """Always `except`, enforced the same way the ledger's is. An exception hanging off a `confirm`
    would be a check switched off by a record that says the reviewer agreed with it."""

    scope: Mapped[str] = mapped_column(String(32), index=True)
    scope_id: Mapped[UUID] = mapped_column()
    """Which finding, item or package. Not a foreign key, because the three scopes point at three
    different tables and a column cannot reference all of them; `scope` says which to look in."""

    reason: Mapped[str] = mapped_column(String(1000))
    """Why this deviation is acceptable. An exception nobody explained is one nobody can review, and
    it is the sentence a future reader needs most."""

    approved_by: Mapped[str] = mapped_column(String(200))
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())
    """Required. The whole control — see the class docstring."""

    __table_args__ = (
        CheckConstraint(f"scope IN ({SCOPE_VALUES})", name="review_exception_scope"),
        CheckConstraint("reason <> ''", name="review_exception_reason_present"),
        CheckConstraint("approved_by <> ''", name="review_exception_approved_by_present"),
        # Not "in the future" — a clock comparison in a CHECK is not immutable and PostgreSQL
        # refuses it. Expiry before creation is still nonsense and can be caught.
        CheckConstraint("expires_at > created_at", name="review_exception_expires_after_creation"),
        ForeignKeyConstraint(
            ["review_action_id", "action"],
            ["review_actions.id", "review_actions.action"],
            name="fk_exception_action_kind",
            ondelete="RESTRICT",
        ),
        CheckConstraint("action = 'except'", name="review_exception_action_is_an_exception"),
        Index("ix_review_exceptions_scope_expiry", "scope", "expires_at"),
    )


class ApprovedFinding(Base, TimestampedUUID, Immutable):
    """Which finding revisions an approval covered.

    An association table rather than a list column, so each one is a foreign key to a server-side
    row. The acceptance says approvals reference server-side finding revisions and never
    client-supplied values; a JSON array of ids would be exactly the client-supplied value it forbids,
    with nothing checking that the ids exist.
    """

    __tablename__ = "approved_findings"

    approval_id: Mapped[UUID] = mapped_column(index=True)
    finding_id: Mapped[UUID] = mapped_column(index=True)
    package_revision_id: Mapped[UUID] = mapped_column(index=True)
    """Resolved against both sides, so an approval for package A cannot list a finding from package
    B. An approval that misstates what it covered is worse than no approval: somebody signed it."""

    __table_args__ = (
        ForeignKeyConstraint(
            ["approval_id", "package_revision_id"],
            ["approvals.id", "approvals.package_revision_id"],
            name="fk_approved_findings_approval_revision",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["finding_id", "package_revision_id"],
            ["findings.id", "findings.package_revision_id"],
            name="fk_approved_findings_finding_revision",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("approval_id", "finding_id", name="uq_approved_findings_link"),
    )
