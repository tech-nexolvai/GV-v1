"""Let `model_invocations` record a call that failed (#313).

Revision ID: 0015_model_invocation_failed
Revises: 0014_outbox

`0005_run_records` installed `model_invocation_outcome` with four values — `ok`, `rejected`, `timeout`,
`refused`. There was no `failed`, so a call that came back with no answer and was neither a timeout nor
a refusal could not be stored: PostgreSQL rejected the insert and the record of a paid attempt was
lost. This widens the constraint to five.

**Two shipped stories were relying on this.** `E2.3` (#251) states that every call is recorded,
including failures. `F5.3` (#266) attributes cost from these rows, and a failed call has already spent
its input tokens — so a spend figure missing them understates the bill in the direction that flatters
us. Both were untrue in a way nothing reported, because the failure mode was an insert nobody retried.

**The values are written out here rather than imported from `app.models.runs`.** A migration describes
one fixed historical state; if it read the live enum it would silently start describing a different
one every time the enum changed, and this file would no longer say what it actually did. `0005`
hardcoded its four for the same reason.

The cost of that correctness is that the enum and this list can drift — which is the bug this
migration exists to fix. `tests/db/test_run_models.py` therefore compares them directly, both by
reading the newest migration to define the constraint and by reading the constraint back out of a
migrated database. The existing tests could not catch it: they build tables from the ORM metadata,
whose constraint is generated from the enum, and Alembic's `compare_metadata` does not compare check
constraints at all.

Source: CodeRabbit review of PR #306; `E2.3` (#251), `F5.3` (#266).
Verification: `tests/db/test_run_models.py`.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0015_model_invocation_failed"
down_revision: str | None = "0014_outbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT = "model_invocation_outcome"
TABLE = "model_invocations"

#: The five outcomes after this migration. Written out, for the reason in the docstring.
MODEL_OUTCOMES = "'ok', 'rejected', 'timeout', 'refused', 'failed'"

#: The four before it, kept so `downgrade` restores exactly what `0005` installed rather than
#: something that merely resembles it.
MODEL_OUTCOMES_BEFORE = "'ok', 'rejected', 'timeout', 'refused'"


def upgrade() -> None:
    """Widen the outcome constraint to include `failed`.

    Dropped and recreated rather than altered: PostgreSQL has no `ALTER ... ALTER CONSTRAINT` for a
    check, and recreating it re-validates every existing row — which is what we want. Widening cannot
    fail on existing data, since every value already stored is still permitted.
    """
    op.drop_constraint(CONSTRAINT, TABLE, type_="check")
    op.create_check_constraint(CONSTRAINT, TABLE, f"outcome IN ({MODEL_OUTCOMES})")


def downgrade() -> None:
    """Narrow the constraint back to the four `0005` installed.

    **This will fail if any `failed` row exists, and that is deliberate.** Recreating a narrower check
    re-validates the table, so PostgreSQL refuses while a row violates it. The alternative — deleting
    those rows so the downgrade succeeds — would destroy the audit record of paid calls to make a
    schema change tidy, and `model_invocations` is append-only precisely so that cannot happen.
    An operator who really means it must decide what to do with the rows first.
    """
    op.drop_constraint(CONSTRAINT, TABLE, type_="check")
    op.create_check_constraint(CONSTRAINT, TABLE, f"outcome IN ({MODEL_OUTCOMES_BEFORE})")
