"""Record a re-check of whether an already stored evidence crop shows GV's markup (#1141).

Revision ID: 0081_evidence_mark_rechecks
Revises: 0080_one_end_wall_layout

Crops cut before #1078 carry a "shows GV markup" flag (`evidence_artifacts.shows_gv_marks`) that
asked only about GV marks baked into the vendor's drawing, never about GV's own annotation layer the
picture paints on. Anant's decision (c) for #952, 2026-10-10: a one-time job asks the corrected
check again and records the answer.

**A sibling table, not an update.** `evidence_artifacts` refuses every `UPDATE` and `DELETE` (0013),
and that guard stays. As 0062 foresaw, adding an answer to a picture that already exists needs a
second table: one append-only row per crop per check version, written only where the new answer
differs from the stored one. Readers use the newest re-check, else the crop's own flag.

**Nothing else changes.** No crop, reading, finding or decision row is touched; the job only inserts
here. The same append-only trigger as every other record table, and the roles' grants derived from
`ROLE_GRANTS` (append-only: `SELECT`, `INSERT`).

**No audit category added.** The rows are the record: `run_by` and `created_at` say who and when,
`run_id` groups one run, so how many it changed is a count. `audit_events` has no place for a count,
and a category for one maintenance job would widen a deliberately closed set.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.db.roles import ROLE_GRANTS

revision: str = "0081_evidence_mark_rechecks"
down_revision: str | None = "0080_one_end_wall_layout"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

IMMUTABLE_TABLES = ("evidence_mark_rechecks",)


def upgrade() -> None:
    op.create_table(
        "evidence_mark_rechecks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("crop_artifact_id", sa.Uuid(), nullable=False),
        sa.Column("shows_gv_marks", sa.Boolean(), nullable=True),
        sa.Column("check_version", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("run_by", sa.String(length=200), nullable=False),
        sa.ForeignKeyConstraint(
            ["crop_artifact_id"], ["evidence_artifacts.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("crop_artifact_id", "check_version"),
        sa.CheckConstraint("check_version !~ '^[[:space:]]*$'", name="mark_recheck_version_named"),
        sa.CheckConstraint("run_by !~ '^[[:space:]]*$'", name="mark_recheck_run_by_named"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_evidence_mark_rechecks_crop_artifact_id",
        "evidence_mark_rechecks",
        ["crop_artifact_id"],
    )
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
    count = op.get_bind().scalar(sa.text("SELECT count(*) FROM evidence_mark_rechecks"))
    if count:
        raise RuntimeError(
            "Cannot downgrade evidence mark re-checks: they are the corrected GV-markup flags of "
            f"{count} stored crop(s) and the record of who corrected them. Preserve these records."
        )
    for table in IMMUTABLE_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
    op.drop_index("ix_evidence_mark_rechecks_crop_artifact_id", table_name="evidence_mark_rechecks")
    op.drop_table("evidence_mark_rechecks")
