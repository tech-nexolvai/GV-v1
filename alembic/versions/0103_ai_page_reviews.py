"""Record the AI page review: one reading per sheet, its rows and a person's answers (decision 2026-10-16).

Revision ID: 0103_ai_page_reviews
Revises: 0102_architect_compare_records

**Why.** The owner chose the sharper AI reading as the compare with the architect: one AI call per
combined sheet reads every vendor dimension and, for each architect dimension code read, the
vendor's dimension of the same span; code decides each row (`extraction/ai_review/checks.py`). A
paid reading is stored once, with its status, model, cost and raw answer (`ai_page_reviews`), its
rows (`ai_page_review_rows`), and a person's answers on a different row (`ai_page_review_answers`).
All three are append-only: the same trigger as every other record table, and the roles' grants
derived from `ROLE_GRANTS` (`SELECT`, `INSERT`). A downgrade with rows stored refuses.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.db.roles import ROLE_GRANTS

revision: str = "0103_ai_page_reviews"
down_revision: str | None = "0102_architect_compare_records"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REVIEWS = "ai_page_reviews"
ROWS = "ai_page_review_rows"
ANSWERS = "ai_page_review_answers"
IMMUTABLE_TABLES = (REVIEWS, ROWS, ANSWERS)
_INDEXED = {
    REVIEWS: ("extraction_run_id", "package_revision_id", "page_id"),
    ROWS: ("review_id", "package_revision_id"),
    ANSWERS: ("row_id", "package_revision_id"),
}


def _common() -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        REVIEWS,
        *_common(),
        sa.Column("extraction_run_id", sa.Uuid(), nullable=False),
        sa.Column("package_revision_id", sa.Uuid(), nullable=False),
        sa.Column("page_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("model_id", sa.String(length=300), nullable=False),
        sa.Column("prompt_id", sa.String(length=300), nullable=False),
        sa.Column("cost_micros", sa.BigInteger(), nullable=True),
        sa.Column("vendor_dpi", sa.Integer(), nullable=True),
        sa.Column("vendor_crop_px", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("raw_answer", sa.Text(), nullable=True),
        sa.Column("dropped", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(["extraction_run_id"], ["extraction_runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["package_revision_id"], ["package_revisions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["page_id"], ["pages.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("status IN ('done', 'failed', 'not_run')", name="ai_page_review_status"),
        sa.CheckConstraint(
            "status = 'done' OR (reason IS NOT NULL AND reason <> '')",
            name="ai_page_review_reason",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        ROWS,
        *_common(),
        sa.Column("review_id", sa.Uuid(), nullable=False),
        sa.Column("package_revision_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("architect_text", sa.Text(), nullable=False),
        sa.Column("architect_numerator", sa.BigInteger(), nullable=True),
        sa.Column("architect_denominator", sa.BigInteger(), nullable=True),
        sa.Column("architect_box", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("measures", sa.Text(), nullable=False),
        sa.Column("vendor_text", sa.Text(), nullable=True),
        sa.Column("vendor_numerator", sa.BigInteger(), nullable=True),
        sa.Column("vendor_denominator", sa.BigInteger(), nullable=True),
        sa.Column("result", sa.String(length=16), nullable=False),
        sa.Column("ai_result", sa.String(length=16), nullable=True),
        sa.Column("why", sa.Text(), nullable=False),
        sa.Column("vendor_box_px", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(["review_id"], [f"{REVIEWS}.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["package_revision_id"], ["package_revisions.id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "result IN ('same', 'different', 'not_shown', 'unsure', 'unreadable')",
            name="ai_page_review_row_result",
        ),
        sa.CheckConstraint(
            "(architect_numerator IS NULL) = (architect_denominator IS NULL) "
            "AND (vendor_numerator IS NULL) = (vendor_denominator IS NULL) "
            "AND (architect_denominator IS NULL OR architect_denominator > 0) "
            "AND (vendor_denominator IS NULL OR vendor_denominator > 0)",
            name="ai_page_review_row_values",
        ),
        sa.CheckConstraint(
            "result NOT IN ('same', 'different') OR "
            "(architect_numerator IS NOT NULL AND vendor_numerator IS NOT NULL)",
            name="ai_page_review_row_decided",
        ),
        sa.CheckConstraint("why <> ''", name="ai_page_review_row_why"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        ANSWERS,
        *_common(),
        sa.Column("row_id", sa.Uuid(), nullable=False),
        sa.Column("package_revision_id", sa.Uuid(), nullable=False),
        sa.Column("answer", sa.String(length=32), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("answered_by", sa.String(length=200), nullable=False),
        sa.ForeignKeyConstraint(["row_id"], [f"{ROWS}.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["package_revision_id"], ["package_revisions.id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "answer IN ('vendor_wrong', 'not_a_difference')", name="ai_page_review_answer_kind"
        ),
        sa.CheckConstraint("note <> '' AND answered_by <> ''", name="ai_page_review_answer_who"),
        sa.PrimaryKeyConstraint("id"),
    )
    for table, columns in _INDEXED.items():
        for column in columns:
            op.create_index(f"ix_{table}_{column}", table, [column])
    for table in IMMUTABLE_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION gv_reject_mutation()"
        )
    for role, grants in ROLE_GRANTS.items():
        for table, privileges in grants.privileges.items():
            if table in IMMUTABLE_TABLES:
                op.execute(f'GRANT {", ".join(privileges)} ON TABLE "{table}" TO {role.value}')


def downgrade() -> None:
    count = op.get_bind().scalar(
        sa.text(
            f"SELECT (SELECT count(*) FROM {REVIEWS}) + (SELECT count(*) FROM {ROWS}) "
            f"+ (SELECT count(*) FROM {ANSWERS})"
        )
    )
    if count:
        raise RuntimeError(
            f"Cannot downgrade the AI page review: {count} row(s) record what a paid reading said "
            "and what a person answered. Preserve these records."
        )
    for table in IMMUTABLE_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
    for table in reversed(IMMUTABLE_TABLES):
        for column in _INDEXED[table]:
            op.drop_index(f"ix_{table}_{column}", table_name=table)
        op.drop_table(table)
