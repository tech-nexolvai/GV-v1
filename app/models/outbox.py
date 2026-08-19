"""The transactional outbox table: an intent to start a workflow, written as business data.

**Why the table lives here and the behaviour lives in `workflow/outbox.py`.** `AGENTS.md` §5 puts the
outbox in the control plane — it is business truth in PostgreSQL, exactly like a package or a
finding — while the polling and starting belong to the workflow seam. Every persisted model in this
project is declared under `app/models/` and imported by `app/models/__init__.py`; a model declared
anywhere else is a table Alembic never sees. `workflow.outbox` re-exports `OutboxEntry`, so the
interface named in the story still reads as one module.

**This table is deliberately not `Immutable`.** Almost every other record here is append-only, and
the marker is the default answer. It would be wrong here: a dispatcher has to stamp `dispatched_at`
and increment `attempts` on the row it just delivered, and #202's trigger would refuse that `UPDATE`
and leave the outbox permanently undeliverable. The audit value of this table is not in the row being
frozen — it is that the row exists at all, written in the same transaction as the business change.

Source: `docs/DESIGN_PLATFORM.md` §6.1, backend proposal §9.2–§9.4, issue #213.
Verification: `tests/workflow/test_outbox.py`.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampedUUID, UTCDateTime


class OutboxEntry(Base, TimestampedUUID):
    """One recorded intention to start a workflow, committed with the change that caused it.

    The row is the whole point. It is written by `workflow.outbox.enqueue()` inside the caller's
    transaction, so it commits if and only if the business change commits. Nothing starts a workflow
    at that moment; a dispatcher does that afterwards, reading only rows that are already committed.

    `dispatched_at` is the delivery record and the only mutable business field beside `attempts`:
    `NULL` means "not yet handed to the workflow engine", and it is set *after* the engine accepted
    the start, never before. That ordering is what makes delivery at-least-once rather than
    at-most-once — see `workflow/outbox.py` for what that does and does not guarantee.

    `attempts` counts hand-offs tried, including ones that failed, which is what makes a wedged row
    tell you it is wedged instead of just sitting there.
    """

    __tablename__ = "outbox_entries"

    workflow: Mapped[str] = mapped_column(String(200))
    """The workflow to start, by name. Resolved by the dispatcher's caller, not by this table."""

    payload: Mapped[dict[str, object]] = mapped_column(JSONB)
    """Start arguments. `enqueue()` rejects floats in here — a persisted approximate number is
    exactly what `AGENTS.md` §6 forbids, and JSONB would happily keep one."""

    dispatched_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    attempts: Mapped[int] = mapped_column(default=0)

    __table_args__ = (
        CheckConstraint("workflow <> ''", name="outbox_entry_workflow"),
        CheckConstraint("attempts >= 0", name="outbox_entry_attempts"),
        # A row cannot claim it was delivered without a hand-off having been tried. This is the
        # schema saying what the dispatcher's ordering says: attempts is incremented before the
        # engine is called, and dispatched_at is only stamped after it returned.
        CheckConstraint(
            "dispatched_at IS NULL OR attempts > 0", name="outbox_entry_dispatched_needs_attempt"
        ),
        # The dispatcher's poll and the stuck-row query are the same shape: undispatched rows,
        # oldest first. One index serves both.
        Index("ix_outbox_entries_dispatched_at_created_at", "dispatched_at", "created_at"),
    )
