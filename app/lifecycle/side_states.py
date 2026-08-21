"""Side states: why a package stopped, in words a reviewer can act on (#212, C3.4).

`docs/DESIGN_PLATFORM.md` §5 names five states for a package that is not going to finish on its own:
`FAILED_RETRYABLE`, `FAILED_PERMANENT`, `NEEDS_INPUT`, `CANCELLED`, `SUPERSEDED`. #209 put them in the
transition table and #211 built supersede. This module is the part that was still a guess.

**Retryable or permanent is decided by a table, not at the call site.** Both states existed and nothing
said which a given failure was, so every caller would have decided for itself — and two callers deciding
differently about the same error is how a package comes to be retried for ever in one code path and
abandoned in another. `FAILURE_CLASSIFICATION` is the one answer, and `classify` walks the exception's
MRO so a subclass inherits its parent's classification rather than falling through.

**An unclassified failure is permanent.** Anant's call, and it is the same shape as ADR-0001's unknown
unit: when we cannot classify something, we stop rather than guess. The alternative is worse in the
direction that matters — a malformed PDF classified retryable retries for ever, spending paid model
calls on work that can never succeed, and nothing reports it. A transient blip classified permanent
fails a package that would have worked, but it fails *visibly*, and §6.3 already says exhaustion is a
visible outcome. A stop somebody can see beats a loop nobody can.

**`NEEDS_INPUT` must say what input.** A package waiting for an answer nobody can identify has stopped
silently while wearing a state name, which is exactly the failure `AGENTS.md` §2.2 is about. `needed` is
required, refused when blank, and written into the state event's `reason` — so it appears in the history
`render_history` prints for a reviewer, rather than in a field only code reads.

**Why the table cannot name `extraction/`'s exceptions.** `app/api/packages.py` imports `app.lifecycle`,
and #208's guard forbids `app/api/` from reaching `extraction/`. A table naming `NovaTimeoutError` would
therefore fail C2.6. So this module classifies what it may legitimately import, and `enter_failure`
accepts an explicit `failure_class` for callers that already know — `extraction/` maps its own
exceptions, which is where that knowledge belongs anyway.

Source: backend proposal §9.1, §9.4 · Design: `docs/DESIGN_PLATFORM.md` §5, §6.3 ·
Verification: `tests/lifecycle/test_side_states.py`
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from sqlalchemy.exc import DataError, IntegrityError, OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from app.lifecycle.states import transition
from app.models import PackageState, PackageStateEvent
from storage.hashing import ArtifactCorrupt, IntegrityRecordMissing
from storage.signing import CapabilityInvalid
from storage.store import ArtifactConflict
from workflow.outbox import OutboxDispatchError

__all__ = [
    "FAILURE_CLASSIFICATION",
    "SIDE_STATE_DESCRIPTIONS",
    "FailureClass",
    "InputNotNamed",
    "cancel",
    "classify",
    "enter_failure",
    "enter_needs_input",
    "is_a_side_state",
]


class FailureClass(StrEnum):
    """Whether trying again could possibly help."""

    RETRYABLE = "retryable"
    """Transient. The work did not fail on its merits — a timeout, contention, an engine that was
    briefly unavailable. The same input may well succeed next time."""

    PERMANENT = "permanent"
    """The work cannot succeed as asked. Malformed input, an unsupported format, a refusal, a
    configuration that is absent. Retrying spends money and changes nothing."""


#: Exception type to failure class. The single answer, so no call site decides for itself.
#:
#: Keyed by class rather than by name or message: the plan is explicit that classification is not
#: message parsing, and a dotted-name table silently unclassifies whatever gets renamed. `classify`
#: walks the MRO, so listing a base class covers its subclasses.
#:
#: `extraction/`'s exceptions are deliberately absent — see the module docstring. They cannot be named
#: here without `app/api/` reaching `extraction/` through `app/lifecycle/`, which #208's guard refuses.
#:
#: The `*NotConfigured` errors are absent for a different reason: they are deployment faults, not package
#: failures. `ArtifactStoreNotConfigured` means nothing can be uploaded at all, which fails a request
#: rather than a package, and naming it here would have `app/lifecycle/` importing `app/api/` — the
#: layering upside down for the sake of an entry that would never be reached.
FAILURE_CLASSIFICATION: dict[type[BaseException], FailureClass] = {
    # Transient: the thing we asked was reasonable and the moment was wrong.
    TimeoutError: FailureClass.RETRYABLE,
    ConnectionError: FailureClass.RETRYABLE,
    # Connection loss, deadlock, a server that went away. Not `DBAPIError`, its parent — see below.
    OperationalError: FailureClass.RETRYABLE,
    OutboxDispatchError: FailureClass.RETRYABLE,
    # Permanent: the input or the configuration is wrong, and time does not fix either.
    #
    # These three are named explicitly rather than left to the default, because they are the common
    # database failures and a reader should see them decided here. They are also the reason `DBAPIError`
    # is **not** in this table: all three inherit from it, so one broad entry classified every constraint
    # violation, every invalid value and every malformed statement as retryable — a unique violation
    # would have retried for ever. Found by CodeRabbit on #378, and it was the exact failure the
    # permanent default exists to prevent, introduced by an entry meant to be helpful.
    IntegrityError: FailureClass.PERMANENT,
    DataError: FailureClass.PERMANENT,
    ProgrammingError: FailureClass.PERMANENT,
    ArtifactCorrupt: FailureClass.PERMANENT,
    IntegrityRecordMissing: FailureClass.PERMANENT,
    ArtifactConflict: FailureClass.PERMANENT,
    CapabilityInvalid: FailureClass.PERMANENT,
    ValueError: FailureClass.PERMANENT,
    TypeError: FailureClass.PERMANENT,
}


class InputNotNamed(ValueError):
    """`NEEDS_INPUT` was entered without saying what input is needed.

    Refused, because a package waiting for an unnamed answer has stopped silently while wearing a state
    name — nobody can act on it, and nobody can tell it apart from a package that is simply stuck.
    `AGENTS.md` §2.2: silence must never read as completion, and this is silence reading as *progress*.
    """


#: What each side state means, in plain English, for the reason line and for a reviewer reading it.
SIDE_STATE_DESCRIPTIONS: dict[PackageState, str] = {
    PackageState.FAILED_RETRYABLE: "stopped by something that may work on a retry",
    PackageState.FAILED_PERMANENT: "stopped by something a retry cannot fix",
    PackageState.NEEDS_INPUT: "waiting for an answer from somebody",
    PackageState.CANCELLED: "cancelled",
    PackageState.SUPERSEDED: "replaced by a newer revision",
}


def is_a_side_state(state: PackageState) -> bool:
    """Whether a package in this state is not going to finish on its own."""
    return state in SIDE_STATE_DESCRIPTIONS


def classify(error: BaseException) -> FailureClass:
    """Whether trying this again could help.

    Walks the exception's MRO, so a subclass inherits its parent's classification and adding one entry
    does not silently unclassify its children. The most specific match wins, which is what makes
    listing both `OperationalError` and `DBAPIError` safe.

    **An unclassified type is `PERMANENT`.** Not a shrug — the deliberate direction to fail in. A
    permanent failure is a visible outcome somebody investigates; a wrongly retryable one is a loop
    that spends money and reports nothing.
    """
    for kind in type(error).__mro__:
        classification = FAILURE_CLASSIFICATION.get(kind)
        if classification is not None:
            return classification
    return FailureClass.PERMANENT


def _state_for(failure_class: FailureClass) -> PackageState:
    return (
        PackageState.FAILED_RETRYABLE
        if failure_class is FailureClass.RETRYABLE
        else PackageState.FAILED_PERMANENT
    )


def enter_failure(
    session: Session,
    package_revision_id: UUID,
    *,
    actor: str,
    error: BaseException | None = None,
    failure_class: FailureClass | None = None,
    reason: str | None = None,
) -> PackageStateEvent:
    """Record that a package stopped, in the right one of the two failure states.

    Give it either the exception (`error`) or the class (`failure_class`). Passing the exception is the
    normal path and lets the table decide; passing the class is for a caller that already knows and
    cannot be classified here — `extraction/`'s own failures, which this module may not import.

    The reason records what happened *and* how it was classified, because "failed" without either is a
    state a reviewer can see and not act on. Nothing commits; the caller owns the transaction.

    Raises:
        ValueError: neither or both of `error` and `failure_class` were given. Both would let them
            disagree, and a disagreement about whether to retry is exactly what the table exists to
            prevent.
        IllegalTransition: the package is not in a state that can fail — see `PROCESSING_STATES`.
    """
    if (error is None) == (failure_class is None):
        raise ValueError(
            "pass either `error` or `failure_class`, not both and not neither. Both could disagree "
            "about whether to retry, which is the decision the classification table exists to make "
            "in one place."
        )

    resolved = failure_class if failure_class is not None else classify(error)  # type: ignore[arg-type]
    described = SIDE_STATE_DESCRIPTIONS[_state_for(resolved)]
    if reason is None:
        detail = (
            f"{type(error).__name__}: {error}" if error is not None else "reported by the caller"
        )
        reason = f"{described} — {detail}"

    return transition(
        session,
        package_revision_id,
        _state_for(resolved),
        actor=actor,
        reason=reason,
    )


def enter_needs_input(
    session: Session,
    package_revision_id: UUID,
    *,
    actor: str,
    needed: str,
) -> PackageStateEvent:
    """Stop the package and say what answer would let it continue.

    `needed` is plain English a reviewer can act on — *"the cabinet schedule for wall B is missing"* —
    not an error code. It is written into the state event's reason, so it appears in the history
    `app/lifecycle/events.py` renders rather than in a field only code reads.

    Raises:
        InputNotNamed: `needed` is blank. A package waiting for an unnamed answer cannot be acted on
            and cannot be told apart from one that is simply stuck.
        IllegalTransition: the package is not in a state that can stop for input.
    """
    if not isinstance(needed, str) or not needed.strip():
        raise InputNotNamed(
            "entering NEEDS_INPUT requires saying what input is needed, in plain English a reviewer "
            "can act on. A package waiting for an unnamed answer has stopped silently while looking "
            "like it is making progress."
        )

    return transition(
        session,
        package_revision_id,
        PackageState.NEEDS_INPUT,
        actor=actor,
        reason=f"{SIDE_STATE_DESCRIPTIONS[PackageState.NEEDS_INPUT]} — {needed.strip()}",
    )


def cancel(
    session: Session,
    package_revision_id: UUID,
    *,
    actor: str,
    reason: str,
) -> PackageStateEvent:
    """Cancel a package revision. Terminal — nothing resumes from here.

    `CANCELLED` has no outgoing edge in the transition table (#209), so this cannot be undone by a state
    change. Restarting the work means a new revision, which leaves the cancellation visible instead of
    replacing it: somebody decided to stop, and that decision is part of the record.

    A reason is required. "Cancelled" with no explanation is the one thing a reviewer cannot argue with
    and cannot learn from.

    Raises:
        ValueError: `reason` is blank.
        IllegalTransition: the package is already final, or is a review outcome — a signed-off approval
            is a decision that was taken, and cancelling it afterwards would rewrite it.
    """
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError(
            "cancelling requires a reason. Somebody decided to stop this package, and the decision is "
            "part of the record — 'cancelled' on its own tells the next reader nothing."
        )

    return transition(
        session,
        package_revision_id,
        PackageState.CANCELLED,
        actor=actor,
        reason=reason.strip(),
    )
