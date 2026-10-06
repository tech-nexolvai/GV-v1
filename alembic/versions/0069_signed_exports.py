"""Pin signed review inputs and the complete publication bundle.

Revision ID: 0069_signed_exports
Revises: 0068_page_measurements
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.db.roles import ROLE_GRANTS

revision: str = "0069_signed_exports"
down_revision: str | None = "0068_page_measurements"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

IMMUTABLE_TABLES = (
    "approval_export_snapshots",
    "approval_export_actions",
    "approval_export_bundles",
    "approval_export_failures",
)


def _identity() -> list[sa.Column]:
    return [
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_review_actions_id_revision", "review_actions", ["id", "package_revision_id"]
    )
    op.create_unique_constraint(
        "uq_output_artifacts_id_revision", "output_artifacts", ["id", "package_revision_id"]
    )
    op.create_table(
        "approval_export_snapshots",
        *_identity(),
        sa.Column("approval_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("package_revision_id", sa.Uuid(), nullable=False),
        sa.Column("canonical_json", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("deterministic_sha256", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(
            ["approval_id", "package_revision_id"],
            ["approvals.id", "approvals.package_revision_id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("id", "package_revision_id", name="uq_export_snapshot_id_revision"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="export_snapshot_hash"),
        sa.CheckConstraint(
            "deterministic_sha256 ~ '^[0-9a-f]{64}$'", name="export_snapshot_facts_hash"
        ),
    )
    op.create_table(
        "approval_export_actions",
        *_identity(),
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("package_revision_id", sa.Uuid(), nullable=False),
        sa.Column(
            "review_action_id",
            sa.Uuid(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id", "package_revision_id"],
            ["approval_export_snapshots.id", "approval_export_snapshots.package_revision_id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("snapshot_id", "review_action_id", name="uq_export_snapshot_action"),
        sa.ForeignKeyConstraint(
            ["review_action_id", "package_revision_id"],
            ["review_actions.id", "review_actions.package_revision_id"],
            ondelete="RESTRICT",
        ),
    )
    op.create_table(
        "approval_export_bundles",
        *_identity(),
        sa.Column("snapshot_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("package_revision_id", sa.Uuid(), nullable=False),
        sa.Column(
            "pdf_id",
            sa.Uuid(),
            nullable=False,
        ),
        sa.Column(
            "workbook_id",
            sa.Uuid(),
            nullable=False,
        ),
        sa.Column(
            "redline_id",
            sa.Uuid(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id", "package_revision_id"],
            ["approval_export_snapshots.id", "approval_export_snapshots.package_revision_id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "pdf_id <> workbook_id AND pdf_id <> redline_id AND workbook_id <> redline_id",
            name="export_bundle_distinct_files",
        ),
        *(
            sa.ForeignKeyConstraint(
                [column, "package_revision_id"],
                ["output_artifacts.id", "output_artifacts.package_revision_id"],
                ondelete="RESTRICT",
            )
            for column in ("pdf_id", "workbook_id", "redline_id")
        ),
    )
    op.create_table(
        "approval_export_failures",
        *_identity(),
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("package_revision_id", sa.Uuid(), nullable=False),
        sa.Column("error_type", sa.String(100), nullable=False),
        sa.ForeignKeyConstraint(
            ["snapshot_id", "package_revision_id"],
            ["approval_export_snapshots.id", "approval_export_snapshots.package_revision_id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("error_type <> ''", name="export_failure_error_type"),
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
                "Cannot downgrade signed exports: stored approval records must not be deleted"
            )
    for table in reversed(IMMUTABLE_TABLES):
        op.drop_table(table)
    op.drop_constraint("uq_output_artifacts_id_revision", "output_artifacts", type_="unique")
    op.drop_constraint("uq_review_actions_id_revision", "review_actions", type_="unique")
