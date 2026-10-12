"""Let a sheet's heading suggest a reference drawing (a brand prototype) (#1193).

Revision ID: 0091_reference_view_suggestion
Revises: 0090_no_countertop_answers

GV's combined sheets also carry `BRAND PROTOTYPE (ELEVATION)` drawings: a brand standard, neither the
vendor's nor the architect's, never read for a countertop and never compared. The heading's
suggestion for such a drawing is `reference`. Only `view_role_proposals` changes: a confirmed role
(`drawing_views.role`, `view_role_confirmations.role`) stays one of the two sides. Nothing stored
changes.

Downgrade puts the two-role CHECK back, and so refuses while any suggestion says `reference`: a
recorded suggestion is never dropped silently (the table is append-only).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0091_reference_view_suggestion"
down_revision: str | None = "0090_no_countertop_answers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "view_role_proposals"
_CONSTRAINT = "view_role_proposal_role"
_OLD = "'arch', 'shop'"
_NEW = "'arch', 'shop', 'reference'"


def _replace(roles: str) -> None:
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(
        _CONSTRAINT, _TABLE, f"proposed_role IS NULL OR proposed_role IN ({roles})"
    )


def upgrade() -> None:
    _replace(_NEW)


def downgrade() -> None:
    _replace(_OLD)
