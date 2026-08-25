"""Parameter sets and values, and the foreign key #192 left waiting (#303, C1.13).

A7 built parameter sets in memory only, so the hashes `Finding.parameter_set_ids` already records point at
nothing after a restart. ADR-0016 requires a finding to pin the exact parameters that judged it; that is
unenforceable while the sets are unpersisted, and the numbers behind a six-month-old verdict would be
unrecoverable.

**Additive, and no shipped migration is edited.** `projects.company_standards_id` has existed since 0003 as
a bare nullable UUID — `app/models/package.py` says why in a comment: *"the A7 parameter-set table has not
landed yet … retain the identity now and add its foreign key in a later, additive migration."* This is that
migration. The column keeps its name, its type and its data; it gains a constraint.

**Both new tables are append-only.** Editing a parameter in place would change the numbers behind every
finding that already cited that set — silently, and after the fact. `IMMUTABLE_TABLES` is written out rather
than computed, because a migration has to keep saying what it said the day it ran;
`tests/db/test_append_only.py` unions this list with 0013's and asserts the total equals what the models
mark.

**The value is two integers, not a float.** `1/8` is stored as `1` over `8` so it returns as
`Fraction(1, 8)`. A `DOUBLE PRECISION` column would store `0.125` and lose the authored form, and `1/3` has
no decimal form at all — `AGENTS.md` §2.4 forbids a float in the decision path, and a parameter is squarely
in it.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import text

from alembic import op
from app.db.roles import ROLE_GRANTS

revision: str = "0027_parameter_sets"
down_revision: str | None = "0026_outbox_trace_context"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The tables this migration makes append-only.
IMMUTABLE_TABLES: tuple[str, ...] = (
    "parameter_sets",
    "parameter_values",
)

#: Allowed layers and provenances, written out for the check constraints.
#:
#: Spelled here rather than imported: a migration must keep meaning what it meant when it ran, and a
#: constraint built from today's enum would silently change as members are added.
#: `tests/db/test_parameter_models.py` asserts these still match `rules/parameters.py`.
_LAYERS = ("global", "project", "run")
_PROVENANCES = ("Company standard", "G.C / Client", "Measured")


def _in_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _in_current_schema(*statements: str) -> str:
    """Wrap statements so `%I` becomes `current_schema()` when they run.

    The same shape 0025 uses, and for the same reason: the test fixture gives every test its own schema,
    so the schema has to be resolved at execution time — but by PostgreSQL, not by this file querying for
    it, because `tests/app/test_migration_matches_models.py` renders every migration offline.
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
        "parameter_sets",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("set_id", sa.String(length=71), nullable=False),
        sa.Column("project_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("layer", sa.String(length=50), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_parameter_sets_project_id_projects",
            # RESTRICT, not CASCADE: deleting a project must not delete the parameters that judged its
            # findings. The findings outlive the project record and still cite these hashes.
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("set_id", name="uq_parameter_sets_set_id"),
        sa.UniqueConstraint(
            "project_id", "layer", "version", name="uq_parameter_sets_project_layer_version"
        ),
        sa.CheckConstraint(f"layer IN ({_in_list(_LAYERS)})", name="layer_in_vocabulary"),
        sa.CheckConstraint("version >= 1", name="version_positive"),
        sa.CheckConstraint("set_id ~ '^sha256:[0-9a-f]{64}$'", name="set_id_is_a_digest"),
        sa.CheckConstraint(
            "(layer = 'global') = (project_id IS NULL)",
            name="global_has_no_project",
        ),
    )
    op.create_index("ix_parameter_sets_set_id", "parameter_sets", ["set_id"])
    op.create_index("ix_parameter_sets_project_id", "parameter_sets", ["project_id"])
    op.create_index("ix_parameter_sets_layer", "parameter_sets", ["layer"])
    op.create_index("ix_parameter_sets_project_layer", "parameter_sets", ["project_id", "layer"])

    op.create_table(
        "parameter_values",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("parameter_set_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("numerator", sa.BigInteger(), nullable=False),
        sa.Column("denominator", sa.BigInteger(), nullable=False),
        sa.Column("unit", sa.String(length=20), nullable=False),
        sa.Column("provenance", sa.String(length=50), nullable=False),
        sa.Column("set_by", sa.String(length=200), nullable=False),
        sa.Column("set_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["parameter_set_id"],
            ["parameter_sets.id"],
            name="fk_parameter_values_parameter_set_id_parameter_sets",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("parameter_set_id", "name", name="uq_parameter_values_set_name"),
        sa.CheckConstraint("denominator > 0", name="denominator_positive"),
        sa.CheckConstraint("name <> ''", name="name_present"),
        sa.CheckConstraint("set_by <> ''", name="set_by_present"),
        sa.CheckConstraint(
            f"provenance IN ({_in_list(_PROVENANCES)})", name="provenance_in_vocabulary"
        ),
    )
    op.create_index(
        "ix_parameter_values_parameter_set_id", "parameter_values", ["parameter_set_id"]
    )
    op.create_index("ix_parameter_values_name", "parameter_values", ["name"])

    # The foreign key #192's plan carried and had to drop, because the table did not exist.
    op.create_foreign_key(
        "fk_projects_company_standards_id_parameter_sets",
        "projects",
        "parameter_sets",
        ["company_standards_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    # **Grants for the two new tables.** 0025 derives its grant list from `Base.metadata`, so it already
    # names these — but it runs two migrations earlier, when they do not exist. #303 made that skip rather
    # than fail (see `_guarded_in_current_schema` there), which leaves the grants to be applied here, by
    # the migration that creates the tables.
    #
    # Read from `app.db.roles` rather than written out, for the reason 0025 gives: a grant is the current
    # privilege set, and two copies drift in the direction of more privilege.
    grant_statements: list[str] = []
    for role, grants in ROLE_GRANTS.items():
        for table, privileges in grants.privileges.items():
            if table in IMMUTABLE_TABLES:
                granted = ", ".join(privileges)
                grant_statements.append(f'GRANT {granted} ON TABLE %I."{table}" TO {role.value}')
    if grant_statements:
        op.get_bind().execute(text(_in_current_schema(*grant_statements)))

    # Append-only, using the function 0013 created.
    for table in IMMUTABLE_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_append_only "
            f"BEFORE UPDATE OR DELETE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION gv_reject_mutation()"
        )


def downgrade() -> None:
    for table in IMMUTABLE_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")

    op.drop_constraint(
        "fk_projects_company_standards_id_parameter_sets", "projects", type_="foreignkey"
    )
    op.drop_index("ix_parameter_values_name", table_name="parameter_values")
    op.drop_index("ix_parameter_values_parameter_set_id", table_name="parameter_values")
    op.drop_table("parameter_values")
    op.drop_index("ix_parameter_sets_project_layer", table_name="parameter_sets")
    op.drop_index("ix_parameter_sets_layer", table_name="parameter_sets")
    op.drop_index("ix_parameter_sets_project_id", table_name="parameter_sets")
    op.drop_index("ix_parameter_sets_set_id", table_name="parameter_sets")
    op.drop_table("parameter_sets")
