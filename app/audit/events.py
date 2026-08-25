"""The audit trail: who did what, when, and under which trace.

Backend proposal §11 names six things that must be audited — state changes, rule publication,
findings, review actions, exceptions and artifact downloads. One event type covers all six rather
than six near-identical tables, because the question an audit answers is *"what happened to this
package, in order?"*, and six tables make that a six-way union nobody writes correctly twice.

**An unaudited state change must not happen.** `emit` writes in the caller's transaction and never
opens its own. If the audit write fails, the operation it describes fails with it and rolls back
together. The alternative — audit on a best-effort basis — produces a trail that is silently
incomplete, which is worse than none: an absent record reads as "nothing happened", and nobody
checks a log they believe is complete.

**Append-only, structurally.** The table carries `Immutable`, so C1.12 revokes UPDATE and DELETE on
it in PostgreSQL. An audit trail that can be edited is not one, and convention will not hold that —
one ORM call is enough.

Source: backend proposal §11, §12; issue #255.
Verification: ``tests/audit/test_events.py``.
"""

from __future__ import annotations

import re
from enum import StrEnum
from uuid import UUID

from sqlalchemy import CheckConstraint, Index, String
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db.base import Base, Immutable, TimestampedUUID
from app.telemetry.tracing import current_trace_id


class AuditCategory(StrEnum):
    """The six things backend §11 requires an audit record for.

    A closed set, and deliberately not extensible by a caller passing a string: a category nobody
    declared is a category no report counts, which is how a class of event goes unwatched.
    """

    STATE_CHANGE = "STATE_CHANGE"
    """A package moved between lifecycle states."""

    RULE_PUBLICATION = "RULE_PUBLICATION"
    """A rule snapshot was published or released."""

    FINDING = "FINDING"
    """A check produced a verdict against a package."""

    REVIEW_ACTION = "REVIEW_ACTION"
    """A reviewer confirmed, corrected, approved or requested changes."""

    EXCEPTION = "EXCEPTION"
    """A documented exception was granted against a rule."""

    ARTIFACT_DOWNLOAD = "ARTIFACT_DOWNLOAD"
    """Somebody retrieved a stored artifact — a drawing, a crop or a report."""


#: The value recorded when the actor is the system rather than a person.
#:
#: Named rather than left null. "who did this?" answered with an empty column is indistinguishable
#: from a record whose actor was lost, and the two want different responses.
SYSTEM_ACTOR = "system"

#: The shape `current_trace_id` produces: 32 lowercase hex characters (W3C).
#:
#: Lowercase specifically, not case-insensitive. The same trace written both ways would not join to
#: itself in a report, and the column is what a trail is reconciled against.
_TRACE_ID = re.compile(r"[0-9a-f]{32}")


class AuditEvent(Base, TimestampedUUID, Immutable):
    """One thing that happened, attributable and ordered.

    Immutable: C1.12 revokes UPDATE and DELETE on every table carrying the mixin. `created_at`
    comes from `TimestampedUUID` and is the event time — there is no separate "recorded at",
    because a gap between the two is a question nobody can answer later.
    """

    __tablename__ = "audit_events"

    category: Mapped[str] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(200))
    """Who did it. A system actor is still named — see `SYSTEM_ACTOR`."""

    target_id: Mapped[UUID]
    """What it happened to. Not a foreign key: the six categories point at six different tables,
    and a nullable key per category would be six columns of which five are always empty."""

    target_type: Mapped[str] = mapped_column(String(64))
    """Which table `target_id` refers to, so the reference can be followed."""

    trace_id: Mapped[str | None] = mapped_column(String(32), default=None)
    """The trace this happened under, so an event joins the request that caused it.

    Nullable because `current_trace_id` returns `None` outside a span rather than a zero id — an
    id you cannot look up is worse than an admitted absence.
    """

    __table_args__ = (
        CheckConstraint(
            "category IN (" + ", ".join(f"'{member.value}'" for member in AuditCategory) + ")",
            name="audit_events_category",
        ),
        # A regex rather than `btrim`: PostgreSQL's `btrim` strips **spaces** by default, while
        # Python's `str.strip` — which `emit` uses — also strips tabs and newlines. A tab-only actor
        # would have passed the database and failed the writer, so the two rules disagreed on
        # exactly the input a direct INSERT would use to get round `emit`.
        CheckConstraint("actor !~ '^[[:space:]]*$'", name="audit_events_actor_named"),
        CheckConstraint("target_type !~ '^[[:space:]]*$'", name="audit_events_target_typed"),
        # The audit question is almost always "what happened to this thing, in order?", so the
        # index matches it rather than indexing the columns individually.
        Index("ix_audit_events_target", "target_type", "target_id", "created_at"),
    )


def emit(
    session: Session,
    *,
    category: AuditCategory,
    actor: str,
    target_id: UUID,
    target_type: str,
    trace_id: str | None = None,
) -> AuditEvent:
    """Record one audited event **in the caller's transaction**.

    Deliberately takes the caller's `Session` and never commits. The audit row and the change it
    describes commit together or roll back together, which is what makes "an unaudited state change
    must not happen" true rather than aspirational. A writer that opened its own transaction would
    leave an audit trail describing changes that were rolled back, and miss changes that succeeded
    after it failed.

    `trace_id` defaults to the active trace, so a caller inside a request does not have to thread it
    through. Pass it explicitly only when recording something on behalf of another trace — and it is
    checked against the shape `current_trace_id` produces, since a hand-passed one is exactly the
    case where a request id or a span id gets supplied by mistake.

    Raises rather than returning a failure: the caller's operation must not proceed unaudited, and a
    return value invites being ignored.
    """
    if not actor.strip():
        raise ValueError(
            "an audit event must name its actor. A system action is still attributable — pass "
            f"{SYSTEM_ACTOR!r} rather than an empty string, so 'who did this?' never answers "
            "with a blank that could equally mean the actor was lost."
        )
    if not target_type.strip():
        raise ValueError("an audit event must say what kind of thing target_id refers to")
    if trace_id is not None and not _TRACE_ID.fullmatch(trace_id):
        raise ValueError(
            f"trace_id must be 32 lowercase hex characters, not {trace_id!r}. `String(32)` would "
            "store a malformed one happily, and the event would then cite a trace nobody can open "
            "— audit evidence pointing at nothing, which is worse than the honest `None` this "
            "argument already allows."
        )

    event = AuditEvent(
        category=category.value,
        actor=actor,
        target_id=target_id,
        target_type=target_type,
        trace_id=trace_id if trace_id is not None else current_trace_id(),
    )
    session.add(event)
    session.flush()
    return event
