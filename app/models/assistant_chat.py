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

All three are append-only (the same trigger and grants as every other record table).
"""

from __future__ import annotations

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

from app.db.base import Base, Immutable, TimestampedUUID

CHAT_ROLES: tuple[str, ...] = ("user", "assistant")
#: Why an answer stopped before it finished; NULL when it finished.
CHAT_STOPS: tuple[str, ...] = ("cost_cap", "cancelled", "error", "turn_limit")


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

    __table_args__ = (
        UniqueConstraint("chat_id", "seq"),
        CheckConstraint(f"role IN ({_listed(CHAT_ROLES)})", name="assistant_chat_message_role"),
        CheckConstraint(
            f"stopped IS NULL OR stopped IN ({_listed(CHAT_STOPS)})",
            name="assistant_chat_message_stopped",
        ),
        CheckConstraint("seq >= 1", name="assistant_chat_message_seq"),
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


__all__ = [
    "CHAT_ROLES",
    "CHAT_STOPS",
    "AssistantChat",
    "AssistantChatFile",
    "AssistantChatMessage",
]
