"""Carry a reviewer's decision over a check re-run when the result is unchanged (#1073).

Revision ID: 0078_finding_decision_carryovers
Revises: 0077_drawn_length_lane

One append-only link per new finding: which finding it replaced, which reviewer action it inherits,
and the hash of the "same result" fingerprint both share (`app/review/carry_over.py`). No review
action is copied and no finding is touched. Composite foreign keys keep the new finding, the old
finding and the action on one package revision; a fourth pins the stored verb to the action's own,
and a CHECK refuses `correct`. The same append-only trigger as every other record table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.db.roles import ROLE_GRANTS

revision: str = "0078_finding_decision_carryovers"
down_revision: str | None = "0077_drawn_length_lane"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

IMMUTABLE_TABLES = ("finding_decision_carryovers",)


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
        "finding_decision_carryovers",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("package_revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("new_finding_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("from_finding_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("review_action_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("matched_on_hash", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(
            ["new_finding_id", "package_revision_id"],
            ["findings.id", "findings.package_revision_id"],
            name="fk_carryover_new_finding_revision",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["from_finding_id", "package_revision_id"],
            ["findings.id", "findings.package_revision_id"],
            name="fk_carryover_from_finding_revision",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["review_action_id", "package_revision_id"],
            ["review_actions.id", "review_actions.package_revision_id"],
            name="fk_carryover_action_revision",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["review_action_id", "action"],
            ["review_actions.id", "review_actions.action"],
            name="fk_carryover_action_kind",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "action IN ('confirm', 'dismiss', 'except')", name="carryover_action_carriable"
        ),
        sa.CheckConstraint("new_finding_id <> from_finding_id", name="carryover_distinct_findings"),
        sa.CheckConstraint("matched_on_hash ~ '^[0-9a-f]{64}$'", name="carryover_hash_shape"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_finding_decision_carryovers_package_revision_id",
        "finding_decision_carryovers",
        ["package_revision_id"],
    )
    op.create_index(
        "ix_finding_decision_carryovers_new_finding_id",
        "finding_decision_carryovers",
        ["new_finding_id"],
        unique=True,
    )
    op.create_index(
        "ix_finding_decision_carryovers_from_finding_id",
        "finding_decision_carryovers",
        ["from_finding_id"],
    )
    op.create_index(
        "ix_finding_decision_carryovers_review_action_id",
        "finding_decision_carryovers",
        ["review_action_id"],
    )
    op.execute(
        "CREATE TRIGGER finding_decision_carryovers_append_only "
        "BEFORE UPDATE OR DELETE ON finding_decision_carryovers "
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
    count = connection.scalar(sa.text("SELECT count(*) FROM finding_decision_carryovers"))
    if count:
        raise RuntimeError(
            "Cannot downgrade carried decisions: the links say which results inherited a "
            "reviewer's decision, and approvals may rest on them. Preserve these records."
        )
    op.execute(
        "DROP TRIGGER IF EXISTS finding_decision_carryovers_append_only "
        "ON finding_decision_carryovers"
    )
    op.drop_index(
        "ix_finding_decision_carryovers_review_action_id",
        table_name="finding_decision_carryovers",
    )
    op.drop_index(
        "ix_finding_decision_carryovers_from_finding_id",
        table_name="finding_decision_carryovers",
    )
    op.drop_index(
        "ix_finding_decision_carryovers_new_finding_id",
        table_name="finding_decision_carryovers",
    )
    op.drop_index(
        "ix_finding_decision_carryovers_package_revision_id",
        table_name="finding_decision_carryovers",
    )
    op.drop_table("finding_decision_carryovers")
