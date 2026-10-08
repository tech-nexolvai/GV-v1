"""Retain the exact two-image question packet on every reader attempt (#1001).

Revision ID: 0074_invocation_question_packet
Revises: 0073_package_product_type
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0074_invocation_question_packet"
down_revision: str | None = "0073_package_product_type"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "model_invocations",
        sa.Column(
            "reader_question_packet",
            postgresql.JSONB(none_as_null=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    connection = op.get_bind()
    stored = connection.scalar(
        sa.text("SELECT count(*) FROM model_invocations WHERE reader_question_packet IS NOT NULL")
    )
    if stored:
        raise RuntimeError(
            f"Cannot downgrade reader question packet migration: {stored} immutable invocation "
            "audit record(s) reference exact images; preserve their packet records."
        )
    op.drop_column("model_invocations", "reader_question_packet")
