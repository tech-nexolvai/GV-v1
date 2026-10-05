"""Allow candidate readings to share immutable crop bytes and record mark checks.

Revision ID: 0066_shared_evidence_crops
Revises: 0065_check_run_defaults_citation
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0066_shared_evidence_crops"
down_revision: str | None = "0065_check_run_defaults_citation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The repository naming convention names the original constraint explicitly. Crop bytes are
    # content-addressed and can safely have several candidate-owned provenance rows.
    op.drop_constraint(
        "uq_evidence_artifacts_storage_key_sha256", "evidence_artifacts", type_="unique"
    )
    op.add_column(
        "evidence_artifacts",
        sa.Column("shows_gv_marks", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    bind = op.get_bind()
    duplicate_groups = bind.execute(
        sa.text(
            "SELECT count(*) FROM ("
            "SELECT storage_key, sha256 FROM evidence_artifacts "
            "GROUP BY storage_key, sha256 HAVING count(*) > 1"
            ") AS shared_objects"
        )
    ).scalar_one()
    if duplicate_groups:
        raise RuntimeError(
            "cannot downgrade shared evidence crops: "
            f"{duplicate_groups} stored object group(s) have multiple artifact rows; "
            "no rows were changed"
        )

    op.create_unique_constraint(
        "uq_evidence_artifacts_storage_key_sha256",
        "evidence_artifacts",
        ["storage_key", "sha256"],
    )
    op.drop_column("evidence_artifacts", "shows_gv_marks")
