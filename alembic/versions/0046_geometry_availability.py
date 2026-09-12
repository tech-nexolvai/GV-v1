"""Tell "we checked and it floats" apart from "there was nothing to check against" (#598).

Revision ID: 0046_geometry_availability
Revises: 0045_measurement_proposals

`workflow/assignment.py` refuses a reading that is attached to no dimension line, because an
unattached number may be a title block or a scale bar and nothing should let a model decide
otherwise. That is right on a drawing made of vector line-work.

**It switched autofill off entirely for scanned drawings, on the strength of a check that never
ran.** Measured on a real uploaded pair: `page texts=0, segments=0, annotation strokes=0` on both
sheets — images in a PDF wrapper. There is no vector geometry, so the detector finds no dimension
lines, so nothing can attach, so every proposal is refused and every field stays empty. The refusal
was recorded, correctly, and read downstream as though the drawing had been examined.

Two columns make the distinction real rather than something inferred from a sentence:

* `observation_associations.lines_on_page` — how many dimension lines were on the page when the
  decision was made. `0` means the check could not run; a larger number means it ran and refused.
* `measurement_proposals.placement_verified` — whether the drawing's own geometry confirmed this
  reading's placement. `False` where there was no geometry to confirm it with.

A proposal on a page with no line-work still passes every other check — right sheet, one reading
per field, the right number of values, a reading this run actually produced — and a reviewer still
confirms it before anything is saved. What changes is that the screen can say which of the two
grounds it is showing, instead of showing nothing at all.

Both nullable/defaulted, because every row written before today was written without the question
being asked, and back-filling an answer nobody computed would be inventing one.

Source: issue #598. Verification: tests/workflow/test_assignment.py, tests/workflow/test_association.py.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0046_geometry_availability"
down_revision: str | None = "0045_measurement_proposals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "observation_associations", sa.Column("lines_on_page", sa.Integer(), nullable=True)
    )
    op.add_column(
        "measurement_proposals",
        sa.Column("placement_verified", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    # The default exists to write the existing rows; new rows state their own answer, and leaving a
    # server default in place would let a future insert omit the question rather than answer it.
    op.alter_column("measurement_proposals", "placement_verified", server_default=None)


def downgrade() -> None:
    """Drops both columns. The associations and proposals themselves are untouched."""
    op.drop_column("measurement_proposals", "placement_verified")
    op.drop_column("observation_associations", "lines_on_page")
