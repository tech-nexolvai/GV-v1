"""Record a picture of every suggested part (#897).

Revision ID: 0061_part_pictures
Revises: 0060_countertop_run_decisions

The admin's decision of 2026-10-04: every suggested part gets its own picture on the Measure page,
cut by the worker that suggests the parts and recorded in a new small table. Until now the only
picture a suggestion could show was the crop of the reading its code came from, so a part with no
code showed none. A part's picture cannot be an `evidence_artifacts` row, which needs a reading as
its owner, so one append-only table holds it:

* `part_pictures` — for one suggestion, where its picture is stored and the digest of its bytes,
  with the margin and resolution it was cut at. **A pointer and a digest, never the image.**

**One picture per suggestion, held by the schema**, and append-only: the first picture cut stands.

**Granted like every other table, and not to the verdict.** A picture is for a person's eyes, and
nothing reads a value from it. The grants come from `app.db.roles.ROLE_GRANTS`, which gives
`gv_verdict` an allowlist that does not name it.

Source: issue #897. Verification: tests/db/test_drawing_models.py,
tests/app/test_migrations_roundtrip.py, tests/db/test_roles.py, tests/db/test_append_only.py.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.db.roles import ROLE_GRANTS

revision: str = "0061_part_pictures"
down_revision: str | None = "0060_countertop_run_decisions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PICTURES_TABLE = "part_pictures"
IMMUTABLE_TABLES: tuple[str, ...] = (PICTURES_TABLE,)

#: Written out rather than imported: a migration records the rules as they stood when it ran.
NOT_BLANK = "!~ '^[[:space:]]*$'"
SHA256_PATTERN = "^[0-9a-f]{64}$"


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
        PICTURES_TABLE,
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("part_proposal_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("storage_key", sa.String(length=1000), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("media_type", sa.String(length=200), nullable=False),
        sa.Column("margin_pt", sa.Numeric(), nullable=False),
        sa.Column("dpi", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["part_proposal_id"], ["part_proposals.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("part_proposal_id", name="uq_part_pictures_part_proposal_id"),
        sa.CheckConstraint(f"storage_key {NOT_BLANK}", name="part_picture_key_not_blank"),
        sa.CheckConstraint(f"sha256 ~ '{SHA256_PATTERN}'", name="part_picture_sha256"),
        sa.CheckConstraint(f"media_type {NOT_BLANK}", name="part_picture_media_not_blank"),
        sa.CheckConstraint(
            "margin_pt > 0 AND margin_pt < 'Infinity'::numeric", name="part_picture_margin"
        ),
        sa.CheckConstraint("dpi > 0", name="part_picture_dpi"),
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
    """Drops the pictures' records. The stored images stay in the object store, which nothing
    deletes; the suggestions, the parts and every decision are untouched."""
    for table in IMMUTABLE_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
    op.drop_table(PICTURES_TABLE)
