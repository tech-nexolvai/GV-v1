"""Record why the architect reader read nothing on a page, or what it left out (#1163).

Revision ID: 0082_architect_page_notes
Revises: 0081_evidence_mark_rechecks

The architect reader, on the architect's own file, says why a page gave no view, which stamp held
no drawing, what ink it left out of every view, which view it refused (a stored view of the same
number sits elsewhere on the page) and which page it could not read. Those reasons lived only in
the stage's result, which `run_stage` reduces to a page count, so they were lost. Never silent: one
append-only row per note, under the extraction run that read the page.

**A closed set of kinds**, held by a CHECK constraint: `no_view_found`, `title_not_view`,
`stamp_not_drawing`, `ink_outside_views`, `view_refused`, `page_unreadable`. Nothing reads a value
from these rows; they say what was not read.

The same append-only trigger as every other record table, and the roles' grants derived from
`ROLE_GRANTS` (append-only: `SELECT`, `INSERT`). A downgrade with notes stored refuses, like 0076
and 0081: they are the record of what a reading left out.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.db.roles import ROLE_GRANTS

revision: str = "0082_architect_page_notes"
down_revision: str | None = "0081_evidence_mark_rechecks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

IMMUTABLE_TABLES = ("architect_page_notes",)

_KINDS = (
    "no_view_found",
    "title_not_view",
    "stamp_not_drawing",
    "ink_outside_views",
    "view_refused",
    "page_unreadable",
)


def upgrade() -> None:
    op.create_table(
        "architect_page_notes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("extraction_run_id", sa.Uuid(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column("page_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("text", sa.String(length=500), nullable=False),
        sa.ForeignKeyConstraint(["extraction_run_id"], ["extraction_runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["document_version_id"], ["document_versions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["page_id"], ["pages.id"], ondelete="RESTRICT"),
        sa.CheckConstraint(
            "kind IN (" + ", ".join(f"'{kind}'" for kind in _KINDS) + ")",
            name="architect_page_note_kind",
        ),
        sa.CheckConstraint("text !~ '^[[:space:]]*$'", name="architect_page_note_text_present"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("extraction_run_id", "document_version_id", "page_id"):
        op.create_index(f"ix_architect_page_notes_{column}", "architect_page_notes", [column])
    for table in IMMUTABLE_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION gv_reject_mutation()"
        )
    for role, grants in ROLE_GRANTS.items():
        for table, privileges in grants.privileges.items():
            if table in IMMUTABLE_TABLES:
                op.execute(f'GRANT {", ".join(privileges)} ON TABLE "{table}" TO {role.value}')


def downgrade() -> None:
    count = op.get_bind().scalar(sa.text("SELECT count(*) FROM architect_page_notes"))
    if count:
        raise RuntimeError(
            f"Cannot downgrade architect page notes: {count} note(s) record why the architect "
            "reader read nothing on a page or what it left out. Preserve these records."
        )
    for table in IMMUTABLE_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
    for column in ("extraction_run_id", "document_version_id", "page_id"):
        op.drop_index(f"ix_architect_page_notes_{column}", table_name="architect_page_notes")
    op.drop_table("architect_page_notes")
