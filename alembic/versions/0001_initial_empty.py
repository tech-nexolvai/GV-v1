"""Establish the migration lineage before the first business table.

Revision ID: 0001_initial_empty
Revises: None
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "0001_initial_empty"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """The foundation revision intentionally creates no business tables."""


def downgrade() -> None:
    """The foundation revision intentionally drops no business tables."""
