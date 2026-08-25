"""Make the audit trail append-only, the same way 0013 did for the tables that existed then.

Revision ID: 0024_audit_events_append_only
Revises: 0023_audit_events

`audit_events` carries `Immutable`, and until this ran the marker was enforced by nothing — an
`UPDATE` succeeded. An audit trail that can be edited is not one, and it is the table somebody would
most want to tidy after the fact.

**A separate migration rather than an edit to 0013.** That migration says in its own docstring that
its list is written out because *"a migration has to keep saying what it said the day it ran"*. So
each migration protects the tables that existed when it was written, and the guard in
`tests/db/test_append_only.py` unions the lists across migrations rather than reading only the first.
The alternative — appending to 0013 — would make an applied migration claim it had protected a table
that did not exist on the day it ran.

The trigger function `gv_reject_mutation` is created by 0013 and reused here rather than redefined:
two definitions of one rule is how they come to differ.

Source: issue #255. Verification: tests/db/test_append_only.py.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0024_audit_events_append_only"
down_revision: str | None = "0023_audit_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Every table carrying `Immutable` that 0013 did not already protect, as of this migration.
IMMUTABLE_TABLES: tuple[str, ...] = ("audit_events",)


def upgrade() -> None:
    for table in IMMUTABLE_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_append_only "
            f"BEFORE UPDATE OR DELETE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION gv_reject_mutation()"
        )


def downgrade() -> None:
    for table in IMMUTABLE_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
