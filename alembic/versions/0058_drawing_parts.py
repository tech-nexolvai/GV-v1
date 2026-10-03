"""Record the drawing's parts: suggested by the computer, confirmed by a person (#852).

Revision ID: 0058_drawing_parts
Revises: 0057_parameter_proposals

Four append-only tables, step 2 of the plan on #748:

* `part_proposals` — what the computer suggests a box on an elevation is: a cabinet, a filler or a
  countertop, with its extent, the code read on it and the reading it came from, the dimension line
  that defined it, what suggested it and why.
* `part_confirmations` — a person's decision on one suggestion, confirmed or withdrawn. A
  confirmation names the `drawing_items` row it made; nothing else makes one.
* `countertop_runs` — which confirmed parts sit beneath a confirmed countertop, one row per member,
  in order, with the edge tolerance used and who confirmed the run.
* `reading_parts` — which confirmed part one canonical observation measures.

**One current decision per suggestion, one live link per reading, held by the schema.** A decision or
a link names the row it replaces. A partial unique index allows one first row per suggestion or
reading, a unique `supersedes_id` lets each row be replaced once, and a two-column foreign key keeps a
replacement on the same suggestion or reading. Together they leave at most one row of each that
nothing replaces.

**Granted like every other table, and not to the verdict.** The grants come from
`app.db.roles.ROLE_GRANTS`, which gives `gv_verdict` an allowlist that names none of the four.

Source: issue #852; #748 plan, step 2. Verification: tests/db/test_drawing_models.py,
tests/app/test_migrations_roundtrip.py, tests/db/test_roles.py.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op
from app.db.roles import ROLE_GRANTS

revision: str = "0058_drawing_parts"
down_revision: str | None = "0057_parameter_proposals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

IMMUTABLE_TABLES: tuple[str, ...] = (
    "part_proposals",
    "part_confirmations",
    "countertop_runs",
    "reading_parts",
)

#: Written out rather than imported: a migration records the vocabulary as it stood when it ran.
KINDS = "'cabinet', 'filler', 'countertop'"
DECISIONS = "'confirmed', 'withdrawn'"
NOT_BLANK = "!~ '^[[:space:]]*$'"


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
        "part_proposals",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("drawing_view_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("extent", JSONB(), nullable=False),
        sa.Column("code_as_printed", sa.String(length=200), nullable=True),
        sa.Column("code_candidate_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("defining_line", JSONB(), nullable=True),
        sa.Column("source", sa.String(length=100), nullable=False),
        sa.Column("source_version", sa.String(length=50), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.ForeignKeyConstraint(["drawing_view_id"], ["drawing_views.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["code_candidate_id"], ["observation_candidates.id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint(f"kind IN ({KINDS})", name="part_proposal_kind"),
        sa.CheckConstraint(
            """extent @> '{"space": "stored"}'::jsonb""", name="part_proposal_extent_stored"
        ),
        sa.CheckConstraint(
            """defining_line IS NULL OR defining_line @> '{"space": "stored"}'::jsonb""",
            name="part_proposal_line_stored",
        ),
        sa.CheckConstraint(
            "(code_as_printed IS NULL) = (code_candidate_id IS NULL)",
            name="part_proposal_code_has_reading",
        ),
        sa.CheckConstraint(
            f"code_as_printed IS NULL OR code_as_printed {NOT_BLANK}",
            name="part_proposal_code_not_blank",
        ),
        sa.CheckConstraint(f"source {NOT_BLANK}", name="part_proposal_source_not_blank"),
        sa.CheckConstraint(f"source_version {NOT_BLANK}", name="part_proposal_version_not_blank"),
        sa.CheckConstraint(f"reason {NOT_BLANK}", name="part_proposal_reason_not_blank"),
    )
    op.create_index("ix_part_proposals_drawing_view_id", "part_proposals", ["drawing_view_id"])
    op.create_index("ix_part_proposals_code_candidate_id", "part_proposals", ["code_candidate_id"])

    op.create_table(
        "part_confirmations",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("part_proposal_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("supersedes_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=True),
        sa.Column("code_as_printed", sa.String(length=200), nullable=True),
        sa.Column("drawing_item_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("confirmed_by", sa.String(length=200), nullable=False),
        sa.ForeignKeyConstraint(["part_proposal_id"], ["part_proposals.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["drawing_item_id"], ["drawing_items.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["supersedes_id", "part_proposal_id"],
            ["part_confirmations.id", "part_confirmations.part_proposal_id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(f"decision IN ({DECISIONS})", name="part_confirmation_decision"),
        sa.CheckConstraint(f"kind IS NULL OR kind IN ({KINDS})", name="part_confirmation_kind"),
        sa.CheckConstraint(
            "(decision = 'confirmed' AND kind IS NOT NULL AND drawing_item_id IS NOT NULL)"
            " OR (decision = 'withdrawn' AND kind IS NULL AND code_as_printed IS NULL"
            " AND drawing_item_id IS NULL)",
            name="part_confirmation_decision_shape",
        ),
        sa.CheckConstraint(
            f"code_as_printed IS NULL OR code_as_printed {NOT_BLANK}",
            name="part_confirmation_code_not_blank",
        ),
        sa.CheckConstraint(f"confirmed_by {NOT_BLANK}", name="part_confirmation_actor_not_blank"),
        sa.UniqueConstraint("drawing_item_id", name="uq_part_confirmations_drawing_item_id"),
        sa.UniqueConstraint("id", "part_proposal_id", name="uq_part_confirmations_id_proposal"),
        sa.UniqueConstraint("supersedes_id", name="uq_part_confirmations_supersedes_id"),
    )
    op.create_index(
        "ix_part_confirmations_part_proposal_id", "part_confirmations", ["part_proposal_id"]
    )
    op.create_index(
        "ix_part_confirmations_first_decision",
        "part_confirmations",
        ["part_proposal_id"],
        unique=True,
        postgresql_where=sa.text("supersedes_id IS NULL"),
    )

    op.create_table(
        "countertop_runs",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("run_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("countertop_item_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("member_item_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("signal", sa.String(length=500), nullable=False),
        sa.Column("proposal_source", sa.String(length=100), nullable=False),
        sa.Column("edge_tolerance", sa.Numeric(), nullable=False),
        sa.Column("confirmed_by", sa.String(length=200), nullable=False),
        sa.ForeignKeyConstraint(["countertop_item_id"], ["drawing_items.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["member_item_id"], ["drawing_items.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("position >= 0", name="countertop_run_position_not_negative"),
        sa.CheckConstraint(
            "member_item_id <> countertop_item_id", name="countertop_run_member_not_countertop"
        ),
        sa.CheckConstraint(
            "edge_tolerance >= 0 AND edge_tolerance < 'Infinity'::numeric",
            name="countertop_run_tolerance_finite",
        ),
        sa.CheckConstraint(f"signal {NOT_BLANK}", name="countertop_run_signal_not_blank"),
        sa.CheckConstraint(f"proposal_source {NOT_BLANK}", name="countertop_run_source_not_blank"),
        sa.CheckConstraint(f"confirmed_by {NOT_BLANK}", name="countertop_run_actor_not_blank"),
        sa.UniqueConstraint("run_id", "position", name="uq_countertop_runs_slot"),
        sa.UniqueConstraint("run_id", "member_item_id", name="uq_countertop_runs_member"),
    )
    op.create_index("ix_countertop_runs_run_id", "countertop_runs", ["run_id"])
    op.create_index(
        "ix_countertop_runs_countertop_item_id", "countertop_runs", ["countertop_item_id"]
    )
    op.create_index("ix_countertop_runs_member_item_id", "countertop_runs", ["member_item_id"])

    op.create_table(
        "reading_parts",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("canonical_observation_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("drawing_item_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("supersedes_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("signal", sa.String(length=500), nullable=False),
        sa.Column("confirmed_by", sa.String(length=200), nullable=False),
        sa.ForeignKeyConstraint(
            ["canonical_observation_id"], ["canonical_observations.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["drawing_item_id"], ["drawing_items.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["supersedes_id", "canonical_observation_id"],
            ["reading_parts.id", "reading_parts.canonical_observation_id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "drawing_item_id IS NOT NULL OR supersedes_id IS NOT NULL",
            name="reading_part_withdraws_a_link",
        ),
        sa.CheckConstraint(f"signal {NOT_BLANK}", name="reading_part_signal_not_blank"),
        sa.CheckConstraint(f"confirmed_by {NOT_BLANK}", name="reading_part_actor_not_blank"),
        sa.UniqueConstraint(
            "id", "canonical_observation_id", name="uq_reading_parts_id_observation"
        ),
        sa.UniqueConstraint("supersedes_id", name="uq_reading_parts_supersedes_id"),
    )
    op.create_index(
        "ix_reading_parts_canonical_observation_id", "reading_parts", ["canonical_observation_id"]
    )
    op.create_index("ix_reading_parts_drawing_item_id", "reading_parts", ["drawing_item_id"])
    op.create_index(
        "ix_reading_parts_first_link",
        "reading_parts",
        ["canonical_observation_id"],
        unique=True,
        postgresql_where=sa.text("supersedes_id IS NULL"),
    )

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
    op.drop_index("ix_reading_parts_first_link", table_name="reading_parts")
    op.drop_index("ix_reading_parts_drawing_item_id", table_name="reading_parts")
    op.drop_index("ix_reading_parts_canonical_observation_id", table_name="reading_parts")
    op.drop_table("reading_parts")
    op.drop_index("ix_countertop_runs_member_item_id", table_name="countertop_runs")
    op.drop_index("ix_countertop_runs_countertop_item_id", table_name="countertop_runs")
    op.drop_index("ix_countertop_runs_run_id", table_name="countertop_runs")
    op.drop_table("countertop_runs")
    op.drop_index("ix_part_confirmations_first_decision", table_name="part_confirmations")
    op.drop_index("ix_part_confirmations_part_proposal_id", table_name="part_confirmations")
    op.drop_table("part_confirmations")
    op.drop_index("ix_part_proposals_code_candidate_id", table_name="part_proposals")
    op.drop_index("ix_part_proposals_drawing_view_id", table_name="part_proposals")
    op.drop_table("part_proposals")
