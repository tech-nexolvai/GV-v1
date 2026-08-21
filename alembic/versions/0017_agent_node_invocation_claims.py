"""Add interrupt-safe agent node claims and direct candidate attribution.

Revision ID: 0017_agent_node_invocation_claims
Revises: 0016_state_event_workflow_run

The claim is deliberately a separate mutable row. ``model_invocations`` and
``observation_candidates`` remain append-only statements about completed events.

Source: issue #247 and admin ruling of 2026-08-21.
Verification: tests/extraction/agent/test_checkpoints.py.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0017_agent_node_invocation_claims"
down_revision: str | None = "0016_state_event_workflow_run"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "model_invocations", sa.Column("node_invocation_key", sa.String(71), nullable=True)
    )
    op.add_column("model_invocations", sa.Column("candidate_id", sa.Uuid(), nullable=True))
    op.create_unique_constraint(
        "uq_model_invocations_node_invocation_key",
        "model_invocations",
        ["node_invocation_key"],
    )
    op.create_unique_constraint(
        "uq_model_invocations_candidate_id", "model_invocations", ["candidate_id"]
    )
    op.create_check_constraint(
        "model_invocation_node_key",
        "model_invocations",
        "node_invocation_key IS NULL OR node_invocation_key ~ '^sha256:[0-9a-f]{64}$'",
    )
    op.create_foreign_key(
        "fk_model_invocations_candidate_id_observation_candidates",
        "model_invocations",
        "observation_candidates",
        ["candidate_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.create_table(
        "agent_node_invocation_claims",
        sa.Column("node_invocation_key", sa.String(71), nullable=False),
        sa.Column("extraction_run_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("model_invocation_id", sa.Uuid(), nullable=True),
        sa.Column("candidate_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["extraction_run_id"], ["extraction_runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["model_invocation_id"], ["model_invocations.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["observation_candidates.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("node_invocation_key"),
        sa.UniqueConstraint("model_invocation_id"),
        sa.UniqueConstraint("candidate_id"),
        sa.CheckConstraint(
            "node_invocation_key ~ '^sha256:[0-9a-f]{64}$'",
            name="agent_node_invocation_claim_key",
        ),
        sa.CheckConstraint(
            "state IN ('in_progress', 'completed', 'failed')",
            name="agent_node_invocation_claim_state",
        ),
        sa.CheckConstraint(
            "(state = 'in_progress' AND model_invocation_id IS NULL AND candidate_id IS NULL) OR "
            "(state = 'completed' AND model_invocation_id IS NOT NULL AND candidate_id IS NOT NULL) OR "
            "(state = 'failed' AND model_invocation_id IS NOT NULL AND candidate_id IS NULL)",
            name="agent_node_invocation_claim_completion",
        ),
    )
    op.create_index(
        "ix_agent_node_invocation_claims_extraction_run_id",
        "agent_node_invocation_claims",
        ["extraction_run_id"],
    )


def downgrade() -> None:
    op.drop_table("agent_node_invocation_claims")
    op.drop_constraint(
        "fk_model_invocations_candidate_id_observation_candidates",
        "model_invocations",
        type_="foreignkey",
    )
    op.drop_constraint(
        "ck_model_invocations_model_invocation_node_key",
        "model_invocations",
        type_="check",
    )
    op.drop_constraint("uq_model_invocations_candidate_id", "model_invocations", type_="unique")
    op.drop_constraint(
        "uq_model_invocations_node_invocation_key", "model_invocations", type_="unique"
    )
    op.drop_column("model_invocations", "candidate_id")
    op.drop_column("model_invocations", "node_invocation_key")
