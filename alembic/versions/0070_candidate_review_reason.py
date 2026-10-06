"""Store a human-readable reason for review-only form candidates.

Revision ID: 0070_candidate_review_reason
Revises: 0069_signed_exports
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0070_candidate_review_reason"
down_revision: str | None = "0069_signed_exports"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("observation_candidates", sa.Column("review_reason", sa.String(300)))


def downgrade() -> None:
    op.drop_column("observation_candidates", "review_reason")
