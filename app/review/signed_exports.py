"""Freeze an approval's findings and review history; never infer a new verdict."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.package import Package, PackageRevision
from app.models.review import (
    Approval,
    ApprovedFinding,
    FindingDecisionCarryover,
    ReviewAction,
    ReviewSession,
)
from app.models.rules import RuleDefinition, RuleSnapshot
from app.models.signed_exports import (
    ApprovalExportAction,
    ApprovalExportBundle,
    ApprovalExportFailure,
    ApprovalExportRetry,
    ApprovalExportSnapshot,
)
from app.models.verdicts import CheckRun, Finding, FindingEvidence, VerdictInput
from app.review.carry_over import decision_records
from app.review.signed_record import ReviewDisposition, SignedReview, SignedReviewFinding
from workflow.changed_values import ChangedValues, changed_values_for_revision
from workflow.outbox import enqueue

WORKFLOW = "generate_signed_exports"

PublicationStatus = Literal["preparing", "ready", "failed"]


class SignedExportRefused(ValueError):
    """The stored record cannot safely be published."""


class ExportSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    package_revision_id: UUID
    revision_number: int
    vendor: str | None
    review: SignedReview
    facts: tuple[dict[str, object], ...]
    changed_values: ChangedValues


def canonical(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def finding_rows(
    db: Session, approval: Approval
) -> list[tuple[Finding, CheckRun, RuleSnapshot, RuleDefinition]]:
    rows = db.execute(
        select(Finding, CheckRun, RuleSnapshot, RuleDefinition)
        .join(ApprovedFinding, ApprovedFinding.finding_id == Finding.id)
        .join(CheckRun, CheckRun.id == Finding.check_run_id)
        .join(RuleSnapshot, RuleSnapshot.id == CheckRun.rule_snapshot_id)
        .join(RuleDefinition, RuleDefinition.id == RuleSnapshot.rule_definition_id)
        .where(
            ApprovedFinding.approval_id == approval.id,
            Finding.package_revision_id == approval.package_revision_id,
        )
        .order_by(Finding.created_at, Finding.id)
    ).all()
    return [(finding, run, snapshot, definition) for finding, run, snapshot, definition in rows]


def _columns(row: object) -> dict[str, object]:
    from sqlalchemy import inspect

    mapper = inspect(type(row))
    if mapper is None:
        raise TypeError("publication needs a stored database row")
    result: dict[str, object] = {}
    for column in mapper.columns:
        value = getattr(row, column.key)
        result[column.key] = (
            value.isoformat()
            if isinstance(value, datetime)
            else str(value) if isinstance(value, UUID) else value
        )
    return result


def deterministic_facts(db: Session, approval: Approval) -> tuple[dict[str, object], ...]:
    result: list[dict[str, object]] = []
    for finding, run, snapshot, definition in finding_rows(db, approval):
        result.append(
            {
                "finding": _columns(finding),
                "rule_id": definition.rule_id,
                "snapshot_id": snapshot.snapshot_id,
                "engine_version": run.engine_version,
                "defaults_set_id": run.defaults_set_id,
                "defaults_canonical_json": run.defaults_canonical_json,
                "operands": [
                    _columns(row)
                    for row in db.scalars(
                        select(VerdictInput)
                        .where(VerdictInput.check_run_id == run.id)
                        .order_by(VerdictInput.id)
                    )
                ],
                "evidence": [
                    _columns(row)
                    for row in db.scalars(
                        select(FindingEvidence)
                        .where(FindingEvidence.finding_id == finding.id)
                        .order_by(FindingEvidence.id)
                    )
                ],
            }
        )
    return tuple(result)


def load_snapshot(db: Session, snapshot: ApprovalExportSnapshot) -> ExportSnapshot:
    if digest(snapshot.canonical_json) != snapshot.sha256:
        raise SignedExportRefused("signed review snapshot hash does not match")
    try:
        payload = ExportSnapshot.model_validate_json(snapshot.canonical_json)
    except ValidationError as exc:
        raise SignedExportRefused("signed review record is malformed or ambiguous") from exc
    approval = db.get(Approval, snapshot.approval_id)
    if (
        approval is None
        or approval.package_revision_id != snapshot.package_revision_id
        or payload.package_revision_id != snapshot.package_revision_id
        or payload.review.approval_id != approval.id
    ):
        raise SignedExportRefused("signed review belongs to a different approval or revision")
    ids = set(
        db.scalars(
            select(ApprovedFinding.finding_id).where(ApprovedFinding.approval_id == approval.id)
        )
    )
    if ids != {f.finding_id for f in payload.review.findings}:
        raise SignedExportRefused("signed review does not contain the exact approved finding set")
    facts = deterministic_facts(db, approval)
    if (
        canonical(facts) != canonical(payload.facts)
        or digest(canonical(facts)) != snapshot.deterministic_sha256
    ):
        raise SignedExportRefused("deterministic finding hash does not match the approved record")
    pinned = set(
        db.scalars(
            select(ApprovalExportAction.review_action_id).where(
                ApprovalExportAction.snapshot_id == snapshot.id
            )
        )
    )
    if pinned != {a.action_id for f in payload.review.findings for a in f.actions}:
        raise SignedExportRefused("signed review action links do not match the frozen record")
    if (
        payload.review.approved_by != approval.approved_by
        or payload.review.approved_at != approval.created_at
    ):
        raise SignedExportRefused("signed review sign-off does not match the approval")
    for frozen in payload.review.findings:
        row = next(row for row in finding_rows(db, approval) if row[0].id == frozen.finding_id)
        if (frozen.rule_id, frozen.outcome, frozen.scope_label) != (
            row[3].rule_id,
            row[0].outcome,
            row[0].scope_label,
        ):
            raise SignedExportRefused("signed finding differs from the stored finding")
        for action in frozen.actions:
            stored = db.get(ReviewAction, action.action_id)
            if action.carried_from_finding_id is not None and (
                db.scalar(
                    select(FindingDecisionCarryover.id).where(
                        FindingDecisionCarryover.new_finding_id == frozen.finding_id,
                        FindingDecisionCarryover.review_action_id == action.action_id,
                        FindingDecisionCarryover.package_revision_id
                        == snapshot.package_revision_id,
                    )
                )
                is None
            ):
                raise SignedExportRefused("signed carried decision has no stored carry-over link")
            if stored is None or (
                stored.package_revision_id,
                stored.finding_id,
                stored.action,
                stored.actor,
                stored.created_at,
                stored.note,
            ) != (
                snapshot.package_revision_id,
                action.carried_from_finding_id or frozen.finding_id,
                action.action,
                action.reviewer,
                action.at,
                action.note,
            ):
                raise SignedExportRefused("signed review action differs from its stored row")
    return payload


def publication_status(db: Session, snapshot: ApprovalExportSnapshot) -> PublicationStatus:
    """Where publication of one frozen snapshot stands, from stored rows only.

    `ready` once the complete bundle exists. Otherwise each request — the first, made with the
    snapshot, plus one per reviewer retry — ends in either the bundle or exactly one recorded
    failure, so `failed` means every request so far has failed, and `preparing` means one has not
    finished yet. Counting rather than comparing timestamps keeps the answer independent of the
    clocks of the API and the worker.
    """
    if (
        db.scalar(
            select(ApprovalExportBundle.id).where(ApprovalExportBundle.snapshot_id == snapshot.id)
        )
        is not None
    ):
        return "ready"
    failures = db.scalar(
        select(func.count(ApprovalExportFailure.id)).where(
            ApprovalExportFailure.snapshot_id == snapshot.id
        )
    )
    retries = db.scalar(
        select(func.count(ApprovalExportRetry.id)).where(
            ApprovalExportRetry.snapshot_id == snapshot.id
        )
    )
    return "failed" if (failures or 0) >= 1 + (retries or 0) else "preparing"


def request_signed_exports(
    db: Session, approval_id: UUID, *, requested_by: str | None = None
) -> ApprovalExportSnapshot:
    """Freeze and queue publication, or queue it again for the same snapshot after a failure.

    A retry happens only when `requested_by` names the person asking and every earlier request has
    a recorded failure. It writes a retry row and queues the same workflow with the same payload:
    the same approval and the same frozen facts, nothing re-signed. A press while a request is
    still preparing, or once the files are ready, queues nothing.
    """
    try:
        return _request_signed_exports(db, approval_id, requested_by=requested_by)
    except ValidationError as exc:
        raise SignedExportRefused(
            "historical review record is malformed or its action order is ambiguous"
        ) from exc


def _request_signed_exports(
    db: Session, approval_id: UUID, *, requested_by: str | None
) -> ApprovalExportSnapshot:
    """Called at approval, or explicitly for an old approval. Commits nothing."""
    # The approval row lock serialises requests, so two presses at once cannot both see "failed".
    approval = db.scalar(select(Approval).where(Approval.id == approval_id).with_for_update())
    if approval is None:
        raise SignedExportRefused("approval not found")
    existing = db.scalar(
        select(ApprovalExportSnapshot).where(ApprovalExportSnapshot.approval_id == approval_id)
    )
    if existing is not None:
        load_snapshot(db, existing)
        if requested_by is not None and publication_status(db, existing) == "failed":
            if not requested_by.strip():
                raise SignedExportRefused("a retry must name who asked for it")
            db.add(
                ApprovalExportRetry(
                    snapshot_id=existing.id,
                    package_revision_id=existing.package_revision_id,
                    requested_by=requested_by,
                )
            )
            _queue(db, existing)
        return existing
    rows = finding_rows(db, approval)
    if not rows:
        raise SignedExportRefused("approval contains no findings")
    revision = db.get(PackageRevision, approval.package_revision_id)
    package = db.get(Package, revision.package_id) if revision is not None else None
    if revision is None or package is None:
        raise SignedExportRefused("approved revision is not available")
    actions = list(
        db.scalars(
            select(ReviewAction)
            .where(
                ReviewAction.finding_id.in_([row[0].id for row in rows]),
                ReviewAction.package_revision_id == revision.id,
                ReviewAction.created_at <= approval.created_at,
            )
            .order_by(ReviewAction.created_at)
        )
    )
    # A decision carried over an unchanged re-run (#1073) is frozen as the reviewer's own action,
    # marked with the finding it was recorded on, before any action taken on the approved finding.
    carried = {
        finding_id: decision.action
        for finding_id, decision in decision_records(
            db, [row[0].id for row in rows]
        ).carried.items()
        if decision.action.package_revision_id == revision.id
        and decision.action.created_at <= approval.created_at
    }
    for action in [*actions, *carried.values()]:
        review = db.get(ReviewSession, action.review_session_id)
        if review is None or review.created_at > approval.created_at:
            raise SignedExportRefused("historical review session is unavailable or ambiguous")

    def _history(finding_id: UUID) -> list[ReviewAction]:
        """The carried decision first (it predates the finding), then the finding's own actions."""
        own = [action for action in actions if action.finding_id == finding_id]
        return [carried[finding_id], *own] if finding_id in carried else own

    review_record = SignedReview(
        approval_id=approval.id,
        approved_by=approval.approved_by,
        approved_at=approval.created_at,
        findings=tuple(
            SignedReviewFinding(
                finding_id=finding.id,
                rule_id=definition.rule_id,
                outcome=finding.outcome,
                scope_label=finding.scope_label,
                actions=tuple(
                    ReviewDisposition(
                        action_id=action.id,
                        action=action.action,  # type: ignore[arg-type]  # Pydantic validates stored verbs.
                        reviewer=action.actor,
                        at=action.created_at,
                        note=action.note,
                        carried_from_finding_id=(
                            None if action.finding_id == finding.id else action.finding_id
                        ),
                    )
                    for action in _history(finding.id)
                ),
            )
            for finding, _, _, definition in rows
        ),
    )
    facts = deterministic_facts(db, approval)
    payload = ExportSnapshot(
        package_revision_id=revision.id,
        revision_number=revision.revision_number,
        vendor=package.vendor,
        review=review_record,
        facts=facts,
        changed_values=changed_values_for_revision(
            db, revision.id, finding_ids=tuple(f.finding_id for f in review_record.findings)
        ),
    )
    encoded = canonical(payload.model_dump(mode="json"))
    snapshot = ApprovalExportSnapshot(
        approval_id=approval.id,
        package_revision_id=revision.id,
        canonical_json=encoded,
        sha256=digest(encoded),
        deterministic_sha256=digest(canonical(facts)),
    )
    db.add(snapshot)
    db.flush()
    db.add_all(
        ApprovalExportAction(
            snapshot_id=snapshot.id, package_revision_id=revision.id, review_action_id=action_id
        )
        for action_id in dict.fromkeys(action.id for action in [*actions, *carried.values()])
    )
    _queue(db, snapshot)
    return snapshot


def _queue(db: Session, snapshot: ApprovalExportSnapshot) -> None:
    """One publication request for the frozen snapshot, in the caller's transaction."""
    enqueue(
        db,
        workflow=WORKFLOW,
        payload={
            "approval_id": str(snapshot.approval_id),
            "package_revision_id": str(snapshot.package_revision_id),
        },
    )
    db.flush()
