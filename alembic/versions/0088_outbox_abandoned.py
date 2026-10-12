"""Let the outbox give up on a row that can never start, and say why (#1182).

Revision ID: 0088_outbox_abandoned
Revises: 0087_reading_progress

A check run asked for after sign-off could never run: the lifecycle table has no way from `APPROVED`
back to `RUNNING_CHECKS`, so the worker refused it on every poll and the dispatcher retried it for
ever (116 attempts in five minutes in the 2026-10-11 audit). `abandoned_at` and `abandoned_reason`
record that the dispatcher stopped trying and why; such a row is no longer polled.

Both nullable and set together (the CHECK), and never on a row that was dispatched: a row either
went out or was given up, not both. `refused_attempts` counts only the refusals no retry can change,
so engine outages before them never count towards giving up. Existing rows stay NULL. `outbox_entries` is not append-only
(0014), so the dispatcher may write these after the fact, exactly like `dispatched_at`.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0088_outbox_abandoned"
down_revision: str | None = "0087_reading_progress"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TOGETHER = "outbox_entry_abandoned_with_reason"
NOT_BOTH = "outbox_entry_dispatched_or_abandoned"
REFUSED = "outbox_entry_refused_attempts"


def upgrade() -> None:
    """Add the two nullable columns and their CHECKs. Existing rows stay NULL."""
    op.add_column(
        "outbox_entries", sa.Column("abandoned_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "outbox_entries", sa.Column("abandoned_reason", sa.String(length=1000), nullable=True)
    )
    op.add_column(
        "outbox_entries",
        sa.Column("refused_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_check_constraint(REFUSED, "outbox_entries", "refused_attempts >= 0")
    op.create_check_constraint(
        TOGETHER,
        "outbox_entries",
        "(abandoned_at IS NULL) = (abandoned_reason IS NULL) "
        "AND (abandoned_reason IS NULL OR abandoned_reason <> '')",
    )
    op.create_check_constraint(
        NOT_BOTH, "outbox_entries", "abandoned_at IS NULL OR dispatched_at IS NULL"
    )


def downgrade() -> None:
    """Drop the CHECKs and the columns, refused while any row records that it was given up."""
    stored = op.get_bind().scalar(
        sa.text("SELECT count(*) FROM outbox_entries WHERE abandoned_at IS NOT NULL")
    )
    if stored:
        raise RuntimeError(
            f"Cannot downgrade the outbox: {stored} row(s) record why the dispatcher gave up on "
            "them. Without the columns they would be polled again for ever."
        )
    op.drop_constraint(REFUSED, "outbox_entries", type_="check")
    op.drop_column("outbox_entries", "refused_attempts")
    op.drop_constraint(NOT_BOTH, "outbox_entries", type_="check")
    op.drop_constraint(TOGETHER, "outbox_entries", type_="check")
    op.drop_column("outbox_entries", "abandoned_reason")
    op.drop_column("outbox_entries", "abandoned_at")
