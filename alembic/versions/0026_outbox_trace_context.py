"""Carry the trace across the workflow boundary (#259, F2.1).

Revision ID: 0026_outbox_trace_context
Revises: 0025_database_roles

A Hatchet task runs in another process, minutes after the request that asked for it, and carries no
trace context automatically. Backend §2 wants `package → workflow → task → model call → finding` to
be one connected story; without something durable in between, it is two stories that happen to
mention the same package.

**Why a column and not the payload.** `payload` is the workflow's input contract. A `traceparent`
sitting in it would be an argument the workflow could read, and a workflow whose behaviour can depend
on whether tracing is switched on is one nobody can reason about. `workflow/outbox.py` also rejects
inexact numbers in the payload, which is a rule about business values and has nothing to say about
telemetry.

**Nullable, and meaningfully so.** `NULL` means nothing was being traced when the row was written —
a cron job, a test, a backfill. The dispatcher starts a fresh trace rather than inventing a parent,
because a trace linked to a span that never existed is worse than an honestly separate one.

`outbox_entries` is not immutable — `dispatched_at` and `attempts` are written after the fact — so a
new column needs no trigger work.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0026_outbox_trace_context"
down_revision: str | None = "0025_database_roles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "outbox_entries",
        sa.Column("trace_context", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("outbox_entries", "trace_context")
