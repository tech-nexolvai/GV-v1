"""Store a reviewer's classification of one item in an ordered run (#684).

Revision ID: 0048_item_classifications
Revises: 0047_layout_proposals

`CAB-FILLER-001` needs to know which cabinet in a run is the equipment cabinet before it can decide
how much the others move, and a reviewer had no way to say. Every reviewer input this system stored
was a `Quantity` — a number and a unit — so `single_door` down that path raised
`UnitNormalisationError`, which is the unit policy working correctly.

**Neither existing table could hold it.** `parameter_values` is numeric to the column level, and
admitting a category would mean making the numerator, denominator and unit nullable — weakening a
guard on every measurement in the system to carry something that is not one. `layout_confirmations`
has the right shape but is the human gate for rule *applicability*; an operand stored there would
have to be told from a discriminator by reading its name.

So the role decides: `cabinet_type` is declared in a rule's `inputs:` and bound to an operand, and
this is where an input that is a category lives.

Append-only, like every reviewer value. A correction is another row and the latest wins, because a
finding cites the version that judged it (ADR-0016).

Source: issue #684. Verification: tests/app/test_item_classifications.py.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.db.roles import ROLE_GRANTS

revision: str = "0048_item_classifications"
down_revision: str | None = "0047_layout_proposals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

IMMUTABLE_TABLES: tuple[str, ...] = ("item_classifications",)
TABLE = "item_classifications"


def _in_current_schema(*statements: str) -> str:
    """Resolve the schema at execution time, the shape every migration from 0025 on uses.

    Every test runs in its own schema, so PostgreSQL resolves it rather than this file querying for
    it — `tests/app/test_migration_matches_models.py` renders every migration offline.
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


def _grant_new_tables() -> None:
    grant_statements = [
        f'GRANT {", ".join(privileges)} ON TABLE %I."{table}" TO {role.value}'
        for role, grants in ROLE_GRANTS.items()
        for table, privileges in grants.privileges.items()
        if table in IMMUTABLE_TABLES
    ]
    if grant_statements:
        op.get_bind().execute(sa.text(_in_current_schema(*grant_statements)))


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("package_revision_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("rule_id", sa.String(length=100), nullable=False),
        sa.Column("input_name", sa.String(length=200), nullable=False),
        # An integer column rather than a `#0` suffix on the name. The order is the run left to
        # right and the distribution adjusts positionally, so a malformed index must be impossible
        # rather than dropped on the way back in.
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(length=50), nullable=False),
        sa.Column("confirmed_by", sa.String(length=200), nullable=False),
        sa.ForeignKeyConstraint(
            ["package_revision_id"], ["package_revisions.id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint("position >= 0", name="item_classification_position_not_negative"),
        sa.CheckConstraint(
            "rule_id !~ '^[[:space:]]*$'", name="item_classification_rule_not_blank"
        ),
        sa.CheckConstraint(
            "input_name !~ '^[[:space:]]*$'", name="item_classification_input_not_blank"
        ),
        sa.CheckConstraint(
            "category !~ '^[[:space:]]*$'", name="item_classification_category_not_blank"
        ),
        sa.CheckConstraint(
            "confirmed_by !~ '^[[:space:]]*$'", name="item_classification_actor_not_blank"
        ),
    )
    op.create_index(f"ix_{TABLE}_package_revision_id", TABLE, ["package_revision_id"])

    for table in IMMUTABLE_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_append_only "
            f"BEFORE UPDATE OR DELETE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION gv_reject_mutation()"
        )

    _grant_new_tables()


def downgrade() -> None:
    for table in IMMUTABLE_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
    op.drop_index(f"ix_{TABLE}_package_revision_id", table_name=TABLE)
    op.drop_table(TABLE)
