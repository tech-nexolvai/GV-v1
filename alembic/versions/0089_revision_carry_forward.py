"""Name where a carried-forward answer came from on a new revision (#1180).

Revision ID: 0089_revision_carry_forward
Revises: 0088_outbox_abandoned

When a vendor resubmits, revision 2 is read again and its countertop rows and drawing views are new
rows. A person's wall answer (`slot_row_review_decisions`) or drawing-role confirmation
(`view_role_confirmations`) from revision 1 is carried to the identical row or view of revision 2
as a new record (`app/lifecycle/carry_forward.py`). Without a link, the new record would look like a
fresh answer given on revision 2; `carried_from_id` names the record it was copied from, the same
way `architect_view_matches.carried_from_id` (0084) does for an architect view pick.

**Nullable, nothing backfilled.** Every existing record was given directly by a person (or by code
for a role), so NULL is the truth for all of them.

Both tables are append-only (their row triggers refuse UPDATE and DELETE); adding a nullable column
fires no row trigger, and the roles' grants are on the tables, so no new `GRANT` is needed. A
downgrade with carried records stored refuses: they are part of the review's record.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0089_revision_carry_forward"
down_revision: str | None = "0088_outbox_abandoned"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Each table and its link's constraint name, spelled out: the naming convention's name would run
#: past PostgreSQL's 63-character limit.
TABLES: dict[str, str] = {
    "slot_row_review_decisions": "fk_slot_row_review_carried_from",
    "view_role_confirmations": "fk_view_role_confirmation_carried_from",
}


def _foreign_key(table: str) -> str:
    return TABLES[table]


def upgrade() -> None:
    """Add a nullable self-reference on each table. Existing rows stay NULL."""
    for table in TABLES:
        op.add_column(
            table, sa.Column("carried_from_id", postgresql.UUID(as_uuid=True), nullable=True)
        )
        op.create_foreign_key(
            _foreign_key(table), table, table, ["carried_from_id"], ["id"], ondelete="RESTRICT"
        )


def downgrade() -> None:
    """Drop the links, refused while any carried record exists."""
    bind = op.get_bind()
    for table in TABLES:
        stored = bind.scalar(
            sa.text(f"SELECT count(*) FROM {table} WHERE carried_from_id IS NOT NULL")
        )
        if stored:
            raise RuntimeError(
                f"Cannot downgrade the revision carry-forward links: {stored} record(s) in {table} "
                "were carried from an earlier revision. Preserve these records."
            )
    for table in TABLES:
        op.drop_constraint(_foreign_key(table), table, type_="foreignkey")
        op.drop_column(table, "carried_from_id")
