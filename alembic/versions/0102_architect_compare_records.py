"""Record where each architect dimension was paired with the vendor's line, and a person's answers on it (#1418).

Revision ID: 0102_architect_compare_records
Revises: 0101_job_value_readings

**Why.** The direct compare (#1417) pairs every dimension the architect printed with the vendor's
dimension line for the same span, by geometry only. Reading a sheet for it takes seconds per page,
so the pairing is done once by the worker and stored; the review reads it. One append-only row per
architect dimension per reading, under the extraction run that read it. No decision is stored here:
whether the vendor's number matches is decided from the stored readings when the review reads it.
A person's answer on a compared dimension ("the vendor is wrong", "not a difference", "checked by
hand") is its own append-only row in `architect_compare_answers`; it never edits the record.

The same append-only trigger as every other record table, and the roles' grants derived from
`ROLE_GRANTS` (append-only: `SELECT`, `INSERT`). A downgrade with rows stored refuses.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.db.roles import ROLE_GRANTS

revision: str = "0102_architect_compare_records"
down_revision: str | None = "0101_job_value_readings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "architect_compare_records"
ANSWERS = "architect_compare_answers"
IMMUTABLE_TABLES = (TABLE, ANSWERS)
_INDEXED = ("extraction_run_id", "package_revision_id", "page_id")


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("extraction_run_id", sa.Uuid(), nullable=False),
        sa.Column("package_revision_id", sa.Uuid(), nullable=False),
        sa.Column("page_id", sa.Uuid(), nullable=False),
        sa.Column("architect_view", sa.Integer(), nullable=False),
        sa.Column("view_title", sa.Text(), nullable=True),
        sa.Column("architect_number", sa.Integer(), nullable=False),
        sa.Column("architect_text", sa.Text(), nullable=False),
        sa.Column("architect_numerator", sa.BigInteger(), nullable=True),
        sa.Column("architect_denominator", sa.BigInteger(), nullable=True),
        sa.Column("axis", sa.String(length=16), nullable=True),
        sa.Column("pairing", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("architect_box", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("vendor_view", sa.Integer(), nullable=True),
        sa.Column("vendor_label_box", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("vendor_line_box", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("vendor_drawn", sa.String(length=64), nullable=True),
        sa.Column("paired_by", sa.String(length=16), nullable=True),
        sa.ForeignKeyConstraint(["extraction_run_id"], ["extraction_runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["package_revision_id"], ["package_revisions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["page_id"], ["pages.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("pairing IN ('paired', 'unpaired')", name="architect_compare_pairing"),
        sa.CheckConstraint(
            "(architect_numerator IS NULL) = (architect_denominator IS NULL) "
            "AND (architect_denominator IS NULL OR architect_denominator > 0)",
            name="architect_compare_value",
        ),
        sa.CheckConstraint(
            "pairing = 'unpaired' OR (vendor_label_box IS NOT NULL AND vendor_line_box IS NOT NULL "
            "AND architect_numerator IS NOT NULL)",
            name="architect_compare_paired_has_vendor",
        ),
        sa.CheckConstraint("reason <> ''", name="architect_compare_reason"),
        sa.CheckConstraint(
            "(pairing = 'paired') = (paired_by IS NOT NULL) "
            "AND (paired_by IS NULL OR paired_by IN ('geometry', 'two AIs'))",
            name="architect_compare_paired_by",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in _INDEXED:
        op.create_index(f"ix_{TABLE}_{column}", TABLE, [column])
    op.create_table(
        ANSWERS,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("record_id", sa.Uuid(), nullable=False),
        sa.Column("package_revision_id", sa.Uuid(), nullable=False),
        sa.Column("answer", sa.String(length=32), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("answered_by", sa.String(length=200), nullable=False),
        sa.ForeignKeyConstraint(["record_id"], [f"{TABLE}.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["package_revision_id"], ["package_revisions.id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "answer IN ('vendor_wrong', 'not_a_difference', 'checked_right', 'checked_wrong', "
            "'not_compared')",
            name="architect_compare_answer_kind",
        ),
        sa.CheckConstraint("note <> '' AND answered_by <> ''", name="architect_compare_answer_who"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("record_id", "package_revision_id"):
        op.create_index(f"ix_{ANSWERS}_{column}", ANSWERS, [column])
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
        sa.text(f"SELECT (SELECT count(*) FROM {TABLE}) + (SELECT count(*) FROM {ANSWERS})")
    )
    if count:
        raise RuntimeError(
            f"Cannot downgrade architect compare records: {count} row(s) record what the compare "
            "paired and what a person was shown. Preserve these records."
        )
    for table in IMMUTABLE_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
    for column in ("record_id", "package_revision_id"):
        op.drop_index(f"ix_{ANSWERS}_{column}", table_name=ANSWERS)
    op.drop_table(ANSWERS)
    for column in _INDEXED:
        op.drop_index(f"ix_{TABLE}_{column}", table_name=TABLE)
    op.drop_table(TABLE)
