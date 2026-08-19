"""The transactional outbox table (#213).

Revision ID: 0014_outbox
Revises: 0013_append_only

The dual-write problem this table exists to remove: write package state to PostgreSQL and start a
workflow, and either can fail after the other succeeded — leaving a package nothing is working on, or
a workflow for a package that was never written. Two systems, no shared transaction, no way to make
the pair atomic.

The outbox turns the pair into one write. The API writes the business change and an `outbox_entries`
row in the *same* transaction, so PostgreSQL commits both or neither. A dispatcher polls rows that
are already committed and starts the workflow afterwards.

**Not append-only, on purpose.** Twenty-eight tables carry `Immutable` and `0013_append_only` refuses
`UPDATE` and `DELETE` on every one of them. This table is not on that list and must not be added to
it: the dispatcher stamps `dispatched_at` and increments `attempts` on the row it delivered, and the
trigger would refuse that update and wedge the outbox permanently. Nothing is lost — the audit value
here is that the row was committed with the business change, not that it never changes afterwards.

**`dispatched_at IS NULL OR attempts > 0`** is the one non-obvious constraint. It makes the
dispatcher's ordering a schema fact rather than a convention: the attempt is recorded before the
workflow engine is called, and `dispatched_at` is stamped only after the engine accepted the start. A
row that claims delivery with no attempt behind it is not representable.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0014_outbox"
down_revision: str | None = "0013_append_only"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _identity_columns() -> tuple[sa.Column[object], sa.Column[object]]:
    return (
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def upgrade() -> None:
    """Create the outbox table and the index its poll and its stuck-row query both use."""

    op.create_table(
        "outbox_entries",
        *_identity_columns(),
        sa.Column("workflow", sa.String(length=200), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.CheckConstraint("workflow <> ''", name="outbox_entry_workflow"),
        sa.CheckConstraint("attempts >= 0", name="outbox_entry_attempts"),
        sa.CheckConstraint(
            "dispatched_at IS NULL OR attempts > 0", name="outbox_entry_dispatched_needs_attempt"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_outbox_entries"),
    )
    op.create_index(
        "ix_outbox_entries_dispatched_at_created_at",
        "outbox_entries",
        ["dispatched_at", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_entries_dispatched_at_created_at", table_name="outbox_entries")
    op.drop_table("outbox_entries")
