"""Let a package say what product its drawing set is for (#994).

Revision ID: 0073_package_product_type
Revises: 0072_countertop_piece_width

The reviewer picks the product at upload (admin, 2026-10-07), so the check run can evaluate only the
rules for that product and the readers can be told what they are looking at. **Nullable on purpose:**
every package created before this has no product, and NULL keeps today's behaviour exactly — every
product's rules are run. Nothing is backfilled; guessing a product for an old package from its vendor
or its file names would decide which checks apply by inference.

On `packages`, not `package_revisions`: the package is the set a reviewer signs off, and a revision
is the same set re-submitted, so its product cannot change between revisions.

**The values are written out, not imported**, for the reason `0015_model_invocation_failed` gives: a
migration describes one fixed state. `tests/db/test_package_product_type.py` reads the constraint
back out of a migrated database and compares it with `ProductType`, which is what catches drift.

Source: #994. Verification: `tests/db/test_package_product_type.py`.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0073_package_product_type"
down_revision: str | None = "0072_countertop_piece_width"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Every `ProductType` value when this migration was written.
PRODUCT_TYPES = "'countertop', 'cabinet'"

CONSTRAINT = "package_product_type"


def upgrade() -> None:
    """Add the nullable column and limit it to the vocabulary. Existing rows stay NULL."""
    op.add_column("packages", sa.Column("product_type", sa.String(length=32), nullable=True))
    op.create_check_constraint(
        CONSTRAINT,
        "packages",
        f"product_type IS NULL OR product_type IN ({PRODUCT_TYPES})",
    )


def downgrade() -> None:
    """Drop the constraint and the column. The product a reviewer chose is lost with it."""
    op.drop_constraint(CONSTRAINT, "packages", type_="check")
    op.drop_column("packages", "product_type")
