"""Store model-versioned vectors for candidate-only dense retrieval.

Revision ID: 0021_dense_embeddings
Revises: 0020_pg_trgm

Source: issue #176. Verification: tests/retrieval/lanes/test_dense.py.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from pgvector.sqlalchemy import VECTOR

from alembic import op

revision: str = "0021_dense_embeddings"
down_revision: str | None = "0020_pg_trgm"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONTENT_KINDS = "'note', 'material_description', 'view_title'"


def _identity_columns() -> tuple[sa.Column[object], ...]:
    return (
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "dense_embeddings",
        *_identity_columns(),
        sa.Column("drawing_item_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("content_kind", sa.String(length=32), nullable=False),
        sa.Column("source_text_hash", sa.String(length=64), nullable=False),
        sa.Column("model_id", sa.String(length=200), nullable=False),
        sa.Column("model_version", sa.String(length=100), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("embedding", VECTOR(), nullable=False),
        sa.CheckConstraint(
            f"content_kind IN ({CONTENT_KINDS})", name="dense_embedding_content_kind"
        ),
        sa.CheckConstraint(
            "source_text_hash ~ '^[0-9a-f]{64}$'", name="dense_embedding_source_text_hash"
        ),
        sa.CheckConstraint("model_id <> ''", name="dense_embedding_model_id_present"),
        sa.CheckConstraint("model_version <> ''", name="dense_embedding_model_version_present"),
        sa.CheckConstraint("dimensions > 0", name="dense_embedding_dimensions_positive"),
        sa.CheckConstraint(
            "vector_dims(embedding) = dimensions", name="dense_embedding_dimensions_match"
        ),
        sa.ForeignKeyConstraint(["drawing_item_id"], ["drawing_items.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "drawing_item_id",
            "content_kind",
            "source_text_hash",
            "model_id",
            "model_version",
            name="uq_dense_embeddings_source_model",
        ),
    )
    op.create_index("ix_dense_embeddings_drawing_item_id", "dense_embeddings", ["drawing_item_id"])
    op.create_index("ix_dense_embeddings_content_kind", "dense_embeddings", ["content_kind"])
    op.create_index("ix_dense_embeddings_model_id", "dense_embeddings", ["model_id"])
    op.create_index("ix_dense_embeddings_model_version", "dense_embeddings", ["model_version"])
    op.create_index(
        "ix_dense_embeddings_model_version_kind",
        "dense_embeddings",
        ["model_id", "model_version", "content_kind"],
    )


def downgrade() -> None:
    op.drop_table("dense_embeddings")
    # Do not drop vector: extensions are database-wide and another schema may depend on it.
