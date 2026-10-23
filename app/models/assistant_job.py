"""The assistant's jobs (#1871): a chat message or a set pack, run by a worker process.

The API only writes a job row (in one transaction, after every check it makes before a stream
starts); a worker process (`app/review/assistant/job_worker.py`) claims it with `FOR UPDATE SKIP
LOCKED`, runs it in a process of its own and writes its progress here; the API streams that
progress to the browser from these rows. So the heavy work never runs in the API, a crash of the
work never takes the API down, and an API restart never stops a running check.

* `AssistantJob`: one row per job. `kind` is `message` (one chat answer: a question or a full
  check; `id` is the answer's message id) or `set_pack` (one set pack's build; `pack_key` is its
  folder). `status` moves `queued` -> `running` -> `done` / `interrupted` / `failed`. The worker
  stamps `heartbeat_at` every few seconds with what the answer has spent and its live card
  (`live`, the full check's whole live state); a job whose heartbeat stops is dead (its process was
  killed) and is stored as interrupted (Continue), or, for a set pack, tried once more. Stop is
  `cancel_requested_at`, which the job's process reads every second.
* `AssistantJobEvent`: the message's events in order (`seq` from 1), exactly as the screen gets
  them over Server-Sent Events (streamed words are merged a few times a second).

**Not append-only, on purpose.** These are execution state and telemetry, like `reading_progress`:
the job's row is updated in place as it runs. The answer itself is stored where it always was
(`assistant_chat_messages`, append-only); nothing reads a job row to decide a finding.

**Partial unique indexes keep the two rules across every process:** one message running per chat
(`ux_assistant_jobs_chat_active`), one paid full check at a time across all chats
(`ux_assistant_jobs_paid_active`, #1770), and one build per set pack (`ux_assistant_jobs_pack_active`).
No foreign keys, like `reading_progress`: a job row is written beside work that may hold locks on
the chat, and chats and packages are never deleted.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSON, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampedUUID, UTCDateTime, utc_now

__all__ = [
    "ACTIVE_STATUSES",
    "JOB_KINDS",
    "JOB_STATUSES",
    "AssistantJob",
    "AssistantJobEvent",
]

JOB_KINDS: Final = ("message", "set_pack")
JOB_STATUSES: Final = ("queued", "running", "done", "interrupted", "failed")
#: A job in one of these holds its chat's (or pack's) slot, and a paid one the paid slot.
ACTIVE_STATUSES: Final = ("queued", "running")


def _listed(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


_ACTIVE: Final = f"status IN ({_listed(ACTIVE_STATUSES)})"


class AssistantJob(Base, TimestampedUUID):
    """One message or set pack the worker runs (see the module)."""

    __tablename__ = "assistant_jobs"

    kind: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="queued")
    chat_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), default=None)
    package_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), default=None)
    pack_key: Mapped[str | None] = mapped_column(String(200), default=None)
    """A set pack's folder name (`set_pack.pack_key`): one build per pack at a time."""
    paid: Mapped[bool] = mapped_column(Boolean(), default=False)
    """A paid full check (#1770): one at a time across every chat and process."""
    request: Mapped[dict[str, Any]] = mapped_column(JSONB)
    """What the job's process needs to run it: a message's body, its chat, who asked; a set
    pack's folder, uploads and listing."""
    created_by: Mapped[str] = mapped_column(String(200))
    attempts: Mapped[int] = mapped_column(Integer(), default=0)
    claimed_by: Mapped[str | None] = mapped_column(String(200), default=None)
    """The worker process that runs it (`host:pid`)."""
    claimed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    spent_micros: Mapped[int] = mapped_column(BigInteger(), default=0)
    """What the running answer has spent so far (millionths of $), with each heartbeat."""
    live: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    """A running full check's live state (`LiveCheck.snapshot()` and `counts()`), with each
    heartbeat."""
    last_seq: Mapped[int] = mapped_column(Integer(), default=0)
    """The newest event's `seq` (0: none yet)."""
    error: Mapped[str | None] = mapped_column(Text(), default=None)
    """Why a job failed or was interrupted, in plain words."""
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)

    __table_args__ = (
        CheckConstraint(f"kind IN ({_listed(JOB_KINDS)})", name="assistant_job_kind"),
        CheckConstraint(f"status IN ({_listed(JOB_STATUSES)})", name="assistant_job_status"),
        CheckConstraint(
            "(kind = 'message' AND chat_id IS NOT NULL) "
            "OR (kind = 'set_pack' AND pack_key IS NOT NULL)",
            name="assistant_job_target",
        ),
        CheckConstraint("attempts >= 0 AND last_seq >= 0", name="assistant_job_counts"),
        Index(
            "ux_assistant_jobs_chat_active",
            "chat_id",
            unique=True,
            postgresql_where=text(f"kind = 'message' AND {_ACTIVE}"),
        ),
        Index(
            "ux_assistant_jobs_paid_active",
            "paid",
            unique=True,
            postgresql_where=text(f"paid AND {_ACTIVE}"),
        ),
        Index(
            "ux_assistant_jobs_pack_active",
            "pack_key",
            unique=True,
            postgresql_where=text(f"kind = 'set_pack' AND {_ACTIVE}"),
        ),
        Index("ix_assistant_jobs_status_created_at", "status", "created_at"),
        Index("ix_assistant_jobs_package_id", "package_id"),
    )


class AssistantJobEvent(Base, TimestampedUUID):
    """One event of a message job, in order (see the module)."""

    __tablename__ = "assistant_job_events"

    job_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    seq: Mapped[int] = mapped_column(Integer())
    event: Mapped[str] = mapped_column(String(64))
    data: Mapped[dict[str, Any]] = mapped_column(JSON)
    """`json`, not `jsonb`: the screen gets the event as it was sent, keys in their order."""

    __table_args__ = (
        UniqueConstraint("job_id", "seq"),
        CheckConstraint("seq >= 1", name="assistant_job_event_seq"),
    )
