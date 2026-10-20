"""The reviewer's Agree can say what the value should be, and a call can be taken back (#1803).

Revision ID: 0112_finding_decision_should_be
Revises: 0111_package_product_registry

**Why.** A Not-compared item can be made Wrong by the reviewer ("It's a mistake: add a red mark").
The vendor is only ever told an item is wrong together with what it should be, and for many
Not-compared items the architect prints no value (that is often why code could not compare it).
Without a place for the reviewer's value, the Agree did nothing on those items.

**What.** One nullable column, `assistant_finding_decisions.should_be`: the value as the reviewer
typed it, kept with the decision row (the table stays append-only; a changed mind is a new row).
A CHECK keeps it to an Agree and never empty. Existing rows keep NULL: their green "should be"
stays what code knows (the architect's value), as before.

A third decision value, `undo`: the reviewer takes a call back ("Undo"), and the item is again
what code found. Kept as one more row like any decision (the table stays append-only), so the
history of what the reviewer clicked is never lost. The value CHECK is replaced to allow it.

One more nullable column, `assistant_chat_files.decisions_held`: for a file made again from the
reviewer's decisions, how many decision rows of its answer it was made from. The send-ready
package is out of date exactly when the answer has more (the decisions table is append-only, so
a count never goes back), and the files keep their own creation time for which is newest.

**Downgrade** drops the columns and puts the two-value CHECK back. It refuses while a decision row
holds a value or is an undo, because those are the reviewer's own words and calls and would be
lost.

Verification: `tests/db/test_finding_decision_should_be.py`.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0112_finding_decision_should_be"
down_revision: str | None = "0111_package_product_registry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DECISIONS = "assistant_finding_decisions"
NAME = "assistant_finding_decision_should_be"
CHECK = "should_be IS NULL OR (decision = 'agree' AND should_be <> '')"
VALUE = "assistant_finding_decision_value"
VALUES_BEFORE = "decision IN ('agree', 'not_a_problem')"
VALUES_AFTER = "decision IN ('agree', 'not_a_problem', 'undo')"
FILES = "assistant_chat_files"
HELD = "assistant_chat_file_decisions_held"


def upgrade() -> None:
    op.add_column(DECISIONS, sa.Column("should_be", sa.Text(), nullable=True))
    op.create_check_constraint(NAME, DECISIONS, CHECK)
    op.drop_constraint(VALUE, DECISIONS, type_="check")
    op.create_check_constraint(VALUE, DECISIONS, VALUES_AFTER)
    op.add_column(FILES, sa.Column("decisions_held", sa.Integer(), nullable=True))
    op.create_check_constraint(HELD, FILES, "decisions_held IS NULL OR decisions_held >= 0")


def downgrade() -> None:
    count = op.get_bind().scalar(
        sa.text(
            f"SELECT count(*) FROM {DECISIONS} WHERE should_be IS NOT NULL OR decision = 'undo'"
        )
    )
    if count:
        raise RuntimeError(
            f"Cannot downgrade: {count} decision row(s) hold a value a reviewer gave or an undo. "
            "Preserve these records."
        )
    op.drop_constraint(HELD, FILES, type_="check")
    op.drop_column(FILES, "decisions_held")
    op.drop_constraint(VALUE, DECISIONS, type_="check")
    op.create_check_constraint(VALUE, DECISIONS, VALUES_BEFORE)
    op.drop_constraint(NAME, DECISIONS, type_="check")
    op.drop_column(DECISIONS, "should_be")
