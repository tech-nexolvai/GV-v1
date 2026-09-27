"""Let a view carry the arch/shop role (ADR-0020, #705).

Revision ID: 0050_drawing_view_role
Revises: 0049_review_invocation_origin

A combined sheet can carry both the ID-set view and the vendor shop view. The role therefore belongs
to `drawing_views`, not only to the uploaded document kind. The column is nullable because an
unconfirmed role is unknown, and unknown views do not participate in matching.

Source: docs/adr/0020-combined-id-and-shop-sheet-intake.md, issue #705.
Verification: tests/db/test_drawing_models.py, tests/workflow/test_view_roles.py.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0050_drawing_view_role"
down_revision: str | None = "0049_review_invocation_origin"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("drawing_views", sa.Column("role", sa.String(length=16), nullable=True))
    op.create_check_constraint(
        "drawing_view_role",
        "drawing_views",
        "role IS NULL OR role IN ('arch', 'shop')",
    )


def downgrade() -> None:
    op.drop_constraint("drawing_view_role", "drawing_views", type_="check")
    op.drop_column("drawing_views", "role")
