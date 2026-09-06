"""Somewhere to keep the document a review produces (#519).

Revision ID: 0031_output_artifacts
Revises: 0030_retention

Checks run and findings are recorded, and nothing turns them into a file anybody can be sent. The
missing piece is not the renderer — `reports/spreadsheet.py` has been finished and tested for months
— it is a table to record what was produced and where the bytes went.

**A third artifact table, rather than reusing either existing one.** `source_artifacts` is the input
side: a file somebody sent us. `evidence_artifacts` must belong to a candidate or a canonical
observation, enforced by a `CHECK`, and a workbook covering a whole revision belongs to neither.
Reusing either would have meant a nullable owner column that is always null for one kind of row,
which is how a table stops being able to say what it holds.

**`Immutable`, like the findings it is built from.** Regenerating produces a new row. A deliverable
that could be edited in place would let the file a vendor was sent differ from the record of what was
sent, with nothing to show it had changed — and `findings` is append-only for exactly that reason.

**`kind` has one permitted value today.** `findings_workbook`. A `redline` value would be a promise
the code does not keep: an annotated drawing needs each finding tied to the region of the sheet it is
about, which needs semantic typing, and candidates are deliberately untyped until the real drawings
(#274) and the vocabulary Q20 defers. The `CHECK` gains the value on the day something writes one —
the same lesson as `ModelInvocationOutcome.FAILED`, which the database rejected for as long as the
enum had a member the constraint did not.

Source: issue #519. Verification: tests/db/test_output_artifacts.py, tests/workflow/test_generate_outputs.py.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.db.roles import ROLE_GRANTS

revision: str = "0031_output_artifacts"
down_revision: str | None = "0030_retention"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Every table carrying `Immutable` that earlier migrations did not already protect.
#:
#: Written out rather than derived, for the reason 0013 gives in its own docstring: a migration has
#: to keep saying what it said the day it ran. `tests/db/test_append_only.py` unions these lists
#: across migrations and compares the result with `immutable_table_names()`.
IMMUTABLE_TABLES: tuple[str, ...] = ("output_artifacts",)

#: The only kind of output anything produces today. See the module docstring for why `redline` is
#: absent rather than reserved.
KINDS: tuple[str, ...] = ("findings_workbook",)


def _in_current_schema(*statements: str) -> str:
    """Wrap statements so `%I` becomes `current_schema()` when they run.

    The same shape 0025, 0027 and 0030 use, and for the same reason: the test fixture gives every
    test its own schema, so the schema is resolved at execution time by PostgreSQL rather than by
    this file querying for it — `tests/app/test_migration_matches_models.py` renders every migration
    offline against no database at all.
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
    kinds = ", ".join(f"'{kind}'" for kind in KINDS)
    op.create_table(
        "output_artifacts",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("package_revision_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("storage_key", sa.String(length=1000), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("media_type", sa.String(length=200), nullable=False),
        sa.Column("findings", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["package_revision_id"], ["package_revisions.id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint(f"kind IN ({kinds})", name="output_artifact_kind"),
        sa.CheckConstraint("storage_key <> ''", name="output_artifact_storage_key"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="output_artifact_sha256"),
        sa.CheckConstraint("media_type <> ''", name="output_artifact_media_type"),
        sa.CheckConstraint("findings >= 0", name="output_artifact_findings"),
        sa.UniqueConstraint("storage_key", "sha256", name="uq_output_artifacts_key_sha"),
    )
    op.create_index(
        "ix_output_artifacts_package_revision_id", "output_artifacts", ["package_revision_id"]
    )

    for table in IMMUTABLE_TABLES:
        # `gv_reject_mutation` is created by 0013 and reused rather than redefined: two definitions
        # of one rule is how they come to differ.
        op.execute(
            f"CREATE TRIGGER {table}_append_only "
            f"BEFORE UPDATE OR DELETE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION gv_reject_mutation()"
        )

    # 0025 derives its grant list from `Base.metadata`, so it already names this table — but it runs
    # six migrations earlier, when the table does not exist, and #303 made that skip rather than
    # fail. The grant therefore belongs to the migration that creates it. Read from `app.db.roles`
    # rather than written out: two copies of a grant drift in the direction of more privilege, and
    # `tests/db/test_roles.py` compares the declaration against `information_schema`.
    grant_statements = [
        f'GRANT {", ".join(privileges)} ON TABLE %I."{table}" TO {role.value}'
        for role, grants in ROLE_GRANTS.items()
        for table, privileges in grants.privileges.items()
        if table == "output_artifacts"
    ]
    if grant_statements:
        op.get_bind().execute(sa.text(_in_current_schema(*grant_statements)))


def downgrade() -> None:
    for table in IMMUTABLE_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
    op.drop_index("ix_output_artifacts_package_revision_id", table_name="output_artifacts")
    op.drop_table("output_artifacts")
