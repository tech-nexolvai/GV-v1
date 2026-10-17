"""Saved assistant chats (#1463): many chats per drawing set, like claude.ai's sidebar.

* `AssistantChat`: one chat about one package (drawing set). `title` is NULL for a chat made empty
  (the usual way): its title is then its first message's opening words, read from that row, so no
  row is ever updated.
* `AssistantChatMessage`: one row per user message and one per assistant answer, numbered by `seq`
  within the chat. An answer keeps what the screen shows (`text`, `blocks`) and, in `api_content`,
  the exact API messages of its turn (the question, the model's replies and the tool results), which
  the next turn sends again unchanged: prompt caching and Claude's preserved thinking both need an
  append-only history. `api_content` is `json`, not `jsonb`: `jsonb` reorders keys, and the replay
  must be byte for byte. Pictures are kept as files in the chat's folder and referenced from it.
* `AssistantChatFile`: a file the assistant handed over with `present_files`. Its bytes are a copy in
  the chat's folder; `message_id` is the answer it belongs to, which is written when the answer ends
  (so it has no foreign key).
* `AssistantFindingDecision` (#1521): the reviewer's ✓ Agree / ✗ Not a problem on one finding of an
  answer. A changed mind is a new row; the latest row per finding wins when read.
* `AssistantSetNote` (#1548): the drawing set's memory across chats: a fact the reviewer or the AI
  established (a decision with the person's name, a correction, what a dimension measures, which
  document governs). Every chat on the set starts with the newest ones.

All five are append-only (the same trigger and grants as every other record table).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSON, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Immutable, TimestampedUUID, UTCDateTime, utc_now

CHAT_ROLES: tuple[str, ...] = ("user", "assistant")
#: Why an answer stopped before it finished; NULL when it finished.
CHAT_STOPS: tuple[str, ...] = ("cost_cap", "cancelled", "error", "turn_limit", "interrupted")
#: With `stopped = 'cost_cap'`, which cap stopped it: the answer's own, or the drawing set's budget.
CHAT_CAPS: tuple[str, ...] = ("message", "package")
#: What a message was (#1513): a typed question, or the one-click full check.
CHAT_MODES: tuple[str, ...] = ("chat", "full_check")


def _listed(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


class AssistantChat(Base, TimestampedUUID, Immutable):
    """One saved chat about one drawing set."""

    __tablename__ = "assistant_chats"

    package_id: Mapped[UUID] = mapped_column(
        ForeignKey("packages.id", ondelete="RESTRICT"), index=True
    )
    title: Mapped[str | None] = mapped_column(Text, default=None)
    created_by: Mapped[str] = mapped_column(String(200))


class AssistantChatMessage(Base, TimestampedUUID, Immutable):
    """One message of a chat: the reviewer's question or the assistant's answer."""

    __tablename__ = "assistant_chat_messages"

    chat_id: Mapped[UUID] = mapped_column(
        ForeignKey("assistant_chats.id", ondelete="RESTRICT"), index=True
    )
    seq: Mapped[int] = mapped_column()
    role: Mapped[str] = mapped_column(String(16))
    created_by: Mapped[str] = mapped_column(String(200))
    model: Mapped[str | None] = mapped_column(String(64), default=None)
    effort: Mapped[str | None] = mapped_column(String(16), default=None)
    thinking: Mapped[bool | None] = mapped_column(Boolean, default=None)
    text: Mapped[str] = mapped_column(Text)
    blocks: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSONB(none_as_null=True), default=None
    )
    """What the screen shows of an answer, in order: thinking, text and steps."""
    api_content: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSON(none_as_null=True), default=None
    )
    """The exact API messages of the answer's turn, replayed unchanged by the next turn."""
    cost_micros: Mapped[int | None] = mapped_column(BigInteger, default=None)
    stopped: Mapped[str | None] = mapped_column(String(16), default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    message_cap_micros: Mapped[int | None] = mapped_column(BigInteger, default=None)
    """The per-answer cap the answer ran under (#1479); NULL for answers stored before it."""
    package_cap_micros: Mapped[int | None] = mapped_column(BigInteger, default=None)
    """The drawing set's AI budget the answer ran under."""
    cap_hit: Mapped[str | None] = mapped_column(String(16), default=None)
    """With `stopped = 'cost_cap'`: `message` or `package`."""
    mode: Mapped[str | None] = mapped_column(String(16), default=None)
    """`chat` or `full_check`, on the question and its answer (#1513); NULL for older rows."""
    products: Mapped[list[str] | None] = mapped_column(JSONB(none_as_null=True), default=None)
    """The products the message was about (`countertop`, `cabinet`); NULL for older rows."""

    __table_args__ = (
        UniqueConstraint("chat_id", "seq"),
        CheckConstraint(f"role IN ({_listed(CHAT_ROLES)})", name="assistant_chat_message_role"),
        CheckConstraint(
            f"stopped IS NULL OR stopped IN ({_listed(CHAT_STOPS)})",
            name="assistant_chat_message_stopped",
        ),
        CheckConstraint("seq >= 1", name="assistant_chat_message_seq"),
        CheckConstraint(
            f"cap_hit IS NULL OR cap_hit IN ({_listed(CHAT_CAPS)})",
            name="assistant_chat_message_cap_hit",
        ),
        CheckConstraint(
            f"mode IS NULL OR mode IN ({_listed(CHAT_MODES)})",
            name="assistant_chat_message_mode",
        ),
    )


class AssistantChatFile(Base, TimestampedUUID, Immutable):
    """A file the assistant handed over in a chat (`present_files`)."""

    __tablename__ = "assistant_chat_files"

    chat_id: Mapped[UUID] = mapped_column(
        ForeignKey("assistant_chats.id", ondelete="RESTRICT"), index=True
    )
    message_id: Mapped[UUID] = mapped_column(index=True)
    name: Mapped[str] = mapped_column(String(255))
    size: Mapped[int] = mapped_column(BigInteger)
    mime: Mapped[str] = mapped_column(String(127))
    sha256: Mapped[str] = mapped_column(String(64))

    __table_args__ = (
        CheckConstraint("size >= 0 AND name <> ''", name="assistant_chat_file_shape"),
    )


#: The reviewer's decision on one finding (#1521).
FINDING_DECISIONS: tuple[str, ...] = ("agree", "not_a_problem")


class AssistantFindingDecision(Base, TimestampedUUID, Immutable):
    """The reviewer's decision on one finding of a stored answer (#1521). The latest row per
    (answer, finding) is the one that counts."""

    __tablename__ = "assistant_finding_decisions"

    chat_id: Mapped[UUID] = mapped_column(
        ForeignKey("assistant_chats.id", ondelete="RESTRICT"), index=True
    )
    message_id: Mapped[UUID] = mapped_column(
        ForeignKey("assistant_chat_messages.id", ondelete="RESTRICT"), index=True
    )
    finding_id: Mapped[str] = mapped_column(String(64))
    decision: Mapped[str] = mapped_column(String(16))
    note: Mapped[str | None] = mapped_column(Text, default=None)
    decided_by: Mapped[str] = mapped_column(String(200))
    """The principal who decided."""
    decided_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)

    __table_args__ = (
        CheckConstraint(
            f"decision IN ({_listed(FINDING_DECISIONS)})", name="assistant_finding_decision_value"
        ),
        CheckConstraint("finding_id <> ''", name="assistant_finding_decision_finding"),
    )


#: Who established a set note (#1548): the AI (`remember`), or a reviewer's decision.
SET_NOTE_SOURCES: tuple[str, ...] = ("ai", "decision")


class AssistantSetNote(Base, TimestampedUUID, Immutable):
    """One fact about a drawing set, kept for every chat on it (#1548). Never changed: a newer
    note that says otherwise is a new row, and the newest notes come first."""

    __tablename__ = "assistant_set_notes"

    package_id: Mapped[UUID] = mapped_column(
        ForeignKey("packages.id", ondelete="RESTRICT"), index=True
    )
    note: Mapped[str] = mapped_column(Text)
    page: Mapped[int | None] = mapped_column(default=None)
    """The page of the vendor's file the note is about, when it is about one."""
    source: Mapped[str] = mapped_column(String(16))
    chat_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("assistant_chats.id", ondelete="RESTRICT"), default=None, index=True
    )
    message_id: Mapped[UUID | None] = mapped_column(default=None)
    """The answer it came from (written before that answer is stored, so no foreign key)."""
    created_by: Mapped[str] = mapped_column(String(200))

    __table_args__ = (
        CheckConstraint(
            f"source IN ({_listed(SET_NOTE_SOURCES)})", name="assistant_set_note_source"
        ),
        CheckConstraint("note <> ''", name="assistant_set_note_text"),
        CheckConstraint("page IS NULL OR page >= 1", name="assistant_set_note_page"),
    )


__all__ = [
    "CHAT_ROLES",
    "CHAT_STOPS",
    "FINDING_DECISIONS",
    "SET_NOTE_SOURCES",
    "AssistantChat",
    "AssistantChatFile",
    "AssistantChatMessage",
    "AssistantFindingDecision",
    "AssistantSetNote",
]
