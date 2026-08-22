"""Bind evidence review actions to original and resulting immutable facts.

Revision ID: 0022_evidence_review_actions
Revises: 0021_dense_embeddings

Source: issue #230. Verification: tests/review/test_evidence_actions.py.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0022_evidence_review_actions"
down_revision: str | None = "0021_dense_embeddings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("review_actions", sa.Column("original_observation_id", sa.Uuid(), nullable=True))
    op.add_column("review_actions", sa.Column("resulting_observation_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_review_action_original_observation",
        "review_actions",
        "canonical_observations",
        ["original_observation_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_review_action_resulting_observation",
        "review_actions",
        "canonical_observations",
        ["resulting_observation_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "review_action_observation_pair",
        "review_actions",
        "(original_observation_id IS NULL AND resulting_observation_id IS NULL) OR "
        "(original_observation_id IS NOT NULL AND resulting_observation_id IS NOT NULL "
        "AND original_observation_id <> resulting_observation_id)",
    )
    op.create_index(
        "ix_review_actions_original_observation_id",
        "review_actions",
        ["original_observation_id"],
    )
    op.create_index(
        "ix_review_actions_resulting_observation_id",
        "review_actions",
        ["resulting_observation_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_review_actions_resulting_observation_id", table_name="review_actions")
    op.drop_index("ix_review_actions_original_observation_id", table_name="review_actions")
    op.drop_constraint("review_action_observation_pair", "review_actions", type_="check")
    op.drop_constraint(
        "fk_review_action_resulting_observation",
        "review_actions",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_review_action_original_observation",
        "review_actions",
        type_="foreignkey",
    )
    op.drop_column("review_actions", "resulting_observation_id")
    op.drop_column("review_actions", "original_observation_id")
