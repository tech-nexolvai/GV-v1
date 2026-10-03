"""Record a person's decision on the run beneath each countertop (#893).

Revision ID: 0060_countertop_run_decisions
Revises: 0059_parameter_value_citations

Step 5 of the plan on #748. The computer suggests which confirmed cabinets and fillers sit beneath a
confirmed countertop; a person confirms, corrects or withdraws that run. `countertop_runs` (0058)
holds a confirmed run's members, one row per member, but nothing said which run is the current one
for a countertop, and nothing could record a person saying a run is wrong. One append-only table
does both:

* `countertop_run_decisions` — a person's decision on one countertop's run: `confirmed`, naming the
  `run_id` whose `countertop_runs` rows hold the members, or `withdrawn`, naming none.

**One current decision per countertop, held by the schema**, the way 0058 holds one per suggested
part: a partial unique index allows one first decision per countertop, a unique `supersedes_id`
lets each decision be replaced once, and a two-column foreign key keeps a replacement on the same
countertop.

**Every member row now belongs to a decision.** `countertop_runs` gains a foreign key from
`(run_id, countertop_item_id)` to the decision that confirmed that run for that countertop, so a
member row written without a person's decision, or under a decision about a different countertop,
is refused. Nothing has written a `countertop_runs` row before this step, so no existing row lacks
one.

**Granted like every other table, and not to the verdict.** The grants come from
`app.db.roles.ROLE_GRANTS`, which gives `gv_verdict` an allowlist that does not name it.

Source: issue #893; #748 plan, step 5. Verification: tests/db/test_drawing_models.py,
tests/app/test_migrations_roundtrip.py, tests/db/test_roles.py.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.db.roles import ROLE_GRANTS

revision: str = "0060_countertop_run_decisions"
down_revision: str | None = "0059_parameter_value_citations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DECISIONS_TABLE = "countertop_run_decisions"
IMMUTABLE_TABLES: tuple[str, ...] = (DECISIONS_TABLE,)

#: Written out rather than imported: a migration records the vocabulary as it stood when it ran.
DECISIONS = "'confirmed', 'withdrawn'"
NOT_BLANK = "!~ '^[[:space:]]*$'"

#: The member rows' link to the decision that confirmed their run.
RUN_DECISION_FK = "fk_countertop_runs_run_id_countertop_run_decisions"


def _in_current_schema(*statements: str) -> str:
    body = "\n".join(f"    EXECUTE format({statement!r}, gv_schema);" for statement in statements)
    return f"""
DO $$
DECLARE
    gv_schema text := current_schema();
BEGIN
{body}
END
$$;
"""


def upgrade() -> None:
    op.create_table(
        DECISIONS_TABLE,
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("countertop_item_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("supersedes_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("run_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("confirmed_by", sa.String(length=200), nullable=False),
        sa.ForeignKeyConstraint(["countertop_item_id"], ["drawing_items.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["supersedes_id", "countertop_item_id"],
            [f"{DECISIONS_TABLE}.id", f"{DECISIONS_TABLE}.countertop_item_id"],
            ondelete="RESTRICT",
            name="fk_countertop_run_decisions_supersedes_id",
        ),
        sa.CheckConstraint(f"decision IN ({DECISIONS})", name="run_decision_value"),
        sa.CheckConstraint(
            "(decision = 'confirmed') = (run_id IS NOT NULL)",
            name="run_decision_shape",
        ),
        sa.CheckConstraint(f"confirmed_by {NOT_BLANK}", name="run_decision_actor_not_blank"),
        sa.UniqueConstraint("run_id", name="uq_countertop_run_decisions_run_id"),
        sa.UniqueConstraint(
            "run_id", "countertop_item_id", name="uq_countertop_run_decisions_run_countertop"
        ),
        sa.UniqueConstraint(
            "id", "countertop_item_id", name="uq_countertop_run_decisions_id_countertop"
        ),
        sa.UniqueConstraint("supersedes_id", name="uq_countertop_run_decisions_supersedes_id"),
    )
    op.create_index(
        f"ix_{DECISIONS_TABLE}_countertop_item_id", DECISIONS_TABLE, ["countertop_item_id"]
    )
    op.create_index(
        f"ix_{DECISIONS_TABLE}_first_decision",
        DECISIONS_TABLE,
        ["countertop_item_id"],
        unique=True,
        postgresql_where=sa.text("supersedes_id IS NULL"),
    )

    op.create_foreign_key(
        RUN_DECISION_FK,
        "countertop_runs",
        DECISIONS_TABLE,
        ["run_id", "countertop_item_id"],
        ["run_id", "countertop_item_id"],
        ondelete="RESTRICT",
    )

    for table in IMMUTABLE_TABLES:
        # `gv_reject_mutation` is created by 0013 and reused rather than redefined.
        op.execute(
            f"CREATE TRIGGER {table}_append_only "
            f"BEFORE UPDATE OR DELETE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION gv_reject_mutation()"
        )

    grant_statements = [
        f'GRANT {", ".join(privileges)} ON TABLE %I."{table}" TO {role.value}'
        for role, grants in ROLE_GRANTS.items()
        for table, privileges in grants.privileges.items()
        if table in IMMUTABLE_TABLES
    ]
    if grant_statements:
        op.get_bind().execute(sa.text(_in_current_schema(*grant_statements)))


def downgrade() -> None:
    """Drops the decisions and the member rows' link to them. Every recorded decision goes with
    them; the member rows, the parts and their confirmations are untouched."""
    op.drop_constraint(RUN_DECISION_FK, "countertop_runs", type_="foreignkey")
    for table in IMMUTABLE_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
    op.drop_index(f"ix_{DECISIONS_TABLE}_first_decision", table_name=DECISIONS_TABLE)
    op.drop_index(f"ix_{DECISIONS_TABLE}_countertop_item_id", table_name=DECISIONS_TABLE)
    op.drop_table(DECISIONS_TABLE)
