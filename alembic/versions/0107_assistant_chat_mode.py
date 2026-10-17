"""A chat message keeps its mode and products (#1513).

Revision ID: 0107_assistant_chat_mode
Revises: 0106_assistant_chat_caps

**Why.** The screen lays a full check out differently from a typed question (summary tiles, tabs,
the cloud drawing first). Saved messages came back without saying which they were, so the screen
guessed from the question's words. Now the question and its answer keep:

- `mode`: `chat` or `full_check`;
- `products`: the products the message was about (`["countertop"]`, `["cabinet"]` or both).

Two nullable columns on an append-only table: rows stored before this keep NULL (the screen then
falls back to the question's words). Adding columns does not touch the append-only trigger.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0107_assistant_chat_mode"
down_revision: str | None = "0106_assistant_chat_caps"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MESSAGES = "assistant_chat_messages"


def upgrade() -> None:
    op.add_column(MESSAGES, sa.Column("mode", sa.String(length=16), nullable=True))
    op.add_column(
        MESSAGES, sa.Column("products", postgresql.JSONB(none_as_null=True), nullable=True)
    )
    op.create_check_constraint(
        "assistant_chat_message_mode",
        MESSAGES,
        "mode IS NULL OR mode IN ('chat', 'full_check')",
    )


def downgrade() -> None:
    op.drop_constraint("assistant_chat_message_mode", MESSAGES, type_="check")
    op.drop_column(MESSAGES, "products")
    op.drop_column(MESSAGES, "mode")
