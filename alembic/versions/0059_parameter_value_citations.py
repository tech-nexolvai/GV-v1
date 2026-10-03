"""Record the passage a confirmed setting was typed from, blind (#866).

Revision ID: 0059_parameter_value_citations
Revises: 0058_drawing_parts

Step 3.3 of the plan on #798. A reviewer is shown the passage in the architect's drawing that states
a setting, types the number without being shown it, and the value is saved only if it matches. Two
append-only tables keep what it was typed against:

* `parameter_value_citations` — one row per stored `parameter_values` row confirmed this way: the
  pointer the reviewer was shown, the document version and the sha256 of its bytes, and the page.
* `parameter_value_citation_runs` — the runs of the passage that hold the number, in order.

**Copied, not only linked.** A pointer is decided again whenever it is read, and stops holding if a
drawing's role is later corrected. What a person typed against is a fact about the moment they
typed it, so it is written down as it was.

**Granted like every other table, and not to the verdict.** The grants come from
`app.db.roles.ROLE_GRANTS`, which gives `gv_verdict` an allowlist that names neither table.

Source: issue #866, plan step 3.3 on #798. Verification: tests/api/test_setting_citations.py,
tests/app/test_migrations_roundtrip.py, tests/db/test_roles.py.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.db.roles import ROLE_GRANTS

revision: str = "0059_parameter_value_citations"
down_revision: str | None = "0058_drawing_parts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CITATIONS = "parameter_value_citations"
RUNS = "parameter_value_citation_runs"
IMMUTABLE_TABLES: tuple[str, ...] = (CITATIONS, RUNS)


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
        CITATIONS,
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("parameter_value_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("parameter_proposal_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("document_version_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("document_sha256", sa.String(length=64), nullable=False),
        sa.Column("page_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["parameter_value_id"], ["parameter_values.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["parameter_proposal_id"], ["parameter_proposals.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"], ["document_versions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["page_id"], ["pages.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("parameter_value_id", name="uq_parameter_value_citations_value"),
        sa.CheckConstraint(
            "document_sha256 ~ '^[0-9a-f]{64}$'", name="parameter_value_citation_sha256"
        ),
    )
    op.create_index(f"ix_{CITATIONS}_parameter_proposal_id", CITATIONS, ["parameter_proposal_id"])
    op.create_index(f"ix_{CITATIONS}_document_version_id", CITATIONS, ["document_version_id"])
    op.create_index(f"ix_{CITATIONS}_page_id", CITATIONS, ["page_id"])

    op.create_table(
        RUNS,
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("citation_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("candidate_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["citation_id"], [f"{CITATIONS}.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["observation_candidates.id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint("position >= 0", name="parameter_value_citation_run_position"),
        sa.UniqueConstraint(
            "citation_id", "position", name="uq_parameter_value_citation_runs_slot"
        ),
        sa.UniqueConstraint(
            "citation_id", "candidate_id", name="uq_parameter_value_citation_runs_candidate"
        ),
    )
    op.create_index(f"ix_{RUNS}_citation_id", RUNS, ["citation_id"])
    op.create_index(f"ix_{RUNS}_candidate_id", RUNS, ["candidate_id"])

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
    """Drops both tables. Every stored citation goes with them; the stored values, the pointers,
    the documents, the pages and the runs are untouched."""
    for table in IMMUTABLE_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
    op.drop_index(f"ix_{RUNS}_candidate_id", table_name=RUNS)
    op.drop_index(f"ix_{RUNS}_citation_id", table_name=RUNS)
    op.drop_table(RUNS)
    op.drop_index(f"ix_{CITATIONS}_page_id", table_name=CITATIONS)
    op.drop_index(f"ix_{CITATIONS}_document_version_id", table_name=CITATIONS)
    op.drop_index(f"ix_{CITATIONS}_parameter_proposal_id", table_name=CITATIONS)
    op.drop_table(CITATIONS)
