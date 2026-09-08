"""Which dimension line a reading annotates, or why that could not be decided (#545).

Revision ID: 0039_observation_associations
Revises: 0038_candidate_source_author

`extraction/geometry/text_association.py` has existed since the geometry work and has never been
called. Its own docstring says why — both its numbers are required arguments because
*"`data/drawings/` is empty and a default here would ship today's guess as ground truth"* — and it
has also never had anywhere to put an answer. This is that place.

**A refusal is a row.** That module is explicit that *"refusing is the deliverable, not the
fallback"*: two lines equally close to one number is the ordinary case on a dimensioned elevation,
not an edge case. Measured on the first real sheet, twenty notes against 2691 segments at a
proximity limit of 0.05, eleven attached and five were refused for equally-close candidates. A schema
that recorded only the eleven would turn "we could not tell" into silence, and silence reads
downstream as a drawing with no dimensions on it.

**The line is inlined and is not an identity.** There is no `dimension_lines` table here. Deciding
which vector primitives *are* dimension lines is a detector (#179), correct only against this
vendor's real CAD output, and `associate` takes a `DimensionExtent` rather than that detector's type
precisely so it need not wait for it. A row says "this reading was attached to the segment running
from A to B" — a measurement, not a claim about what the segment is.

**Coordinates as text.** The same choice `0037` made for `pages.media_box`, for the same reason: a
JSON float loses the exactness the units layer exists to keep, and these numbers decide which line a
dimension belongs to.

**Attached or refused, never both.** Endpoints *and* a reason would be two answers to one question;
neither would be a decision nobody can read. The pairing is a check constraint rather than a
convention, as `candidate_corroboration_paired` is one table over.

**The constraint names are short on purpose.** Written as `association_attached_or_refused` and
`association_refusal_reason_not_blank`, the second became 64 characters once the
`ck_%(table_name)s_%(constraint_name)s` convention was applied — one past PostgreSQL's identifier
limit, which truncates silently. The migration then built a constraint whose name differed from the
model's by that one character, and `tests/app/test_migrations_roundtrip.py` failed with autogenerate
wanting to drop and re-add it. The table name is already twenty-seven characters, so the constraint
names do not repeat it.

Unique on `(candidate_id, extraction_run_id)`: one answer per reading per run. A re-association under
different thresholds is a different run — `open_extraction_run` keys a run on its configuration — so
this constrains a repeat of the same work rather than a genuine second opinion.

Source: issue #545. Verification: tests/db/test_evidence_models.py, tests/workflow/test_association.py.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op
from app.db.roles import ROLE_GRANTS

revision: str = "0039_observation_associations"
down_revision: str | None = "0038_candidate_source_author"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Every table carrying `Immutable` that this migration creates.
#:
#: Written out rather than derived, for the reason 0013 gives: a migration has to keep saying what it
#: said the day it ran. `tests/db/test_append_only.py` unions these lists across migrations and
#: compares the result with `immutable_table_names()`.
IMMUTABLE_TABLES: tuple[str, ...] = ("observation_associations",)

TABLE = "observation_associations"

#: Attached or refused. Written here as the migration's own copy of the model's constraint, because
#: the migration is what builds production — 0035 exists because those two once disagreed.
ATTACHED_OR_REFUSED = (
    "(refusal_reason IS NULL"
    " AND start_x IS NOT NULL AND start_y IS NOT NULL"
    " AND end_x IS NOT NULL AND end_y IS NOT NULL"
    " AND jsonb_array_length(signals) > 0)"
    " OR (refusal_reason IS NOT NULL"
    " AND start_x IS NULL AND start_y IS NULL"
    " AND end_x IS NULL AND end_y IS NULL"
    " AND jsonb_array_length(signals) = 0)"
)


def _in_current_schema(*statements: str) -> str:
    """Wrap statements so `%I` becomes `current_schema()` when they run.

    The shape 0025, 0027, 0030 and 0031 use, and for the same reason: the test fixture gives every
    test its own schema, so the schema is resolved at execution time by PostgreSQL rather than by
    this file querying for it.
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
        sa.Column("candidate_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("extraction_run_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("start_x", sa.String(length=64), nullable=True),
        sa.Column("start_y", sa.String(length=64), nullable=True),
        sa.Column("end_x", sa.String(length=64), nullable=True),
        sa.Column("end_y", sa.String(length=64), nullable=True),
        sa.Column("signals", JSONB(), nullable=False),
        sa.Column("refusal_reason", sa.String(length=1000), nullable=True),
        sa.Column("candidate_lines", JSONB(), nullable=True),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["observation_candidates.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["extraction_run_id"], ["extraction_runs.id"], ondelete="RESTRICT"),
        sa.CheckConstraint(ATTACHED_OR_REFUSED, name="attached_or_refused"),
        # A blank reason says nothing while reading as though something was recorded. The regex
        # rather than `btrim`, which strips spaces and nothing else — see 0035.
        sa.CheckConstraint(
            "refusal_reason IS NULL OR refusal_reason !~ '^[[:space:]]*$'",
            name="refusal_reason_not_blank",
        ),
        sa.UniqueConstraint(
            "candidate_id", "extraction_run_id", name="uq_observation_associations_candidate_run"
        ),
    )
    op.create_index(f"ix_{TABLE}_candidate_id", TABLE, ["candidate_id"])
    op.create_index(f"ix_{TABLE}_extraction_run_id", TABLE, ["extraction_run_id"])

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
    # `information_schema`. 0025 derives its list from `Base.metadata` but runs earlier, when this
    # table does not exist, so the grant belongs to the migration that creates it.
    grant_statements = [
        f'GRANT {", ".join(privileges)} ON TABLE %I."{table}" TO {role.value}'
        for role, grants in ROLE_GRANTS.items()
        for table, privileges in grants.privileges.items()
        if table == TABLE
    ]
    if grant_statements:
        op.get_bind().execute(sa.text(_in_current_schema(*grant_statements)))


def downgrade() -> None:
    """Drops the table, and with it every association and every recorded refusal."""
    for table in IMMUTABLE_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
    op.drop_index(f"ix_{TABLE}_extraction_run_id", table_name=TABLE)
    op.drop_index(f"ix_{TABLE}_candidate_id", table_name=TABLE)
    op.drop_table(TABLE)
