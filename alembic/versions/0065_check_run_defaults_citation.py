"""Pin the exact synthetic rule defaults cited by each new check run.

Revision ID: 0065_check_run_defaults_citation
Revises: 0064_countertop_run_wall_layout

Null columns identify legacy runs whose defaults cannot be verified.  No old row is backfilled.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0065_check_run_defaults_citation"
down_revision: str | None = "0064_countertop_run_wall_layout"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("check_runs", sa.Column("defaults_set_id", sa.String(71), nullable=True))
    op.add_column("check_runs", sa.Column("defaults_canonical_json", sa.Text(), nullable=True))
    op.create_check_constraint(
        "check_run_defaults_paired",
        "check_runs",
        "(defaults_set_id IS NULL) = (defaults_canonical_json IS NULL)",
    )
    op.create_check_constraint(
        "check_run_defaults_digest_shape",
        "check_runs",
        "defaults_set_id IS NULL OR defaults_set_id ~ '^sha256:[0-9a-f]{64}$'",
    )


def downgrade() -> None:
    op.drop_constraint("check_run_defaults_digest_shape", "check_runs", type_="check")
    op.drop_constraint("check_run_defaults_paired", "check_runs", type_="check")
    op.drop_column("check_runs", "defaults_canonical_json")
    op.drop_column("check_runs", "defaults_set_id")
