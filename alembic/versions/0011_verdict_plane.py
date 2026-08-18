"""Check runs, sealed operands, findings and their evidence (#199).

Revision ID: 0011_verdict_plane
Revises: 0010_matching_plane

This is the plane a client or an auditor is shown. `verdict_inputs` keeps every operand that entered
the arithmetic as an exact rational, so a finding can be recomputed years later and compared — a
finding that cannot be reproduced from its own stored inputs is an audit trail in name only.

Two constraints carry the safety argument. `verdict_input_status_qualified` admits only the two
evidence statuses `verdict/operands.py` lets into a verdict; a `RAW_CANDIDATE` here would be one
unverified extraction with the weight of corroborated evidence. `verdict_input_denominator_positive`
keeps the rational well formed, because a zero denominator is not a number.

The two observation foreign keys are named explicitly. Their generated names run past PostgreSQL's
63-character limit and acquire a hash suffix, which a hand-written migration then has to guess —
#198 lost four CI rounds to exactly that.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0011_verdict_plane"
down_revision: str | None = "0010_matching_plane"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Spelled out rather than imported: a migration keeps saying what it said the day it ran, and
# tests/db/test_verdict_models.py asserts these still match the live enums.
_OUTCOMES = "'PASS', 'FAIL', 'NOT_FOUND', 'REVIEW_REQUIRED', 'NO_APPLICABLE_RULE'"
_SEVERITIES = "'CRITICAL', 'MAJOR', 'MINOR', 'ADVISORY'"
_UNITS = "'mm', 'in'"
_QUALIFIED = "'CORROBORATED', 'HUMAN_CONFIRMED'"


def _identity_columns() -> tuple[sa.Column[object], sa.Column[object]]:
    return (
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def upgrade() -> None:
    op.create_table(
        "check_runs",
        *_identity_columns(),
        sa.Column("package_revision_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("rule_snapshot_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("engine_version", sa.String(length=64), nullable=False),
        sa.CheckConstraint("engine_version <> ''", name="check_run_engine_version_present"),
        sa.ForeignKeyConstraint(
            ["package_revision_id"],
            ["package_revisions.id"],
            name="fk_check_runs_package_revision_id_package_revisions",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["rule_snapshot_id"],
            ["rule_snapshots.id"],
            name="fk_check_runs_rule_snapshot_id_rule_snapshots",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_check_runs"),
        # Lets a child carry the revision and have the database prove it is this run's own.
        sa.UniqueConstraint("id", "package_revision_id", name="uq_check_runs_id_revision"),
    )
    op.create_index("ix_check_runs_package_revision_id", "check_runs", ["package_revision_id"])
    op.create_index(
        "ix_check_runs_revision_snapshot", "check_runs", ["package_revision_id", "rule_snapshot_id"]
    )
    op.create_index("ix_check_runs_rule_snapshot_id", "check_runs", ["rule_snapshot_id"])

    op.create_table(
        "verdict_inputs",
        *_identity_columns(),
        sa.Column("check_run_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("operand_name", sa.String(length=100), nullable=False),
        sa.Column("value_numerator", sa.Integer(), nullable=False),
        sa.Column("value_denominator", sa.Integer(), nullable=False),
        sa.Column("unit", sa.String(length=16), nullable=False),
        sa.Column("evidence_status", sa.String(length=32), nullable=False),
        sa.Column("canonical_observation_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.CheckConstraint("operand_name <> ''", name="verdict_input_operand_name_present"),
        sa.CheckConstraint("value_denominator > 0", name="verdict_input_denominator_positive"),
        sa.CheckConstraint(f"unit IN ({_UNITS})", name="verdict_input_unit"),
        sa.CheckConstraint(
            f"evidence_status IN ({_QUALIFIED})", name="verdict_input_status_qualified"
        ),
        sa.ForeignKeyConstraint(
            ["check_run_id"],
            ["check_runs.id"],
            name="fk_verdict_inputs_check_run_id_check_runs",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["canonical_observation_id"],
            ["canonical_observations.id"],
            name="fk_verdict_inputs_observation",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_verdict_inputs"),
        sa.UniqueConstraint("check_run_id", "operand_name", name="uq_verdict_inputs_run_operand"),
    )
    op.create_index(
        "ix_verdict_inputs_canonical_observation_id", "verdict_inputs", ["canonical_observation_id"]
    )
    op.create_index("ix_verdict_inputs_check_run_id", "verdict_inputs", ["check_run_id"])

    op.create_table(
        "findings",
        *_identity_columns(),
        sa.Column("check_run_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("package_revision_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("trace", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "parameter_set_versions", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.CheckConstraint(f"outcome IN ({_OUTCOMES})", name="finding_outcome"),
        sa.CheckConstraint(f"severity IN ({_SEVERITIES})", name="finding_severity"),
        # Composite, so a finding cannot claim a revision its own run does not have.
        sa.ForeignKeyConstraint(
            ["check_run_id", "package_revision_id"],
            ["check_runs.id", "check_runs.package_revision_id"],
            name="fk_findings_run_revision",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_findings"),
        sa.UniqueConstraint("id", "package_revision_id", name="uq_findings_id_revision"),
    )
    op.create_index("ix_findings_check_run_id", "findings", ["check_run_id"], unique=True)
    op.create_index("ix_findings_outcome", "findings", ["outcome"])
    op.create_index("ix_findings_outcome_severity", "findings", ["outcome", "severity"])
    op.create_index("ix_findings_package_revision_id", "findings", ["package_revision_id"])

    op.create_table(
        "finding_evidence",
        *_identity_columns(),
        sa.Column("finding_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("canonical_observation_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(length=50), nullable=False),
        sa.CheckConstraint("role <> ''", name="finding_evidence_role_present"),
        sa.ForeignKeyConstraint(
            ["finding_id"],
            ["findings.id"],
            name="fk_finding_evidence_finding_id_findings",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["canonical_observation_id"],
            ["canonical_observations.id"],
            name="fk_finding_evidence_observation",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_finding_evidence"),
        sa.UniqueConstraint(
            "finding_id", "canonical_observation_id", "role", name="uq_finding_evidence_link"
        ),
    )
    op.create_index(
        "ix_finding_evidence_canonical_observation_id",
        "finding_evidence",
        ["canonical_observation_id"],
    )
    op.create_index("ix_finding_evidence_finding_id", "finding_evidence", ["finding_id"])


def downgrade() -> None:
    op.drop_table("finding_evidence")
    op.drop_table("findings")
    op.drop_table("verdict_inputs")
    op.drop_table("check_runs")
