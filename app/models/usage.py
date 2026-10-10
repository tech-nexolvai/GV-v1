"""Earlier AI spend recorded outside this project's reviews (#1165)."""

from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampedUUID
from app.usage_history import PURPOSES, ROUTES

_ROUTE_VALUES = ", ".join(f"'{route}'" for route in ROUTES)
_PURPOSE_VALUES = ", ".join(f"'{purpose}'" for purpose in PURPOSES)


class AiSpendHistory(Base, TimestampedUUID):
    """One day's calls to one model for one purpose, made outside this project's reviews.

    Earlier reading runs, bake-offs and proofs were recorded in `model_invocations` of other local
    databases, so the Usage page never saw them. `scripts/import_spend_history.py` counts each call
    once (by its id, which copies of a database keep), leaves out calls this database already holds,
    and writes one row per (day, model, purpose). Only counts, tokens and cost: never a prompt, an
    answer or anything from a drawing.

    **Not append-only, on purpose.** A row is a summary of records kept elsewhere, keyed by
    `(project_id, source_key)`; importing again replaces the row's numbers with the same or newer
    counts instead of adding a second row.
    """

    __tablename__ = "ai_spend_history"

    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT"))
    occurred_on: Mapped[date] = mapped_column(Date())
    """The UTC day the calls were made."""
    model_id: Mapped[str] = mapped_column(String(300))
    route: Mapped[str] = mapped_column(String(32))
    """bedrock, openrouter, anthropic or unknown (`app.usage_history.infer_route`)."""
    purpose: Mapped[str] = mapped_column(String(32))
    """reading, row-choice, chat, assistant, bake-off or other (`infer_purpose`)."""
    calls: Mapped[int] = mapped_column(Integer())
    input_tokens: Mapped[int] = mapped_column(BigInteger())
    output_tokens: Mapped[int] = mapped_column(BigInteger())
    cost_micros: Mapped[int] = mapped_column(BigInteger())
    """The summed cost of the priced calls, in millionths of a US dollar."""
    unpriced_calls: Mapped[int] = mapped_column(Integer())
    """Calls with no recorded price: `cost_micros` leaves them out, so the real cost is higher."""
    source_label: Mapped[str] = mapped_column(String(200))
    """Short plain words for where these calls were recorded ("Earlier runs on this machine")."""
    source_key: Mapped[str] = mapped_column(String(500))
    """The import grouping, `day|model|purpose`: importing again updates this row."""

    __table_args__ = (
        UniqueConstraint("project_id", "source_key"),
        CheckConstraint(f"route IN ({_ROUTE_VALUES})", name="spend_history_route"),
        CheckConstraint(f"purpose IN ({_PURPOSE_VALUES})", name="spend_history_purpose"),
        CheckConstraint("model_id <> ''", name="spend_history_model_id"),
        CheckConstraint("calls > 0", name="spend_history_calls"),
        CheckConstraint("input_tokens >= 0 AND output_tokens >= 0", name="spend_history_tokens"),
        CheckConstraint("cost_micros >= 0", name="spend_history_cost"),
        CheckConstraint(
            "unpriced_calls >= 0 AND unpriced_calls <= calls", name="spend_history_unpriced"
        ),
        CheckConstraint("source_label !~ '^[[:space:]]*$'", name="spend_history_source_label"),
        CheckConstraint("source_key !~ '^[[:space:]]*$'", name="spend_history_source_key"),
    )


__all__ = ["AiSpendHistory"]
