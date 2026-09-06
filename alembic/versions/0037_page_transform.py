"""Keep enough to rebuild the transform that read a page (#530).

Revision ID: 0037_page_transform
Revises: 0036_candidate_corroboration

A candidate's polygon is integer pixels in the image the reader worked from. Turning that back into
stored page geometry — which `evidence/normalize.py` must do to mint a canonical observation —
normalises by the page's **crop box** at the **dpi the reading used**, and neither was written down.

So the reading half and the deciding half could not be joined at all: a reviewer could confirm what an
extracted value *is*, and nothing could place it on the drawing well enough to become evidence. This
was noted as a gap when crops were built, and is now load-bearing.

**Boxes on the page, dpi on the run.** The boxes describe the drawing and never change; the dpi
describes one act of reading, and the same page read at 150 and at 300 produces candidates in two
different image spaces. Putting dpi on the page would make the second reading overwrite the first's
frame and land every earlier polygon somewhere it never was.

**Four exact decimal strings per box, not floats.** `Decimal("612.0")` survives a JSON round trip and
`612.0` does not — the same reason every other exact value in this schema is stored as text.

Everything is nullable and nothing is backfilled. A page recorded before this migration genuinely has
no boxes, and `(0, 0, width_pt, height_pt)` would be a plausible guess: right for most PDFs, silently
wrong for any page whose crop box is inset, and wrong in the direction of putting evidence on a region
of the drawing nobody wrote. `normalize` refuses a page with no transform instead.

Source: issue #530. Verification: tests/db/test_document_models.py, tests/evidence/test_bridge.py.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0037_page_transform"
down_revision: str | None = "0036_candidate_corroboration"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("pages", sa.Column("media_box", JSONB(), nullable=True))
    op.add_column("pages", sa.Column("crop_box", JSONB(), nullable=True))
    op.add_column("extraction_runs", sa.Column("dpi", sa.Integer(), nullable=True))
    op.create_check_constraint("extraction_run_dpi", "extraction_runs", "dpi IS NULL OR dpi > 0")


def downgrade() -> None:
    op.drop_constraint("extraction_run_dpi", "extraction_runs", type_="check")
    op.drop_column("extraction_runs", "dpi")
    op.drop_column("pages", "crop_box")
    op.drop_column("pages", "media_box")
