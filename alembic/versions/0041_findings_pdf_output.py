"""Permit the reviewer-readable findings PDF beside the audit workbook.

Revision ID: 0041_findings_pdf_output
Revises: 0040_v1_uniform_flag_severity

The output table names files the pipeline can actually produce.  This migration widens its kind
constraint only after ``reports/findings_pdf.py`` and ``DatabaseStages.generate_outputs`` gained
the deterministic renderer and writer.  `redline` remains intentionally excluded: rendering a PDF
does not make it an evidence-placed markup.

Source: docs/DEMO_PLAN.md. Verification: tests/db/test_output_artifacts.py.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0041_findings_pdf_output"
down_revision: str | None = "0040_v1_uniform_flag_severity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_WORKBOOK = "'findings_workbook'"
_WITH_PDF = "'findings_pdf', " + _WORKBOOK


def _replace_constraint(values: str) -> None:
    op.drop_constraint("output_artifact_kind", "output_artifacts", type_="check")
    op.create_check_constraint("output_artifact_kind", "output_artifacts", f"kind IN ({values})")


def upgrade() -> None:
    _replace_constraint(_WITH_PDF)


def downgrade() -> None:
    _replace_constraint(_WORKBOOK)
