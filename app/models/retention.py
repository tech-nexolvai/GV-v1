"""Legal holds: content that does not expire, and the record of why (#258, F1.7).

A retention policy that could not be suspended would be a liability rather than a control. When a
dispute or an audit is live, the content it concerns has to survive its schedule — and it has to be
provable afterwards that it did.

Source: backend proposal §11; `AGENTS.md` §6 · Verification: ``tests/test_retention.py``
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampedUUID, UTCDateTime


class LegalHold(Base, TimestampedUUID):
    """A reason some project's content must not be deleted, and who said so.

    **Scoped to a project, not to an artifact.** A hold is placed on a *matter* — a dispute, an
    audit, a request from counsel — and it covers what belongs to that project including content
    that does not exist yet. A flag per artifact could not express that, and would have to be set on
    every future upload by whoever remembered to.

    **Not `Immutable`, and that is the exception rather than an oversight.** Almost everything in
    this schema is append-only because a record of what happened must not be editable. A hold is
    different: it is placed and later lifted, and the lifting is an ordinary update to a live piece
    of state. What must not vanish is the *fact that a hold existed during some window*, which is
    why release stamps `released_at` instead of deleting the row — "was this content under hold when
    retention ran?" has to be answerable years later, and a deleted row answers nothing.
    """

    __tablename__ = "legal_holds"

    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), index=False
    )

    reason: Mapped[str] = mapped_column(String(1000))
    """Why, in plain English. Required: a hold nobody can explain is one nobody dares release, and
    content held forever by default is the failure a retention policy exists to prevent."""

    placed_by: Mapped[str] = mapped_column(String(200))

    released_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    """`NULL` while the hold is in force. Retention reads this column and nothing else."""

    released_by: Mapped[str | None] = mapped_column(String(200), default=None)

    __table_args__ = (
        CheckConstraint("reason !~ '^[[:space:]]*$'", name="legal_hold_reason"),
        CheckConstraint("placed_by !~ '^[[:space:]]*$'", name="legal_hold_placed_by"),
        # A release has to say who lifted it. "The hold came off at some point, by someone" is an
        # answer nobody can act on, and release is the moment content becomes deletable.
        CheckConstraint(
            "(released_at IS NULL AND released_by IS NULL) OR "
            "(released_at IS NOT NULL AND released_by IS NOT NULL "
            "AND released_by !~ '^[[:space:]]*$')",
            name="legal_hold_release_attributed",
        ),
        Index(
            "ix_legal_holds_active",
            "project_id",
            postgresql_where=text("released_at IS NULL"),
        ),
    )

    @property
    def in_force(self) -> bool:
        return self.released_at is None
