"""Retain each form-reader provider response on its append-only invocation record.

Revision ID: 0071_model_invocation_raw_response
Revises: 0070_candidate_review_reason
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0071_model_invocation_raw_response"
down_revision: str | None = "0070_candidate_review_reason"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("model_invocations", sa.Column("private_raw_response", sa.Text(), nullable=True))
    op.add_column("model_invocations", sa.Column("reader_page_index", sa.Integer(), nullable=True))
    op.add_column(
        "model_invocations", sa.Column("reader_attempt_number", sa.Integer(), nullable=True)
    )
    op.create_check_constraint(
        "model_invocation_reader_attempt_position",
        "model_invocations",
        "(reader_page_index IS NULL) = (reader_attempt_number IS NULL) "
        "AND (reader_page_index IS NULL OR reader_page_index >= 0) "
        "AND (reader_attempt_number IS NULL OR reader_attempt_number >= 1)",
    )


def downgrade() -> None:
    connection = op.get_bind()
    stored = connection.scalar(
        sa.text(
            "SELECT count(*) FROM model_invocations "
            "WHERE private_raw_response IS NOT NULL "
            "OR reader_page_index IS NOT NULL OR reader_attempt_number IS NOT NULL"
        )
    )
    if stored:
        raise RuntimeError(
            f"Cannot downgrade raw-response audit migration: {stored} invocation audit record(s) "
            "are stored; preserve the append-only audit records."
        )
    op.drop_constraint(
        "model_invocation_reader_attempt_position", "model_invocations", type_="check"
    )
    op.drop_column("model_invocations", "reader_attempt_number")
    op.drop_column("model_invocations", "reader_page_index")
    op.drop_column("model_invocations", "private_raw_response")
