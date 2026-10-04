"""Record whether a part's picture shows GV's own coloured marks (#921).

Revision ID: 0062_part_picture_gv_marks
Revises: 0061_part_pictures

The admin's decision of 2026-10-04: a part picture that shows GV's coloured marks says so on the
Measure page. On AI_Set_2 page 14, 2 of the 34 pictures show GV's red and yellow markup baked into
the vendor's drawing, which the vendor-only render cannot strip (#901's case). The worker now asks
the agreement gate's own test whether a picture's pixels hold markup drawn in colour when it cuts
the picture, and records the answer with it:

* `part_pictures.shows_gv_marks` — true where the picture shows such markup, false where it was
  checked and does not, **null where it was not checked**.

**A column, not a sibling table.** The answer is a fact about the picture's pixels, settled once
when they are cut, so it belongs in the same row, written in the same insert. A second table would
only be needed to add the answer to an existing picture later, and nothing does: the pictures cut
before this are not re-checked.

**Append-only safe.** `part_pictures` refuses every `UPDATE` and `DELETE` (0061). Adding a nullable
column with no default is DDL: it rewrites no row and fires no row trigger, so the existing pictures
keep their bytes, their digests and their trigger, and read null, "not checked" — never a guess that
they are clean. The column is filled only when a row is inserted.

**No new grants.** The roles' privileges on `part_pictures` are table-wide (0061), so they cover the
new column, and `gv_verdict` still has none.

Source: issue #921. Verification: tests/db/test_drawing_models.py,
tests/app/test_migrations_roundtrip.py, tests/db/test_append_only.py.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0062_part_picture_gv_marks"
down_revision: str | None = "0061_part_pictures"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable and without a default: every existing picture reads null, "not checked". Spelt out
    # rather than named by a constant, so `tests/app/test_migration_matches_models.py` can read it.
    op.add_column("part_pictures", sa.Column("shows_gv_marks", sa.Boolean(), nullable=True))


def downgrade() -> None:
    """Drops the answers. The pictures, their stored images and every decision are untouched."""
    op.drop_column("part_pictures", "shows_gv_marks")
