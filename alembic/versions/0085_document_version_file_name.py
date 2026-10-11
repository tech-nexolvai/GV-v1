"""Keep the file name the reviewer uploaded on each document version (#1174).

Revision ID: 0085_document_version_file_name
Revises: 0084_architect_view_matches

The results, the architect view picker and the reports named the architect's own file "the
architect's drawings", because an upload kept no file name. A reviewer with several files or
revisions could not tell which upload was compared.

**Display text only.** The column is never a key, never compared, never read by a check. On
`document_versions`, because a name belongs to one upload: a re-upload of the same document may be
named differently, and the stored bytes (`source_artifacts`) have no name of their own.

**Nullable on purpose.** Every upload before this has no name, and nothing is backfilled: a name
cannot be recovered from the bytes, and a guessed one would name the wrong file. A reader says "the
architect's drawings" for NULL, exactly as before.

The CHECK holds what the API's cleaning guarantees (`app.models.document.clean_file_name`) for
every writer: not blank, no `/` or `\\`, no control characters. 255 characters at most.

`document_versions` is append-only (0013): adding a nullable column fires no row trigger, and the
roles' grants are on the table, so they cover the new column with no new `GRANT`. A downgrade with
names stored refuses, like 0071: they are part of the upload's record.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0085_document_version_file_name"
down_revision: str | None = "0084_architect_view_matches"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT = "document_version_file_name"


def upgrade() -> None:
    """Add the nullable name and its CHECK. Existing versions stay NULL."""
    op.add_column("document_versions", sa.Column("file_name", sa.String(length=255), nullable=True))
    op.create_check_constraint(
        CONSTRAINT,
        "document_versions",
        "file_name IS NULL OR (file_name !~ '^[[:space:]]*$' AND file_name !~ '[/\\\\]' "
        "AND file_name !~ '[[:cntrl:]]')",
    )


def downgrade() -> None:
    """Drop the CHECK and the column, refused while any upload has a name stored."""
    stored = op.get_bind().scalar(
        sa.text("SELECT count(*) FROM document_versions WHERE file_name IS NOT NULL")
    )
    if stored:
        raise RuntimeError(
            f"Cannot downgrade document version file names: {stored} upload(s) have the file name "
            "the reviewer uploaded stored. Preserve these records."
        )
    op.drop_constraint(CONSTRAINT, "document_versions", type_="check")
    op.drop_column("document_versions", "file_name")
