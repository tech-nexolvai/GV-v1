"""Tie a plan or section view of a vendor drawing to the countertop it shows (#1290).

Revision ID: 0098_item_view_links
Revises: 0097_not_part_reason

**Why.** The depth and sink reader (#1265) reads a countertop's depth, sink cut-out and front offset
on a plan or a section view. Bala's countertop (phase 4) came from the front view's countertop row,
so nothing yet said which countertop a plan view shows. Without that, a depth read on one
countertop's plan could fill another countertop's check.

**`item_view_links`**, one row per view tied to one of Bala's countertop suggestions:

* `part_proposal_id` (Bala's countertop suggestion), `page_id`, `region` (the view's outline in
  stored page space, refused unless it says so), `view_kind` (`plan`, `section`, `side`,
  `elevation` or `detail`), `heading_text` (the vendor's heading, or `NULL`), `judged_by`
  (`code+ais` or `person`) and `extraction_run_id` (the reader's run; required unless a person
  linked it);
* **unique per countertop, page and region** (`md5(region::text)`), so asking again finds the
  link already there.

**Append-only**, with the same trigger as every evidence table; a person's correction is a new
row. Grants are derived from `ROLE_GRANTS` (`SELECT`, `INSERT`). **A downgrade with links stored
refuses**: they say which countertop each reading belongs to, and the table's trigger forbids
clearing them.

The orchestrator approved this migration for #1290 (phase 5, PR 4).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.db.roles import ROLE_GRANTS

revision: str = "0098_item_view_links"
down_revision: str | None = "0097_not_part_reason"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

IMMUTABLE_TABLES = ("item_view_links",)

#: Written out here, not imported from the model: a migration describes one fixed state.
#: `tests/db/test_item_view_links_migration.py` compares them with `ITEM_VIEW_KINDS`/`_JUDGES`.
KINDS = "'plan', 'section', 'side', 'elevation', 'detail'"
JUDGES = "'code+ais', 'person'"


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
    # Written literally: `tests/app/test_migration_matches_models.py` reads migrations as text.
    op.create_table(
        "item_view_links",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("part_proposal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("page_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("region", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("view_kind", sa.String(length=16), nullable=False),
        sa.Column("heading_text", sa.String(length=300), nullable=True),
        sa.Column("judged_by", sa.String(length=16), nullable=False),
        sa.Column("extraction_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(["part_proposal_id"], ["part_proposals.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["page_id"], ["pages.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["extraction_run_id"], ["extraction_runs.id"], ondelete="RESTRICT"),
        sa.CheckConstraint(f"view_kind IN ({KINDS})", name="item_view_link_kind"),
        sa.CheckConstraint(f"judged_by IN ({JUDGES})", name="item_view_link_judge"),
        sa.CheckConstraint(
            "judged_by = 'person' OR extraction_run_id IS NOT NULL", name="item_view_link_run"
        ),
        sa.CheckConstraint(
            """region @> '{"space": "stored"}'::jsonb""", name="item_view_link_region_stored"
        ),
        sa.CheckConstraint(
            "heading_text IS NULL OR heading_text !~ '^[[:space:]]*$'",
            name="item_view_link_heading_not_blank",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_item_view_links_part_proposal_id", "item_view_links", ["part_proposal_id"]
    )
    op.create_index(
        "ix_item_view_links_extraction_run_id", "item_view_links", ["extraction_run_id"]
    )
    op.create_index(
        "uq_item_view_links_item_page_region",
        "item_view_links",
        ["part_proposal_id", "page_id", sa.text("md5(region::text)")],
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
    """Drop the links, refused while any is stored."""
    stored = op.get_bind().scalar(sa.text("SELECT count(*) FROM item_view_links"))
    if stored:
        raise RuntimeError(
            f"Cannot downgrade the countertop view links: {stored} link(s) say which countertop a "
            "plan or section view shows, and item_view_links is append-only. Preserve these records."
        )
    for table in IMMUTABLE_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
    op.drop_index("uq_item_view_links_item_page_region", table_name="item_view_links")
    op.drop_index("ix_item_view_links_extraction_run_id", table_name="item_view_links")
    op.drop_index("ix_item_view_links_part_proposal_id", table_name="item_view_links")
    op.drop_table("item_view_links")
