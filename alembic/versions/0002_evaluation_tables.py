"""Gold sets, gold cases, evaluation runs and metric results (#201).

Revision ID: 0002_evaluation_tables
Revises: 0001_initial_empty
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0002_evaluation_tables"
down_revision: str | None = "0001_initial_empty"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the evaluation-history tables."""

    op.create_table(
        "gold_sets",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("notes", sa.String(length=1000), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", "version"),
    )

    op.create_table(
        "gold_cases",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("gold_set_id", sa.Uuid(as_uuid=True), nullable=False),
        # No foreign key: document_versions arrives with C1.3 (#193). The constraint must be added
        # in a NEW migration when that lands — never by editing this one (CLAUDE.md).
        sa.Column("document_version_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("annotations", postgresql.JSONB(), nullable=False),
        sa.Column("annotated_by", sa.String(length=200), nullable=False),
        sa.Column("annotated_on", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["gold_set_id"], ["gold_sets.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("gold_set_id", "document_version_id"),
    )
    op.create_index("ix_gold_cases_gold_set_id", "gold_cases", ["gold_set_id"])

    op.create_table(
        "evaluation_runs",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("gold_set_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("gold_set_version", sa.String(length=32), nullable=False),
        sa.Column("code_version", sa.String(length=64), nullable=False),
        sa.Column("rule_snapshot_ids", postgresql.JSONB(), nullable=False),
        sa.Column("extractor_versions", postgresql.JSONB(), nullable=False),
        sa.Column("is_baseline", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["gold_set_id"], ["gold_sets.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_evaluation_runs_created_at", "evaluation_runs", ["created_at"])

    op.create_table(
        "metric_results",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evaluation_run_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("metric", sa.String(length=64), nullable=False),
        sa.Column("check_type", sa.String(length=32), nullable=False),
        # NUMERIC, never DOUBLE PRECISION — a release decision is made from this column.
        # Nullable because NOT MEASURED and zero are different facts.
        sa.Column("value", sa.Numeric(precision=18, scale=9), nullable=True),
        sa.Column("numerator", sa.Integer(), nullable=False),
        sa.Column("denominator", sa.Integer(), nullable=False),
        sa.Column("gate_threshold", sa.Numeric(precision=18, scale=9), nullable=True),
        sa.Column("passed", sa.Boolean(), nullable=True),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.ForeignKeyConstraint(["evaluation_run_id"], ["evaluation_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("evaluation_run_id", "metric", "check_type"),
    )
    op.create_index(
        "ix_metric_results_metric_check_type", "metric_results", ["metric", "check_type"]
    )


def downgrade() -> None:
    """Drop them in dependency order."""

    op.drop_index("ix_metric_results_metric_check_type", table_name="metric_results")
    op.drop_table("metric_results")
    op.drop_index("ix_evaluation_runs_created_at", table_name="evaluation_runs")
    op.drop_table("evaluation_runs")
    op.drop_index("ix_gold_cases_gold_set_id", table_name="gold_cases")
    op.drop_table("gold_cases")
    op.drop_table("gold_sets")
