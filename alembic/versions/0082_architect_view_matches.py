"""The architect file's view index, and which architect view each vendor countertop row is matched to (#1166).

When the architect's drawings are uploaded as their own PDF, type 1 must first know which architect
view shows the same countertop as each vendor view. Two append-only tables:

* `architect_view_index`: one row per view per architect reader run, on an architect-kind file only:
  its page, page-local number and tag (`view-<n>`, or `panel-<n>` for a drawing pasted there), the
  sheet number from the title block, the bubble, title and scale note as printed, its scale as exact
  text, its extent, whether it stands clearly apart from its neighbours, whether its role was
  confirmed, how many dimension rows were read in it, and the picture of it a reviewer and the AIs
  see (its hash and storage key).
* `architect_view_matches`: one record per vendor countertop row per run (`automatic`), a
  reviewer's pick or "none of these" as a new record superseding the latest (`reviewer`), or a
  match remembered from the previous revision of the same item (`carried`). Code's verdict, both
  AIs' picks and the ranked candidate list with their evidence are kept on each record.

Never edited: the same append-only trigger as every other evidence table. Grants are derived from
`ROLE_GRANTS` (append-only: `SELECT`, `INSERT`).

Revision ID: 0082_architect_view_matches
Revises: 0081_evidence_mark_rechecks
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.db.roles import ROLE_GRANTS

revision: str = "0082_architect_view_matches"
down_revision: str | None = "0081_evidence_mark_rechecks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

IMMUTABLE_TABLES = ("architect_view_index", "architect_view_matches")

_MATCHED_STATUSES = "('auto_matched', 'reviewer_confirmed', 'carried_over', 'not_separated')"


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
        "architect_view_index",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("extraction_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("page_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("view_number", sa.Integer(), nullable=False),
        sa.Column("view_tag", sa.String(length=32), nullable=False),
        sa.Column("drawing_view_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("sheet_number", sa.String(length=64), nullable=True),
        sa.Column("bubble", sa.String(length=200), nullable=True),
        sa.Column("title", sa.String(length=300), nullable=True),
        sa.Column("scale_note", sa.String(length=100), nullable=True),
        sa.Column("points_per_inch", sa.String(length=64), nullable=True),
        sa.Column("extent", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("label_box", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("separated", sa.Boolean(), nullable=False),
        sa.Column("role_confirmed", sa.Boolean(), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("picture_sha256", sa.String(length=64), nullable=True),
        sa.Column("picture_storage_key", sa.String(length=500), nullable=True),
        sa.ForeignKeyConstraint(["extraction_run_id"], ["extraction_runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["document_version_id"], ["document_versions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["page_id"], ["pages.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["drawing_view_id"], ["drawing_views.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint(
            "extraction_run_id", "page_id", "view_number", name="uq_architect_view_index_view"
        ),
        sa.CheckConstraint(
            "view_tag ~ '^(view|panel)-[0-9]+$'", name="architect_view_index_tag_shape"
        ),
        sa.CheckConstraint("view_number >= 0", name="architect_view_index_number_not_negative"),
        sa.CheckConstraint("row_count >= 0", name="architect_view_index_rows_not_negative"),
        sa.CheckConstraint(
            "jsonb_typeof(extent) = 'object'", name="architect_view_index_extent_object"
        ),
        sa.CheckConstraint(
            "label_box IS NULL OR jsonb_typeof(label_box) = 'object'",
            name="architect_view_index_label_box_object",
        ),
        sa.CheckConstraint(
            "picture_sha256 IS NULL OR picture_sha256 ~ '^[0-9a-f]{64}$'",
            name="architect_view_index_picture_sha",
        ),
        sa.CheckConstraint(
            "(picture_sha256 IS NULL) = (picture_storage_key IS NULL)",
            name="architect_view_index_picture_pair",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_architect_view_index_document_version_id",
        "architect_view_index",
        ["document_version_id"],
    )
    op.create_index("ix_architect_view_index_page_id", "architect_view_index", ["page_id"])

    op.create_table(
        "architect_view_matches",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("package_revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("vendor_page_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("row_anchor_candidate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("extraction_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("matched_view_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("code_verdict", sa.String(length=24), nullable=True),
        sa.Column("code_pick_view_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("ai_picks", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("candidates", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("vendor_title", sa.String(length=300), nullable=True),
        sa.Column("vendor_references", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("reasons", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("supersedes_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("carried_from_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("decided_by", sa.String(length=200), nullable=True),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.ForeignKeyConstraint(
            ["package_revision_id"], ["package_revisions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["vendor_page_id"], ["pages.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["row_anchor_candidate_id"], ["observation_candidates.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["extraction_run_id"], ["extraction_runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["matched_view_id"], ["architect_view_index.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["code_pick_view_id"], ["architect_view_index.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["carried_from_id"], ["architect_view_matches.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_id", "row_anchor_candidate_id"],
            ["architect_view_matches.id", "architect_view_matches.row_anchor_candidate_id"],
            name="fk_architect_view_match_supersedes_same_row",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("id", "row_anchor_candidate_id", name="uq_architect_view_match_id_row"),
        sa.CheckConstraint(
            "source IN ('automatic', 'reviewer', 'carried')", name="architect_view_match_source"
        ),
        sa.CheckConstraint(
            "status IN ('auto_matched', 'needs_reviewer', 'reviewer_confirmed', 'none_matches', "
            "'carried_over', 'not_separated', 'no_candidates')",
            name="architect_view_match_status",
        ),
        sa.CheckConstraint(
            "source <> 'reviewer' OR status IN ('reviewer_confirmed', 'none_matches', "
            "'not_separated')",
            name="architect_view_match_reviewer_status",
        ),
        sa.CheckConstraint(
            "(source = 'carried') = (status = 'carried_over')",
            name="architect_view_match_carried_status",
        ),
        sa.CheckConstraint(
            "source <> 'automatic' OR status IN ('auto_matched', 'needs_reviewer', "
            "'not_separated', 'no_candidates')",
            name="architect_view_match_automatic_status",
        ),
        sa.CheckConstraint(
            f"(matched_view_id IS NOT NULL) = (status IN {_MATCHED_STATUSES})",
            name="architect_view_match_matched_view",
        ),
        sa.CheckConstraint(
            "code_verdict IS NULL OR code_verdict IN ('reference', 'geometry_clear', "
            "'geometry_tie', 'geometry_none', 'no_geometry')",
            name="architect_view_match_code_verdict",
        ),
        sa.CheckConstraint(
            "(source = 'reviewer') = (extraction_run_id IS NULL)",
            name="architect_view_match_automatic_run",
        ),
        sa.CheckConstraint(
            "(source = 'reviewer') = (decided_by IS NOT NULL)",
            name="architect_view_match_reviewer_named",
        ),
        sa.CheckConstraint(
            "(source = 'carried') = (carried_from_id IS NOT NULL)",
            name="architect_view_match_carried_from",
        ),
        sa.CheckConstraint(
            "decided_by IS NULL OR decided_by !~ '^[[:space:]]*$'",
            name="architect_view_match_actor_not_blank",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(ai_picks) = 'array'", name="architect_view_match_ai_picks_array"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(candidates) = 'array'", name="architect_view_match_candidates_array"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(vendor_references) = 'array'",
            name="architect_view_match_references_array",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(reasons) = 'array'", name="architect_view_match_reasons_array"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(details) = 'object'", name="architect_view_match_details_object"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_architect_view_matches_package_revision_id",
        "architect_view_matches",
        ["package_revision_id"],
    )
    op.create_index(
        "ix_architect_view_matches_row_anchor_candidate_id",
        "architect_view_matches",
        ["row_anchor_candidate_id"],
    )
    op.create_index(
        "uq_architect_view_match_root",
        "architect_view_matches",
        ["row_anchor_candidate_id"],
        unique=True,
        postgresql_where=sa.text("supersedes_id IS NULL"),
    )
    op.create_index(
        "uq_architect_view_match_superseded_once",
        "architect_view_matches",
        ["supersedes_id"],
        unique=True,
        postgresql_where=sa.text("supersedes_id IS NOT NULL"),
    )
    for table in IMMUTABLE_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_append_only "
            f"BEFORE UPDATE OR DELETE ON {table} "
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
    connection = op.get_bind()
    for table in IMMUTABLE_TABLES:
        count = connection.scalar(sa.text(f"SELECT count(*) FROM {table}"))
        if count:
            raise RuntimeError(
                f"Cannot downgrade the architect view matches: {table} holds stored records "
                "(the view index, automatic matches and reviewers' picks) that would be lost. "
                "Preserve these records."
            )
    for table in reversed(IMMUTABLE_TABLES):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
    op.drop_index("uq_architect_view_match_superseded_once", table_name="architect_view_matches")
    op.drop_index("uq_architect_view_match_root", table_name="architect_view_matches")
    op.drop_index(
        "ix_architect_view_matches_row_anchor_candidate_id", table_name="architect_view_matches"
    )
    op.drop_index(
        "ix_architect_view_matches_package_revision_id", table_name="architect_view_matches"
    )
    op.drop_table("architect_view_matches")
    op.drop_index("ix_architect_view_index_page_id", table_name="architect_view_index")
    op.drop_index("ix_architect_view_index_document_version_id", table_name="architect_view_index")
    op.drop_table("architect_view_index")
