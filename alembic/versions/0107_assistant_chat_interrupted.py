"""An assistant answer can be stored as interrupted by a server restart (#1514).

Revision ID: 0107_assistant_chat_interrupted
Revises: 0106_assistant_chat_caps

**Why.** When the API restarts while an answer runs (we restart to deploy), the answer used to be
lost: its question stayed without a reply. Now the next start stores an assistant row with
`stopped = 'interrupted'`, the work it had saved so far, and what it cost, and the screen offers
"Continue". This only widens the `stopped` check to allow the new word; no row changes, and the
append-only trigger is untouched.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0107_assistant_chat_interrupted"
down_revision: str | None = "0106_assistant_chat_caps"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MESSAGES = "assistant_chat_messages"
NAME = "assistant_chat_message_stopped"
BEFORE = "stopped IS NULL OR stopped IN ('cost_cap', 'cancelled', 'error', 'turn_limit')"
AFTER = (
    "stopped IS NULL OR stopped IN ('cost_cap', 'cancelled', 'error', 'turn_limit', 'interrupted')"
)


def upgrade() -> None:
    op.drop_constraint(NAME, MESSAGES, type_="check")
    op.create_check_constraint(NAME, MESSAGES, AFTER)


def downgrade() -> None:
    # An interrupted answer stored meanwhile would break the narrower check: refuse plainly.
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM assistant_chat_messages "
        "WHERE stopped = 'interrupted') THEN RAISE EXCEPTION "
        "'interrupted answers are stored; they cannot be downgraded away'; END IF; END $$"
    )
    op.drop_constraint(NAME, MESSAGES, type_="check")
    op.create_check_constraint(NAME, MESSAGES, BEFORE)
