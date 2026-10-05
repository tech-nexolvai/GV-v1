"""Keep reviewer-requested reading proposals scoped to their drawing page.

Revision ID: 0068_page_measurements
Revises: 0067_vendor_page_pictures
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0068_page_measurements"
down_revision: str | None = "0067_vendor_page_pictures"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "measurement_proposals",
        sa.Column("page_number", sa.Integer(), nullable=True),
    )
    op.create_check_constraint(
        "measurement_proposal_page_number_positive",
        "measurement_proposals",
        "page_number IS NULL OR page_number > 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "measurement_proposal_page_number_positive",
        "measurement_proposals",
        type_="check",
    )
    op.drop_column("measurement_proposals", "page_number")
