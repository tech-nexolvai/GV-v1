"""Opening a review session, and the four things a reviewer may do to a finding.

The golden rule ends "a reviewer signs off". This module is where that begins: a named person opens a
sitting over one package revision, and everything they then do is written down as a new row that
nobody can go back and tidy.

**The caller names an id; the server decides what it means.** Every function here takes UUIDs and
resolves them against real rows before writing anything. In particular, the revision an action is
recorded against is read off the *finding row on the server* — there is no parameter for it, so a
caller cannot say which revision they were reviewing. That is `C2.5`: a client-supplied value would
let the thing being acted on be chosen by whoever is asking, which is the same hole as trusting a
client-supplied id.

**There is no way to change an action.** Not "we ask you not to" — there is no function for it, and
`review_actions` carries the append-only trigger from `#202`, so an `UPDATE` is refused by PostgreSQL
whoever is connected. A reviewer who changes their mind records a second action, and the first one
stays. That is the whole point of the ledger: the record of what we first concluded is exactly what
somebody would be tempted to remove.

**A superseded revision cannot be reviewed.** Signing off a drawing that has already been replaced
produces an approval that names work nobody is going to build, and it is worse than no approval
because a named human is on it. Two things can make a revision superseded and nothing in the schema
keeps them in step, so `open_session` asks both — see `_is_superseded`.

**Refusals are one family on purpose.** Everything raised here derives from `ReviewRefused`, so the
HTTP boundary can map the lot to a single 404 without enumerating them. Project scope is an isolation
boundary (`docs/DESIGN_PLATFORM.md` §4.3), a 403 confirms the thing exists, and a boundary that
answers "yes, but not for you" has already told the caller what they wanted to know. The membership
check itself belongs at the router, in `require_project_access` — applied there so that no route can
forget it. This module deliberately does not repeat it, and it says nothing in a refusal that the
caller did not already supply.

Nothing here imports `verdict/`, `rules/`, `extraction/` or `retrieval/`. `docs/DESIGN_PRODUCT.md` §2
allows `app/review/` only `app/` and `evidence/`, and a reviewer correction must never be able to
reach the rulebook by import.

Source: backend proposal §10.1, §10.2 · Design: `docs/DESIGN_PRODUCT.md` §4 ·
Verification: `tests/review/test_session.py`
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import utc_now
from app.models.package import PackageRevision, PackageState
from app.models.review import (
    ExceptionScope,
    ReviewAction,
    ReviewActionKind,
    ReviewException,
    ReviewSession,
)
from app.models.verdicts import Finding

__all__ = [
    "ActionOutsideTheSession",
    "ActorNotNamed",
    "ExceptionAlreadyOver",
    "ExceptionNeedsAReason",
    "ExceptionScopeMismatch",
    "NoSuchFinding",
    "NoSuchPackageRevision",
    "NoSuchReviewSession",
    "ReviewActionKind",
    "ReviewRefused",
    "RevisionSuperseded",
    "SessionAlreadyComplete",
    "UnknownReviewAction",
    "action_history",
    "complete_session",
    "exceptions_for_revision",
    "grant_exception",
    "open_session",
    "record_action",
]

# `ReviewActionKind` is re-exported rather than redefined. `#200` already put the four verbs in
# `app/models/review.py`, where the database CHECK constraint is generated from them; a second copy
# here would be a second answer to "what may a reviewer do?", and the two would drift the first time
# anybody added a verb.


class ReviewRefused(Exception):
    """Base class for every refusal in this module.

    One family so that the HTTP boundary can answer all of them with a single 404. Separate
    unrelated exception types would eventually get separate status codes, and the first one mapped to
    403 would confirm to a caller outside the project that the thing they named exists.
    """


class NoSuchPackageRevision(ReviewRefused):
    """No revision with that id. Also what a caller sees for a revision they may not reach."""


class RevisionSuperseded(ReviewRefused):
    """The revision has been replaced, so there is nothing here worth signing off."""


class NoSuchReviewSession(ReviewRefused):
    """No session with that id."""


class SessionAlreadyComplete(ReviewRefused):
    """The sitting has ended.

    Refused rather than reopened. `completed_at` says when a reviewer stopped, and moving it or
    appending to a closed session would make the record say something that did not happen.
    """


class NoSuchFinding(ReviewRefused):
    """No finding with that id."""


class ExceptionNeedsAReason(ReviewRefused):
    """An exception with no explanation. Refused, because it is unreviewable.

    `app/review/exceptions.py` says it plainly — an exception nobody explained is one nobody can
    review, and the reason is the sentence a future reader needs most. The database refuses an empty
    string; this refuses whitespace too, and says why rather than raising an integrity error.
    """


class ExceptionAlreadyOver(ReviewRefused):
    """The expiry has already passed, so the exception would cover nothing.

    The database can only check `expires_at > created_at` — a clock comparison in a CHECK is not
    immutable and PostgreSQL refuses it — so "already over" is enforced here, where a clock is
    allowed. Accepting it would let a reviewer believe they had granted cover they had not.
    """


class ExceptionScopeMismatch(ReviewRefused):
    """A finding-scoped exception naming a different finding.

    The one scope where naming something else is certainly a mistake rather than a wider decision.
    Item and package scopes are *meant* to name something larger, so they are not checked here —
    `decide` refuses to widen at read time instead, by matching scope and id exactly.
    """


class ActionOutsideTheSession(ReviewRefused):
    """The finding belongs to a different package revision than the session under review.

    The database refuses this too, through the composite foreign key on `review_actions`. It is
    checked here as well so the caller gets a sentence rather than a constraint violation, and so the
    refusal happens before anything is written.
    """


class ActorNotNamed(ReviewRefused):
    """Nobody was named.

    `docs/DESIGN_PRODUCT.md` §4: there is no anonymous confirmation. A confirmation is a direct write
    into the trusted set, and one that no human is attached to is a door in the back of the evidence
    gate.
    """


class UnknownReviewAction(ReviewRefused):
    """Not one of the four verbs.

    There are four and there is no fifth — in particular no `edit`, which would collapse confirm,
    correct, except and dismiss into one and defeat the ledger that keeps them apart.
    """


def _is_superseded(db: Session, revision: PackageRevision) -> bool:
    """Ask both questions, because either one alone can be wrong.

    `state` is set by whatever moved the package lifecycle on. `supersedes_id` is set by whatever
    created the newer revision. Nothing in the schema keeps the two in step, so a revision can have a
    successor while its own state still reads `AWAITING_REVIEW`, and that revision is superseded in
    every sense that matters to a reviewer.

    Fails closed: if either says superseded, it is superseded.
    """
    if revision.state == PackageState.SUPERSEDED:
        return True
    successor = db.scalars(
        select(PackageRevision.id).where(PackageRevision.supersedes_id == revision.id).limit(1)
    ).first()
    return successor is not None


def open_session(db: Session, *, package_revision_id: UUID, reviewer: str) -> ReviewSession:
    """Start a sitting over one package revision. Refuses if that revision has been replaced.

    `db` is the SQLAlchemy session. It is not called `session` because in this module that word
    already means a reviewer's sitting, and a function holding both cannot use one name for either.

    The row is flushed but not committed: opening a session is usually the first step of a larger
    unit of work, and committing here would leave a session behind when the rest of that work failed.
    The caller's `unit_of_work` decides.
    """
    if not reviewer.strip():
        # Checked before anything is looked up, so a request with no reviewer learns nothing about
        # which revisions exist.
        raise ActorNotNamed(
            "a review session needs a named reviewer. There is no anonymous review: the point of "
            "the record is that a person can be asked about it later."
        )

    revision = db.get(PackageRevision, package_revision_id)
    if revision is None:
        raise NoSuchPackageRevision(
            f"no package revision {package_revision_id}. If you expected one, check the id — this "
            "is also the answer for a revision outside your projects."
        )

    if _is_superseded(db, revision):
        raise RevisionSuperseded(
            f"package revision {package_revision_id} has been superseded, so it cannot be reviewed. "
            "Open a session over the revision that replaced it: signing off a drawing that has "
            "already been replaced puts a named person's approval on work nobody will build."
        )

    review_session = ReviewSession(package_revision_id=revision.id, reviewer=reviewer)
    db.add(review_session)
    db.flush()
    return review_session


def complete_session(db: Session, *, review_session_id: UUID) -> ReviewSession:
    """End the sitting. Refuses a session that has already ended.

    Not idempotent on purpose. Completing twice would move `completed_at`, and a timestamp that can
    be moved cannot answer "when did this reviewer stop?".
    """
    review_session = db.get(ReviewSession, review_session_id)
    if review_session is None:
        raise NoSuchReviewSession(f"no review session {review_session_id}.")
    if review_session.completed_at is not None:
        raise SessionAlreadyComplete(
            f"review session {review_session_id} was completed at "
            f"{review_session.completed_at.isoformat()} and cannot be completed again."
        )
    review_session.completed_at = utc_now()
    db.flush()
    return review_session


def record_action(
    db: Session,
    *,
    review_session_id: UUID,
    finding_id: UUID,
    action: ReviewActionKind,
    actor: str,
    note: str | None = None,
) -> ReviewAction:
    """Write down one thing a reviewer did to one finding. Append-only, always a new row.

    There is deliberately no `package_revision_id` parameter. The revision stored on the action is
    read off the finding row the server just loaded, so a caller cannot state which revision they
    were reviewing — that is what "an action references a server-side finding revision, never a
    client-supplied value" means in code rather than in a comment.

    A `correct` action is only half of the record: the correction ledger stores the original value
    beside the corrected one, and that entry belongs in the same transaction as this row. Writing it
    is `ledger.py`'s job (`D5.3`) and this module does not do it, so a caller recording a correction
    must write the ledger entry itself until that module lands.
    """
    if not actor.strip():
        raise ActorNotNamed(
            "a review action must name who did it. The session records who opened it, but a sitting "
            "can be picked up by somebody else, so the action has to say who actually acted."
        )

    try:
        kind = ReviewActionKind(action)
    except ValueError as unknown:
        raise UnknownReviewAction(
            f"{action!r} is not a review action. There are four — "
            f"{', '.join(sorted(member.value for member in ReviewActionKind))} — and no general "
            "'edit': a changed mind is a new action, not a rewrite of the old one."
        ) from unknown

    review_session = db.get(ReviewSession, review_session_id)
    if review_session is None:
        raise NoSuchReviewSession(f"no review session {review_session_id}.")
    if review_session.completed_at is not None:
        raise SessionAlreadyComplete(
            f"review session {review_session_id} has ended, so nothing more can be recorded in it. "
            "Open a new session: an action added to a closed sitting would misdate what happened."
        )

    finding = db.get(Finding, finding_id)
    if finding is None:
        raise NoSuchFinding(f"no finding {finding_id}.")
    if finding.package_revision_id != review_session.package_revision_id:
        raise ActionOutsideTheSession(
            f"finding {finding_id} belongs to a different package revision than review session "
            f"{review_session_id}. A session reviewing one package cannot carry an action on a "
            "finding from another, or the record would misstate what was reviewed."
        )

    recorded = ReviewAction(
        review_session_id=review_session.id,
        finding_id=finding.id,
        # From the finding, not from the caller and not from the session. The three agree by the
        # check above; taking it from the finding is the honest one, because the finding is the
        # server-side row this action is about.
        package_revision_id=finding.package_revision_id,
        action=kind.value,
        actor=actor,
        note=note,
    )
    db.add(recorded)
    db.flush()
    return recorded


def grant_exception(
    db: Session,
    *,
    review_session_id: UUID,
    finding_id: UUID,
    actor: str,
    scope: ExceptionScope,
    scope_id: UUID,
    reason: str,
    expires_at: datetime,
    now: datetime | None = None,
) -> tuple[ReviewAction, ReviewException]:
    """Record one exception and the action that authorised it, in one transaction.

    **Nothing has ever written one of these.** The table, its required expiry, the exact scope
    matching and `apply_exceptions` were all built and left with no way in, so the control existed
    and did nothing: no exception could be granted, and no finding was ever checked against one.

    Two rows, and both are needed for the record to mean anything. The action says a reviewer did
    something to a finding; the exception says what they accepted, how far it reaches and when it
    runs out. `review_exceptions` resolves `(review_action_id, action)` against `review_actions`, so
    an exception cannot hang off a `confirm` — which would be a check switched off by a record
    saying the reviewer agreed with it.

    The action row comes from `record_action`, so every refusal it makes applies here too: a closed
    sitting, an unknown finding, a finding from another revision, an unnamed actor. Duplicating
    those checks would be a second answer to "may this be recorded".

    **`approved_by` is the actor, not a field.** `AGENTS.md` §2.6 calls an anonymous exception
    nobody's decision, and the reason lands on the action's `note` as well so that the action
    history reads on its own — `action_history` is what a later reader walks, and an action saying
    only "except" would send them hunting for the terms.

    `scope_id` is not checked to exist. It cannot be: the three scopes name rows in three different
    tables, which is why the column is not a foreign key. A finding-scoped exception naming a
    different finding *is* checked, because that one is certainly a mistake.
    """
    if not reason.strip():
        raise ExceptionNeedsAReason(
            "an exception needs a reason. One nobody explained is one nobody can review, and it is "
            "the sentence a future reader needs most."
        )
    if not isinstance(expires_at, datetime) or expires_at.tzinfo is None:
        raise ExceptionAlreadyOver(
            "expires_at must be timezone-aware. A naive datetime is how an exception ends up living "
            "hours longer than it was granted for."
        )
    at = now if now is not None else datetime.now(UTC)
    if at.tzinfo is None:
        raise ExceptionAlreadyOver("now must be timezone-aware")
    if expires_at <= at:
        raise ExceptionAlreadyOver(
            f"an exception expiring at {expires_at.isoformat()} is already over at "
            f"{at.isoformat()}, so it would cover nothing. Granting cover that never applies "
            "records a decision nobody can act on."
        )
    try:
        kind = ExceptionScope(scope)
    except ValueError as unknown:
        raise ExceptionScopeMismatch(
            f"{scope!r} is not an exception scope. There are three — "
            f"{', '.join(sorted(member.value for member in ExceptionScope))} — and no 'this rule, "
            "everywhere': a rule that should not fire is a rule change and goes through the rulebook."
        ) from unknown
    if kind is ExceptionScope.FINDING and scope_id != finding_id:
        raise ExceptionScopeMismatch(
            f"a finding-scoped exception must name the finding it was granted on. This one names "
            f"{scope_id} while the reviewer was looking at {finding_id}."
        )

    action = record_action(
        db,
        review_session_id=review_session_id,
        finding_id=finding_id,
        action=ReviewActionKind.EXCEPT,
        actor=actor,
        note=reason.strip(),
    )
    granted = ReviewException(
        review_action_id=action.id,
        action=ReviewActionKind.EXCEPT.value,
        scope=kind.value,
        scope_id=scope_id,
        reason=reason.strip(),
        approved_by=actor.strip(),
        expires_at=expires_at,
    )
    db.add(granted)
    db.flush()
    return action, granted


def exceptions_for_revision(
    db: Session, *, package_revision_id: UUID
) -> tuple[ReviewException, ...]:
    """Every exception granted anywhere in this revision's reviews, oldest first.

    **Expired ones included, deliberately.** `decide` needs them in order to say "this was excepted
    and the cover has run out", which is the moment a finding most needs looking at again. Filtering
    them here would make that report impossible while looking like tidiness.
    """
    rows = db.scalars(
        select(ReviewException)
        .join(ReviewAction, ReviewException.review_action_id == ReviewAction.id)
        .where(ReviewAction.package_revision_id == package_revision_id)
        .order_by(ReviewException.created_at, ReviewException.id)
    ).all()
    return tuple(rows)


def action_history(db: Session, *, finding_id: UUID) -> tuple[ReviewAction, ...]:
    """Everything any reviewer has ever done to this finding, oldest first.

    The whole history, because a changed mind is a new row and the earlier one is still true: it says
    what somebody thought at the time, and that is the part an audit asks about.

    There is deliberately no `latest_action` helper. Two actions written in the same transaction can
    share a `created_at`, and `review_actions` has no sequence column to break the tie, so a function
    returning "the current position" would sometimes pick arbitrarily while reading as authoritative.
    A caller that needs to know what stands now gets the list and can see for itself when the answer
    is ambiguous.
    """
    rows = db.scalars(
        select(ReviewAction).where(ReviewAction.finding_id == finding_id)
        # `id` only breaks ties, and it is a random UUID — see the docstring. It is here so the order
        # is at least stable between two calls rather than whatever the planner returns.
        .order_by(ReviewAction.created_at, ReviewAction.id)
    ).all()
    return tuple(rows)
