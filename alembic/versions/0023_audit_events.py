"""The append-only audit trail: who did what, when, under which trace.

Revision ID: 0023_audit_events
Revises: 0022_evidence_review_actions

One table for all six categories backend §11 requires. `target_id` carries no foreign key on
purpose: the six categories point at six different tables, and a nullable key per category would be
six columns of which five are always empty on every row.

Source: issue #255. Verification: tests/audit/test_events.py.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0023_audit_events"
down_revision: str | None = "0022_evidence_review_actions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CATEGORIES = (
    "STATE_CHANGE",
    "RULE_PUBLICATION",
    "FINDING",
    "REVIEW_ACTION",
    "EXCEPTION",
    "ARTIFACT_DOWNLOAD",
)


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("actor", sa.String(length=200), nullable=False),
        sa.Column("target_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("target_type", sa.String(length=64), nullable=False),
        sa.Column("trace_id", sa.String(length=32), nullable=True),
        sa.CheckConstraint(
            "category IN (" + ", ".join(f"'{name}'" for name in _CATEGORIES) + ")",
            name="audit_events_category",
        ),
        sa.CheckConstraint("length(actor) > 0", name="audit_events_actor_named"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_events")),
    )
    op.create_index(
        "ix_audit_events_target",
        "audit_events",
        ["target_type", "target_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_audit_events_target", table_name="audit_events")
    op.drop_table("audit_events")
