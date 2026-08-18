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

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import utc_now
from app.models.package import PackageRevision, PackageState
from app.models.review import ReviewAction, ReviewActionKind, ReviewSession
from app.models.verdicts import Finding

__all__ = [
    "ActionOutsideTheSession",
    "ActorNotNamed",
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
