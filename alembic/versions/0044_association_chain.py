"""Which run of end-to-end dimensions a reading's line belongs to (#591).

Revision ID: 0044_association_chain
Revises: 0043_redline_output

`extraction/geometry/dimension_lines.py` has grouped dimension lines into chains since #588 — lines
drawn end to end along one axis, which is what a cabinet run looks like on a sheet. `workflow/
assignment.py` then refuses a many-valued field whose readings were gathered from two different
chains, because `CT-WIDTH-001` compares two runs position by position and values taken from
unrelated places would produce a check comparing the second cabinet against the fifth, with every
number in it real and every one in the wrong slot.

**That refusal could not fire in production, because the chains were thrown away.** The detector
runs in the association stage, `associate` is handed only the extents, and the grouping was gone on
the next line. So this adds the two columns that keep it: which chain, and where along it.

Nullable, because most lines are in no chain and a drawing that dimensions its parts without
chaining them is one this cannot check — a limit worth recording rather than a reason to invent a
refusal. `chain_paired` makes the two columns arrive together and only on a row that was attached:
a position with no chain cannot be ordered against anything, and a chain on a refused row would
claim the line it belongs to while the same row says no line was decided.

Adding columns to an append-only table is DDL, not row mutation, so the `append_only` trigger 0039
installed is untouched and still rejects every UPDATE and DELETE.

Source: issue #591. Verification: tests/db/test_evidence_models.py, tests/workflow/test_stages.py.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0044_association_chain"
down_revision: str | None = "0043_redline_output"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "observation_associations"

#: The migration's own copy of the model's constraint. 0035 exists because those two once disagreed.
CHAIN_PAIRED = (
    "(chain_key IS NULL AND chain_position IS NULL)"
    " OR (chain_key IS NOT NULL AND chain_position IS NOT NULL"
    " AND refusal_reason IS NULL)"
)


def upgrade() -> None:
    op.add_column(TABLE, sa.Column("chain_key", sa.String(length=120), nullable=True))
    op.add_column(TABLE, sa.Column("chain_position", sa.Integer(), nullable=True))
    op.create_check_constraint("chain_paired", TABLE, CHAIN_PAIRED)


def downgrade() -> None:
    """Drops the chain columns. Existing associations keep their line; only the grouping is lost."""
    op.drop_constraint(op.f("ck_observation_associations_chain_paired"), TABLE, type_="check")
    op.drop_column(TABLE, "chain_position")
    op.drop_column(TABLE, "chain_key")
