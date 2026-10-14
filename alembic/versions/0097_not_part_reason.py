"""Keep why a person said a suggested piece is not a part (#1246, with phase 5 decision 6).

Revision ID: 0097_not_part_reason
Revises: 0096_reading_events

**Why.** On one of Bala's pieces the Measure page asks "what is it?" and offers "Appliance space" and
"Something else" beside "Cabinet" and "Filler". Both stored the same bare `withdrawn` decision, so
after a reload the piece only said "Not a part" and the person's actual answer was gone. An
appliance space will matter later, for the cabinet and filler width sums. Phase 5 decision 6 adds a
third answer, "Side panel" (the end panels in the client's sink cabinet formula), which needs the
same place to be stored.

**`part_confirmations.not_part_reason`**, one of `appliance-space`, `side-panel` or `other`:

* allowed **only on a `withdrawn` decision**: a confirmation says what a part is, not why it is not
  one;
* **never on code's decision**: code takes back its own countertop only because a later read no
  longer agrees, and it cannot know what the piece is (`confirmed_by` starting `code:`);
* **nullable**: every decision before this keeps no reason, and none is guessed. A withdrawal
  through "Not a part" or Undo keeps none either.

Nothing reads it to decide anything: it is the person's answer, shown back to them.

`part_confirmations` stays append-only (0058): adding a nullable column fires no row trigger, and
the roles' grants are on the table, so they cover the new column with no new `GRANT`. **A downgrade
with a reason stored refuses**, like 0085 and 0092: the reasons are part of the review's record, and
the table's trigger forbids clearing them.

The orchestrator approved this migration for #1246 (phase 5, R4).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0097_not_part_reason"
down_revision: str | None = "0096_reading_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "part_confirmations"
COLUMN = "not_part_reason"
CONSTRAINT = "part_confirmation_not_part_reason"

#: Written out, not imported from the model: a migration describes one fixed state.
#: `tests/db/test_not_part_reason_migration.py` compares them with `NotPartReason`.
REASONS = "'appliance-space', 'side-panel', 'other'"

REASON_CHECK = (
    f"{COLUMN} IS NULL OR (decision = 'withdrawn' AND confirmed_by NOT LIKE 'code:%' "
    f"AND {COLUMN} IN ({REASONS}))"
)


def upgrade() -> None:
    """Add the nullable reason and its CHECK. Existing decisions stay NULL."""
    op.add_column(TABLE, sa.Column(COLUMN, sa.String(length=24), nullable=True))
    op.create_check_constraint(CONSTRAINT, TABLE, REASON_CHECK)


def downgrade() -> None:
    """Drop the CHECK and the column, refused while any decision keeps a reason."""
    stored = op.get_bind().scalar(
        sa.text(f"SELECT count(*) FROM {TABLE} WHERE {COLUMN} IS NOT NULL")
    )
    if stored:
        raise RuntimeError(
            f"Cannot downgrade the not-a-part reasons: {stored} decision(s) keep why a person said "
            "a piece is not a part, and part_confirmations is append-only. Preserve these records."
        )
    op.drop_constraint(CONSTRAINT, TABLE, type_="check")
    op.drop_column(TABLE, COLUMN)
