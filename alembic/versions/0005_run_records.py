"""Workflow, task, extraction and model invocation records (#194).

Revision ID: 0005_run_records
Revises: 0004_document_aggregate
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005_run_records"
down_revision: str | None = "0004_document_aggregate"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MODEL_OUTCOMES = "'ok', 'rejected', 'timeout', 'refused'"


def upgrade() -> None:
    """Create the execution-record chain in dependency order."""

    op.create_table(
        "workflow_runs",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("package_revision_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("engine_run_id", sa.String(length=500), nullable=False),
        sa.CheckConstraint("engine_run_id <> ''", name="workflow_engine_run_id"),
        sa.ForeignKeyConstraint(
            ["package_revision_id"], ["package_revisions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("engine_run_id"),
    )
    op.create_index(
        "ix_workflow_runs_package_revision_id", "workflow_runs", ["package_revision_id"]
    )
    op.create_table(
        "task_runs",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("workflow_run_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.String(length=500), nullable=False),
        sa.Column("task_type", sa.String(length=200), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(length=100), nullable=False),
        sa.CheckConstraint("idempotency_key <> ''", name="task_run_idempotency_key"),
        sa.CheckConstraint("task_type <> ''", name="task_run_task_type"),
        sa.CheckConstraint("outcome <> ''", name="task_run_outcome"),
        sa.ForeignKeyConstraint(["workflow_run_id"], ["workflow_runs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index("ix_task_runs_workflow_run_id", "task_runs", ["workflow_run_id"])
    op.create_table(
        "extraction_runs",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("task_run_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("extractor", sa.String(length=200), nullable=False),
        sa.Column("extractor_version", sa.String(length=200), nullable=False),
        sa.Column("config_hash", sa.String(length=200), nullable=False),
        sa.CheckConstraint("extractor <> ''", name="extraction_run_extractor"),
        sa.CheckConstraint("extractor_version <> ''", name="extraction_run_extractor_version"),
        sa.CheckConstraint("config_hash <> ''", name="extraction_run_config_hash"),
        sa.ForeignKeyConstraint(["task_run_id"], ["task_runs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_extraction_runs_task_run_id", "extraction_runs", ["task_run_id"])
    op.create_table(
        "model_invocations",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("extraction_run_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("model_id", sa.String(length=300), nullable=False),
        sa.Column("prompt_id", sa.String(length=300), nullable=False),
        sa.Column("template_id", sa.String(length=300), nullable=False),
        sa.Column("crop_artifact_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_micros", sa.Integer(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.CheckConstraint("model_id <> ''", name="model_invocation_model_id"),
        sa.CheckConstraint("prompt_id <> ''", name="model_invocation_prompt_id"),
        sa.CheckConstraint("template_id <> ''", name="model_invocation_template_id"),
        sa.CheckConstraint("input_tokens >= 0", name="model_invocation_input_tokens"),
        sa.CheckConstraint("output_tokens >= 0", name="model_invocation_output_tokens"),
        sa.CheckConstraint("cost_micros >= 0", name="model_invocation_cost"),
        sa.CheckConstraint("latency_ms >= 0", name="model_invocation_latency"),
        sa.CheckConstraint(f"outcome IN ({MODEL_OUTCOMES})", name="model_invocation_outcome"),
        sa.ForeignKeyConstraint(["extraction_run_id"], ["extraction_runs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_model_invocations_extraction_run_id", "model_invocations", ["extraction_run_id"]
    )
    op.create_index("ix_model_invocations_created_at", "model_invocations", ["created_at"])


def downgrade() -> None:
    """Drop the execution-record chain in reverse dependency order."""

    op.drop_table("model_invocations")
    op.drop_table("extraction_runs")
    op.drop_table("task_runs")
    op.drop_table("workflow_runs")
