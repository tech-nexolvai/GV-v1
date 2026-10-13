"""Let a model call that failed or was never sent keep why (#1215).

Revision ID: 0094_invocation_failure_reason
Revises: 0093_model_invocation_refusals

`model_invocations.rejection_reason` was allowed only on a `rejected` row (`0051`). Two causes were
therefore lost on a client-set run:

* a reader question refused before it was sent, because the close-up was wider than the readers'
  picture limit (`PictureWouldBeResized`), was stored as a bare `failed`;
* the findings wording call failed outright, and its error reached no column at all.

This relaxes the `model_invocation_rejection_reason` check from "only on `rejected`" to "on any
outcome except `ok`", still never empty. The constraint keeps its name. Rows already stored all
satisfy the new check, so the upgrade cannot fail on existing data.

**The downgrade clears the new reasons, and that is the one place this migration writes rows.** The
old check cannot be restored while a non-`rejected` row holds a reason, and refusing the downgrade
would leave a database that cannot go back at all. So the downgrade sets `rejection_reason` to NULL
on every row whose outcome is not `rejected` (the outcome, tokens, cost and every other column are
kept), then restores the old check. `model_invocations` is append-only: its `append_only` trigger is
disabled for that one `UPDATE` only and enabled again in the same transaction. The cleared text was
diagnostic only; nothing counts or prices from it.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0094_invocation_failure_reason"
down_revision: str | None = "0093_model_invocation_refusals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT = "model_invocation_rejection_reason"
TABLE = "model_invocations"
TRIGGER = "model_invocations_append_only"

#: After this migration: a reason on any outcome except `ok`, never empty.
REASON_CHECK = "rejection_reason IS NULL OR (outcome <> 'ok' AND rejection_reason <> '')"

#: Before it, as `0051` installed it: a reason only on `rejected`.
REASON_CHECK_BEFORE = (
    "rejection_reason IS NULL OR (outcome = 'rejected' AND rejection_reason <> '')"
)


def upgrade() -> None:
    """Allow a reason on every outcome except `ok`."""
    op.drop_constraint(CONSTRAINT, TABLE, type_="check")
    op.create_check_constraint(CONSTRAINT, TABLE, REASON_CHECK)


def downgrade() -> None:
    """Clear the reasons the old check forbids, then restore it (see the module docstring)."""
    op.drop_constraint(CONSTRAINT, TABLE, type_="check")
    op.execute(f"ALTER TABLE {TABLE} DISABLE TRIGGER {TRIGGER}")
    op.execute(
        f"UPDATE {TABLE} SET rejection_reason = NULL "
        "WHERE rejection_reason IS NOT NULL AND outcome <> 'rejected'"
    )
    op.execute(f"ALTER TABLE {TABLE} ENABLE TRIGGER {TRIGGER}")
    op.create_check_constraint(CONSTRAINT, TABLE, REASON_CHECK_BEFORE)
