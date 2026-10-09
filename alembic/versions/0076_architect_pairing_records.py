"""Append-only records of which architect dimension pairs with which vendor piece (#1053).

One automatic record per vendor countertop row per extraction run (code by drawn position, both
Claude readers with identical answers, or nobody), and a reviewer's pairing as a new record that
supersedes the latest. Never edited: the same append-only trigger as every other evidence table.

Revision ID: 0076_architect_pairing_records
Revises: 0075_slot_row_review_scope
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.db.roles import ROLE_GRANTS

revision: str = "0076_architect_pairing_records"
down_revision: str | None = "0075_slot_row_review_scope"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

IMMUTABLE_TABLES = ("architect_pairing_records",)


def _in_current_schema(*statements: str) -> str:
    body = "\n".join(f"    EXECUTE format({statement!r}, gv_schema);" for statement in statements)
    return f"""
DO $$
DECLARE
    gv_schema text := current_schema();
BEGIN
{body}
END
$$;
"""


def upgrade() -> None:
    op.create_table(
        "architect_pairing_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("package_revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("page_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("row_anchor_candidate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("extraction_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("pairs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("supersedes_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("decided_by", sa.String(length=200), nullable=True),
        sa.ForeignKeyConstraint(
            ["package_revision_id"], ["package_revisions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["page_id"], ["pages.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["row_anchor_candidate_id"], ["observation_candidates.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["extraction_run_id"], ["extraction_runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["supersedes_id", "row_anchor_candidate_id"],
            ["architect_pairing_records.id", "architect_pairing_records.row_anchor_candidate_id"],
            name="fk_architect_pairing_supersedes_same_row",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("id", "row_anchor_candidate_id", name="uq_architect_pairing_id_row"),
        sa.CheckConstraint(
            "source IN ('code', 'both-ais', 'reviewer', 'none')",
            name="architect_pairing_source",
        ),
        sa.CheckConstraint(
            "status IN ('paired', 'ambiguous', 'no_fit', 'no_scale', 'nothing_comparable', "
            "'ais-disagree', 'ais-refused', 'reviewer')",
            name="architect_pairing_status",
        ),
        sa.CheckConstraint("jsonb_typeof(pairs) = 'array'", name="architect_pairing_pairs_array"),
        sa.CheckConstraint(
            "jsonb_typeof(details) = 'object'", name="architect_pairing_details_object"
        ),
        sa.CheckConstraint(
            "(source = 'reviewer') = (decided_by IS NOT NULL)",
            name="architect_pairing_reviewer_named",
        ),
        sa.CheckConstraint(
            "(source = 'reviewer') = (status = 'reviewer')",
            name="architect_pairing_reviewer_status",
        ),
        sa.CheckConstraint(
            "(source = 'reviewer') = (extraction_run_id IS NULL)",
            name="architect_pairing_automatic_run",
        ),
        sa.CheckConstraint(
            "decided_by IS NULL OR decided_by !~ '^[[:space:]]*$'",
            name="architect_pairing_actor_not_blank",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_architect_pairing_records_package_revision_id",
        "architect_pairing_records",
        ["package_revision_id"],
    )
    op.create_index(
        "ix_architect_pairing_records_row_anchor_candidate_id",
        "architect_pairing_records",
        ["row_anchor_candidate_id"],
    )
    op.create_index(
        "uq_architect_pairing_root",
        "architect_pairing_records",
        ["row_anchor_candidate_id"],
        unique=True,
        postgresql_where=sa.text("supersedes_id IS NULL"),
    )
    op.create_index(
        "uq_architect_pairing_superseded_once",
        "architect_pairing_records",
        ["supersedes_id"],
        unique=True,
        postgresql_where=sa.text("supersedes_id IS NOT NULL"),
    )
    op.execute(
        "CREATE TRIGGER architect_pairing_records_append_only "
        "BEFORE UPDATE OR DELETE ON architect_pairing_records "
        "FOR EACH ROW EXECUTE FUNCTION gv_reject_mutation()"
    )
    grant_statements = [
        f'GRANT {", ".join(privileges)} ON TABLE %I."{table}" TO {role.value}'
        for role, grants in ROLE_GRANTS.items()
        for table, privileges in grants.privileges.items()
        if table in IMMUTABLE_TABLES
    ]
    if grant_statements:
        op.get_bind().execute(sa.text(_in_current_schema(*grant_statements)))


def downgrade() -> None:
    connection = op.get_bind()
    count = connection.scalar(sa.text("SELECT count(*) FROM architect_pairing_records"))
    if count:
        raise RuntimeError(
            "Cannot downgrade architect pairings: stored pairing records (automatic and "
            "reviewer) would be lost. Preserve these records."
        )
    op.execute(
        "DROP TRIGGER IF EXISTS architect_pairing_records_append_only ON architect_pairing_records"
    )
    op.drop_index("uq_architect_pairing_superseded_once", table_name="architect_pairing_records")
    op.drop_index("uq_architect_pairing_root", table_name="architect_pairing_records")
    op.drop_index(
        "ix_architect_pairing_records_row_anchor_candidate_id",
        table_name="architect_pairing_records",
    )
    op.drop_index(
        "ix_architect_pairing_records_package_revision_id", table_name="architect_pairing_records"
    )
    op.drop_table("architect_pairing_records")
