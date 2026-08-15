"""Documents, immutable versions, pages and source artifacts (#193).

Revision ID: 0004_document_aggregate
Revises: 0003_package_aggregate
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0004_document_aggregate"
down_revision: str | None = "0003_package_aggregate"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DOCUMENT_KINDS = "'architectural', 'shop', 'schedule', 'product_spec'"
PAGE_TYPES = "'plan', 'elevation', 'section', 'detail', 'schedule', 'title'"
SHA256_PATTERN = "^[0-9a-f]{64}$"


def upgrade() -> None:
    """Create the document aggregate and close the earlier gold-case reference gap."""

    op.create_table(
        "source_artifacts",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("storage_key", sa.String(length=1000), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("backend_version_id", sa.String(length=500), nullable=True),
        sa.CheckConstraint("storage_key <> ''", name="source_artifact_storage_key"),
        sa.CheckConstraint(f"sha256 ~ '{SHA256_PATTERN}'", name="source_artifact_sha256"),
        sa.CheckConstraint("size >= 0", name="source_artifact_size"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "sha256"),
        sa.UniqueConstraint("storage_key"),
    )
    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("package_revision_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.CheckConstraint(f"kind IN ({DOCUMENT_KINDS})", name="document_kind"),
        sa.ForeignKeyConstraint(
            ["package_revision_id"], ["package_revisions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "document_versions",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("document_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("source_artifact_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.CheckConstraint(f"sha256 ~ '{SHA256_PATTERN}'", name="document_version_sha256"),
        sa.CheckConstraint("page_count >= 0", name="document_version_page_count"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["source_artifact_id", "sha256"],
            ["source_artifacts.id", "source_artifacts.sha256"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", "sha256"),
        sa.UniqueConstraint("source_artifact_id"),
    )
    op.create_table(
        "pages",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("document_version_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("index", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("width_pt", sa.Numeric(), nullable=False),
        sa.Column("height_pt", sa.Numeric(), nullable=False),
        sa.Column("rotation", sa.Integer(), nullable=False),
        sa.Column("has_vector_text", sa.Boolean(), nullable=False),
        sa.Column("render_failed", sa.Boolean(), nullable=False),
        sa.Column("sheet_number", sa.String(length=100), nullable=True),
        sa.Column("page_type", sa.String(length=32), nullable=True),
        sa.Column("revision_label", sa.String(length=100), nullable=True),
        sa.Column("revision_date_raw", sa.String(length=100), nullable=True),
        sa.Column("revision_date_interpretations", postgresql.JSONB(), nullable=True),
        sa.Column("revision_sequence_index", sa.Integer(), nullable=True),
        sa.CheckConstraint("index >= 0", name="page_index"),
        sa.CheckConstraint(f"content_hash ~ '{SHA256_PATTERN}'", name="page_content_hash"),
        sa.CheckConstraint("width_pt > 0", name="page_width"),
        sa.CheckConstraint("height_pt > 0", name="page_height"),
        sa.CheckConstraint("rotation IN (0, 90, 180, 270)", name="page_rotation"),
        sa.CheckConstraint(
            f"page_type IS NULL OR page_type IN ({PAGE_TYPES})",
            name="page_type",
        ),
        sa.CheckConstraint(
            "revision_sequence_index IS NULL OR revision_sequence_index >= 0",
            name="page_revision_sequence",
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"], ["document_versions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_version_id", "index"),
    )
    op.create_foreign_key(
        "fk_gold_cases_document_version_id_document_versions",
        "gold_cases",
        "document_versions",
        ["document_version_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    """Remove the additive reference, then drop the aggregate in dependency order."""

    op.drop_constraint(
        "fk_gold_cases_document_version_id_document_versions",
        "gold_cases",
        type_="foreignkey",
    )
    op.drop_table("pages")
    op.drop_table("document_versions")
    op.drop_table("documents")
    op.drop_table("source_artifacts")
