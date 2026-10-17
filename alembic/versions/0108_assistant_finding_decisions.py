"""The reviewer's decision on each finding of a chat answer: agree, or not a problem (#1521).

Revision ID: 0108_assistant_finding_decisions
Revises: 0107_assistant_chat_mode

**Why.** Step 5 of the reviewer's path: after a full check the reviewer clicks ✓ Agree or ✗ Not a
problem on each finding, and the cloud drawing sent to the vendor carries only the agreed items.
One table, `assistant_finding_decisions`: one row per click (chat, answer, finding id, `agree` or
`not_a_problem`, an optional note, who decided and when).

Append-only, like every other record table (the same trigger, and the roles' grants derived from
`ROLE_GRANTS`): a changed mind is a new row, and the latest row per (answer, finding) is the one
read. A downgrade with rows stored refuses.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.db.roles import ROLE_GRANTS

revision: str = "0108_assistant_finding_decisions"
down_revision: str | None = "0107_assistant_chat_mode"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DECISIONS = "assistant_finding_decisions"
IMMUTABLE_TABLES = (DECISIONS,)
_INDEXED = ("chat_id", "message_id")

#: Written out, not imported from the model: a migration describes one fixed state.
_VALUES = ("agree", "not_a_problem")


def upgrade() -> None:
    listed = ", ".join(f"'{value}'" for value in _VALUES)
    op.create_table(
        DECISIONS,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("chat_id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("finding_id", sa.String(length=64), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("decided_by", sa.String(length=200), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["chat_id"], ["assistant_chats.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["message_id"], ["assistant_chat_messages.id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint(f"decision IN ({listed})", name="assistant_finding_decision_value"),
        sa.CheckConstraint("finding_id <> ''", name="assistant_finding_decision_finding"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in _INDEXED:
        op.create_index(f"ix_{DECISIONS}_{column}", DECISIONS, [column])
    op.execute(
        f"CREATE TRIGGER {DECISIONS}_append_only BEFORE UPDATE OR DELETE ON {DECISIONS} "
        "FOR EACH ROW EXECUTE FUNCTION gv_reject_mutation()"
    )
    for role, grants in ROLE_GRANTS.items():
        for table, privileges in grants.privileges.items():
            if table in IMMUTABLE_TABLES:
                op.execute(f'GRANT {", ".join(privileges)} ON TABLE "{table}" TO {role.value}')


def downgrade() -> None:
    count = op.get_bind().scalar(sa.text(f"SELECT count(*) FROM {DECISIONS}"))
    if count:
        raise RuntimeError(
            f"Cannot downgrade the finding decisions: {count} row(s) record what reviewers "
            "decided. Preserve these records."
        )
    op.execute(f"DROP TRIGGER IF EXISTS {DECISIONS}_append_only ON {DECISIONS}")
    for column in _INDEXED:
        op.drop_index(f"ix_{DECISIONS}_{column}", table_name=DECISIONS)
    op.drop_table(DECISIONS)
