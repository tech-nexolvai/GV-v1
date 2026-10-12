"""Where a package's reading is while it runs (#1178).

Revision ID: 0087_reading_progress
Revises: 0086_project_sinks

The reading of a drawing set takes minutes, and until now nothing about it was visible until it
ended: the package said `UPLOADED` and the screen could only say "Reading…". The pipeline now
commits at each stage boundary, and within a stage it writes this one row, in its own short
transaction, to say which part is running and how many pages it has done.

**Updated in place, on purpose.** It is execution telemetry, one row per package revision, and no
check, finding or decision reads it. The readings, the stage claims and the package state stay in
the stage's own transaction and stay append-only. So no append-only trigger, and the grants are the
mutable set derived from `ROLE_GRANTS` (`SELECT`, `INSERT`, `UPDATE`).

The stage is a closed list of codes (the CHECK below); the words a person sees live in the API.
`pages_done` never exceeds `pages_total` and is never stated without it.

**No foreign key to `package_revisions`, on purpose.** A running stage holds `FOR UPDATE` on its
revision row (the state-event numbering lock), and a foreign-key check from the progress
transaction must lock that row too, so every progress line would wait until the stage ended.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.db.roles import ROLE_GRANTS

revision: str = "0087_reading_progress"
down_revision: str | None = "0086_project_sinks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "reading_progress"

#: Written out here, not imported from the model: a migration must create the same table whenever it
#: runs. A stage added to `app.models.runs.READING_STAGES` later needs its own migration to widen it.
_STAGES = ", ".join(
    f"'{stage}'"
    for stage in (
        "waiting",
        "ingest",
        "pages",
        "architect",
        "preparing",
        "rows",
        "dimensions",
        "chains",
        "architect_pairing",
        "whole_pages",
        "saving",
        "match",
        "validate",
        "form",
    )
)


def _in_current_schema(*statements: str) -> str:
    body = "\n".join(f"    EXECUTE format({statement!r}, gv_schema);" for statement in statements)
    return f"""
DO $$
DECLARE
    gv_schema text := current_schema();
BEGIN
{body}
END
$$;
"""


def upgrade() -> None:
    """Create the one-row-per-revision progress table and grant it to the roles."""
    op.create_table(
        TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("package_revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("stage", sa.String(length=32), nullable=False),
        sa.Column("pages_done", sa.Integer(), nullable=True),
        sa.Column("pages_total", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("package_revision_id"),
        sa.CheckConstraint(f"stage IN ({_STAGES})", name="reading_progress_stage"),
        sa.CheckConstraint(
            "pages_total IS NULL OR pages_total >= 0", name="reading_progress_pages_total"
        ),
        sa.CheckConstraint(
            "pages_done IS NULL OR (pages_total IS NOT NULL AND pages_done >= 0 "
            "AND pages_done <= pages_total)",
            name="reading_progress_pages_done",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    grant_statements = [
        f'GRANT {", ".join(privileges)} ON TABLE %I."{table}" TO {role.value}'
        for role, grants in ROLE_GRANTS.items()
        for table, privileges in grants.privileges.items()
        if table == TABLE
    ]
    if grant_statements:
        op.get_bind().execute(sa.text(_in_current_schema(*grant_statements)))


def downgrade() -> None:
    """Drop the table. Nothing in it is a record anyone signs: it only said where a reading was."""
    op.drop_table(TABLE)
