"""Approve a reviewed package revision, or return it for named changes.

The service selects findings from PostgreSQL rather than accepting an approval manifest from the
caller.  An approval therefore records the exact immutable finding rows that existed for the package
revision at sign-off.  A ``REVIEW_REQUIRED`` finding needs at least one explicit review action before
approval; silence is never treated as resolution.

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
from app.lifecycle.states import transition
from app.models.package import PackageState, PackageStateEvent
from app.models.review import Approval, ApprovedFinding, ReviewAction, ReviewSession
from app.models.verdicts import Finding
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
    """At least one abstaining finding has no explicit reviewer action."""


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
    review = db.get(ReviewSession, review_session_id)
    if review is None:
        raise NoSuchReviewSession(f"no review session {review_session_id}")
    return review


def _findings(db: Session, package_revision_id: UUID) -> tuple[Finding, ...]:
    return tuple(
        db.scalars(
            select(Finding)
            .where(Finding.package_revision_id == package_revision_id)
            .order_by(Finding.created_at, Finding.id)
        ).all()
    )


def _unaddressed(db: Session, findings: tuple[Finding, ...]) -> tuple[UUID, ...]:
    required = {finding.id for finding in findings if finding.outcome == REVIEW_REQUIRED}
    if not required:
        return ()
    addressed = set(
        db.scalars(
            select(ReviewAction.finding_id).where(ReviewAction.finding_id.in_(required))
        ).all()
    )
    return tuple(sorted(required - addressed, key=str))


def approve_package(
    db: Session, *, principal: Principal, review_session_id: UUID
) -> ApprovalDecision:
    """Approve the server-selected finding set after every abstention was explicitly addressed."""
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
            f"REVIEW REQUIRED findings must each be explicitly addressed before approval: {listed}"
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
