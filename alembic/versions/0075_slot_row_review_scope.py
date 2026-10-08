"""Persist reviewer decisions for a single slot-reader row.

Revision ID: 0075_slot_row_review_scope
Revises: 0074_invocation_question_packet
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.db.roles import ROLE_GRANTS

revision: str = "0075_slot_row_review_scope"
down_revision: str | None = "0074_invocation_question_packet"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

IMMUTABLE_TABLES = ("slot_row_review_decisions",)


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
    op.add_column(
        "findings",
        sa.Column("scope_row_candidate_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_findings_scope_slot_row",
        "findings",
        "observation_candidates",
        ["scope_row_candidate_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.drop_constraint("finding_scope_pair", "findings", type_="check")
    op.create_check_constraint(
        "finding_scope_pair",
        "findings",
        "(scope_item_id IS NULL AND scope_row_candidate_id IS NULL AND scope_label IS NULL) OR "
        "(scope_item_id IS NOT NULL AND scope_row_candidate_id IS NULL "
        "AND scope_label IS NOT NULL AND scope_label <> '') OR "
        "(scope_item_id IS NULL AND scope_row_candidate_id IS NOT NULL "
        "AND scope_label IS NOT NULL AND scope_label <> '')",
    )
    op.create_index(
        "ix_findings_revision_slot_scope",
        "findings",
        ["package_revision_id", "scope_row_candidate_id"],
    )
    op.create_table(
        "slot_row_review_decisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("row_candidate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("supersedes_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("wall_config", sa.String(length=32), nullable=True),
        sa.Column("measurements", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("confirmed_by", sa.String(length=200), nullable=False),
        sa.ForeignKeyConstraint(
            ["row_candidate_id"], ["observation_candidates.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_id", "row_candidate_id"],
            ["slot_row_review_decisions.id", "slot_row_review_decisions.row_candidate_id"],
            name="fk_slot_row_review_supersedes_same_row",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("id", "row_candidate_id", name="uq_slot_row_review_id_row"),
        sa.CheckConstraint(
            "wall_config IS NULL OR wall_config IN ('back_left_right', 'back_only', 'island')",
            name="slot_row_review_wall_config",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(measurements) = 'object'",
            name="slot_row_review_measurements_object",
        ),
        sa.CheckConstraint(
            "confirmed_by !~ '^[[:space:]]*$'", name="slot_row_review_actor_not_blank"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.add_column(
        "verdict_inputs",
        sa.Column("slot_row_review_decision_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_verdict_inputs_row_review",
        "verdict_inputs",
        "slot_row_review_decisions",
        ["slot_row_review_decision_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "verdict_input_one_evidence_source",
        "verdict_inputs",
        "canonical_observation_id IS NULL OR slot_row_review_decision_id IS NULL",
    )
    op.create_index(
        "ix_verdict_inputs_slot_row_review_decision_id",
        "verdict_inputs",
        ["slot_row_review_decision_id"],
    )
    op.create_index(
        "ix_slot_row_review_decisions_row_candidate_id",
        "slot_row_review_decisions",
        ["row_candidate_id"],
    )
    op.create_index(
        "uq_slot_row_review_root",
        "slot_row_review_decisions",
        ["row_candidate_id"],
        unique=True,
        postgresql_where=sa.text("supersedes_id IS NULL"),
    )
    op.create_index(
        "uq_slot_row_review_superseded_once",
        "slot_row_review_decisions",
        ["supersedes_id"],
        unique=True,
        postgresql_where=sa.text("supersedes_id IS NOT NULL"),
    )
    op.execute(
        "CREATE TRIGGER slot_row_review_decisions_append_only "
        "BEFORE UPDATE OR DELETE ON slot_row_review_decisions "
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
    decision_count = connection.scalar(sa.text("SELECT count(*) FROM slot_row_review_decisions"))
    finding_count = connection.scalar(
        sa.text("SELECT count(*) FROM findings WHERE scope_row_candidate_id IS NOT NULL")
    )
    if decision_count or finding_count:
        raise RuntimeError(
            "Cannot downgrade row-scoped review decisions: stored reviewer decisions or "
            "row-scoped findings would be lost. Preserve these records."
        )
    op.execute(
        "DROP TRIGGER IF EXISTS slot_row_review_decisions_append_only ON slot_row_review_decisions"
    )
    op.drop_index("uq_slot_row_review_superseded_once", table_name="slot_row_review_decisions")
    op.drop_index("uq_slot_row_review_root", table_name="slot_row_review_decisions")
    op.drop_index(
        "ix_slot_row_review_decisions_row_candidate_id", table_name="slot_row_review_decisions"
    )
    op.drop_index("ix_verdict_inputs_slot_row_review_decision_id", table_name="verdict_inputs")
    op.drop_constraint("verdict_input_one_evidence_source", "verdict_inputs", type_="check")
    op.drop_constraint(
        "fk_verdict_inputs_row_review",
        "verdict_inputs",
        type_="foreignkey",
    )
    op.drop_column("verdict_inputs", "slot_row_review_decision_id")
    op.drop_table("slot_row_review_decisions")
    op.drop_index("ix_findings_revision_slot_scope", table_name="findings")
    op.drop_constraint("finding_scope_pair", "findings", type_="check")
    op.create_check_constraint(
        "finding_scope_pair",
        "findings",
        "(scope_item_id IS NULL AND scope_label IS NULL) OR "
        "(scope_item_id IS NOT NULL AND scope_label IS NOT NULL AND scope_label <> '')",
    )
    op.drop_constraint("fk_findings_scope_slot_row", "findings", type_="foreignkey")
    op.drop_column("findings", "scope_row_candidate_id")
