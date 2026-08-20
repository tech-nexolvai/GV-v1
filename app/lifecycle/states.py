"""The package state machine: the table, the entry conditions, and the only way to move (#209, C3.1).

`docs/DESIGN_PLATFORM.md` §5 fixes the states. This module fixes what may follow what, and is the one
place a package revision's state changes — `tests/lifecycle/test_states.py` walks the whole tree and
fails if any other module assigns to the column or writes a `PackageStateEvent`.

**The value is in what is refused, not in what is allowed.** A package cannot reach `AWAITING_REVIEW`
without checks having run, and cannot be approved out of any side state. Those two are the reason this
exists: `AGENTS.md` §2.2 says silence must never read as completion, and a package sitting in front of a
reviewer having skipped its checks is precisely silence reading as completion.

**The table is data.** `TRANSITIONS` is a plain dict of state to allowed successors, so it can be
rendered (`render_transition_table`) and reviewed by somebody who does not read Python. A machine
expressed as `if` statements spread across the callers is one nobody can audit, and this one carries a
safety property worth auditing.

**What the design left open, and what was decided.** §5 draws the happy path and names five side states
without saying how a package enters or leaves one. Anant settled it:

- Every processing state may fail into `FAILED_RETRYABLE`, `FAILED_PERMANENT` or `NEEDS_INPUT`.
  `FAILED_RETRYABLE` and `NEEDS_INPUT` return to the pipeline; `FAILED_PERMANENT` does not.
- `SUPERSEDED` is reachable from every non-terminal state, `APPROVED` included, because §5 says a new
  document revision supersedes the prior package revision whatever state it had reached.
- `CHANGES_REQUESTED` leaves only by being superseded. A correction is a new revision, so the question
  *"what did you tell us in March?"* keeps exactly one answer.
- `SUPERSEDED` and `CANCELLED` are terminal.

**Why an entry condition guards resumption.** A dict from state to successors cannot say "back to where
you came from": `FAILED_RETRYABLE` has to name every processing state it might resume into, which also
permits resuming at a later one. A failure in `EXTRACTING` could resume at `RUNNING_CHECKS`, skipping
`MATCHING` and `VALIDATING_EVIDENCE`, and the checks would then run against evidence nothing validated
— with `AWAITING_REVIEW`'s own condition none the wiser, because checks really would have run. So the
table says which edges exist and `resumes_where_it_failed` says when one may be taken, read from the
event log rather than from the caller's good intentions.

**Nothing here commits.** `transition` writes into the caller's transaction, exactly as
`workflow.outbox.enqueue` does, so a state change and the outbox row that justifies it become durable
together or not at all. A function that committed on its own behalf would put them in two
transactions, which is the dual write the outbox exists to remove.

Source: backend proposal §9.1 · Design: `docs/DESIGN_PLATFORM.md` §5 ·
Verification: `tests/lifecycle/test_states.py`
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from itertools import pairwise
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models import PackageRevision, PackageState, PackageStateEvent

__all__ = [
    "ENTRY_CONDITIONS",
    "FAILURE_STATES",
    "MAIN_LINE",
    "PROCESSING_STATES",
    "RESUMABLE_STATES",
    "REVIEW_OUTCOMES",
    "SIDE_STATES",
    "TERMINAL_STATES",
    "TRANSITIONS",
    "EntryCondition",
    "EntryConditionUnmet",
    "IllegalTransition",
    "UnknownRevision",
    "begin",
    "render_transition_table",
    "transition",
]

#: The first state of every revision, and the first event's sequence number.
INITIAL_STATE = PackageState.CREATED
FIRST_SEQUENCE = 1

#: The states where the system is doing work of its own and can therefore fail.
#:
#: `CREATED` and `UPLOADING` are not here: nothing of ours is running, so there is no failure of ours
#: to record. They can still be cancelled or superseded like anything else.
PROCESSING_STATES: frozenset[PackageState] = frozenset(
    {
        PackageState.INGESTING,
        PackageState.EXTRACTING,
        PackageState.MATCHING,
        PackageState.VALIDATING_EVIDENCE,
        PackageState.RUNNING_CHECKS,
        PackageState.GENERATING_OUTPUTS,
    }
)

#: Where a processing state goes when it does not finish.
FAILURE_STATES: frozenset[PackageState] = frozenset(
    {
        PackageState.FAILED_RETRYABLE,
        PackageState.FAILED_PERMANENT,
        PackageState.NEEDS_INPUT,
    }
)

#: The two failure states work may resume from. `FAILED_PERMANENT` is absent on purpose.
RESUMABLE_STATES: frozenset[PackageState] = frozenset(
    {PackageState.FAILED_RETRYABLE, PackageState.NEEDS_INPUT}
)

#: Nothing leaves these. A superseded revision is the historical record a later one replaced, and a
#: cancelled one is a decision somebody took; reopening either would rewrite what was already reported.
TERMINAL_STATES: frozenset[PackageState] = frozenset(
    {PackageState.SUPERSEDED, PackageState.CANCELLED}
)

#: A reviewer's decision. Not terminal — a later revision still supersedes it — but finished: the only
#: way out is to be superseded.
#:
#: Cancelling one is deliberately *not* possible. Cancellation says "stop, this is not going to
#: happen"; both of these are records that it already did, and letting a signed-off approval be
#: cancelled afterwards would rewrite a completed review.
REVIEW_OUTCOMES: frozenset[PackageState] = frozenset(
    {PackageState.APPROVED, PackageState.CHANGES_REQUESTED}
)

#: Every state off the main line, for the "approval is unreachable from a side state" property.
SIDE_STATES: frozenset[PackageState] = FAILURE_STATES | TERMINAL_STATES

#: The main line, in order. Read as consecutive pairs to build the happy path, so the order here *is*
#: the pipeline — there is no second place that also has an opinion about what follows what.
MAIN_LINE: tuple[PackageState, ...] = (
    PackageState.CREATED,
    PackageState.UPLOADING,
    PackageState.UPLOADED,
    PackageState.INGESTING,
    PackageState.EXTRACTING,
    PackageState.MATCHING,
    PackageState.VALIDATING_EVIDENCE,
    PackageState.RUNNING_CHECKS,
    PackageState.GENERATING_OUTPUTS,
    PackageState.AWAITING_REVIEW,
)


def _build_transitions() -> dict[PackageState, frozenset[PackageState]]:
    """Assemble the table from the rules above, so no edge exists without a stated reason.

    Written as a build rather than a literal because a hand-maintained literal of seventeen states is
    where an edge nobody meant creeps in — and the edge that matters here is the one that lets a
    package skip a step.
    """
    table: dict[PackageState, set[PackageState]] = {state: set() for state in PackageState}

    # The happy path, taken straight from the order of MAIN_LINE.
    for current, following in pairwise(MAIN_LINE):
        table[current].add(following)

    # Review is the one fork: a reviewer approves or asks for changes.
    table[PackageState.AWAITING_REVIEW].update(
        {PackageState.APPROVED, PackageState.CHANGES_REQUESTED}
    )

    # Work of ours can fail; nothing else can.
    for state in PROCESSING_STATES:
        table[state].update(FAILURE_STATES)

    # Resumption. The table has to name every processing state, which is why
    # `resumes_where_it_failed` exists — see the module docstring.
    for state in RESUMABLE_STATES:
        table[state].update(PROCESSING_STATES)

    # Superseding reaches everything that is not already final: §5 says a new document revision
    # supersedes the prior package revision, and it does not ask what state that revision reached.
    for state in PackageState:
        if state not in TERMINAL_STATES:
            table[state].add(PackageState.SUPERSEDED)

    # Cancelling reaches less. A review outcome is a decision that was taken, and cancelling it
    # afterwards would rewrite a completed review — so `APPROVED` and `CHANGES_REQUESTED` leave only by
    # being superseded.
    for state in PackageState:
        if state not in TERMINAL_STATES and state not in REVIEW_OUTCOMES:
            table[state].add(PackageState.CANCELLED)

    return {state: frozenset(following) for state, following in table.items()}


#: State to the states that may follow it. Data, so `render_transition_table` can print it and a
#: reviewer can check it against §5 without reading any code.
TRANSITIONS: Mapping[PackageState, frozenset[PackageState]] = _build_transitions()


class IllegalTransition(Exception):
    """The table does not contain this edge.

    Names both states, because "illegal transition" without them sends the reader to the table to work
    out which move was refused.
    """


class EntryConditionUnmet(Exception):
    """The edge exists, but the target state's conditions are not satisfied.

    Deliberately a different exception from `IllegalTransition`. "You may never do that" and "not yet"
    call for different responses — a caller may retry the second once the condition holds, and retrying
    the first for ever is a stuck package nobody is watching.
    """


class UnknownRevision(Exception):
    """No such package revision, so there is no state to change."""


@dataclass(frozen=True, slots=True)
class EntryCondition:
    """One named requirement for entering a state.

    Named rather than inline so a failure can say *which* condition was unmet, and so the conditions
    are listed as data beside the table instead of buried in the function that checks them. The
    acceptance criterion asks for exactly this: explicit and checked, never implied by call order.
    """

    name: str
    describe: str
    """Plain English, quoted into the failure. The reader of a refusal is usually not its author."""

    holds: Callable[[Session, UUID, PackageState, PackageState], bool]
    """`(session, package_revision_id, from_state, to_state) -> bool`.

    Both states are passed because a condition can depend on either. `checks_have_run` cares about
    neither; `resumes_where_it_failed` needs both — the failure state it is leaving and the state it is
    being asked to resume at.
    """


def _checks_have_run(
    session: Session,
    package_revision_id: UUID,
    from_state: PackageState,
    to_state: PackageState,
) -> bool:
    """Whether any check has been run against this revision.

    **The property this whole module exists for.** A package in front of a reviewer that never ran its
    checks is silence reading as completion (`AGENTS.md` §2.2) — the reviewer sees no failures and
    concludes there are none.

    Deliberately "at least one", not "all of them": which rules apply to a package is the applicability
    resolver's answer (`A5.3`), and duplicating that judgement here would give two places an opinion
    about what a complete run is. This catches the failure that matters — no checks at all — and says
    so rather than implying more.
    """
    del from_state, to_state
    from app.models import CheckRun

    statement = (
        select(func.count())
        .select_from(CheckRun)
        .where(CheckRun.package_revision_id == package_revision_id)
    )
    return bool(session.execute(statement).scalar_one())


def _resumes_where_it_failed(
    session: Session,
    package_revision_id: UUID,
    from_state: PackageState,
    to_state: PackageState,
) -> bool:
    """When resuming from a failure, whether the target is the state that failed.

    **The hole in the table, closed.** `TRANSITIONS` must let `FAILED_RETRYABLE` reach every processing
    state, so on its own it also permits resuming at a *later* one — a failure in `EXTRACTING` resuming
    at `RUNNING_CHECKS`, with `MATCHING` and `VALIDATING_EVIDENCE` skipped and the checks then running
    against evidence nothing validated. The table cannot express "back to where you came from"; the
    event log can, because it recorded where that was.

    Only consulted when arriving from a resumable state — every other entry into a processing state is
    the ordinary forward step and this returns True without reading anything.
    """
    if from_state not in RESUMABLE_STATES:
        return True

    entered_failure = session.execute(
        select(PackageStateEvent.from_state)
        .where(
            PackageStateEvent.package_revision_id == package_revision_id,
            PackageStateEvent.to_state == from_state.value,
        )
        .order_by(PackageStateEvent.sequence.desc())
        .limit(1)
    ).scalar_one_or_none()

    # No recorded entry into the failure state means the history does not support any resume target.
    # Refusing is the safe direction: the alternative is trusting a caller's word about where it was.
    return entered_failure is not None and entered_failure == to_state.value


# ---------------------------------------------------------------------------
# Entry conditions, as data beside the table
# ---------------------------------------------------------------------------

CHECKS_HAVE_RUN = EntryCondition(
    name="checks_have_run",
    describe=(
        "at least one check must have been run against this revision before a reviewer sees it — a "
        "package with no checks shows a reviewer no failures, which reads as none"
    ),
    holds=_checks_have_run,
)

RESUMES_WHERE_IT_FAILED = EntryCondition(
    name="resumes_where_it_failed",
    describe=(
        "work may only resume at the state that failed — resuming further along would skip the steps "
        "in between, and a check run against evidence nothing validated is worse than no check"
    ),
    holds=_resumes_where_it_failed,
)

#: `outputs_generated` is **not** here, and its absence is deliberate. The story's sketch names it, but
#: there is nothing for it to read: `app/models/` has no outputs, report or redline table, and D2.1
#: (#225) writes reports as files without a persisted record. A predicate returning True because it has
#: nothing to check would read as enforcement while enforcing nothing, and the next author would trust
#: it. When an outputs record exists, the condition attaches to `AWAITING_REVIEW` alongside the one
#: below.
ENTRY_CONDITIONS: Mapping[PackageState, tuple[EntryCondition, ...]] = {
    PackageState.AWAITING_REVIEW: (CHECKS_HAVE_RUN,),
    # Every processing state, because every one of them is a possible resume target and the condition
    # is what stops a resume from skipping the ones before it. Built rather than listed so a state
    # added to PROCESSING_STATES cannot be left out of this.
    **{state: (RESUMES_WHERE_IT_FAILED,) for state in sorted(PROCESSING_STATES)},
}


# ---------------------------------------------------------------------------
# The only way a state changes
# ---------------------------------------------------------------------------


def _current_state(session: Session, package_revision_id: UUID) -> PackageState:
    """The revision's state right now, or `UnknownRevision`."""
    stored = session.execute(
        select(PackageRevision.state).where(PackageRevision.id == package_revision_id)
    ).scalar_one_or_none()
    if stored is None:
        raise UnknownRevision(f"no package revision {package_revision_id}")
    return PackageState(stored)


def _next_sequence(session: Session, package_revision_id: UUID) -> int:
    """One past the highest sequence recorded for this revision.

    `package_state_events` carries a unique constraint on `(package_revision_id, sequence)`, so two
    concurrent transitions cannot both take a number — the loser fails on the constraint rather than
    silently interleaving. That is the intended outcome: a package's history has one order.
    """
    highest = session.execute(
        select(func.max(PackageStateEvent.sequence)).where(
            PackageStateEvent.package_revision_id == package_revision_id
        )
    ).scalar_one_or_none()
    return FIRST_SEQUENCE if highest is None else int(highest) + 1


def begin(
    session: Session,
    package_revision_id: UUID,
    *,
    actor: str,
    reason: str | None = "package created",
) -> PackageStateEvent:
    """Open a revision's history at `CREATED`. Not a transition — there is no state to leave.

    Here rather than in the endpoint that creates a package, so `app/lifecycle/` really is the only
    place a state or a state event is written and the guard test needs no exemptions. The genesis event
    is the one with `from_state = NULL`, and it is refused if the revision already has a history: a
    second genesis would give the same revision two beginnings.

    Does not commit. The caller owns the transaction — see the module docstring.
    """
    state = _current_state(session, package_revision_id)
    if state is not INITIAL_STATE:
        raise IllegalTransition(
            f"package revision {package_revision_id} is already in {state.value}, so its history "
            "cannot be opened. `begin` is for a revision's first event; use `transition` to move it."
        )
    if _next_sequence(session, package_revision_id) != FIRST_SEQUENCE:
        raise IllegalTransition(
            f"package revision {package_revision_id} already has a recorded history, so opening it "
            "again would give it two beginnings."
        )

    event = PackageStateEvent(
        package_revision_id=package_revision_id,
        sequence=FIRST_SEQUENCE,
        from_state=None,
        to_state=INITIAL_STATE.value,
        actor=actor,
        reason=reason,
    )
    session.add(event)
    return event


def transition(
    session: Session,
    package_revision_id: UUID,
    to: PackageState,
    *,
    actor: str,
    reason: str | None = None,
) -> PackageStateEvent:
    """Move a package revision to `to`, or raise. The only way a state changes.

    Reads the current state from the database rather than taking it from the caller — a caller that
    told us where it thought the package was would be authorising its own move, and the interesting
    failures are exactly the ones where it is wrong.

    Checks the table first and the target's entry conditions second, so a refused move reports the
    reason a reader can act on: an edge that does not exist is a different problem from one that is not
    yet allowed.

    Does not commit, so the state change and whatever justified it — an outbox row, a finding — land in
    one transaction or not at all.

    Raises:
        UnknownRevision: no such revision.
        IllegalTransition: the table has no edge from the current state to `to`.
        EntryConditionUnmet: the edge exists but a named condition on `to` does not hold.
    """
    if not isinstance(to, PackageState):
        raise TypeError("`to` must be a PackageState")

    current = _current_state(session, package_revision_id)
    allowed = TRANSITIONS[current]
    if to not in allowed:
        permitted = (
            ", ".join(sorted(state.value for state in allowed)) or "nothing — it is terminal"
        )
        raise IllegalTransition(
            f"a package revision in {current.value} may not move to {to.value}. "
            f"From {current.value} the table allows: {permitted}. "
            "The table in app/lifecycle/states.py is the whole answer; there is no fallback."
        )

    for condition in ENTRY_CONDITIONS.get(to, ()):
        if not condition.holds(session, package_revision_id, current, to):
            raise EntryConditionUnmet(
                f"a package revision in {current.value} cannot enter {to.value} yet: "
                f"{condition.name} is not satisfied — {condition.describe}."
            )

    event = PackageStateEvent(
        package_revision_id=package_revision_id,
        sequence=_next_sequence(session, package_revision_id),
        from_state=current.value,
        to_state=to.value,
        actor=actor,
        reason=reason,
    )
    session.add(event)
    # The event is the record and the column is the shortcut for reading it. Both, in one transaction,
    # so a query on the column can never disagree with the history that produced it.
    session.execute(
        update(PackageRevision)
        .where(PackageRevision.id == package_revision_id)
        .values(state=to.value)
    )
    return event


def render_transition_table() -> str:
    """The table as a Markdown table, for the docs and for review by somebody who reads no Python.

    The acceptance criterion asks for a table that *can be rendered and reviewed*, which is the point
    of holding it as data. A machine somebody has to reconstruct from `if` statements is one nobody
    checks against the design.
    """
    lines = [
        "| State | May move to | Notes |",
        "| --- | --- | --- |",
    ]
    for state in PackageState:
        following = sorted(next_state.value for next_state in TRANSITIONS[state])
        notes: list[str] = []
        if state in TERMINAL_STATES:
            notes.append("terminal")
        if state in PROCESSING_STATES:
            notes.append("may fail")
        if state in RESUMABLE_STATES:
            notes.append("resumes where it failed")
        for condition in ENTRY_CONDITIONS.get(state, ()):
            notes.append(f"entry: {condition.name}")
        lines.append(
            f"| `{state.value}` | {', '.join(f'`{item}`' for item in following) or '—'} | "
            f"{'; '.join(notes) or '—'} |"
        )
    return "\n".join(lines)
