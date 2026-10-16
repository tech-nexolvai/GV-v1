"""Saved assistant chats: chats per drawing set, their messages and the files they hand over (#1463).

Revision ID: 0105_assistant_chats
Revises: 0104_ai_review_evaluator

**Why.** The chat is the product now (owner, 2026-10-17): after an upload the reviewer lands in a
chat about the drawing set, and each set keeps many saved chats, like claude.ai's sidebar. Three
tables:

- `assistant_chats`: one chat about one package. `title` is NULL for a chat made empty; its title is
  then its first message's opening words, so no row is ever updated.
- `assistant_chat_messages`: one row per question and one per answer, numbered by `seq` in the
  chat (unique). An answer keeps what the screen shows (`text`, `blocks` as `jsonb`) and the exact
  API messages of its turn (`api_content`), which the next turn sends again unchanged. That column
  is `json`, not `jsonb`, on purpose: `jsonb` reorders an object's keys, and the replay must be byte
  for byte (prompt caching and Claude's preserved thinking both check the history is unchanged).
- `assistant_chat_files`: a file handed over with `present_files` (its bytes are kept in the chat's
  folder, with their SHA-256 here). `message_id` names the answer, which is written when the answer
  ends, so it has no foreign key.

All three are append-only: the same trigger as every other record table, and the roles' grants
derived from `ROLE_GRANTS` (`SELECT`, `INSERT`). A downgrade with rows stored refuses.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.db.roles import ROLE_GRANTS

revision: str = "0105_assistant_chats"
down_revision: str | None = "0104_ai_review_evaluator"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CHATS = "assistant_chats"
MESSAGES = "assistant_chat_messages"
FILES = "assistant_chat_files"
IMMUTABLE_TABLES = (CHATS, MESSAGES, FILES)
_INDEXED = {
    CHATS: ("package_id",),
    MESSAGES: ("chat_id",),
    FILES: ("chat_id", "message_id"),
}

#: Written out, not imported from the model: a migration describes one fixed state.
_ROLES = ("user", "assistant")
_STOPS = ("cost_cap", "cancelled", "error", "turn_limit")


def _listed(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _common() -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        CHATS,
        *_common(),
        sa.Column("package_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=200), nullable=False),
        sa.ForeignKeyConstraint(["package_id"], ["packages.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        MESSAGES,
        *_common(),
        sa.Column("chat_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("created_by", sa.String(length=200), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=True),
        sa.Column("effort", sa.String(length=16), nullable=True),
        sa.Column("thinking", sa.Boolean(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("blocks", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("api_content", postgresql.JSON(astext_type=sa.Text()), nullable=True),
        sa.Column("cost_micros", sa.BigInteger(), nullable=True),
        sa.Column("stopped", sa.String(length=16), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["chat_id"], [f"{CHATS}.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("chat_id", "seq"),
        sa.CheckConstraint(f"role IN ({_listed(_ROLES)})", name="assistant_chat_message_role"),
        sa.CheckConstraint(
            f"stopped IS NULL OR stopped IN ({_listed(_STOPS)})",
            name="assistant_chat_message_stopped",
        ),
        sa.CheckConstraint("seq >= 1", name="assistant_chat_message_seq"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        FILES,
        *_common(),
        sa.Column("chat_id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("mime", sa.String(length=127), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(["chat_id"], [f"{CHATS}.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("size >= 0 AND name <> ''", name="assistant_chat_file_shape"),
        sa.PrimaryKeyConstraint("id"),
    )
    for table, columns in _INDEXED.items():
        for column in columns:
            op.create_index(f"ix_{table}_{column}", table, [column])
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
    count = op.get_bind().scalar(
        sa.text(
            f"SELECT (SELECT count(*) FROM {CHATS}) + (SELECT count(*) FROM {MESSAGES}) "
            f"+ (SELECT count(*) FROM {FILES})"
        )
    )
    if count:
        raise RuntimeError(
            f"Cannot downgrade the assistant chats: {count} row(s) record what reviewers asked and "
            "what the paid assistant answered. Preserve these records."
        )
    for table in IMMUTABLE_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
    for table in reversed(IMMUTABLE_TABLES):
        for column in _INDEXED[table]:
            op.drop_index(f"ix_{table}_{column}", table_name=table)
        op.drop_table(table)
