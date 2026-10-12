"""The sink a project uses: its name and note, beside the numbers it saved (#1181).

Revision ID: 0086_project_sinks
Revises: 0085_document_version_file_name

One append-only row per sink entry: the project, the sink's model name as the reviewer typed it, an
optional note on where its size came from, who saved it, and the project settings version
(`parameter_sets`) its inside width and depth were saved in. The numbers themselves live in that
settings version, the layer every check already reads; this table holds only what a number cannot.

The same append-only trigger as every other record table, and the roles' grants derived from
`ROLE_GRANTS` (append-only: `SELECT`, `INSERT`). A downgrade with entries stored refuses: they are the
record of which sink a project's checks were run against.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.db.roles import ROLE_GRANTS

revision: str = "0086_project_sinks"
down_revision: str | None = "0085_document_version_file_name"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

IMMUTABLE_TABLES = ("project_sinks",)


def upgrade() -> None:
    op.create_table(
        "project_sinks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("model_name", sa.String(length=80), nullable=False),
        sa.Column("source_note", sa.String(length=100), nullable=True),
        sa.Column("parameter_set_id", sa.Uuid(), nullable=False),
        sa.Column("set_by", sa.String(length=200), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["parameter_set_id"], ["parameter_sets.id"], ondelete="RESTRICT"),
        sa.CheckConstraint(
            "model_name !~ '^[[:space:]]*$'", name="project_sink_model_name_present"
        ),
        sa.CheckConstraint(
            "source_note IS NULL OR source_note !~ '^[[:space:]]*$'",
            name="project_sink_source_note_not_blank",
        ),
        sa.CheckConstraint("set_by <> ''", name="project_sink_set_by_present"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("project_id", "parameter_set_id"):
        op.create_index(f"ix_project_sinks_{column}", "project_sinks", [column])
    for table in IMMUTABLE_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION gv_reject_mutation()"
        )
    for role, grants in ROLE_GRANTS.items():
        for table, privileges in grants.privileges.items():
            if table in IMMUTABLE_TABLES:
                op.execute(f'GRANT {", ".join(privileges)} ON TABLE "{table}" TO {role.value}')


def downgrade() -> None:
    count = op.get_bind().scalar(sa.text("SELECT count(*) FROM project_sinks"))
    if count:
        raise RuntimeError(
            f"Cannot downgrade project sinks: {count} entry(ies) record which sink a project's "
            "checks were run against. Preserve these records."
        )
    for table in IMMUTABLE_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
    for column in ("project_id", "parameter_set_id"):
        op.drop_index(f"ix_project_sinks_{column}", table_name="project_sinks")
    op.drop_table("project_sinks")
