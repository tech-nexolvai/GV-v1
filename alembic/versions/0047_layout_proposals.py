"""Record model-proposed layout discriminators and reviewer confirmations (#655).

Revision ID: 0047_layout_proposals
Revises: 0046_geometry_availability

A closed-question layout classifier can now say which rulebook discriminator value it read from a
page region, but that answer is still only a proposal. The reviewer must confirm it before it can
travel to ``run_checks``.

The proposal row records the value, the crop artifact, and the model/prompt identity. The
confirmation row records the separate human act, including who did it and when. Both are
append-only; a re-run or a correction is another row, never an edit.

Source: issue #655. Verification: tests/app/test_layout_proposals.py.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.db.roles import ROLE_GRANTS

revision: str = "0047_layout_proposals"
down_revision: str | None = "0046_geometry_availability"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

IMMUTABLE_TABLES: tuple[str, ...] = ("layout_proposals", "layout_confirmations")
PROPOSALS = "layout_proposals"
CONFIRMATIONS = "layout_confirmations"


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


def _grant_new_tables() -> None:
    grant_statements = [
        f'GRANT {", ".join(privileges)} ON TABLE %I."{table}" TO {role.value}'
        for role, grants in ROLE_GRANTS.items()
        for table, privileges in grants.privileges.items()
        if table in IMMUTABLE_TABLES
    ]
    if grant_statements:
        op.get_bind().execute(sa.text(_in_current_schema(*grant_statements)))


def upgrade() -> None:
    op.create_table(
        PROPOSALS,
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("package_revision_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("discriminator_name", sa.String(length=100), nullable=False),
        sa.Column("proposed_value", sa.String(length=200), nullable=False),
        sa.Column("crop_artifact_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("model_id", sa.String(length=200), nullable=False),
        sa.Column("prompt_id", sa.String(length=100), nullable=False),
        sa.ForeignKeyConstraint(
            ["package_revision_id"], ["package_revisions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["crop_artifact_id"], ["evidence_artifacts.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(
            "package_revision_id",
            "discriminator_name",
            "proposed_value",
            "crop_artifact_id",
            "model_id",
            "prompt_id",
            name="uq_layout_proposals_same_evidence",
        ),
        sa.CheckConstraint(
            "discriminator_name !~ '^[[:space:]]*$'",
            name="layout_proposal_discriminator_not_blank",
        ),
        sa.CheckConstraint(
            "proposed_value !~ '^[[:space:]]*$'",
            name="layout_proposal_value_not_blank",
        ),
        sa.CheckConstraint("model_id !~ '^[[:space:]]*$'", name="layout_proposal_model_not_blank"),
        sa.CheckConstraint(
            "prompt_id !~ '^[[:space:]]*$'", name="layout_proposal_prompt_not_blank"
        ),
    )
    op.create_index(f"ix_{PROPOSALS}_package_revision_id", PROPOSALS, ["package_revision_id"])
    op.create_index(f"ix_{PROPOSALS}_crop_artifact_id", PROPOSALS, ["crop_artifact_id"])

    op.create_table(
        CONFIRMATIONS,
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("package_revision_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("discriminator_name", sa.String(length=100), nullable=False),
        sa.Column("value", sa.String(length=200), nullable=False),
        sa.Column("confirmed_by", sa.String(length=200), nullable=False),
        sa.ForeignKeyConstraint(
            ["package_revision_id"], ["package_revisions.id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "discriminator_name !~ '^[[:space:]]*$'",
            name="layout_confirmation_discriminator_not_blank",
        ),
        sa.CheckConstraint("value !~ '^[[:space:]]*$'", name="layout_confirmation_value_not_blank"),
        sa.CheckConstraint(
            "confirmed_by !~ '^[[:space:]]*$'",
            name="layout_confirmation_actor_not_blank",
        ),
    )
    op.create_index(
        f"ix_{CONFIRMATIONS}_package_revision_id", CONFIRMATIONS, ["package_revision_id"]
    )

    for table in IMMUTABLE_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_append_only "
            f"BEFORE UPDATE OR DELETE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION gv_reject_mutation()"
        )

    _grant_new_tables()


def downgrade() -> None:
    for table in IMMUTABLE_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
    op.drop_index(f"ix_{CONFIRMATIONS}_package_revision_id", table_name=CONFIRMATIONS)
    op.drop_table(CONFIRMATIONS)
    op.drop_index(f"ix_{PROPOSALS}_crop_artifact_id", table_name=PROPOSALS)
    op.drop_index(f"ix_{PROPOSALS}_package_revision_id", table_name=PROPOSALS)
    op.drop_table(PROPOSALS)
