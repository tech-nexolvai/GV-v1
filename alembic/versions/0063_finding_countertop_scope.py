"""Give each recorded finding its confirmed countertop subject (#935).

Revision ID: 0063_finding_countertop_scope
Revises: 0062_part_picture_gv_marks

Nullable columns leave every older finding revision-scoped. Findings remain immutable;
later checks append new rows and supersede their earlier check runs.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0063_finding_countertop_scope"
down_revision: str | None = "0062_part_picture_gv_marks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("findings", sa.Column("scope_item_id", sa.Uuid(as_uuid=True), nullable=True))
    op.add_column("findings", sa.Column("scope_label", sa.Text(), nullable=True))
    op.create_foreign_key(
        "fk_findings_scope_item",
        "findings",
        "drawing_items",
        ["scope_item_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "finding_scope_pair",
        "findings",
        "(scope_item_id IS NULL AND scope_label IS NULL) OR "
        "(scope_item_id IS NOT NULL AND scope_label IS NOT NULL AND scope_label <> '')",
    )
    op.create_index(
        "ix_findings_revision_scope",
        "findings",
        ["package_revision_id", "scope_item_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_findings_revision_scope", table_name="findings")
    op.drop_constraint("finding_scope_pair", "findings", type_="check")
    op.drop_constraint("fk_findings_scope_item", "findings", type_="foreignkey")
    op.drop_column("findings", "scope_label")
    op.drop_column("findings", "scope_item_id")
