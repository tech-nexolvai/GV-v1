"""Permit a draft cloud PDF beside the other review outputs (#1340).

Revision ID: 0099_draft_redline_output
Revises: 0098_item_view_links

**Why.** A reviewer wants to see the clouded drawing before signing off: what will be clouded as
wrong, what still needs a person and what was not checked. The signed cloud PDF (`redline`) is made
only after sign-off. `draft_redline` is the kind for the draft, which the worker renders on request
with a "DRAFT - not signed off" banner on every page.

**A separate kind on purpose.** The signed downloads serve only the `redline` named in a signed
bundle, and check its kind; a draft can never be served as the signed file.

**A downgrade with drafts stored refuses**: `output_artifacts` is append-only, and the old CHECK
could not hold them.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0099_draft_redline_output"
down_revision: str | None = "0098_item_view_links"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Written out here, not imported from the model: a migration describes one fixed state.
#: `tests/db/test_draft_redline_migration.py` compares them with `OutputArtifactKind`.
WITHOUT_DRAFT = "'findings_pdf', 'findings_workbook', 'redline'"
WITH_DRAFT = "'draft_redline', " + WITHOUT_DRAFT


def _replace_constraint(values: str) -> None:
    op.drop_constraint("output_artifact_kind", "output_artifacts", type_="check")
    op.create_check_constraint("output_artifact_kind", "output_artifacts", f"kind IN ({values})")


def upgrade() -> None:
    _replace_constraint(WITH_DRAFT)


def downgrade() -> None:
    """Narrow the CHECK again, refused while any draft cloud PDF is stored."""
    stored = op.get_bind().scalar(
        sa.text("SELECT count(*) FROM output_artifacts WHERE kind = 'draft_redline'")
    )
    if stored:
        raise RuntimeError(
            f"Cannot downgrade the draft cloud PDFs: {stored} draft(s) are stored, and "
            "output_artifacts is append-only. Preserve these records."
        )
    _replace_constraint(WITHOUT_DRAFT)
