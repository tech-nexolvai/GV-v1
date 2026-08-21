"""Record the exact bounded context used by each model invocation.

Revision ID: 0019_model_context
Revises: 0018_agent_checkpoints

Source: issue #252. Verification: tests/extraction/models/test_context.py.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0019_model_context"
down_revision: str | None = "0018_agent_checkpoints"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "model_invocations",
        sa.Column("assembled_context", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column("model_invocations", sa.Column("bound_pt", sa.Numeric(), nullable=True))
    op.create_check_constraint(
        "model_invocation_context_pair",
        "model_invocations",
        "(assembled_context IS NULL) = (bound_pt IS NULL)",
    )
    op.create_check_constraint(
        "model_invocation_context_bound",
        "model_invocations",
        "bound_pt IS NULL OR bound_pt >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_model_invocations_model_invocation_context_bound",
        "model_invocations",
        type_="check",
    )
    op.drop_constraint(
        "ck_model_invocations_model_invocation_context_pair",
        "model_invocations",
        type_="check",
    )
    op.drop_column("model_invocations", "bound_pt")
    op.drop_column("model_invocations", "assembled_context")
