"""Keep a pointer to the passage that states a setting, and never the number (#849).

Revision ID: 0057_parameter_proposals
Revises: 0056_text_phrases

A setting the rules need could only be typed. `parameter_proposals` lets the app point at the
passage in the package that states it: the setting, the phrase, the runs within the phrase that hold
the number, the source the passage would make the value, and the code that proposed it.

**There is no value column**, following `measurement_proposals` (0045). The number stays on the
runs, and confirming it is a person's job (step 3.3 of #798). There is no side column either:
whether a passage is the architect's is decided when the pointer is made and again when it is read
back, because a drawing's confirmed role can change after the row is written.

**The database refuses a source that may cite nothing.** `claimed_source` admits only the one
source `rules.parameter_sources.CITABLE_SIDES` lets cite a package: G.C / Client, from the
architect's drawings. A company standard, a fabricator's number or a site measurement can never be
proposed from a package, and a row claiming one is refused here as well as in code.

**Append-only and granted like every other table, and not to the verdict.** The grants come from
`app.db.roles.ROLE_GRANTS`, which gives `gv_verdict` an allowlist that does not name this table.

Source: issue #849, plan step 3.2 on #798.
Verification: tests/workflow/test_parameter_proposals.py, tests/db/test_roles.py.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.db.roles import ROLE_GRANTS

revision: str = "0057_parameter_proposals"
down_revision: str | None = "0056_text_phrases"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "parameter_proposals"
IMMUTABLE_TABLES: tuple[str, ...] = (TABLE,)

#: Written out rather than derived: a migration records the rule as it stood when it ran. A test
#: holds it to `app.models.parameter_proposals.CLAIMABLE_SOURCES_SQL`.
CLAIMABLE_SOURCES = "'G.C / Client'"


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
        TABLE,
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("package_revision_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("setting_name", sa.String(length=200), nullable=False),
        sa.Column("phrase_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("first_member", sa.Integer(), nullable=False),
        sa.Column("last_member", sa.Integer(), nullable=False),
        sa.Column("claimed_source", sa.String(length=50), nullable=False),
        sa.Column("proposer", sa.String(length=200), nullable=False),
        sa.Column("proposer_version", sa.String(length=100), nullable=False),
        sa.ForeignKeyConstraint(
            ["package_revision_id"], ["package_revisions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["phrase_id"], ["text_phrases.id"], ondelete="RESTRICT"),
        sa.CheckConstraint(
            "first_member >= 0", name="parameter_proposal_first_member_not_negative"
        ),
        sa.CheckConstraint(
            "last_member >= first_member", name="parameter_proposal_span_not_reversed"
        ),
        sa.CheckConstraint(
            f"claimed_source IN ({CLAIMABLE_SOURCES})", name="parameter_proposal_claimed_source"
        ),
        sa.CheckConstraint(
            "setting_name !~ '^[[:space:]]*$'", name="parameter_proposal_setting_not_blank"
        ),
        sa.CheckConstraint(
            "proposer !~ '^[[:space:]]*$'", name="parameter_proposal_proposer_not_blank"
        ),
        sa.CheckConstraint(
            "proposer_version !~ '^[[:space:]]*$'",
            name="parameter_proposal_proposer_version_not_blank",
        ),
    )
    op.create_index(f"ix_{TABLE}_package_revision_id", TABLE, ["package_revision_id"])
    op.create_index(f"ix_{TABLE}_phrase_id", TABLE, ["phrase_id"])

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
    """Drops the table. Every stored pointer goes with it; the phrases and runs are untouched."""
    for table in IMMUTABLE_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
    op.drop_index(f"ix_{TABLE}_phrase_id", table_name=TABLE)
    op.drop_index(f"ix_{TABLE}_package_revision_id", table_name=TABLE)
    op.drop_table(TABLE)
