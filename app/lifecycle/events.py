"""The state-event trail: how a transition is recorded, and how it reads in a dispute (#210, C3.2).

`package_state_events` answers one question — *"what happened to this package, and when?"* — and it is
asked when a review is disputed, often months later. That shapes everything here: the trail is
append-only, every event names who caused it, and it renders into sentences rather than a log dump,
because the person asking is usually not a programmer.

**Append-only is enforced below this module, not by it.** The rows are `Immutable` and
`0013_append_only` installs a trigger that refuses `UPDATE` and `DELETE` on this table. Nothing here
has to remember to be careful, which is the point — a convention that each writer must honour is one
that eventually is not.

**The sequence is allocated under a row lock.** `SELECT ... FOR UPDATE` on the revision before reading
the highest sequence, so two transitions arriving at once serialise into 2 and 3 rather than one of them
failing on the unique constraint. #209 did this without the lock and called the loser failing "the
intended outcome"; C3.2 asks for the lock, and it is the better answer — a caller whose perfectly valid
transition fails because another arrived in the same instant has to know to retry, and a workflow that
does not retry leaves a package stuck in a state nobody is watching. The unique constraint stays as the
backstop for anything that writes without taking the lock.

**Ordering is by sequence, never by timestamp.** Two events written in the same microsecond have no
order by `created_at`, and "which happened first?" is exactly what a dispute asks. The sequence is the
only total order this table has.

Source: backend proposal §9.1 · Design: `docs/DESIGN_PLATFORM.md` §5 ·
Verification: `tests/lifecycle/test_events.py`
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import PackageRevision, PackageState, PackageStateEvent

__all__ = [
    "FIRST_SEQUENCE",
    "STATE_PHRASES",
    "ActorMissing",
    "history",
    "record",
    "render_history",
]

#: The first event of a revision's history.
FIRST_SEQUENCE = 1


class ActorMissing(ValueError):
    """No actor was named for this event.

    Refused rather than stored blank or filled with a placeholder. `AGENTS.md` §2.4: every record must
    be attributable, and an event attributed to `""` or to `"system"` is one nobody can be asked about
    — which is precisely what the trail exists to make possible. A system actor is still a named actor:
    pass the name of the worker or the service.
    """


def record(
    session: Session,
    *,
    package_revision_id: UUID,
    from_state: PackageState | None,
    to_state: PackageState,
    actor: str,
    reason: str | None = None,
    workflow_run_id: UUID | None = None,
) -> PackageStateEvent:
    """Append one transition to a revision's history, in the caller's transaction.

    Does not commit. The event and the state change it records must land together or not at all — an
    event without its state change describes something that did not happen, and a state change without
    its event is a package whose history has a hole exactly where the dispute is. `app/lifecycle/states.py`
    calls this inside the same transaction as the column update.

    `from_state` is `None` only for a revision's genesis, which is the one event no transition can
    produce: there is no prior state to come from.

    `workflow_run_id` is left `None` when nothing ran — a reviewer's decision, or the genesis event.
    Filling it with a placeholder would make "which run did this?" answerable and wrong.

    Raises:
        ActorMissing: `actor` is blank. The database refuses this too, since #210 added the check;
            refusing here as well means the caller gets a sentence rather than an `IntegrityError`.
    """
    if not isinstance(actor, str) or not actor.strip():
        raise ActorMissing(
            f"no actor named for the move to {to_state.value}. Every event must say who caused it — "
            "a system actor is still a named actor, so pass the worker or service name rather than a "
            "blank or a placeholder."
        )

    event = PackageStateEvent(
        package_revision_id=package_revision_id,
        sequence=_next_sequence(session, package_revision_id),
        from_state=None if from_state is None else from_state.value,
        to_state=to_state.value,
        actor=actor.strip(),
        reason=reason,
        workflow_run_id=workflow_run_id,
    )
    session.add(event)
    return event


def _next_sequence(session: Session, package_revision_id: UUID) -> int:
    """The next sequence number for this revision, allocated under a row lock.

    The lock is on the *revision*, taken before the highest sequence is read, so a second transition
    for the same revision waits rather than reading the same maximum. Without it both read `n` and one
    fails on `uq_package_state_events_package_revision_id_sequence` — which is safe but makes a valid
    transition fail for a reason its caller did nothing to deserve.

    Locking the revision rather than the events serialises per package, so unrelated packages never
    wait on each other. A revision that does not exist takes no lock and starts at 1; the foreign key
    on the event is what refuses it, with a better message than anything this function could invent.
    """
    session.execute(
        select(PackageRevision.id)
        .where(PackageRevision.id == package_revision_id)
        .with_for_update()
    )
    highest = session.execute(
        select(func.max(PackageStateEvent.sequence)).where(
            PackageStateEvent.package_revision_id == package_revision_id
        )
    ).scalar_one_or_none()
    return FIRST_SEQUENCE if highest is None else int(highest) + 1


def history(session: Session, package_revision_id: UUID) -> list[PackageStateEvent]:
    """Every event for one revision, oldest first, ordered by sequence.

    By sequence and not by `created_at`: two events written in the same microsecond have no order by
    timestamp, and "which happened first?" is the question a dispute turns on. The sequence is the only
    total order here, which is why it is allocated so carefully above.
    """
    return list(
        session.scalars(
            select(PackageStateEvent)
            .where(PackageStateEvent.package_revision_id == package_revision_id)
            .order_by(PackageStateEvent.sequence)
        )
    )


# ---------------------------------------------------------------------------
# Reading it back in plain English
# ---------------------------------------------------------------------------

#: What each state means, as a phrase that finishes the sentence "…".
#:
#: The acceptance criterion is that the trail *renders in plain English for a reviewer*, and the test
#: plan sharpens it: readable without knowing the state names. So the renderer never prints a state
#: value — `AWAITING_REVIEW` tells a reviewer nothing, and a history that needs the enum beside it to
#: be understood is a log dump with extra steps.
STATE_PHRASES: Mapping[PackageState, str] = {
    PackageState.CREATED: "the package was created",
    PackageState.UPLOADING: "the upload started",
    PackageState.UPLOADED: "the upload finished",
    PackageState.INGESTING: "reading the drawings started",
    PackageState.EXTRACTING: "pulling out the dimensions started",
    PackageState.MATCHING: "matching the drawings to each other started",
    PackageState.VALIDATING_EVIDENCE: "checking the evidence started",
    PackageState.RUNNING_CHECKS: "running the rule checks started",
    PackageState.GENERATING_OUTPUTS: "building the report started",
    PackageState.AWAITING_REVIEW: "the package became ready for review",
    PackageState.APPROVED: "the package was approved",
    PackageState.CHANGES_REQUESTED: "changes were requested",
    PackageState.FAILED_RETRYABLE: "it stopped with a problem that can be retried",
    PackageState.FAILED_PERMANENT: "it stopped with a problem that cannot be retried",
    PackageState.NEEDS_INPUT: "it stopped and needs an answer from somebody",
    PackageState.CANCELLED: "it was cancelled",
    PackageState.SUPERSEDED: "it was replaced by a newer revision of the package",
}


def _stamp(moment: datetime | None) -> str:
    """A time a person reads, not an ISO string.

    `created_at` is timezone-aware in this schema (`UTCDateTime`), so this prints UTC and says so — a
    bare `14:02` in a dispute invites the question "in whose timezone?".
    """
    if moment is None:  # pragma: no cover - created_at is set on construction
        return "at an unrecorded time"
    return f"{moment:%d %b %Y %H:%M} UTC"


def render_history(events: Sequence[PackageStateEvent]) -> str:
    """The history as sentences, for a reviewer or a dispute. Not a log dump.

    One line per event: when, what happened, who caused it, and why if a reason was given. A state name
    never appears — see `STATE_PHRASES`.

    Takes the events rather than a session so the caller decides what to render: the whole history, or
    the part of it a dispute is about.
    """
    if not events:
        return "Nothing has happened to this package yet."

    lines: list[str] = []
    for event in events:
        phrase = STATE_PHRASES.get(
            PackageState(event.to_state),
            # A state with no phrase is a bug, and the honest rendering says so rather than quietly
            # printing the enum and looking like a translation.
            f"it moved to a state this report has no words for ({event.to_state})",
        )
        line = f"{_stamp(event.created_at)} — {phrase}, by {event.actor}"
        if event.reason:
            line += f" ({event.reason})"
        if event.workflow_run_id is not None:
            line += f" [automated run {event.workflow_run_id}]"
        lines.append(line)
    return "\n".join(lines)
