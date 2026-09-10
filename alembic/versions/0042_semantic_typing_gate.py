"""Record exact mechanical semantic-tag qualification distinctly from review action.

Revision ID: 0042_semantic_typing_gate
Revises: 0041_findings_pdf_output

An automatic semantic type is allowed only when an exact vector vocabulary tag and numeric reading
share a resolved dimension line.  The existing evidence provenance model stores both candidates and
already requires two supporting rows for `CORROBORATED`; this migration adds the named lane that says
*why* that corroboration exists and the audit category that distinguishes it from a human review.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0042_semantic_typing_gate"
down_revision: str | None = "0041_findings_pdf_output"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_LANES = "'SECOND_READER', 'DUAL_UNIT', 'HUMAN'"
_NEW_LANES = "'MECHANICAL_TAG', " + _OLD_LANES
_OLD_CATEGORIES = (
    "'STATE_CHANGE', 'RULE_PUBLICATION', 'FINDING', 'REVIEW_ACTION', 'EXCEPTION', "
    "'ARTIFACT_DOWNLOAD', 'ARTIFACT_DELETION'"
)
_NEW_CATEGORIES = "'EVIDENCE_QUALIFICATION', " + _OLD_CATEGORIES


def _replace(table: str, constraint: str, expression: str) -> None:
    op.drop_constraint(constraint, table, type_="check")
    op.create_check_constraint(constraint, table, expression)


def upgrade() -> None:
    _replace(
        "evidence_corroboration_lanes",
        "evidence_corroboration_lane",
        f"lane IN ({_NEW_LANES})",
    )
    _replace("audit_events", "audit_events_category", f"category IN ({_NEW_CATEGORIES})")


def downgrade() -> None:
    _replace(
        "evidence_corroboration_lanes",
        "evidence_corroboration_lane",
        f"lane IN ({_OLD_LANES})",
    )
    _replace("audit_events", "audit_events_category", f"category IN ({_OLD_CATEGORIES})")
