"""Let a model call's cost be unknown rather than zero (#700).

Revision ID: 0053_invocation_cost_unknown
Revises: 0052_view_role_proposals

`model_invocations.cost_micros` was `NOT NULL`, and both writers filled it with a literal `0`, so
every call on record says it cost nothing and `app/budget/attribution.py` reported a free system.
From now on the cost comes from the deployment's stated price file (`app/runs/rates.py`), and a model
with no stated price records `NULL` — unknown — which the attribution counts separately instead of
adding as zero.

**Rows already written keep their `0`.** The table is append-only and its trigger refuses updates,
so they cannot be corrected in place; they predate this change, and a report over that window is a
lower bound. That is recorded here rather than papered over with a backfill the table forbids.

Source: issue #700. Verification: tests/app/test_model_rates.py.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0053_invocation_cost_unknown"
down_revision: str | None = "0052_view_role_proposals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("model_invocations", "cost_micros", existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    # Refused by PostgreSQL while any cost is unknown — the right answer: a downgrade cannot invent a
    # cost for an unknown one, and must not record it as free.
    op.alter_column("model_invocations", "cost_micros", existing_type=sa.Integer(), nullable=False)
