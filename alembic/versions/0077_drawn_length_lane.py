"""Name the lane that qualifies the architect's printed value: its drawn-length witness (#1054).

Revision ID: 0077_drawn_length_lane
Revises: 0076_architect_pairing_records

The architect's dimensions on the client's combined sheets are real text, read exactly by code
(#1052), and kept only when the length drawn between their ticks, through the drawing's scale,
agrees. That is one exact reading plus a non-model witness, and it is recorded as the
`DRAWN_LENGTH` corroboration lane so the reason a value was qualified is stored beside it. The
evidence model allows it for the architect's side only (`evidence/canonical.py`, `evidence/gate.py`).

Only the canonical observation's lane table is widened: the lane is never written on a candidate,
and the candidate constraint is left exactly as migration 0036 wrote it. Nothing is backfilled.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0077_drawn_length_lane"
down_revision: str | None = "0076_architect_pairing_records"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_LANES = "'SECOND_READER', 'DUAL_UNIT', 'HUMAN', 'MECHANICAL_TAG'"
_NEW_LANES = _OLD_LANES + ", 'DRAWN_LENGTH'"


def _replace(table: str, constraint: str, expression: str) -> None:
    op.drop_constraint(constraint, table, type_="check")
    op.create_check_constraint(constraint, table, expression)


def _apply(lanes: str) -> None:
    _replace(
        "evidence_corroboration_lanes",
        "evidence_corroboration_lane",
        f"lane IN ({lanes})",
    )


def upgrade() -> None:
    _apply(_NEW_LANES)


def downgrade() -> None:
    """Refused by the database if any row already carries the lane, which is the right answer."""
    _apply(_OLD_LANES)
