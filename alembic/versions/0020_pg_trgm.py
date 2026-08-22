"""Enable indexed trigram search for OCR-variant identifiers.

Revision ID: 0020_pg_trgm
Revises: 0019_model_context

Source: issue #174. Verification: tests/retrieval/lanes/test_trigram.py.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0020_pg_trgm"
down_revision: str | None = "0019_model_context"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        "CREATE INDEX ix_item_identifiers_value_trigram "
        "ON item_identifiers USING gin (value_as_printed gin_trgm_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_item_identifiers_value_trigram")
    # Do not drop pg_trgm: extensions are database-wide and another schema may depend on it.
