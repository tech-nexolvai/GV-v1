"""Earlier AI calls recorded outside this project's reviews (#1165).

Revision ID: 0082_ai_spend_history
Revises: 0081_evidence_mark_rechecks

The Usage page counted only calls tied to a review in the project. Earlier reading runs, bake-offs
and proofs were recorded (model, tokens, cost) in other local databases and never appeared. This
table holds them, one row per call; `scripts/import_spend_history.py` fills it and
`GET /projects/{id}/usage/history` adds it up. Only the call's id, day, model, route, purpose,
tokens and cost: no prompt, answer or drawing data.

**Append-only, one row per call** (unique per project and call id), with the same trigger as every
other record table: importing again only adds calls not seen before, so nothing imported is ever
lost or rewritten. The services only read it (`SERVICES_READ_ONLY` in `app/db/roles.py`); the import
script writes as the owner.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.db.roles import ROLE_GRANTS

revision: str = "0082_ai_spend_history"
down_revision: str | None = "0081_evidence_mark_rechecks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

IMMUTABLE_TABLES = ("ai_spend_history",)


def upgrade() -> None:
    op.create_table(
        "ai_spend_history",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("call_id", sa.Uuid(), nullable=False),
        sa.Column("occurred_on", sa.Date(), nullable=False),
        sa.Column("model_id", sa.String(length=300), nullable=False),
        sa.Column("route", sa.String(length=32), nullable=False),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_micros", sa.BigInteger(), nullable=True),
        sa.Column("priced_later", sa.Boolean(), nullable=False),
        sa.Column("source_label", sa.String(length=200), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("project_id", "call_id"),
        sa.CheckConstraint(
            "route IN ('bedrock', 'openrouter', 'anthropic', 'unknown')",
            name="spend_history_route",
        ),
        sa.CheckConstraint(
            "purpose IN ('reading', 'row-choice', 'chat', 'assistant', 'findings', 'bake-off', "
            "'other')",
            name="spend_history_purpose",
        ),
        sa.CheckConstraint("model_id <> ''", name="spend_history_model_id"),
        sa.CheckConstraint("input_tokens >= 0 AND output_tokens >= 0", name="spend_history_tokens"),
        sa.CheckConstraint("cost_micros IS NULL OR cost_micros >= 0", name="spend_history_cost"),
        sa.CheckConstraint(
            "NOT priced_later OR cost_micros IS NOT NULL", name="spend_history_priced_later"
        ),
        sa.CheckConstraint("source_label !~ '^[[:space:]]*$'", name="spend_history_source_label"),
        sa.PrimaryKeyConstraint("id"),
    )
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
    count = op.get_bind().scalar(sa.text("SELECT count(*) FROM ai_spend_history"))
    if count:
        raise RuntimeError(
            f"Cannot downgrade the AI spend history: it holds {count} imported call(s) of earlier "
            "spend. Preserve these records."
        )
    for table in IMMUTABLE_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
    op.drop_table("ai_spend_history")
