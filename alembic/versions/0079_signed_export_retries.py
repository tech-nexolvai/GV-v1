"""Record each output file's size, and a reviewer's retry of a failed signed export (#1065).

Revision ID: 0079_signed_export_retries
Revises: 0078_finding_decision_carryovers

1. `output_artifacts.size`: the stored file's length in bytes, written with the file so the
   signed-exports status can show it without opening anything. Nullable, and nothing is backfilled:
   files written before this migration honestly have no recorded size.
2. `approval_export_retries`: one append-only row each time a reviewer asks to publish the same
   frozen snapshot again after a recorded failure. It points at the existing snapshot, so the
   approval and its facts stay exactly as signed. The same append-only trigger as every other record
   table.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.db.roles import ROLE_GRANTS

revision: str = "0079_signed_export_retries"
down_revision: str | None = "0078_finding_decision_carryovers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

IMMUTABLE_TABLES = ("approval_export_retries",)


def upgrade() -> None:
    op.add_column("output_artifacts", sa.Column("size", sa.BigInteger(), nullable=True))
    op.create_check_constraint(
        "output_artifact_size", "output_artifacts", "size IS NULL OR size >= 0"
    )
    op.create_table(
        "approval_export_retries",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("package_revision_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by", sa.String(200), nullable=False),
        sa.ForeignKeyConstraint(
            ["snapshot_id", "package_revision_id"],
            ["approval_export_snapshots.id", "approval_export_snapshots.package_revision_id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("requested_by <> ''", name="export_retry_requested_by"),
    )
    op.create_index(
        "ix_approval_export_retries_snapshot_id", "approval_export_retries", ["snapshot_id"]
    )
    for table in IMMUTABLE_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION gv_reject_mutation()"
        )
    for role, grants in ROLE_GRANTS.items():
        for table, privileges in grants.privileges.items():
            if table in IMMUTABLE_TABLES:
                op.execute(f'GRANT {", ".join(privileges)} ON TABLE "{table}" TO {role.value}')


def downgrade() -> None:
    for table in IMMUTABLE_TABLES:
        if op.get_bind().scalar(sa.text(f"SELECT EXISTS (SELECT 1 FROM {table})")):
            raise RuntimeError(
                "Cannot downgrade signed export retries: stored retry records must not be deleted"
            )
    op.drop_index("ix_approval_export_retries_snapshot_id", "approval_export_retries")
    op.drop_table("approval_export_retries")
    # Dropping a recorded size loses nothing the files themselves do not still hold.
    op.drop_constraint("output_artifact_size", "output_artifacts", type_="check")
    op.drop_column("output_artifacts", "size")
