"""The drawing set's memory across chats: set notes (#1548).

Revision ID: 0110_assistant_set_notes
Revises: 0109_assistant_chat_interrupted

**Why.** Each chat on a drawing set used to start from nothing: what the reviewer decided in one chat
(agree / not a problem, with their name), what the AI established ("the 3'-6" on page 3 measures the
blocking", "the ID drawing governs over the brand prototype") was lost to the next chat. One table,
`assistant_set_notes`: one row per fact (drawing set, the note, its page if any, who established it:
`ai` or a reviewer's `decision`, the chat and answer it came from, who and when). Every chat on the
set starts with the newest notes.

Append-only, like every other record table (the same trigger, and the roles' grants derived from
`ROLE_GRANTS`): a fact that changed is a newer row. A downgrade with rows stored refuses.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.db.roles import ROLE_GRANTS

revision: str = "0110_assistant_set_notes"
down_revision: str | None = "0109_assistant_chat_interrupted"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOTES = "assistant_set_notes"
IMMUTABLE_TABLES = (NOTES,)
_INDEXED = ("package_id", "chat_id")

#: Written out, not imported from the model: a migration describes one fixed state.
_SOURCES = ("ai", "decision")


def upgrade() -> None:
    listed = ", ".join(f"'{value}'" for value in _SOURCES)
    op.create_table(
        NOTES,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("package_id", sa.Uuid(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("chat_id", sa.Uuid(), nullable=True),
        sa.Column("message_id", sa.Uuid(), nullable=True),
        sa.Column("created_by", sa.String(length=200), nullable=False),
        sa.ForeignKeyConstraint(["package_id"], ["packages.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["chat_id"], ["assistant_chats.id"], ondelete="RESTRICT"),
        sa.CheckConstraint(f"source IN ({listed})", name="assistant_set_note_source"),
        sa.CheckConstraint("note <> ''", name="assistant_set_note_text"),
        sa.CheckConstraint("page IS NULL OR page >= 1", name="assistant_set_note_page"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in _INDEXED:
        op.create_index(f"ix_{NOTES}_{column}", NOTES, [column])
    op.execute(
        f"CREATE TRIGGER {NOTES}_append_only BEFORE UPDATE OR DELETE ON {NOTES} "
        "FOR EACH ROW EXECUTE FUNCTION gv_reject_mutation()"
    )
    for role, grants in ROLE_GRANTS.items():
        for table, privileges in grants.privileges.items():
            if table in IMMUTABLE_TABLES:
                op.execute(f'GRANT {", ".join(privileges)} ON TABLE "{table}" TO {role.value}')


def downgrade() -> None:
    count = op.get_bind().scalar(sa.text(f"SELECT count(*) FROM {NOTES}"))
    if count:
        raise RuntimeError(
            f"Cannot downgrade the set notes: {count} row(s) record what reviewers and the AI "
            "established about drawing sets. Preserve these records."
        )
    op.execute(f"DROP TRIGGER IF EXISTS {NOTES}_append_only ON {NOTES}")
    for column in _INDEXED:
        op.drop_index(f"ix_{NOTES}_{column}", table_name=NOTES)
    op.drop_table(NOTES)
