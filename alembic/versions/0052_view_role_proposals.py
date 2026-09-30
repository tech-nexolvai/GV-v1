"""Record what the sheet suggests each drawing is, and a person's confirmation of it (#710).

Revision ID: 0052_view_role_proposals
Revises: 0051_model_rejection_reason

A combined sheet carries the architect's drawing and the vendor's, and labels each one. The label
gives a *suggestion* for the drawing's role; only a person's confirmation sets `drawing_views.role`.
The two are separate append-only tables so the one cannot become the other by accident, and so they
cannot be confused with `layout_proposals` / `layout_confirmations`, whose rows are rule inputs.

Source: issue #710. Verification: tests/workflow/test_view_roles.py.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.db.roles import ROLE_GRANTS

revision: str = "0052_view_role_proposals"
down_revision: str | None = "0051_model_rejection_reason"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PROPOSALS = "view_role_proposals"
CONFIRMATIONS = "view_role_confirmations"
IMMUTABLE_TABLES: tuple[str, ...] = (PROPOSALS, CONFIRMATIONS)
ROLES = "'arch', 'shop'"


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
        sa.Column("drawing_view_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("proposed_role", sa.String(length=16), nullable=True),
        sa.Column("heading", sa.String(length=200), nullable=True),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("source", sa.String(length=100), nullable=False),
        sa.ForeignKeyConstraint(["drawing_view_id"], ["drawing_views.id"], ondelete="RESTRICT"),
        sa.CheckConstraint(
            f"proposed_role IS NULL OR proposed_role IN ({ROLES})",
            name="view_role_proposal_role",
        ),
        sa.CheckConstraint(
            "reason !~ '^[[:space:]]*$'", name="view_role_proposal_reason_not_blank"
        ),
        sa.CheckConstraint(
            "source !~ '^[[:space:]]*$'", name="view_role_proposal_source_not_blank"
        ),
    )
    op.create_index(f"ix_{PROPOSALS}_drawing_view_id", PROPOSALS, ["drawing_view_id"])

    op.create_table(
        CONFIRMATIONS,
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("drawing_view_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("confirmed_by", sa.String(length=200), nullable=False),
        sa.ForeignKeyConstraint(["drawing_view_id"], ["drawing_views.id"], ondelete="RESTRICT"),
        sa.CheckConstraint(f"role IN ({ROLES})", name="view_role_confirmation_role"),
        sa.CheckConstraint(
            "confirmed_by !~ '^[[:space:]]*$'", name="view_role_confirmation_actor_not_blank"
        ),
    )
    op.create_index(f"ix_{CONFIRMATIONS}_drawing_view_id", CONFIRMATIONS, ["drawing_view_id"])

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
    op.drop_index(f"ix_{CONFIRMATIONS}_drawing_view_id", table_name=CONFIRMATIONS)
    op.drop_table(CONFIRMATIONS)
    op.drop_index(f"ix_{PROPOSALS}_drawing_view_id", table_name=PROPOSALS)
    op.drop_table(PROPOSALS)
