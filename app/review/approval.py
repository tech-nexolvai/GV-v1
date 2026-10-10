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
from datetime import datetime
from uuid import UUID

from sqlalchemy import Subquery, func, select, union_all
from sqlalchemy.orm import Session

from app.auth.roles import Action, Principal
from app.db.base import utc_now
from app.lifecycle.states import transition
from app.models.document import PackageRevisionDocument, Page
from app.models.drawing import (
    CountertopRunDecision,
    DrawingItem,
    DrawingView,
    PartConfirmation,
    PartProposal,
    ReadingPart,
    ViewRoleConfirmation,
)
from app.models.evidence import (
    ArchitectPairingRecord,
    CanonicalObservation,
    ItemClassification,
    LayoutConfirmation,
    ObservationCandidate,
    SlotRowReviewDecision,
)
from app.models.package import Package, PackageRevision, PackageState, PackageStateEvent
from app.models.parameters import ParameterSet
from app.models.review import Approval, ApprovedFinding, ReviewAction, ReviewSession
from app.models.verdicts import CheckRun, Finding
from app.review.carry_over import DecisionRecords, decision_holds, decision_records
from app.review.requirements import BLOCKING_OUTCOMES
from app.review.session import complete_session
from evidence.canonical import EvidenceStatus
from rules.parameters import ParameterLayer
from workflow.view_roles import CODE_CONFIRMERS

REVIEW_REQUIRED = "REVIEW_REQUIRED"

__all__ = [
    "INPUTS_CHANGED_NEEDS_RERUN",
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

    The decision may be the reviewer's own on this finding or one carried over an unchanged re-run
    (#1073); `decision_records` is the one reader both use, and `decision_holds` the one rule.
    """
    if not findings:
        return ()
    records = decision_records(db, [finding.id for finding in findings])
    return _unaddressed_from_records(findings, records, when=utc_now())


def _unaddressed_from_records(
    findings: tuple[Finding, ...] | list[Finding],
    records: DecisionRecords,
    *,
    when: datetime,
) -> tuple[UUID, ...]:
    """Shared decision rule for single-revision and batched readiness reads."""
    if not findings:
        return ()
    required = {finding.id for finding in findings if finding.outcome in BLOCKING_OUTCOMES}
    ids = {finding.id for finding in findings}
    corrected = set(records.corrected) & ids
    addressed = {finding.id for finding in findings if decision_holds(finding, records, when=when)}
    return tuple(sorted((required | corrected) - addressed, key=str))


#: Why sign-off waits when a reviewer changed something the checks read after they last ran (#1137).
INPUTS_CHANGED_NEEDS_RERUN = (
    "You changed inputs after the last check run. Run the checks before signing off."
)


#: Who records a drawing's role when code, not a person, decided it (`workflow/view_roles.py`).
_CODE_CONFIRMERS = tuple(sorted(CODE_CONFIRMERS))


def _inputs_recorded(revision_ids: Collection[UUID]) -> Subquery:
    """Every reviewer input the check stage reads, as `(revision, recorded at)`, for these revisions.

    One branch per input, each the record a person (never code) wrote, joined to the revisions whose
    checks read it the way `workflow/stages.py:run_checks` does:

    - typed measurements and run settings: this revision's RUN parameter sets
      (`workflow/measurements.py`);
    - project settings: PROJECT sets of this revision's own project (`load_parameter_sets`). GLOBAL
      is the company layer, not a reviewer's input to one review, and is not counted;
    - classifications and confirmed layout answers, keyed by the revision;
    - architect pairings a reviewer decided (#1088);
    - slot-row wall choices and typed widths, on a reading of this revision's documents;
    - countertop-run decisions, part decisions and reading-to-part links (`workflow/
      part_operands.py`), and a person's drawing-role confirmation (code's is not reviewer input);
    - a reading a reviewer confirmed (`confirm_candidate`): HUMAN_CONFIRMED evidence that no review
      action produced. An evidence confirm/correct writes one too, but it is a decision on its
      finding, already governed by `decision_holds` and the correction rule, so it is not counted
      again here.

    Each branch is filtered to `revision_ids`, so the union is one bounded statement.
    """
    ids = tuple(revision_ids)
    on_page = (
        select(Page.id.label("page_id"), PackageRevisionDocument.package_revision_id)
        .join(
            PackageRevisionDocument,
            PackageRevisionDocument.document_version_id == Page.document_version_id,
        )
        .where(PackageRevisionDocument.package_revision_id.in_(ids))
        .subquery()
    )
    on_view = (
        select(DrawingView.id.label("view_id"), on_page.c.package_revision_id)
        .join(on_page, on_page.c.page_id == DrawingView.page_id)
        .subquery()
    )
    produced_by_action = select(ReviewAction.resulting_observation_id).where(
        ReviewAction.resulting_observation_id.is_not(None)
    )
    branches = (
        select(ParameterSet.package_revision_id, ParameterSet.created_at).where(
            ParameterSet.layer == ParameterLayer.RUN.value,
            ParameterSet.package_revision_id.in_(ids),
        ),
        select(PackageRevision.id, ParameterSet.created_at)
        .join(Package, Package.id == PackageRevision.package_id)
        .join(ParameterSet, ParameterSet.project_id == Package.project_id)
        .where(ParameterSet.layer == ParameterLayer.PROJECT.value, PackageRevision.id.in_(ids)),
        select(ItemClassification.package_revision_id, ItemClassification.created_at).where(
            ItemClassification.package_revision_id.in_(ids)
        ),
        select(LayoutConfirmation.package_revision_id, LayoutConfirmation.created_at).where(
            LayoutConfirmation.package_revision_id.in_(ids)
        ),
        select(ArchitectPairingRecord.package_revision_id, ArchitectPairingRecord.created_at).where(
            ArchitectPairingRecord.package_revision_id.in_(ids),
            ArchitectPairingRecord.decided_by.is_not(None),
        ),
        select(on_page.c.package_revision_id, SlotRowReviewDecision.created_at)
        .join(
            ObservationCandidate, ObservationCandidate.id == SlotRowReviewDecision.row_candidate_id
        )
        .join(on_page, on_page.c.page_id == ObservationCandidate.page_id),
        select(on_view.c.package_revision_id, CountertopRunDecision.created_at)
        .join(DrawingItem, DrawingItem.id == CountertopRunDecision.countertop_item_id)
        .join(on_view, on_view.c.view_id == DrawingItem.drawing_view_id),
        select(on_view.c.package_revision_id, PartConfirmation.created_at)
        .join(PartProposal, PartProposal.id == PartConfirmation.part_proposal_id)
        .join(on_view, on_view.c.view_id == PartProposal.drawing_view_id),
        select(on_page.c.package_revision_id, ReadingPart.created_at)
        .join(CanonicalObservation, CanonicalObservation.id == ReadingPart.canonical_observation_id)
        .join(on_page, on_page.c.page_id == CanonicalObservation.page_id),
        select(on_view.c.package_revision_id, ViewRoleConfirmation.created_at)
        .join(on_view, on_view.c.view_id == ViewRoleConfirmation.drawing_view_id)
        .where(ViewRoleConfirmation.confirmed_by.not_in(_CODE_CONFIRMERS)),
        select(on_page.c.package_revision_id, CanonicalObservation.created_at)
        .join(on_page, on_page.c.page_id == CanonicalObservation.page_id)
        .where(
            CanonicalObservation.status == EvidenceStatus.HUMAN_CONFIRMED.value,
            CanonicalObservation.id.not_in(produced_by_action),
        ),
    )
    return union_all(*branches).subquery()


def _revisions_with_unchecked_inputs(db: Session, revision_ids: Collection[UUID]) -> set[UUID]:
    """Revisions holding a reviewer input newer than their latest live check run (#1088, #1137).

    The same rule as a corrected value: something a reviewer recorded after the checks ran has not
    been checked yet, so it must not be signed off past. Only the live runs count: a re-run
    supersedes the old ones and clears this. A revision never checked is not listed here; it has no
    findings, which readiness already refuses. One statement for any number of revisions.
    """
    if not revision_ids:
        return set()
    checked = (
        select(
            CheckRun.package_revision_id.label("revision_id"),
            func.max(CheckRun.created_at).label("checked_at"),
        )
        .where(CheckRun.package_revision_id.in_(revision_ids), CheckRun.superseded_at.is_(None))
        .group_by(CheckRun.package_revision_id)
        .subquery()
    )
    recorded = _inputs_recorded(revision_ids)
    revision_column, recorded_at = recorded.c[0], recorded.c[1]
    return set(
        db.scalars(
            select(revision_column)
            .join(checked, checked.c.revision_id == revision_column)
            .where(recorded_at > checked.c.checked_at)
            .distinct()
        ).all()
    )


@dataclass(frozen=True, slots=True)
class ApprovalReadiness:
    revision_id: UUID
    can_approve: bool
    blocking_findings: int
    blocking_finding_ids: tuple[UUID, ...]
    reason: str | None


def approval_readiness(db: Session, revision_id: UUID) -> ApprovalReadiness:
    """The live finding set and lifecycle, shared by the screen and the approval write."""
    return readiness_and_decisions(db, revision_id)[0]


def readiness_and_decisions(
    db: Session, revision_id: UUID
) -> tuple[ApprovalReadiness, DecisionRecords]:
    """Readiness plus the decision records it was computed from, for a screen that shows both.

    The countertop results read the standing decision of every row from the very records sign-off
    readiness used, so the two cannot disagree and the decisions are not read twice.
    """
    findings = _findings(db, revision_id)
    records = decision_records(db, [finding.id for finding in findings])
    blocked = _unaddressed_from_records(findings, records, when=utc_now())
    revision = db.get(PackageRevision, revision_id)
    reason = None
    if not findings:
        reason = "There are no findings to sign off. Run the checks first."
    elif blocked:
        reason = f"{len(blocked)} findings still need a valid reviewer decision or a check rerun after correction. Add a note when required."
    elif revision_id in _revisions_with_unchecked_inputs(db, [revision_id]):
        reason = INPUTS_CHANGED_NEEDS_RERUN
    elif revision is None or revision.state != PackageState.AWAITING_REVIEW.value:
        reason = "The package is not awaiting review."
    return ApprovalReadiness(revision_id, reason is None, len(blocked), blocked, reason), records


def approval_readiness_many(db: Session, revision_ids: list[UUID]) -> dict[UUID, ApprovalReadiness]:
    """The same readiness rule for a page of revisions, using a bounded query plan.

    This is used by the Documents summary so the screen's decision count cannot drift from sign-off
    readiness and does not issue one readiness query per package.
    """
    if not revision_ids:
        return {}
    findings_by_revision: dict[UUID, list[Finding]] = {identity: [] for identity in revision_ids}
    found = db.scalars(
        select(Finding)
        .join(CheckRun, CheckRun.id == Finding.check_run_id)
        .where(Finding.package_revision_id.in_(revision_ids), CheckRun.superseded_at.is_(None))
        .order_by(Finding.created_at, Finding.id)
    ).all()
    finding_by_id: dict[UUID, Finding] = {}
    for finding in found:
        findings_by_revision[finding.package_revision_id].append(finding)
        finding_by_id[finding.id] = finding
    records = decision_records(db, list(finding_by_id))
    revisions = {
        revision.id: revision
        for revision in db.scalars(
            select(PackageRevision).where(PackageRevision.id.in_(revision_ids))
        ).all()
    }
    when = utc_now()
    unchecked = _revisions_with_unchecked_inputs(db, revision_ids)
    result: dict[UUID, ApprovalReadiness] = {}
    for revision_id, findings in findings_by_revision.items():
        blocked = _unaddressed_from_records(tuple(findings), records, when=when)
        revision = revisions.get(revision_id)
        reason = None
        if not findings:
            reason = "There are no findings to sign off. Run the checks first."
        elif blocked:
            reason = f"{len(blocked)} findings still need a valid reviewer decision or a check rerun after correction. Add a note when required."
        elif revision_id in unchecked:
            reason = INPUTS_CHANGED_NEEDS_RERUN
        elif revision is None or revision.state != PackageState.AWAITING_REVIEW.value:
            reason = "The package is not awaiting review."
        result[revision_id] = ApprovalReadiness(
            revision_id, reason is None, len(blocked), blocked, reason
        )
    return result


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
    if review.package_revision_id in _revisions_with_unchecked_inputs(
        db, [review.package_revision_id]
    ):
        raise UnaddressedReviewRequired(INPUTS_CHANGED_NEEDS_RERUN)

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
