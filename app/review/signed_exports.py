"""Freeze an approval's findings and review history; never infer a new verdict."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.visual_countertops import _countertop_results_for_revision
from app.audit.events import AuditEvent
from app.lifecycle.carry_forward import CARRIED_CONFIRMATION_TARGET
from app.models.package import Package, PackageRevision
from app.models.review import (
    Approval,
    ApprovedFinding,
    FindingDecisionCarryover,
    ReviewAction,
    ReviewSession,
    decision_rows,
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
from app.review.left_for_person import FrozenLeftForPerson, left_for_person
from app.review.reviewer_marks import FrozenReviewerMarks, reviewer_marks_for
from app.review.signed_record import ReviewDisposition, SignedReview, SignedReviewFinding
from app.schemas.visual_ui import CountertopResultsOut
from workflow.changed_values import ChangedValues, changed_values_for_revision
from workflow.outbox import enqueue

WORKFLOW = "generate_signed_exports"

PublicationStatus = Literal["preparing", "ready", "failed"]


class SignedExportRefused(ValueError):
    """The stored record cannot safely be published."""


#: The version of the frozen countertop section written today. Raise it, and add a step to
#: `_COUNTERTOP_UPGRADES`, only when the stored shape can no longer be read by the current
#: `CountertopResultsOut`; `tests/review/test_frozen_countertops_golden.py` fails until you do.
COUNTERTOP_SECTION_VERSION = 1


class FrozenCountertops(BaseModel):
    """The countertop results as the screen showed them at sign-off (#1182), kept as plain JSON.

    **Plain JSON on purpose, not the screen's own model.** The snapshot is validated strictly every
    time it is published, retried or downloaded. Had it embedded `CountertopResultsOut`, a later
    change to that screen model (a new required field, a dropped word in a `Literal`) would make
    every signed review unreadable at once. Here the snapshot only checks that the section is a JSON
    object with a version; the object is under the snapshot's sha256 like everything else, and it is
    turned into the screen model only when the files are drawn (`printable_countertops`).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: int
    results: dict[str, Any]


#: The sign-off snapshot's format, written into every new snapshot. A snapshot with no
#: `format_version` key was frozen before #1363 and is format 1.
#:
#: * 1: findings, review history, facts, changed values, countertop results (#1182).
#: * 2 (#1363): adds `reviewer_marks`, a person's confirms, fixes and answers and the results
#:   printed NOT CHECKED, which the signed cloud PDF draws. A format-1 snapshot has none and its
#:   cloud PDF is drawn exactly as before (`tests/reports/test_redline_old_format_golden.py`).
#: * 3 (QA 2026-10-16): adds `old_compare_off`, whether the old row-by-row architect compare was
#:   switched off at sign-off. Its cloud PDF names each mark by its tag only (01, A1, R1) off the
#:   drawing's words, and its cover lists what is clouded as wrong and the architect compare; the
#:   old compare's leftover rows are left out of the cover and the drawing's summary while it was
#:   off. A format-1 or format-2 snapshot is drawn exactly as before.
#: * 4 (Set 3 QA 2026-10-16): no new field. Its signed cover says the not-checked results nobody
#:   answered once per reason ("NOT CHECKED (N): grouped by reason"), as the draft does, instead of
#:   one by one with raw ids. A format-1, -2 or -3 snapshot is drawn exactly as before.
#: * 5 (owner decision 2026-10-16, every question optional): adds `left_for_person`, every question
#:   nobody answered at sign-off. Its cloud PDF draws a grey "Not confirmed by a person" mark at each
#:   one's place and its cover lists them ("Left for a person (N)", grouped by kind). A format-1 to
#:   format-4 snapshot is drawn exactly as before.
SNAPSHOT_FORMAT_VERSION = 5


class FrozenArchitectCompare(BaseModel):
    """The direct compare as the review showed it at sign-off (#1418), kept as plain JSON (for the
    reason `FrozenCountertops` gives): every compared number and the counts. The signed cloud PDF
    prints it from here (`workflow/signed_outputs.py`), never from the live database."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    items: tuple[dict[str, Any], ...]
    counts: dict[str, Any]


class FrozenAiReview(BaseModel):
    """The AI page review as the review showed it at sign-off (decision 2026-10-16), as plain
    JSON: each page with its rows, and the counts. The signed cloud PDF prints it from here."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    pages: tuple[dict[str, Any], ...]
    counts: dict[str, Any]


class ExportSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    format_version: int = 1
    package_revision_id: UUID
    revision_number: int
    vendor: str | None
    review: SignedReview
    facts: tuple[dict[str, object], ...]
    changed_values: ChangedValues
    countertop_results: FrozenCountertops | None = None
    """The countertop results exactly as the screen showed them at sign-off (#1182).

    The signed PDF, workbook and redline print their countertop section from this, never from the
    live database: before #1182 they rebuilt it at export time, so a wall choice or a pairing saved
    after the approval (or before a retried export) reached the signed files while their findings
    page showed the frozen findings. `None` only on a snapshot frozen before #1182; see
    `frozen_countertop_results`."""
    reviewer_marks: FrozenReviewerMarks | None = None
    """Format 2 (#1363): every person's confirm, fix and answer at sign-off, and the results whose
    check could not run, as the signed cloud PDF prints them. `None` exactly on format 1."""
    architect_compare: FrozenArchitectCompare | None = None
    """The numbers compared with the architect at sign-off (#1418); `None` when the compare had
    not run, or on a snapshot frozen before it."""
    ai_review: FrozenAiReview | None = None
    """The AI page review at sign-off; `None` when it had not run, or on an older snapshot."""
    old_compare_off: bool | None = None
    """Format 3: whether the old row-by-row architect compare (`GV_ARCH_COMPARE_RECORDS`) was
    switched off at sign-off, frozen so a re-made file never follows a later setting. `None`
    exactly on formats 1 and 2."""
    left_for_person: FrozenLeftForPerson | None = None
    """Format 5: every question nobody answered at sign-off, printed "Not confirmed by a person".
    `None` exactly on formats 1 to 4."""

    @model_validator(mode="after")
    def known_format(self) -> ExportSnapshot:
        if not 1 <= self.format_version <= SNAPSHOT_FORMAT_VERSION:
            raise ValueError(f"unknown sign-off snapshot format {self.format_version}")
        if (self.format_version >= 2) != (self.reviewer_marks is not None):
            raise ValueError("a format-2 snapshot carries reviewer marks, and only it")
        if (self.format_version >= 3) != (self.old_compare_off is not None):
            raise ValueError(
                "a format-3 snapshot says whether the old compare was off, and only it"
            )
        if (self.format_version >= 5) != (self.left_for_person is not None):
            raise ValueError("a format-5 snapshot lists what was left for a person, and only it")
        return self


def _old_compare_off() -> bool:
    """Whether the old row-by-row architect compare is switched off now (the very switch the
    review screen and the draft read)."""
    from app.review.architect_compare import arch_compare_records_on

    return not arch_compare_records_on()


def _freeze_compare(db: Session, revision_id: UUID) -> FrozenArchitectCompare | None:
    from app.review.architect_compare import architect_compare_for_revision

    items, counts = architect_compare_for_revision(db, revision_id)
    if not items:
        return None
    return FrozenArchitectCompare(
        items=tuple(item.model_dump(mode="json") for item in items),
        counts=counts.model_dump(mode="json"),
    )


def _freeze_ai_review(db: Session, revision_id: UUID) -> FrozenAiReview | None:
    from app.review.ai_review import ai_review_for_revision

    review = ai_review_for_revision(db, revision_id)
    if not review.pages:
        return None
    return FrozenAiReview(
        pages=tuple(page.model_dump(mode="json") for page in review.pages),
        counts=review.counts.model_dump(mode="json"),
    )


def freeze_countertops(results: CountertopResultsOut) -> FrozenCountertops:
    return FrozenCountertops(
        schema_version=COUNTERTOP_SECTION_VERSION, results=results.model_dump(mode="json")
    )


#: How to bring an older frozen section up to the current version, one step each. Empty while
#: there is only version 1.
_COUNTERTOP_UPGRADES: dict[int, Callable[[dict[str, Any]], dict[str, Any]]] = {}


def printable_countertops(frozen: FrozenCountertops) -> CountertopResultsOut:
    """The frozen section as the report writers take it, or a refusal naming why it cannot be read.

    A refusal is recorded as a failed publication (and can be retried once the code is fixed); it
    never touches files already published, which are served as stored.
    """
    if not 1 <= frozen.schema_version <= COUNTERTOP_SECTION_VERSION:
        raise SignedExportRefused(
            f"the frozen countertop section has version {frozen.schema_version}, which this code "
            "does not know"
        )
    results = dict(frozen.results)
    for version in range(frozen.schema_version, COUNTERTOP_SECTION_VERSION):
        results = _COUNTERTOP_UPGRADES[version](results)
    try:
        return CountertopResultsOut.model_validate(results)
    except ValidationError as exc:
        raise SignedExportRefused(
            "the frozen countertop section cannot be read by this version of the code; add an "
            "upgrade step for it"
        ) from exc


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
            else str(value)
            if isinstance(value, UUID)
            else value
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


def frozen_countertop_results(db: Session, payload: ExportSnapshot) -> CountertopResultsOut:
    """The countertop results the signed outputs print: the snapshot's own, frozen at sign-off.

    **A snapshot frozen before #1182 has none.** For it, and only for it, the results are rebuilt
    from the database, as every export did before. That is safe from #1182 on because nothing about
    a signed-off revision can change any more (`app/api/signed_off.py`); a change saved after such an
    old approval and before #1182 would show, exactly as it did then.
    """
    if payload.countertop_results is not None:
        return printable_countertops(payload.countertop_results)
    revision = db.get(PackageRevision, payload.package_revision_id)
    if revision is None:
        raise SignedExportRefused("approved revision is not available")
    return _countertop_results_for_revision(db, revision.package_id, revision)


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
                # A person's answer about a held reading is not a decision on its finding (#1367);
                # the cloud marks draw it from its reading, never as this result's disposition.
                # A second safety net: sign-off already waits for the re-run an answer needs.
                decision_rows(),
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

    # The signer's confirmation that answers carried from an earlier revision still apply (#1180).
    confirmed = db.scalar(
        select(AuditEvent)
        .where(
            AuditEvent.target_id == approval.id,
            AuditEvent.target_type == CARRIED_CONFIRMATION_TARGET,
        )
        .order_by(AuditEvent.created_at)
        .limit(1)
    )
    review_record = SignedReview(
        approval_id=approval.id,
        approved_by=approval.approved_by,
        approved_at=approval.created_at,
        carried_answers_confirmation=(
            None
            if confirmed is None
            else (
                "Answers carried from an earlier revision were confirmed to still apply by "
                f"{confirmed.actor} on {confirmed.created_at.isoformat()}."
            )
        ),
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
        format_version=SNAPSHOT_FORMAT_VERSION,
        package_revision_id=revision.id,
        revision_number=revision.revision_number,
        vendor=package.vendor,
        review=review_record,
        facts=facts,
        changed_values=changed_values_for_revision(
            db, revision.id, finding_ids=tuple(f.finding_id for f in review_record.findings)
        ),
        # Frozen in the approval's own transaction, from the same projection the screen read.
        countertop_results=freeze_countertops(
            _countertop_results_for_revision(db, package.id, revision)
        ),
        # Frozen in the same transaction (#1363): the marks the signed cloud PDF draws.
        reviewer_marks=reviewer_marks_for(
            db,
            revision.id,
            findings=[(finding, definition.rule_id) for finding, _, _, definition in rows],
            decisions=review_record.findings,
            until=approval.created_at,
        ),
        architect_compare=_freeze_compare(db, revision.id),
        ai_review=_freeze_ai_review(db, revision.id),
        old_compare_off=_old_compare_off(),
        # Format 5: every question nobody answered, from the screen's own list, in the same
        # transaction; printed "Not confirmed by a person", never dropped.
        left_for_person=(
            left_for_person(db, revision, package) if SNAPSHOT_FORMAT_VERSION >= 5 else None
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
