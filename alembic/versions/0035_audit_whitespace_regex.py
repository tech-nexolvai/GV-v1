"""Make the audit trail's whitespace rule match the one the writer applies (#522).

Revision ID: 0035_audit_whitespace_regex
Revises: 0034_pg_trgm_in_public

`audit_events` was created by 0023 with `length(btrim(actor)) > 0`, and the model was later corrected
to `actor !~ '^[[:space:]]*$'` — with a comment saying exactly why. The migration was never updated,
so the two have disagreed ever since, and it is the migration that built production.

The difference is not cosmetic. PostgreSQL's `btrim` strips **spaces** by default and nothing else,
while Python's `str.strip`, which `app/audit/events.py:emit` uses, also strips tabs and newlines:

    SELECT length(btrim(E'\\t')) = 0;   -- false: the database sees a non-empty actor
    SELECT E'\\t' ~ '^[[:space:]]*$';   -- true:  the writer sees a blank one

So a tab-only or newline-only actor passed the database and failed the writer — and a direct INSERT,
which is precisely how somebody would get round `emit`, put an unattributable row in the audit trail.
An audit trail whose actor column can hold a tab is not an audit trail.

**Found by making tests run against the migrated schema.** `tests/audit/test_events.py` built its
tables from the models, so it asserted the constraint the model declares and never the one production
has. #522 moved those tests onto a schema built by migrations, and three of them failed immediately
with `DID NOT RAISE IntegrityError`. The drift had been there since 0023.

Existing rows are not touched. A row already carrying whitespace would fail the new constraint, so
`ALTER TABLE ... VALIDATE` would refuse — but `audit_events` is append-only and this repository has
never run `emit` with a blank actor, and a `NOT VALID` constraint that silently tolerates existing
violations is exactly the kind of half-enforced rule this is fixing. If a deployment does hold such a
row, this migration will fail loudly, which is the correct outcome: somebody must look at it.

Source: issue #522, found while doing it. Verification: tests/audit/test_events.py.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0035_audit_whitespace_regex"
down_revision: str | None = "0034_pg_trgm_in_public"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: (constraint name, column) for every audit column that must not be blank.
COLUMNS: tuple[tuple[str, str], ...] = (
    ("audit_events_actor_named", "actor"),
    ("audit_events_target_typed", "target_type"),
)


def upgrade() -> None:
    for name, column in COLUMNS:
        op.drop_constraint(name, "audit_events", type_="check")
        op.create_check_constraint(name, "audit_events", f"{column} !~ '^[[:space:]]*$'")


def downgrade() -> None:
    """Back to `btrim`, which is weaker: it accepts a tab-only actor. Named so nobody runs it idly."""
    for name, column in COLUMNS:
        op.drop_constraint(name, "audit_events", type_="check")
        op.create_check_constraint(name, "audit_events", f"length(btrim({column})) > 0")
