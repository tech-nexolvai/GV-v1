"""Write down the drawing that could not be read (#491).

Revision ID: 0029_extraction_failures
Revises: 0028_check_run_supersession

A document whose PDF would not parse contributed no pages, no candidates and no result, and the
package still reported extraction as complete. A drawing nobody could read was therefore
indistinguishable from a drawing with nothing on it — the package-level shape of a false PASS, which
is the failure this system exists to prevent.

**A row, not an exception, and the distinction is the point.** `_fetch` raises when an artifact cannot
be fetched, because that may well succeed next time and `run_stage` redelivers the stage. A corrupt
PDF is not transient: raising would roll back the claim and retry the same broken file for ever,
paying for each attempt and recording nothing on any of them. So this failure is written down once and
the stage carries on with the drawings it can read.

**No message column.** A `pdfminer` error can quote the bytes it choked on, and `AGENTS.md` §6 forbids
drawing content in a trace for exactly the reason it should not sit in a table either. The closed
`reason` vocabulary carries the meaning and `error_type` carries the diagnosis.

**The scope constraint is what makes a row interpretable.** `page_index IS NULL` means the whole
document rather than page zero, so the check pairs it with `reason`: `document_unreadable` may not
name a page and `page_unreadable` must. Without it the two states are one nullable column apart and a
reader has to guess which was meant.

Append-only, like every other record of what the system saw. A later successful re-read adds rows
elsewhere; it does not erase this one, because that a drawing once could not be read is a fact about
the package a reviewer may want.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import text

from alembic import op
from app.db.roles import ROLE_GRANTS

revision: str = "0029_extraction_failures"
down_revision: str | None = "0028_check_run_supersession"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Read by `tests/db/test_append_only.py`, which collects this name from **every** migration that
#: declares it rather than from 0013 alone — a table created here cannot be protected by a migration
#: that ran before it existed.
IMMUTABLE_TABLES = ("extraction_failures",)


def _in_current_schema(*statements: str) -> str:
    """Wrap statements so `%I` becomes `current_schema()` when they run.

    The same shape 0025 and 0027 use, and for the same reason: every test runs in its own schema, so
    the schema is resolved by PostgreSQL at execution time rather than by this file querying for it —
    `tests/app/test_migration_matches_models.py` renders every migration offline, against no database.
    """
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
    op.create_table(
        "extraction_failures",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("extraction_run_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("document_version_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("page_index", sa.Integer(), nullable=True),
        sa.Column("reason", sa.String(length=32), nullable=False),
        sa.Column("error_type", sa.String(length=100), nullable=False),
        sa.ForeignKeyConstraint(
            ["extraction_run_id"],
            ["extraction_runs.id"],
            name="fk_extraction_failures_extraction_run_id_extraction_runs",
            # RESTRICT everywhere in this schema: the record of what was read, or could not be, must
            # outlive tidying of the rows it points at.
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["document_versions.id"],
            name="fk_extraction_failures_document_version_id_document_versions",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "reason IN ('document_unreadable', 'page_unreadable')",
            name="extraction_failure_reason",
        ),
        sa.CheckConstraint(
            "page_index IS NULL OR page_index >= 0", name="extraction_failure_page_index"
        ),
        sa.CheckConstraint(
            "(reason = 'document_unreadable' AND page_index IS NULL) "
            "OR (reason = 'page_unreadable' AND page_index IS NOT NULL)",
            name="extraction_failure_scope",
        ),
        sa.CheckConstraint("error_type <> ''", name="extraction_failure_error_type"),
    )
    op.create_index(
        "ix_extraction_failures_extraction_run_id",
        "extraction_failures",
        ["extraction_run_id"],
    )
    op.create_index(
        "ix_extraction_failures_document_version_id",
        "extraction_failures",
        ["document_version_id"],
    )
    # **Grants for the new table**, following 0027. 0025 derives its grant list from `Base.metadata`,
    # so it already names this table — but it ran four migrations earlier, when the table did not exist,
    # and #303 made that skip rather than fail. The migration that creates a table applies its grants.
    #
    # Caught by `tests/db/test_roles.py`, which compares the declaration against `information_schema`
    # and found this table granted to nobody. That is worth more than a tidy-up: a failure record no
    # role can read is a record nobody sees, and being seen is the entire purpose of this table.
    #
    # Read from `app.db.roles` rather than written out, for the reason 0025 gives: a grant is the
    # current privilege set, and two copies of it drift in the direction of more privilege.
    grant_statements: list[str] = []
    for role, grants in ROLE_GRANTS.items():
        for table, privileges in grants.privileges.items():
            if table in IMMUTABLE_TABLES:
                granted = ", ".join(privileges)
                grant_statements.append(f'GRANT {granted} ON TABLE %I."{table}" TO {role.value}')
    if grant_statements:
        op.get_bind().execute(text(_in_current_schema(*grant_statements)))

    # Append-only, using the function 0013 created.
    for table in IMMUTABLE_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_append_only "
            f"BEFORE UPDATE OR DELETE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION gv_reject_mutation()"
        )


def downgrade() -> None:
    for table in IMMUTABLE_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
    op.drop_index("ix_extraction_failures_document_version_id", table_name="extraction_failures")
    op.drop_index("ix_extraction_failures_extraction_run_id", table_name="extraction_failures")
    op.drop_table("extraction_failures")
