"""A reviewer's one click on each page listed as having no countertop (#1191).

Revision ID: 0090_no_countertop_answers
Revises: 0089_revision_carry_forward

Every AI saying "no countertop on this page" no longer lets a page pass silently: the reviewer
confirms it ("Confirm: no countertop here") or says there is one ("There is a countertop", which
makes the page a blocking item like a split page). Sign-off waits until every listed page has an
answer. One append-only table, `no_countertop_answers`: the revision the click was made on, the
reader's row-choice record it answers, the page, its number, its content hash and the reading's
own answer (to find the identical page on another reading), the answer, who and when.

Never edited: the same append-only trigger as every other evidence table. Grants are derived from
`ROLE_GRANTS` (append-only: `SELECT`, `INSERT`). A downgrade with answers stored refuses: they are
part of the review's record.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.db.roles import ROLE_GRANTS

revision: str = "0090_no_countertop_answers"
down_revision: str | None = "0089_revision_carry_forward"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

IMMUTABLE_TABLES = ("no_countertop_answers",)


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
        "no_countertop_answers",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("package_revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("row_choice_candidate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("page_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("page_content_hash", sa.String(length=64), nullable=False),
        sa.Column("reading_answer", sa.String(length=1000), nullable=False),
        sa.Column("answer", sa.String(length=16), nullable=False),
        sa.Column("answered_by", sa.String(length=200), nullable=False),
        sa.ForeignKeyConstraint(
            ["package_revision_id"], ["package_revisions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["row_choice_candidate_id"], ["observation_candidates.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["page_id"], ["pages.id"], ondelete="RESTRICT"),
        sa.CheckConstraint(
            "answer IN ('no_countertop', 'countertop')", name="no_countertop_answer_value"
        ),
        sa.CheckConstraint("page_number >= 1", name="no_countertop_answer_page_number"),
        sa.CheckConstraint(
            "answered_by !~ '^[[:space:]]*$'", name="no_countertop_answer_actor_not_blank"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_no_countertop_answers_package_revision_id",
        "no_countertop_answers",
        ["package_revision_id"],
    )
    op.create_index(
        "ix_no_countertop_answers_row_choice_candidate_id",
        "no_countertop_answers",
        ["row_choice_candidate_id"],
    )
    op.create_index(
        "ix_no_countertop_answers_page_content_hash",
        "no_countertop_answers",
        ["page_content_hash"],
    )
    op.execute(
        "CREATE TRIGGER no_countertop_answers_append_only "
        "BEFORE UPDATE OR DELETE ON no_countertop_answers "
        "FOR EACH ROW EXECUTE FUNCTION gv_reject_mutation()"
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
    stored = op.get_bind().scalar(sa.text("SELECT count(*) FROM no_countertop_answers"))
    if stored:
        raise RuntimeError(
            f"Cannot downgrade the no-countertop answers: {stored} reviewer answer(s) are stored "
            "and would be lost. Preserve these records."
        )
    op.execute("DROP TRIGGER IF EXISTS no_countertop_answers_append_only ON no_countertop_answers")
    op.drop_index("ix_no_countertop_answers_page_content_hash", table_name="no_countertop_answers")
    op.drop_index(
        "ix_no_countertop_answers_row_choice_candidate_id", table_name="no_countertop_answers"
    )
    op.drop_index(
        "ix_no_countertop_answers_package_revision_id", table_name="no_countertop_answers"
    )
    op.drop_table("no_countertop_answers")
