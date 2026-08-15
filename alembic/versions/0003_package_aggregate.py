"""Projects, packages, revisions and ordered state events (#192).

Revision ID: 0003_package_aggregate
Revises: 0002_evaluation_tables
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_package_aggregate"
down_revision: str | None = "0002_evaluation_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PACKAGE_STATES = (
    "CREATED",
    "UPLOADING",
    "UPLOADED",
    "INGESTING",
    "EXTRACTING",
    "MATCHING",
    "VALIDATING_EVIDENCE",
    "RUNNING_CHECKS",
    "GENERATING_OUTPUTS",
    "AWAITING_REVIEW",
    "APPROVED",
    "CHANGES_REQUESTED",
    "FAILED_RETRYABLE",
    "FAILED_PERMANENT",
    "NEEDS_INPUT",
    "CANCELLED",
    "SUPERSEDED",
)


def _state_type(name: str) -> sa.Enum:
    return sa.Enum(
        *PACKAGE_STATES,
        name=name,
        native_enum=False,
        create_constraint=True,
    )


def upgrade() -> None:
    """Create the package aggregate without cascade-delete paths."""

    op.create_table(
        "projects",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("company_standards_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "packages",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("project_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("vendor", sa.String(length=200), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "package_revisions",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("package_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("state", _state_type("package_revision_state"), nullable=False),
        sa.Column("supersedes_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(["package_id"], ["packages.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["supersedes_id"], ["package_revisions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("package_id", "revision_number"),
    )
    op.create_table(
        "package_state_events",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("package_revision_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("from_state", _state_type("package_event_from_state"), nullable=True),
        sa.Column("to_state", _state_type("package_event_to_state"), nullable=False),
        sa.Column("actor", sa.String(length=200), nullable=False),
        sa.Column("reason", sa.String(length=1000), nullable=True),
        sa.ForeignKeyConstraint(
            ["package_revision_id"], ["package_revisions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("package_revision_id", "sequence"),
    )


def downgrade() -> None:
    """Drop the package aggregate in dependency order."""

    op.drop_table("package_state_events")
    op.drop_table("package_revisions")
    op.drop_table("packages")
    op.drop_table("projects")
