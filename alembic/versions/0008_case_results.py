"""Per-case evaluation results, so a regression names the cases (#315).

Revision ID: 0008_case_results
Revises: 0007_rule_plane

`metric_results` records that a rate moved. This records which cases moved, which is what a person
actually needs: 1% to 4% on fifty cases is three cases, and the useful question is always *which*
three.

`check` is a reserved SQL keyword and is quoted throughout. Unquoted it is a syntax error rather
than a column, and the failure arrives at migration time on whichever machine runs it first.

No finding reference. `#315`'s scope asks for the finding behind each result, and no findings table
exists — that is `#199`, deferred behind `#195` and `#198`. A column pointing at nothing would be
worse than its absence; a later migration adds it once there is something to point at.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008_case_results"
down_revision: str | None = "0007_rule_plane"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _identity_columns() -> tuple[sa.Column[object], sa.Column[object]]:
    return (
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def upgrade() -> None:
    op.create_table(
        "case_results",
        *_identity_columns(),
        sa.Column("evaluation_run_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("gold_case_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("check", sa.String(length=200), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("expected", sa.String(length=32), nullable=False),
        sa.CheckConstraint("\"check\" <> ''", name="case_result_check_present"),
        sa.CheckConstraint("outcome <> ''", name="case_result_outcome_present"),
        sa.CheckConstraint("expected <> ''", name="case_result_expected_present"),
        sa.ForeignKeyConstraint(
            ["evaluation_run_id"],
            ["evaluation_runs.id"],
            name="fk_case_results_evaluation_run_id_evaluation_runs",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["gold_case_id"],
            ["gold_cases.id"],
            name="fk_case_results_gold_case_id_gold_cases",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_case_results"),
        sa.UniqueConstraint(
            "evaluation_run_id", "gold_case_id", "check", name="uq_case_results_run_case_check"
        ),
    )
    op.create_index("ix_case_results_check", "case_results", ["check"])
    op.create_index("ix_case_results_evaluation_run_id", "case_results", ["evaluation_run_id"])
    op.create_index("ix_case_results_gold_case_id", "case_results", ["gold_case_id"])
    op.create_index("ix_case_results_run_check", "case_results", ["evaluation_run_id", "check"])


def downgrade() -> None:
    op.drop_table("case_results")
