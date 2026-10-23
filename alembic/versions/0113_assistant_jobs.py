"""The assistant's jobs, run by a worker process (#1871).

Revision ID: 0113_assistant_jobs
Revises: 0112_finding_decision_should_be

**Why.** A chat answer (the full check above all) ran as a thread of the API, and the set pack was
started from API requests, with the running answer's events, live card, Stop and the one-paid-check
rule kept in the API's memory. So a crash in the heavy work could take the API down, an API restart
stopped a running check, and the API could not run more than one process. Now the API writes a job
row and a worker process runs it, writing its progress here; the API streams it from these rows.

Two tables:

- `assistant_jobs`: one row per job (a message, or a set pack's build), updated in place as it runs
  (status, heartbeat, spend, live card, cancel flag). Partial unique indexes keep one running
  message per chat, one paid full check at a time across every process (#1770), and one build per
  set pack.
- `assistant_job_events`: a message's events in order, as the screen gets them.

**Updated in place, on purpose** (execution state, like `reading_progress`): no append-only
trigger, and the grants are the mutable set derived from `ROLE_GRANTS`. The answers themselves stay
in the append-only `assistant_chat_messages`. No foreign keys (see `reading_progress`).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.db.roles import ROLE_GRANTS

revision: str = "0113_assistant_jobs"
down_revision: str | None = "0112_finding_decision_should_be"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JOBS = "assistant_jobs"
EVENTS = "assistant_job_events"
_TABLES = (JOBS, EVENTS)

#: Written out, not imported from the model: a migration describes one fixed state.
_KINDS = "'message', 'set_pack'"
_STATUSES = "'queued', 'running', 'done', 'interrupted', 'failed'"
_ACTIVE = "status IN ('queued', 'running')"


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
    """Create the two tables, their indexes, and grant them to the roles."""
    op.create_table(
        JOBS,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("chat_id", sa.Uuid(), nullable=True),
        sa.Column("package_id", sa.Uuid(), nullable=True),
        sa.Column("pack_key", sa.String(length=200), nullable=True),
        sa.Column("paid", sa.Boolean(), nullable=False),
        sa.Column("request", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_by", sa.String(length=200), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("claimed_by", sa.String(length=200), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("spent_micros", sa.BigInteger(), nullable=False),
        sa.Column("live", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("last_seq", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(f"kind IN ({_KINDS})", name="assistant_job_kind"),
        sa.CheckConstraint(f"status IN ({_STATUSES})", name="assistant_job_status"),
        sa.CheckConstraint(
            "(kind = 'message' AND chat_id IS NOT NULL) "
            "OR (kind = 'set_pack' AND pack_key IS NOT NULL)",
            name="assistant_job_target",
        ),
        sa.CheckConstraint("attempts >= 0 AND last_seq >= 0", name="assistant_job_counts"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ux_assistant_jobs_chat_active",
        JOBS,
        ["chat_id"],
        unique=True,
        postgresql_where=sa.text(f"kind = 'message' AND {_ACTIVE}"),
    )
    op.create_index(
        "ux_assistant_jobs_paid_active",
        JOBS,
        ["paid"],
        unique=True,
        postgresql_where=sa.text(f"paid AND {_ACTIVE}"),
    )
    op.create_index(
        "ux_assistant_jobs_pack_active",
        JOBS,
        ["pack_key"],
        unique=True,
        postgresql_where=sa.text(f"kind = 'set_pack' AND {_ACTIVE}"),
    )
    op.create_index("ix_assistant_jobs_status_created_at", JOBS, ["status", "created_at"])
    op.create_index("ix_assistant_jobs_package_id", JOBS, ["package_id"])
    op.create_table(
        EVENTS,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("event", sa.String(length=64), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.CheckConstraint("seq >= 1", name="assistant_job_event_seq"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "seq"),
    )
    grant_statements = [
        f'GRANT {", ".join(privileges)} ON TABLE %I."{table}" TO {role.value}'
        for role, grants in ROLE_GRANTS.items()
        for table, privileges in grants.privileges.items()
        if table in _TABLES
    ]
    if grant_statements:
        op.get_bind().execute(sa.text(_in_current_schema(*grant_statements)))


def downgrade() -> None:
    """Drop both tables. Nothing in them is a record anyone signs: the answers are stored apart;
    a job still queued or running is lost (its question is then stored as interrupted)."""
    op.drop_table(EVENTS)
    op.drop_index("ix_assistant_jobs_package_id", table_name=JOBS)
    op.drop_index("ix_assistant_jobs_status_created_at", table_name=JOBS)
    op.drop_index("ux_assistant_jobs_pack_active", table_name=JOBS)
    op.drop_index("ux_assistant_jobs_paid_active", table_name=JOBS)
    op.drop_index("ux_assistant_jobs_chat_active", table_name=JOBS)
    op.drop_table(JOBS)
