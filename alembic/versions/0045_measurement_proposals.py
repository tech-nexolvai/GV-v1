"""Keep the model's reading-to-field proposal, so a reviewer opens a form already filled (#596).

Revision ID: 0045_measurement_proposals
Revises: 0044_association_chain

#591 wired a model to propose which reading fills which field, behind a button. That made the
proposal a thing a reviewer had to *ask* for: the form was empty every time it was opened, the model
was paid for again on every reload, and the answer to "was this filled by AI?" lasted exactly as
long as the browser tab.

The proposal is now computed once, when the drawings are read, and kept here. A reviewer arriving at
the form finds it already filled, marked, and editable.

**Only accepted proposals are rows.** `workflow/assignment.py` refuses a batch whole if any part of
it fails, and a refusal leaves the fields empty — which is exactly what an absent row already says.
A refusal row would be a second way of saying nothing; the reason goes to the worker's log, where
somebody diagnosing it will look.

**A proposal is not a measurement.** No value is stored: a row names a candidate, and the number
comes from that candidate's own exact numerator and denominator. The reviewer still saves the form,
and saving is what records a measurement — as `Provenance.MEASURED`, with their name on it. Nothing
here can reach a verdict without a person in between, which is the rule this table is shaped around.

`proposal_id` groups one run's rows. Append-only means a re-proposal is a second set beside the
first rather than an edit, so the newest set is the current answer and the older ones are the record
of what it replaced.

Source: issue #596. Verification: tests/api/test_measurement_proposal.py, tests/workflow/test_propose.py.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.db.roles import ROLE_GRANTS

revision: str = "0045_measurement_proposals"
down_revision: str | None = "0044_association_chain"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Every table carrying `Immutable` that this migration creates. Written out rather than derived,
#: for the reason 0013 gives: a migration has to keep saying what it said the day it ran.
IMMUTABLE_TABLES: tuple[str, ...] = ("measurement_proposals",)

TABLE = "measurement_proposals"


def _in_current_schema(*statements: str) -> str:
    """Wrap statements so `%I` becomes `current_schema()` when they run.

    The shape 0025, 0027, 0030, 0031 and 0039 use, and for the same reason: the test fixture gives
    every test its own schema, so the schema is resolved at execution time by PostgreSQL.
    """
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
        TABLE,
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("package_revision_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("proposal_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("field_key", sa.String(length=200), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("model_id", sa.String(length=200), nullable=False),
        sa.Column("prompt_id", sa.String(length=100), nullable=False),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["observation_candidates.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(
            "proposal_id", "field_key", "position", name="uq_measurement_proposals_slot"
        ),
        sa.CheckConstraint("position >= 0", name="position_not_negative"),
    )
    op.create_index(f"ix_{TABLE}_package_revision_id", TABLE, ["package_revision_id"])
    op.create_index(f"ix_{TABLE}_proposal_id", TABLE, ["proposal_id"])
    op.create_index(f"ix_{TABLE}_candidate_id", TABLE, ["candidate_id"])

    for table in IMMUTABLE_TABLES:
        # `gv_reject_mutation` is created by 0013 and reused rather than redefined: two definitions
        # of one rule is how they come to differ.
        op.execute(
            f"CREATE TRIGGER {table}_append_only "
            f"BEFORE UPDATE OR DELETE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION gv_reject_mutation()"
        )

    # Read from `app.db.roles` rather than written out: two copies of a grant drift in the direction
    # of more privilege, and `tests/db/test_roles.py` compares the declaration against
    # `information_schema`.
    grant_statements = [
        f'GRANT {", ".join(privileges)} ON TABLE %I."{table}" TO {role.value}'
        for role, grants in ROLE_GRANTS.items()
        for table, privileges in grants.privileges.items()
        if table == TABLE
    ]
    if grant_statements:
        op.get_bind().execute(sa.text(_in_current_schema(*grant_statements)))


def downgrade() -> None:
    """Drops the table. Every stored proposal goes with it; the readings themselves are untouched."""
    for table in IMMUTABLE_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
    op.drop_index(f"ix_{TABLE}_candidate_id", table_name=TABLE)
    op.drop_index(f"ix_{TABLE}_proposal_id", table_name=TABLE)
    op.drop_index(f"ix_{TABLE}_package_revision_id", table_name=TABLE)
    op.drop_table(TABLE)
