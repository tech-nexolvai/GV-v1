"""Record a person's wall-layout choice on an immutable countertop run decision (#937).

Revision ID: 0064_countertop_run_wall_layout
Revises: 0063_finding_countertop_scope

Older confirmed decisions remain null: they have no per-countertop choice to assume.
Every new confirmation is validated by the writer and API against the published rule.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0064_countertop_run_wall_layout"
down_revision: str | None = "0063_finding_countertop_scope"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "countertop_run_decisions",
        sa.Column("wall_config", sa.String(length=32), nullable=True),
    )
    op.create_check_constraint(
        "countertop_run_wall_config",
        "countertop_run_decisions",
        "wall_config IS NULL OR (decision = 'confirmed' AND "
        "wall_config IN ('back_left_right', 'back_only', 'island'))",
    )


def downgrade() -> None:
    op.drop_constraint("countertop_run_wall_config", "countertop_run_decisions", type_="check")
    op.drop_column("countertop_run_decisions", "wall_config")
