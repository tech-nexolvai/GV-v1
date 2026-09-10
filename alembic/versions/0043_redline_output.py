"""Permit evidence-grounded redline PDFs beside the other review outputs.

Revision ID: 0043_redline_output
Revises: 0042_semantic_typing_gate

``redline`` is added only with the output path that reaches it through a stored finding, linked
typed canonical evidence and a recorded page transform.  The database may name no artifact the
pipeline cannot honestly produce.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0043_redline_output"
down_revision: str | None = "0042_semantic_typing_gate"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_WITHOUT_REDLINE = "'findings_pdf', 'findings_workbook'"
_WITH_REDLINE = "'redline', " + _WITHOUT_REDLINE


def _replace_constraint(values: str) -> None:
    op.drop_constraint("output_artifact_kind", "output_artifacts", type_="check")
    op.create_check_constraint("output_artifact_kind", "output_artifacts", f"kind IN ({values})")


def upgrade() -> None:
    _replace_constraint(_WITH_REDLINE)


def downgrade() -> None:
    _replace_constraint(_WITHOUT_REDLINE)
