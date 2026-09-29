"""Record why local code rejected a model response (#701).

Revision ID: 0051_model_rejection_reason
Revises: 0050_drawing_view_role

Rejected model invocations already retained cost and identity, but not the reason the local
validator refused the response. This adds a nullable reason column and constrains it so only
rejected rows may carry one. Existing rejected rows remain null: `model_invocations` is append-only,
so the migration records the schema change without rewriting old paid-call history.

Source: issue #701.
Verification: tests/extraction/models/test_nova.py, tests/extraction/models/test_invocations.py,
tests/scripts/test_reading_funnel.py.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0051_model_rejection_reason"
down_revision: str | None = "0050_drawing_view_role"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "model_invocations",
        sa.Column("rejection_reason", sa.String(length=500), nullable=True),
    )
    op.create_check_constraint(
        "model_invocation_rejection_reason",
        "model_invocations",
        "rejection_reason IS NULL OR (outcome = 'rejected' AND rejection_reason <> '')",
    )


def downgrade() -> None:
    op.drop_constraint("model_invocation_rejection_reason", "model_invocations", type_="check")
    op.drop_column("model_invocations", "rejection_reason")
