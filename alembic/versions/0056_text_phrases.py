"""Keep a package's own words as passages a search can point at (#836).

Revision ID: 0056_text_phrases
Revises: 0055_setting_source_reference

The readers store text one run at a time, and a run is often a single word. Two append-only tables
hold the passages built from those runs: `text_phrases`, one row per passage, and
`text_phrase_members`, the runs it was built from in order. A search over them answers with phrase
ids and run ids, never with text or a number.

**Two GIN indexes on the passage's text.** One over `to_tsvector('simple', content)` for words, and
one with `public.gin_trgm_ops` for fragments a parser cannot split. The operator class is qualified
because the extension lives in `public` (0034), and an unqualified name resolves through whatever
`search_path` the migrating session has.

**Granted like every other table, and not to the verdict.** The grants come from
`app.db.roles.ROLE_GRANTS`, which gives `gv_verdict` an allowlist that names neither table.

Source: issue #836. Verification: tests/retrieval/test_package_text.py, tests/db/test_roles.py.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.db.roles import ROLE_GRANTS

revision: str = "0056_text_phrases"
down_revision: str | None = "0055_setting_source_reference"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PHRASES = "text_phrases"
MEMBERS = "text_phrase_members"
IMMUTABLE_TABLES: tuple[str, ...] = (PHRASES, MEMBERS)

#: Written out rather than imported: a migration records the vocabulary as it stood when it ran.
TAGS = "'markup', 'dimension', 'note'"

#: Where the extension lives since 0034, restated for the same reason.
EXTENSION_SCHEMA = "public"


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
        PHRASES,
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("package_revision_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("build_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("build_digest", sa.String(length=64), nullable=False),
        sa.Column("grouping", sa.String(length=200), nullable=False),
        sa.Column("page_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("extraction_run_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("tag", sa.String(length=16), nullable=False),
        sa.Column("content", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["package_revision_id"], ["package_revisions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["page_id"], ["pages.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["extraction_run_id"], ["extraction_runs.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("position >= 0", name="text_phrase_position_not_negative"),
        sa.CheckConstraint(f"tag IN ({TAGS})", name="text_phrase_tag"),
        sa.CheckConstraint("content !~ '^[[:space:]]*$'", name="text_phrase_content_not_blank"),
        sa.CheckConstraint("grouping !~ '^[[:space:]]*$'", name="text_phrase_grouping_not_blank"),
        sa.CheckConstraint("build_digest ~ '^[0-9a-f]{64}$'", name="text_phrase_build_digest"),
        sa.UniqueConstraint("build_id", "position", name="uq_text_phrases_build_position"),
    )
    op.create_index(f"ix_{PHRASES}_package_revision_id", PHRASES, ["package_revision_id"])
    op.create_index(f"ix_{PHRASES}_build_id", PHRASES, ["build_id"])
    op.create_index(f"ix_{PHRASES}_page_id", PHRASES, ["page_id"])
    op.create_index(
        "ix_text_phrases_content_words",
        PHRASES,
        [sa.text("to_tsvector('simple'::regconfig, content)")],
        postgresql_using="gin",
    )
    op.execute(
        "CREATE INDEX ix_text_phrases_content_trigram ON text_phrases "
        f"USING gin (content {EXTENSION_SCHEMA}.gin_trgm_ops)"
    )

    op.create_table(
        MEMBERS,
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("phrase_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("candidate_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["phrase_id"], ["text_phrases.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["observation_candidates.id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint("position >= 0", name="text_phrase_member_position_not_negative"),
        sa.UniqueConstraint("phrase_id", "position", name="uq_text_phrase_members_slot"),
        sa.UniqueConstraint("phrase_id", "candidate_id", name="uq_text_phrase_members_candidate"),
    )
    op.create_index(f"ix_{MEMBERS}_phrase_id", MEMBERS, ["phrase_id"])
    op.create_index(f"ix_{MEMBERS}_candidate_id", MEMBERS, ["candidate_id"])

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
    op.drop_index(f"ix_{MEMBERS}_candidate_id", table_name=MEMBERS)
    op.drop_index(f"ix_{MEMBERS}_phrase_id", table_name=MEMBERS)
    op.drop_table(MEMBERS)
    op.execute("DROP INDEX IF EXISTS ix_text_phrases_content_trigram")
    op.drop_index("ix_text_phrases_content_words", table_name=PHRASES)
    op.drop_index(f"ix_{PHRASES}_page_id", table_name=PHRASES)
    op.drop_index(f"ix_{PHRASES}_build_id", table_name=PHRASES)
    op.drop_index(f"ix_{PHRASES}_package_revision_id", table_name=PHRASES)
    op.drop_table(PHRASES)
