"""Record what the job values reader found for a package revision (#1409).

Revision ID: 0101_job_value_readings
Revises: 0100_review_action_tags

**Why.** The job values reader (#1407) reads cabinet depth, countertop overhang, backsplash
thickness and cabinet side thickness from the architect's drawings, and the simple review's "Enter
this job's values" form shows what it found beside each field. Its results need a home that no
reader of dimension readings looks at: `observation_candidates` is read by automatic typing, the
confirm route and the operands, so a job value there could be taken for a drawn dimension. One
append-only row per value per reading, under the extraction run that read it.

The same append-only trigger as every other record table, and the roles' grants derived from
`ROLE_GRANTS` (append-only: `SELECT`, `INSERT`). A downgrade with rows stored refuses: they are the
record of what the reader found and what the person was shown.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.db.roles import ROLE_GRANTS

revision: str = "0101_job_value_readings"
down_revision: str | None = "0100_review_action_tags"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "job_value_readings"
IMMUTABLE_TABLES = (TABLE,)

#: Written out, not imported from the model: a migration describes one fixed state.
_SETTINGS = (
    "cabinet_depth",
    "countertop_overhang",
    "backsplash_thickness",
    "cabinet_side_thickness",
)
_OUTCOMES = ("auto", "suggestion", "refused", "nothing")


def _listed(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("extraction_run_id", sa.Uuid(), nullable=False),
        sa.Column("package_revision_id", sa.Uuid(), nullable=False),
        sa.Column("setting", sa.String(length=64), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("suggestion", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("reasons", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("not_read", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(["extraction_run_id"], ["extraction_runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["package_revision_id"], ["package_revisions.id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint(f"setting IN ({_listed(_SETTINGS)})", name="job_value_reading_setting"),
        sa.CheckConstraint(f"outcome IN ({_listed(_OUTCOMES)})", name="job_value_reading_outcome"),
        sa.CheckConstraint(
            "suggestion IS NULL OR (jsonb_typeof(suggestion) = 'object' "
            "AND outcome IN ('auto', 'suggestion'))",
            name="job_value_reading_suggestion",
        ),
        sa.CheckConstraint("jsonb_typeof(reasons) = 'array'", name="job_value_reading_reasons"),
        sa.CheckConstraint("jsonb_typeof(not_read) = 'array'", name="job_value_reading_not_read"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("extraction_run_id", "package_revision_id"):
        op.create_index(f"ix_{TABLE}_{column}", TABLE, [column])
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
    count = op.get_bind().scalar(sa.text(f"SELECT count(*) FROM {TABLE}"))
    if count:
        raise RuntimeError(
            f"Cannot downgrade job value readings: {count} row(s) record what the job values "
            "reader found and what a person was shown. Preserve these records."
        )
    for table in IMMUTABLE_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
    for column in ("extraction_run_id", "package_revision_id"):
        op.drop_index(f"ix_{TABLE}_{column}", table_name=TABLE)
    op.drop_table(TABLE)
