"""Earlier AI calls recorded outside this project's reviews (#1165)."""

from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Immutable, TimestampedUUID
from app.usage_history import PURPOSES, ROUTES

_ROUTE_VALUES = ", ".join(f"'{route}'" for route in ROUTES)
_PURPOSE_VALUES = ", ".join(f"'{purpose}'" for purpose in PURPOSES)


class AiSpendHistory(Base, TimestampedUUID, Immutable):
    """One earlier model call, made outside this project's reviews: what it used and cost.

    Earlier reading runs, bake-offs and proofs were recorded in `model_invocations` of other local
    databases, so the Usage page never saw them. `scripts/import_spend_history.py` copies each call
    once (by its id, which copies of a database keep) and leaves out calls this project's reviews
    already count. Only the call's id, day, model, route, purpose, tokens and cost: never a prompt,
    an answer or anything from a drawing.

    **Append-only, one row per call.** Importing again adds the calls not seen before and changes
    nothing already here, so a later import from other sources (or a source that could not be read)
    never loses an earlier one. The API adds the rows up.
    """

    __tablename__ = "ai_spend_history"

    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT"))
    call_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    """The call's own `model_invocations.id` in the database it was recorded in."""
    occurred_on: Mapped[date] = mapped_column(Date())
    """The UTC day the call was made."""
    model_id: Mapped[str] = mapped_column(String(300))
    route: Mapped[str] = mapped_column(String(32))
    """bedrock, openrouter, anthropic or unknown (`app.usage_history.infer_route`)."""
    purpose: Mapped[str] = mapped_column(String(32))
    """reading, row-choice, chat, assistant, findings, bake-off or other."""
    input_tokens: Mapped[int] = mapped_column(Integer())
    output_tokens: Mapped[int] = mapped_column(Integer())
    cost_micros: Mapped[int | None] = mapped_column(BigInteger(), default=None)
    """What the call cost in millionths of a US dollar; `NULL` when no price is known."""
    priced_later: Mapped[bool] = mapped_column(Boolean(), default=False)
    """True when the call's own record predates real costs (#754) and the cost was worked out at
    import from the published price file."""
    source_label: Mapped[str] = mapped_column(String(200))
    """Short plain words for where the call was recorded ("Earlier runs on this machine")."""

    __table_args__ = (
        UniqueConstraint("project_id", "call_id"),
        CheckConstraint(f"route IN ({_ROUTE_VALUES})", name="spend_history_route"),
        CheckConstraint(f"purpose IN ({_PURPOSE_VALUES})", name="spend_history_purpose"),
        CheckConstraint("model_id <> ''", name="spend_history_model_id"),
        CheckConstraint("input_tokens >= 0 AND output_tokens >= 0", name="spend_history_tokens"),
        CheckConstraint("cost_micros IS NULL OR cost_micros >= 0", name="spend_history_cost"),
        CheckConstraint(
            "NOT priced_later OR cost_micros IS NOT NULL", name="spend_history_priced_later"
        ),
        CheckConstraint("source_label !~ '^[[:space:]]*$'", name="spend_history_source_label"),
    )


__all__ = ["AiSpendHistory"]
