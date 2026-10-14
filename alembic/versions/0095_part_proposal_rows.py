"""Link each of Bala's suggested parts to the countertop row and piece it came from (#1236).

Revision ID: 0095_part_proposal_rows
Revises: 0094_invocation_failure_reason

Bala (`workflow/bala_items.py`) suggests one countertop for each countertop row the slot reader
read, and one part for each piece of that row, for a person to confirm with one click. One
append-only table says where each suggestion came from:

* `part_proposal_rows` — for one suggestion, the row's anchor reading (`row_candidate_id`, the
  `slot:0` reading of the slot-reader run), the piece's place along the row (`slot_index`, `NULL`
  for the countertop itself), the slot-reader run, and whether the drawing names the suggested
  kind (`kind_named`).

**One suggestion per row and piece, and one row and piece per suggestion, held by the schema**: a
unique `part_proposal_id`, and a unique expression index on
`(row_candidate_id, coalesce(slot_index, -1))`, so the countertop (`NULL`) is unique per row too.

**A link, never a part.** Nothing here writes a `drawing_items` row: only a person's confirmation
makes one (`workflow/parts.py:confirm_part`).

**Granted like every other table, and not to the verdict.** The grants come from
`app.db.roles.ROLE_GRANTS`, which gives `gv_verdict` an allowlist that does not name it.

The orchestrator approved this migration for #1236 (phase 4, PR 1).

Source: issue #1236. Verification: tests/db/test_drawing_models.py,
tests/app/test_migrations_roundtrip.py, tests/db/test_roles.py, tests/db/test_append_only.py.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.db.roles import ROLE_GRANTS

revision: str = "0095_part_proposal_rows"
down_revision: str | None = "0094_invocation_failure_reason"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LINKS_TABLE = "part_proposal_rows"
IMMUTABLE_TABLES: tuple[str, ...] = (LINKS_TABLE,)


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
        LINKS_TABLE,
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("part_proposal_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("row_candidate_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("slot_index", sa.Integer(), nullable=True),
        sa.Column("extraction_run_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("kind_named", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["part_proposal_id"], ["part_proposals.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["row_candidate_id"], ["observation_candidates.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["extraction_run_id"], ["extraction_runs.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("part_proposal_id", name="uq_part_proposal_rows_part_proposal_id"),
        sa.CheckConstraint(
            "slot_index IS NULL OR slot_index >= 0", name="part_proposal_row_slot_not_negative"
        ),
    )
    op.create_index("ix_part_proposal_rows_row_candidate_id", LINKS_TABLE, ["row_candidate_id"])
    op.create_index("ix_part_proposal_rows_extraction_run_id", LINKS_TABLE, ["extraction_run_id"])
    op.create_index(
        "uq_part_proposal_rows_row_slot",
        LINKS_TABLE,
        ["row_candidate_id", sa.text("coalesce(slot_index, -1)")],
        unique=True,
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
    """Drops the links. The suggestions, any decision on them and every item stay."""
    for table in IMMUTABLE_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
    op.drop_index("uq_part_proposal_rows_row_slot", table_name=LINKS_TABLE)
    op.drop_index("ix_part_proposal_rows_extraction_run_id", table_name=LINKS_TABLE)
    op.drop_index("ix_part_proposal_rows_row_candidate_id", table_name=LINKS_TABLE)
    op.drop_table(LINKS_TABLE)
