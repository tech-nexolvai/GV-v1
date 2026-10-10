"""Earlier AI spend recorded outside this project's reviews (#1165).

Revision ID: 0082_ai_spend_history
Revises: 0081_evidence_mark_rechecks

The Usage page counted only calls tied to a review in the project. Earlier reading runs, bake-offs
and proofs were recorded (model, tokens, cost) in other local databases and never appeared. This
table holds them as one row per day, model and purpose; `scripts/import_spend_history.py` fills it,
and `GET /projects/{id}/usage/history` reads it. Only counts, tokens and cost: no prompt, answer or
drawing data.

**Mutable, unlike the record tables.** A row summarises records kept elsewhere, keyed by
`(project_id, source_key)`, so importing again updates it rather than adding a second row. The roles'
grants come from `ROLE_GRANTS`, which gives a mutable table `SELECT, INSERT, UPDATE` to the API and
the worker, `SELECT` to reporting, and nothing to the verdict role.
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

TABLE = "ai_spend_history"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("occurred_on", sa.Date(), nullable=False),
        sa.Column("model_id", sa.String(length=300), nullable=False),
        sa.Column("route", sa.String(length=32), nullable=False),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("calls", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cost_micros", sa.BigInteger(), nullable=False),
        sa.Column("unpriced_calls", sa.Integer(), nullable=False),
        sa.Column("source_label", sa.String(length=200), nullable=False),
        sa.Column("source_key", sa.String(length=500), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("project_id", "source_key"),
        sa.CheckConstraint(
            "route IN ('bedrock', 'openrouter', 'anthropic', 'unknown')",
            name="spend_history_route",
        ),
        sa.CheckConstraint(
            "purpose IN ('reading', 'row-choice', 'chat', 'assistant', 'bake-off', 'other')",
            name="spend_history_purpose",
        ),
        sa.CheckConstraint("model_id <> ''", name="spend_history_model_id"),
        sa.CheckConstraint("calls > 0", name="spend_history_calls"),
        sa.CheckConstraint("input_tokens >= 0 AND output_tokens >= 0", name="spend_history_tokens"),
        sa.CheckConstraint("cost_micros >= 0", name="spend_history_cost"),
        sa.CheckConstraint(
            "unpriced_calls >= 0 AND unpriced_calls <= calls", name="spend_history_unpriced"
        ),
        sa.CheckConstraint("source_label !~ '^[[:space:]]*$'", name="spend_history_source_label"),
        sa.CheckConstraint("source_key !~ '^[[:space:]]*$'", name="spend_history_source_key"),
        sa.PrimaryKeyConstraint("id"),
    )
    for role, grants in ROLE_GRANTS.items():
        privileges = grants.privileges.get(TABLE)
        if privileges:
            op.execute(f'GRANT {", ".join(privileges)} ON TABLE "{TABLE}" TO {role.value}')


def downgrade() -> None:
    count = op.get_bind().scalar(sa.text(f"SELECT count(*) FROM {TABLE}"))
    if count:
        raise RuntimeError(
            f"Cannot downgrade the AI spend history: it holds {count} imported row(s) of earlier "
            "spend. Export or re-import them elsewhere first."
        )
    op.drop_table(TABLE)
