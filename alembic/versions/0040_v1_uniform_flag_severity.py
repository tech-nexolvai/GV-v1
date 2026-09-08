"""Add V1's unsplit reviewer flag to the finding severity contract.

Revision ID: 0040_v1_uniform_flag_severity
Revises: 0039_observation_associations

CLIENT_FACTS Q4 records the client's V1 decision: every mismatch is a reviewer flag and no rule
declares a criticality tier. ``FLAG`` is deliberately not ``ADVISORY``: advisory means a warning
that never fails, while a V1 flag retains the deterministic PASS/FAIL calculation and asks the
reviewer to decide its disposition.

The original constraint stays historically accurate in 0011. This migration widens the live
contract instead of editing a shipped migration, so databases with historical CRITICAL findings
remain readable.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0040_v1_uniform_flag_severity"
down_revision: str | None = "0039_observation_associations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_SEVERITIES = "'CRITICAL', 'MAJOR', 'MINOR', 'ADVISORY'"
_V1_SEVERITIES = "'FLAG', " + _OLD_SEVERITIES


def _replace_constraint(values: str) -> None:
    op.drop_constraint("finding_severity", "findings", type_="check")
    op.create_check_constraint("finding_severity", "findings", f"severity IN ({values})")


def upgrade() -> None:
    _replace_constraint(_V1_SEVERITIES)


def downgrade() -> None:
    _replace_constraint(_OLD_SEVERITIES)
