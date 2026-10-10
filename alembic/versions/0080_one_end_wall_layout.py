"""Accept the wall layouts "back wall and left end" / "... right end" on wall choices (#1138).

Revision ID: 0080_one_end_wall_layout
Revises: 0079_signed_export_retries

CT-WIDTH-001 1.2.0 adds the layouts `back_and_left` and `back_and_right`: a countertop with a wall
at one end only takes one field cut (the client lead: 1 inch per wall end); the side says where the picture
draws it. Both tables that record a person's wall choice listed the three older layouts in a CHECK;
each now lists five. Nothing stored changes.

Downgrade puts the three-layout CHECK back, and so refuses while any row holds a new layout:
a choice a person made is never dropped silently.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0080_one_end_wall_layout"
down_revision: str | None = "0079_signed_export_retries"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = "'back_left_right', 'back_only', 'island'"
_NEW = "'back_left_right', 'back_and_left', 'back_and_right', 'back_only', 'island'"


def _replace(layouts: str) -> None:
    op.drop_constraint("countertop_run_wall_config", "countertop_run_decisions", type_="check")
    op.create_check_constraint(
        "countertop_run_wall_config",
        "countertop_run_decisions",
        f"wall_config IS NULL OR (decision = 'confirmed' AND wall_config IN ({layouts}))",
    )
    op.drop_constraint("slot_row_review_wall_config", "slot_row_review_decisions", type_="check")
    op.create_check_constraint(
        "slot_row_review_wall_config",
        "slot_row_review_decisions",
        f"wall_config IS NULL OR wall_config IN ({layouts})",
    )


def upgrade() -> None:
    _replace(_NEW)


def downgrade() -> None:
    _replace(_OLD)
