"""Let `model_invocations` say why a reader call was refused before it was served (#1206).

Revision ID: 0093_model_invocation_refusals
Revises: 0092_no_countertop_batches

Two causes were recorded as a bare `failed`, so the Usage page and the run's own records could not
tell them from a provider error:

* `budget_refused`: the drawing set's AI budget would have been exceeded, so the question was not
  sent (`SpendCapExceeded`). Since #1206 the reading stops cleanly at the first one and lists the
  pages left unread.
* `account_refused`: the provider refused the account (HTTP 402, out of credit). The reading stops
  and the package waits, retryable, for a top-up.

Neither made an answer or cost anything. This widens the `model_invocation_outcome` constraint from
five values to seven. The values are written out, as `0005` and `0015` do, because a migration
describes one fixed state; `tests/db/test_run_models.py` compares them with the enum.

Widening cannot fail on existing data. A downgrade with either value stored refuses, as `0015`'s
does: the rows are the audit record of attempted calls and are never deleted to make a schema tidy.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0093_model_invocation_refusals"
down_revision: str | None = "0092_no_countertop_batches"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT = "model_invocation_outcome"
TABLE = "model_invocations"

#: The seven outcomes after this migration. Written out, for the reason in the docstring.
MODEL_OUTCOMES = (
    "'ok', 'rejected', 'timeout', 'refused', 'failed', 'budget_refused', 'account_refused'"
)

#: The five before it, as `0015` installed them.
MODEL_OUTCOMES_BEFORE = "'ok', 'rejected', 'timeout', 'refused', 'failed'"


def upgrade() -> None:
    """Widen the outcome constraint to include the two refusals."""
    op.drop_constraint(CONSTRAINT, TABLE, type_="check")
    op.create_check_constraint(CONSTRAINT, TABLE, f"outcome IN ({MODEL_OUTCOMES})")


def downgrade() -> None:
    """Narrow it back to the five `0015` installed; refused while a row uses either new value."""
    op.drop_constraint(CONSTRAINT, TABLE, type_="check")
    op.create_check_constraint(CONSTRAINT, TABLE, f"outcome IN ({MODEL_OUTCOMES_BEFORE})")
