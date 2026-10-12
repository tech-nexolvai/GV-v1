"""A note and a batch on the reviewer's answers about pages not checked (#1195).

Revision ID: 0092_no_countertop_batches
Revises: 0091_reference_view_suggestion

"Confirm these N pages" answers every ticked page listed with no countertop in one action. Each page
still gets its own answer row, exactly like a single answer (who and when), plus two optional
columns on `no_countertop_answers`:

* `note`: the reviewer's optional note (single answers may carry one too); never blank when given;
* `batch_id`: the id of the one action the answer was given in, also the target of that action's
  own audit event, so the audit trail shows one event per page and one for the batch.

Nothing stored changes: existing answers get no note and no batch. The table stays append-only (the
trigger refuses UPDATE and DELETE, not ALTER TABLE). A downgrade with a note or a batch stored
refuses: they are part of the review's record.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0092_no_countertop_batches"
down_revision: str | None = "0091_reference_view_suggestion"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "no_countertop_answers"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("note", sa.String(length=500), nullable=True))
    op.add_column(_TABLE, sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_check_constraint(
        "no_countertop_answer_note_not_blank", _TABLE, "note IS NULL OR note !~ '^[[:space:]]*$'"
    )
    op.create_index("ix_no_countertop_answers_batch_id", _TABLE, ["batch_id"])


def downgrade() -> None:
    stored = op.get_bind().scalar(
        sa.text(f"SELECT count(*) FROM {_TABLE} WHERE note IS NOT NULL OR batch_id IS NOT NULL")
    )
    if stored:
        raise RuntimeError(
            f"Cannot downgrade the no-countertop answers: {stored} answer(s) carry a reviewer's "
            "note or a batch and would lose it. Preserve these records."
        )
    op.drop_index("ix_no_countertop_answers_batch_id", table_name=_TABLE)
    op.drop_constraint("no_countertop_answer_note_not_blank", _TABLE, type_="check")
    op.drop_column(_TABLE, "batch_id")
    op.drop_column(_TABLE, "note")
