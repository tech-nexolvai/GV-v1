"""Approve a reviewed package revision, or return it for named changes.

The service selects findings from PostgreSQL rather than accepting an approval manifest from the
caller.  An approval therefore records the exact immutable finding rows that existed for the package
revision at sign-off. FAIL, REVIEW_REQUIRED and NOT_FOUND each need an explicit review action before
approval; silence is never treated as resolution. Confirming or dismissing an abstention needs a note.

Both decisions use :func:`app.lifecycle.states.transition`, the sole package-state writer.  That
keeps approval unreachable from processing, failure and other side states.  Nothing here commits:
the approval links or change-request reason, terminal state and completed review session belong to
one caller-owned transaction.

Source: issue #231 · Design: ``docs/DESIGN_PRODUCT.md`` §4 ·
Verification: ``tests/review/test_approval.py``
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.roles import Action, Principal
from app.db.base import utc_now
from app.lifecycle.states import transition
from app.models.package import PackageRevision, PackageState, PackageStateEvent
from app.models.review import (
    Approval,
    ApprovedFinding,
    ReviewAction,
    ReviewException,
    ReviewSession,
)
from app.models.verdicts import CheckRun, Finding
from app.review.exceptions import ExceptionGrant, FindingRef, decide
from app.review.requirements import BLOCKING_OUTCOMES, needs_note
from app.review.session import complete_session

REVIEW_REQUIRED = "REVIEW_REQUIRED"

__all__ = [
    "ApprovalDecision",
    "ApprovalNotAuthorised",
    "ApprovalRefused",
    "ChangeRequestDecision",
    "DriverFindingRequired",
    "FindingOutsideReview",
    "NoFindingsToApprove",
    "NoSuchReviewSession",
    "UnaddressedReviewRequired",
    "approve_package",
    "request_changes",
]


class ApprovalRefused(Exception):
    """Base class for a package decision that cannot safely be recorded."""


class ApprovalNotAuthorised(ApprovalRefused):
    """The principal is not allowed to sign off packages."""


class NoSuchReviewSession(ApprovalRefused):
    """The named review session does not exist."""


class NoFindingsToApprove(ApprovalRefused):
    """The revision has no finding rows, so approval would turn silence into PASS."""


class UnaddressedReviewRequired(ApprovalRefused):
    """At least one blocking finding has no valid explicit reviewer action."""


class DriverFindingRequired(ApprovalRefused):
    """A change request did not name any finding that drove it."""


class FindingOutsideReview(ApprovalRefused):
    """A proposed driver belongs to another package revision or does not exist."""


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    """The immutable approval and lifecycle event written together."""

    approval: Approval
    finding_ids: tuple[UUID, ...]
    state_event: PackageStateEvent


@dataclass(frozen=True, slots=True)
class ChangeRequestDecision:
    """The validated findings driving an immutable change-request transition."""

    finding_ids: tuple[UUID, ...]
    state_event: PackageStateEvent


def _authorise(principal: Principal) -> None:
    # Before lookup, so an unauthorised caller learns nothing about stored review sessions.
    if not principal.may(Action.APPROVE_PACKAGE):
        raise ApprovalNotAuthorised(
            "this action requires a reviewer or administrator authorised to approve packages"
        )
    if not principal.id.strip():
        raise ApprovalNotAuthorised("a package decision must name the person making it")


def _review(db: Session, review_session_id: UUID) -> ReviewSession:
    review = db.scalar(
        select(ReviewSession)
        .where(ReviewSession.id == review_session_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if review is None:
        raise NoSuchReviewSession(f"no review session {review_session_id}")
    return review


def _findings(db: Session, package_revision_id: UUID) -> tuple[Finding, ...]:
    """The findings in force for this revision — the live run's, not every run's ever.

    **Superseded runs are excluded, and leaving them in was a real defect (#532).** `run_checks`
    supersedes its previous run rather than deleting it, because a finding cites the run that judged
    it. Re-running is ordinary: it is what happens the moment a reviewer confirms a reading and the
    checks can decide from evidence rather than abstaining.

    Without the filter, approval read both runs at once. Two consequences, and the second is worse
    than the first. A reviewer who addressed every abstention on screen was refused, because the
    superseded run's abstentions were unaddressed and invisible — the list, the summary and the
    export all show only the live run. And once approved, `ApprovedFinding` rows were written for
    superseded findings too, so the record of what GV signed for included verdicts that had already
    been replaced.

    The same filter `app/api/findings.py` applies, and for the reason stated there: the list, the
    summary and the export must not disagree about which run is current. Approval is the fourth
    reader of that question and was answering it differently.
    """
    return tuple(
        db.scalars(
            select(Finding)
            .join(CheckRun, CheckRun.id == Finding.check_run_id)
            .where(
                Finding.package_revision_id == package_revision_id,
                CheckRun.superseded_at.is_(None),
            )
            .order_by(Finding.created_at, Finding.id)
        ).all()
    )


def _unaddressed(db: Session, findings: tuple[Finding, ...]) -> tuple[UUID, ...]:
    """Only a real, still-valid decision clears a finding; a correction requires a rerun.

    Even a correction with its proper ledger row cannot decide the old check. Nor can a later
    confirm/except dismiss that pending rerun: only superseding the old run removes it from here.
    This also protects a previously passing finding whose evidence was subsequently corrected.
    """
    by_id = {finding.id: finding for finding in findings}
    if not by_id:
        return ()
    required = {finding.id for finding in findings if finding.outcome in BLOCKING_OUTCOMES}
    latest: dict[UUID, ReviewAction] = {}
    corrected: set[UUID] = set()
    for action in db.scalars(
        select(ReviewAction)
        .where(ReviewAction.finding_id.in_(by_id))
        .order_by(ReviewAction.created_at, ReviewAction.id)
    ):
        latest[action.finding_id] = action
        if action.action == "correct":
            corrected.add(action.finding_id)
    grants = {
        grant.review_action_id: ExceptionGrant.from_stored(grant)
        for grant in db.scalars(
            select(ReviewException).where(
                ReviewException.review_action_id.in_([action.id for action in latest.values()])
            )
        )
    }
    when = utc_now()
    addressed: set[UUID] = set()
    for identity, action in latest.items():
        if identity in corrected:
            continue
        finding = by_id[identity]
        if action.action in {"confirm", "dismiss"}:
            if not needs_note(finding.outcome, action.action) or bool(
                action.note and action.note.strip()
            ):
                addressed.add(identity)
        elif (
            action.action == "except"
            and action.id in grants
            and decide(
                FindingRef(
                    finding_id=identity,
                    package_revision_id=finding.package_revision_id,
                    item_id=finding.scope_item_id,
                ),
                (grants[action.id],),
                when=when,
            ).is_excepted
        ):
            addressed.add(identity)
    return tuple(sorted((required | corrected) - addressed, key=str))


@dataclass(frozen=True, slots=True)
class ApprovalReadiness:
    revision_id: UUID
    can_approve: bool
    blocking_findings: int
    blocking_finding_ids: tuple[UUID, ...]
    reason: str | None


def approval_readiness(db: Session, revision_id: UUID) -> ApprovalReadiness:
    """The live finding set and lifecycle, shared by the screen and the approval write."""
    findings = _findings(db, revision_id)
    blocked = _unaddressed(db, findings)
    revision = db.get(PackageRevision, revision_id)
    reason = None
    if not findings:
        reason = "There are no findings to sign off. Run the checks first."
    elif blocked:
        reason = f"{len(blocked)} findings still need a valid reviewer decision or a check rerun after correction. Add a note when required."
    elif revision is None or revision.state != PackageState.AWAITING_REVIEW.value:
        reason = "The package is not awaiting review."
    return ApprovalReadiness(revision_id, reason is None, len(blocked), blocked, reason)


def approve_package(
    db: Session, *, principal: Principal, review_session_id: UUID
) -> ApprovalDecision:
    """Approve the server-selected finding set after every blocking result was addressed."""
    _authorise(principal)
    review = _review(db, review_session_id)
    findings = _findings(db, review.package_revision_id)
    if not findings:
        raise NoFindingsToApprove(
            "this package revision has no findings to approve; an empty result is not a clean review"
        )

    unresolved = _unaddressed(db, findings)
    if unresolved:
        listed = ", ".join(str(finding_id) for finding_id in unresolved)
        raise UnaddressedReviewRequired(
            f"FAIL, REVIEW REQUIRED and NOT FOUND findings must each be explicitly addressed before approval: {listed}"
        )

    approval = Approval(package_revision_id=review.package_revision_id, approved_by=principal.id)
    event = transition(
        db,
        review.package_revision_id,
        PackageState.APPROVED,
        actor=principal.id,
        reason=f"approved {len(findings)} finding revisions under approval {approval.id}",
    )
    db.add(approval)
    db.add_all(
        ApprovedFinding(
            approval_id=approval.id,
            finding_id=finding.id,
            package_revision_id=review.package_revision_id,
        )
        for finding in findings
    )
    complete_session(db, review_session_id=review.id)
    db.flush()
    from app.review.signed_exports import request_signed_exports

    request_signed_exports(db, approval.id)
    return ApprovalDecision(approval, tuple(finding.id for finding in findings), event)


def request_changes(
    db: Session,
    *,
    principal: Principal,
    review_session_id: UUID,
    finding_ids: Collection[UUID],
) -> ChangeRequestDecision:
    """Request changes for a non-empty, server-validated set of findings."""
    _authorise(principal)
    review = _review(db, review_session_id)
    requested = tuple(sorted(set(finding_ids), key=str))
    if not requested:
        raise DriverFindingRequired(
            "a change request must name at least one finding that drove the request"
        )

    resolved = set(
        db.scalars(
            select(Finding.id).where(
                Finding.id.in_(requested),
                Finding.package_revision_id == review.package_revision_id,
            )
        ).all()
    )
    missing = tuple(finding_id for finding_id in requested if finding_id not in resolved)
    if missing:
        listed = ", ".join(str(finding_id) for finding_id in missing)
        raise FindingOutsideReview(
            f"these findings are not part of the package revision under review: {listed}"
        )

    listed = ", ".join(str(finding_id) for finding_id in requested)
    event = transition(
        db,
        review.package_revision_id,
        PackageState.CHANGES_REQUESTED,
        actor=principal.id,
        reason=f"changes requested for findings: {listed}",
    )
    complete_session(db, review_session_id=review.id)
    db.flush()
    return ChangeRequestDecision(requested, event)
