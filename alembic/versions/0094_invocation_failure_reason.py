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

**The downgrade refuses rather than rewrites**, as `0049` and `0093` do. The old check cannot be
restored while a non-`rejected` row holds a reason, and `model_invocations` is append-only: clearing
those reasons would rewrite the audit record of calls that really happened. So the downgrade counts
them and, if there are any, stops with a `RuntimeError` naming how many and what an operator must
decide first. With none, it restores the old check.

The orchestrator approved this migration as an exception to the "no migrations" rule of the
2026-10-13 reading-fixes build, for #1215 only.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0094_invocation_failure_reason"
down_revision: str | None = "0093_model_invocation_refusals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT = "model_invocation_rejection_reason"
TABLE = "model_invocations"

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
    """Restore the old check, refused while a non-`rejected` row names a cause (see the docstring)."""
    bind = op.get_bind()
    kept = bind.execute(
        sa.text(
            f"SELECT count(*) FROM {TABLE} "
            "WHERE rejection_reason IS NOT NULL AND outcome <> 'rejected'"
        )
    ).scalar_one()
    if kept:
        raise RuntimeError(
            f"{kept} model_invocations row(s) record why a call failed or was never sent (#1215). "
            "The check this downgrade restores allows a reason only on a rejected row, and this "
            "table is append-only, so those reasons cannot be cleared or the rows deleted here. "
            "Decide what to do with those rows first (keep this revision, or move the records "
            "elsewhere deliberately), then downgrade."
        )
    op.drop_constraint(CONSTRAINT, TABLE, type_="check")
    op.create_check_constraint(CONSTRAINT, TABLE, REASON_CHECK_BEFORE)
