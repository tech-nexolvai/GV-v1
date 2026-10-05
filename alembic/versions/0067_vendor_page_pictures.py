"""Store vendor-only page pictures for click-to-place.

Revision ID: 0067_vendor_page_pictures
Revises: 0066_shared_evidence_crops
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.db.roles import ROLE_GRANTS

revision: str = "0067_vendor_page_pictures"
down_revision: str | None = "0066_shared_evidence_crops"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PICTURES_TABLE = "vendor_page_pictures"
IMMUTABLE_TABLES: tuple[str, ...] = (PICTURES_TABLE,)


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
    op.create_table(
        PICTURES_TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("page_id", sa.Uuid(), nullable=False),
        sa.Column("storage_key", sa.String(length=1000), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("media_type", sa.String(length=200), nullable=False),
        sa.Column("dpi", sa.Integer(), nullable=False),
        sa.Column("width_px", sa.Integer(), nullable=False),
        sa.Column("height_px", sa.Integer(), nullable=False),
        sa.Column("snap_points", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("snap_tolerance", sa.String(length=64), nullable=True),
        sa.CheckConstraint("dpi > 0", name="vendor_page_picture_dpi_positive"),
        sa.CheckConstraint(
            "width_px > 0 AND height_px > 0", name="vendor_page_picture_size_positive"
        ),
        sa.CheckConstraint(
            "storage_key !~ '^[[:space:]]*$'", name="vendor_page_picture_key_not_blank"
        ),
        sa.CheckConstraint(
            "media_type !~ '^[[:space:]]*$'", name="vendor_page_picture_media_not_blank"
        ),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="vendor_page_picture_sha256"),
        sa.ForeignKeyConstraint(["page_id"], ["pages.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("page_id", name="uq_vendor_page_pictures_page_id"),
    )

    for table in IMMUTABLE_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_append_only "
            f"BEFORE UPDATE OR DELETE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION gv_reject_mutation()"
        )

    grant_statements = [
        f'GRANT {", ".join(privileges)} ON TABLE %I."{table}" TO {role.value}'
        for role, grants in ROLE_GRANTS.items()
        for table, privileges in grants.privileges.items()
        if table in IMMUTABLE_TABLES
    ]
    if grant_statements:
        op.get_bind().execute(sa.text(_in_current_schema(*grant_statements)))


def downgrade() -> None:
    for table in IMMUTABLE_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
    op.drop_table("vendor_page_pictures")
