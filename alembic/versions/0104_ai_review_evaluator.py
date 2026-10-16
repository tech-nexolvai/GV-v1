"""Record the evaluator's second look on AI page review rows (decision 2026-10-16).

Revision ID: 0104_ai_review_evaluator
Revises: 0103_ai_page_reviews

**Why.** AI page review differences are clouded automatically, so each row code calls `different`
is checked again by an independent evaluator (a different model, its own close-ups, a neutral
claim) before it is clouded (`extraction/ai_review/evaluator.py`). Three nullable columns on
`ai_page_review_rows` record it on the row as stored: `verified_by` (`evaluator` when it confirmed
the difference), `evaluator_reason` (its reason, or why it could not be asked) and
`evaluator_answer` (its answer as given). The rows stay append-only: a re-check writes a new
reading with new rows, never an update. Rows stored before this carry nulls (not re-checked). A
downgrade with any evaluator value stored refuses.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0104_ai_review_evaluator"
down_revision: str | None = "0103_ai_page_reviews"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROWS = "ai_page_review_rows"


def upgrade() -> None:
    op.add_column(ROWS, sa.Column("verified_by", sa.String(length=16), nullable=True))
    op.add_column(ROWS, sa.Column("evaluator_reason", sa.Text(), nullable=True))
    op.add_column(
        ROWS,
        sa.Column("evaluator_answer", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_check_constraint(
        "ai_page_review_row_verified",
        ROWS,
        "verified_by IS NULL OR (verified_by = 'evaluator' AND result = 'different' "
        "AND evaluator_reason IS NOT NULL AND evaluator_reason <> '')",
    )


def downgrade() -> None:
    count = op.get_bind().scalar(
        sa.text(
            f"SELECT count(*) FROM {ROWS} WHERE verified_by IS NOT NULL "
            "OR evaluator_reason IS NOT NULL OR evaluator_answer IS NOT NULL"
        )
    )
    if count:
        raise RuntimeError(
            f"Cannot downgrade the AI review evaluator: {count} row(s) record what a paid "
            "evaluator said. Preserve these records."
        )
    op.drop_constraint("ai_page_review_row_verified", ROWS, type_="check")
    for column in ("evaluator_answer", "evaluator_reason", "verified_by"):
        op.drop_column(ROWS, column)
