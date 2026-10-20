"""The reviewer's Agree can say what the value should be (#1803).

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

**Downgrade** drops the column. It refuses while a decision row holds a value, because those are
the reviewer's own words and would be lost.

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


def upgrade() -> None:
    op.add_column(DECISIONS, sa.Column("should_be", sa.Text(), nullable=True))
    op.create_check_constraint(NAME, DECISIONS, CHECK)


def downgrade() -> None:
    count = op.get_bind().scalar(
        sa.text(f"SELECT count(*) FROM {DECISIONS} WHERE should_be IS NOT NULL")
    )
    if count:
        raise RuntimeError(
            f"Cannot downgrade: {count} decision row(s) hold the value a reviewer gave. "
            "Preserve these records."
        )
    op.drop_constraint(NAME, DECISIONS, type_="check")
    op.drop_column(DECISIONS, "should_be")
