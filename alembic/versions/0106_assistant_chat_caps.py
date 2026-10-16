"""An assistant answer keeps the caps it ran under, and which one stopped it (#1479).

Revision ID: 0106_assistant_chat_caps
Revises: 0105_assistant_chats

**Why.** A stopped answer says why under it: "Stopped: this answer reached its $5 limit", or, when
the drawing set's budget ran out, "Stopped: this drawing set has used its $40 AI budget". The caps
are settings that can change later, so the answer keeps the ones it actually ran under:

- `message_cap_micros`: the per-answer cap;
- `package_cap_micros`: the drawing set's AI budget;
- `cap_hit`: with `stopped = 'cost_cap'`, `message` or `package`.

Three nullable columns on an append-only table: rows stored before this keep NULL (the screen then
falls back to today's per-answer cap). Adding columns does not touch the append-only trigger.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0106_assistant_chat_caps"
down_revision: str | None = "0105_assistant_chats"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MESSAGES = "assistant_chat_messages"


def upgrade() -> None:
    op.add_column(MESSAGES, sa.Column("message_cap_micros", sa.BigInteger(), nullable=True))
    op.add_column(MESSAGES, sa.Column("package_cap_micros", sa.BigInteger(), nullable=True))
    op.add_column(MESSAGES, sa.Column("cap_hit", sa.String(length=16), nullable=True))
    op.create_check_constraint(
        "assistant_chat_message_cap_hit",
        MESSAGES,
        "cap_hit IS NULL OR cap_hit IN ('message', 'package')",
    )


def downgrade() -> None:
    op.drop_constraint("assistant_chat_message_cap_hit", MESSAGES, type_="check")
    op.drop_column(MESSAGES, "cap_hit")
    op.drop_column(MESSAGES, "package_cap_micros")
    op.drop_column(MESSAGES, "message_cap_micros")
