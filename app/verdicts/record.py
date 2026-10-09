"""Persisting a decision the engine made.

The first writer of `check_runs`, `verdict_inputs` and `findings`. Everything in `app/` that touches
the verdict plane until now has read it — the list endpoint, the chain, the export — and the rows they
read had to be put there by hand.

Four things this has to get right, each of which the schema half-enforces and half-trusts.

**A finding belongs to exactly one run.** `findings.check_run_id` is unique, so a rule is a run. A
re-run writes a new run and a new finding and never edits the old ones: `findings` carries the
append-only trigger, so an `UPDATE` is refused by the database rather than quietly applied. Which set
is current is answered by `check_runs.superseded_at`, not by deleting anything.

**The revision on the finding must be the run's own.** A composite foreign key says so
(`fk_findings_run_revision`), and it exists because the alternative — a finding claiming a revision
its run does not have — would misstate which drawings were reviewed, and an approval built on it
would misstate what was signed off.

**Only qualified operands are sealed.** `verdict_inputs` has a check constraint admitting
`CORROBORATED` and `HUMAN_CONFIRMED` and nothing else, so an unqualified value is refused by the
database. That is the same gate `verdict/operands.py` applies before the arithmetic; this writes it
down so the finding can be recomputed from its own stored inputs years later.

**A decision must carry its evidence; an abstention need not.** `app/models/verdicts.py` says a
finding with no evidence cannot exist, and notes that no `CHECK` can express it — the deferred trigger
belongs to a later story, so today the writer is the enforcement. The line drawn here is narrower than
that sentence and truer to it: a `PASS` or `FAIL` rests on evidence and is refused without it, while an
abstention has no evidence *because there was none*, and manufacturing an empty link for it would be
inventing provenance for a check that never ran.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from fractions import Fraction
from uuid import UUID

from sqlalchemy import exists, select, update
from sqlalchemy.orm import Session, aliased

from app.db.base import utc_now
from app.models import (
    CanonicalObservation,
    Document,
    DocumentVersion,
    DrawingItem,
    DrawingView,
    ObservationCandidate,
    PackageRevisionDocument,
    Page,
    PartConfirmation,
    PartDecision,
    ViewRole,
)
from app.models.evidence import (
    EvidenceCorroborationLane,
    EvidenceSupportingCandidate,
    SlotRowReviewDecision,
)
from app.models.rules import RuleSnapshot as RuleSnapshotRow
from app.models.runs import ExtractionRun, TaskRun, WorkflowRun
from app.models.verdicts import CheckRun, VerdictInput
from app.models.verdicts import Finding as FindingRow
from app.verdicts.trace import abstention_trace, calculation_trace, missing_operand_reason
from evidence.canonical import CorroborationLane
from units.measurement import Measurement
from verdict.finding import Finding
from verdict.operands import QUALIFIED_STATUSES, VerdictOperand
from verdict.outcomes import DECISIVE_OUTCOMES, Outcome
from vocabulary.part_kinds import PartKind
from vocabulary.semantic_types import DocumentRole
from workflow.architect_pairing_contract import EffectivePairing
from workflow.architect_row_evidence import architect_candidate_refusal

__all__ = ["EvidenceMissing", "record_finding", "supersede_runs"]


class EvidenceMissing(ValueError):
    """Raised when a decided finding would be written with nothing behind it.

    A `ValueError` and not a refusal object: there is no sensible way for a caller to continue. A
    `PASS` with no operands is not a lenient finding, it is a claim about a drawing nobody read.
    """


def supersede_runs(session: Session, package_revision_id: UUID) -> int:
    """Mark every live run for this revision replaced, returning how many.

    Called before writing a new set, so the window in which both are live is inside one transaction
    and no reader ever sees two. `check_runs` is not append-only precisely so this can happen — the
    findings themselves are untouched and every one ever written is still there.

    Returns the count because a caller reporting "checks re-run, 8 previous results superseded" is
    saying something a reviewer needs, and a silent replacement is how somebody comes to believe a
    verdict they are looking at is the only one there has ever been.
    """
    live = list(
        session.execute(
            select(CheckRun.id).where(
                CheckRun.package_revision_id == package_revision_id,
                CheckRun.superseded_at.is_(None),
            )
        ).scalars()
    )
    if not live:
        return 0

    session.execute(update(CheckRun).where(CheckRun.id.in_(live)).values(superseded_at=utc_now()))
    return len(live)


def record_finding(
    session: Session,
    *,
    package_revision_id: UUID,
    finding: Finding,
    operands: Mapping[str, VerdictOperand],
    parameter_set_ids: Mapping[str, str],
    defaults_set_id: str | None = None,
    defaults_canonical_json: str | None = None,
    missing: Mapping[str, str] | None = None,
    scope_item_id: UUID | None = None,
    scope_row_candidate_id: UUID | None = None,
    scope_label: str | None = None,
    architect_pairing: EffectivePairing | None = None,
) -> FindingRow:
    """Write one decision: its run, the operands it was computed from, and the finding itself.

    `parameter_set_ids` is supplied by the caller rather than read off the finding, because
    `verdict/engine.py` returns it empty — the engine is handed resolved parameters and never learns
    which sets they came from. Mapping layer to content hash keeps the column able to answer "which
    numbers judged this?" for each layer separately, which a flat list of hashes cannot.

    `missing` names the operands that were never read, and only reaches the stored trace when the
    check abstained. It is what turns "NOT_FOUND" into a sentence somebody can act on.

    `architect_pairing` is the row's effective pairing (#1053), given only by the vendor-vs-architect
    check (#1054). It is the one thing that lets an architect's value support a row-scoped finding,
    and only the exact architect dimension it names, on the row's own page (`_paired_architect`).
    """
    snapshot_row = session.execute(
        select(RuleSnapshotRow).where(RuleSnapshotRow.snapshot_id == finding.snapshot_id)
    ).scalar_one_or_none()
    if snapshot_row is None:
        raise EvidenceMissing(
            f"no published snapshot {finding.snapshot_id!r} for rule {finding.rule_id!r}. A finding "
            "citing a snapshot the database does not hold could never be reproduced."
        )

    if scope_item_id is not None and scope_row_candidate_id is not None:
        raise EvidenceMissing("a finding cannot name both a confirmed item and a slot-reader row")
    if architect_pairing is not None and scope_row_candidate_id is None:
        raise EvidenceMissing("an architect pairing belongs to one countertop row's finding")
    if (scope_item_id is None and scope_row_candidate_id is None) != (scope_label is None):
        raise EvidenceMissing("a scoped finding needs its subject and plain name")
    if scope_item_id is not None:
        if not scope_label or not scope_label.strip():
            raise EvidenceMissing("a countertop finding needs a plain name")
        later = aliased(PartConfirmation)
        scoped = session.scalar(
            select(DrawingItem.id)
            .join(DrawingView, DrawingView.id == DrawingItem.drawing_view_id)
            .join(Page, Page.id == DrawingView.page_id)
            .join(
                PackageRevisionDocument,
                PackageRevisionDocument.document_version_id == Page.document_version_id,
            )
            .join(PartConfirmation, PartConfirmation.drawing_item_id == DrawingItem.id)
            .where(
                DrawingItem.id == scope_item_id,
                DrawingItem.item_type == PartKind.COUNTERTOP.item_type.value,
                DrawingView.role == ViewRole.SHOP.value,
                PartConfirmation.decision == PartDecision.CONFIRMED.value,
                ~exists().where(later.supersedes_id == PartConfirmation.id),
                PackageRevisionDocument.package_revision_id == package_revision_id,
            )
            .limit(1)
        )
        if scoped is None:
            raise EvidenceMissing(
                "the finding's countertop is not confirmed on this vendor revision"
            )
    if scope_row_candidate_id is not None:
        candidate = session.execute(
            select(ObservationCandidate)
            .join(Page, Page.id == ObservationCandidate.page_id)
            .join(DocumentVersion, DocumentVersion.id == Page.document_version_id)
            .join(Document, Document.id == DocumentVersion.document_id)
            .join(
                PackageRevisionDocument,
                PackageRevisionDocument.document_version_id == Page.document_version_id,
            )
            .where(
                ObservationCandidate.id == scope_row_candidate_id,
                PackageRevisionDocument.package_revision_id == package_revision_id,
                Document.kind == "shop",
            )
        ).scalar_one_or_none()
        flags = set(candidate.ambiguity_flags or []) if candidate is not None else set()
        if candidate is None or "slot-reader" not in flags or "slot:0" not in flags:
            raise EvidenceMissing("the finding's row is not a selected vendor slot-reader row")
        latest_run_id = session.execute(
            select(ExtractionRun.id)
            .join(TaskRun, TaskRun.id == ExtractionRun.task_run_id)
            .join(WorkflowRun, WorkflowRun.id == TaskRun.workflow_run_id)
            .where(
                WorkflowRun.package_revision_id == package_revision_id,
                ExtractionRun.extractor_version.like("slot-reader%"),
            )
            .order_by(ExtractionRun.created_at.desc(), ExtractionRun.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        if candidate.extraction_run_id != latest_run_id:
            raise EvidenceMissing("the finding's row is not from the current slot-reader run")
        row_rank = next(
            (flag for flag in flags if flag.startswith("row-rank:")),
            None,
        )
        if row_rank is None:
            raise EvidenceMissing("the finding's row has no stored row identity")
        for operand in operands.values():
            if operand.row_review_decision_id is not None:
                decision_id = _row_review_decision_id(operand)
                decision = session.get(SlotRowReviewDecision, decision_id)
                if decision is None or decision.row_candidate_id != scope_row_candidate_id:
                    raise EvidenceMissing("review input belongs to a different countertop row")
            if operand.evidence_observation_id is None:
                continue
            observation_id = _observation_id(operand)
            observation = session.get(CanonicalObservation, observation_id)
            if (
                observation is None
                or observation.page_id != candidate.page_id
                or observation.document_version_id != candidate.document_version_id
            ):
                raise EvidenceMissing("drawing evidence belongs to a different row or page")
            if observation.document_role == DocumentRole.ARCH.value:
                # The architect's value is never on the vendor's row: it is allowed only as the
                # exact dimension this row's pairing names (#1054). Everything else stays refused.
                _paired_architect(session, observation, candidate, architect_pairing)
                continue
            supporters = session.execute(
                select(ObservationCandidate)
                .join(
                    EvidenceSupportingCandidate,
                    EvidenceSupportingCandidate.candidate_id == ObservationCandidate.id,
                )
                .where(EvidenceSupportingCandidate.canonical_observation_id == observation.id)
            ).scalars()
            same_row = False
            for supporter in supporters:
                supporter_flags = set(supporter.ambiguity_flags or ())
                parent_id = next(
                    (
                        flag.removeprefix("supports:")
                        for flag in supporter_flags
                        if flag.startswith("supports:")
                    ),
                    None,
                )
                parent = (
                    None
                    if parent_id is None
                    else session.get(ObservationCandidate, UUID(parent_id))
                )
                if (
                    supporter.page_id == candidate.page_id
                    and supporter.extraction_run_id == candidate.extraction_run_id
                    and row_rank in supporter_flags
                    and "slot-reader" in supporter_flags
                ):
                    same_row = True
                    break
                if (
                    parent is not None
                    and parent.page_id == candidate.page_id
                    and parent.extraction_run_id == candidate.extraction_run_id
                    and row_rank in (parent.ambiguity_flags or [])
                    and "slot-reader" in (parent.ambiguity_flags or [])
                ):
                    same_row = True
                    break
            if not same_row:
                raise EvidenceMissing("drawing evidence is not supported by this countertop row")
    elif any(operand.row_review_decision_id is not None for operand in operands.values()):
        raise EvidenceMissing("a row review input requires a finding scoped to that same row")

    sealed = {
        name: operand
        for name, operand in operands.items()
        if operand.status in QUALIFIED_STATUSES and operand.value is not None
    }

    # The invariant the schema cannot hold. Checked before anything is inserted, so a refusal leaves
    # no half-written run behind.
    if finding.outcome in DECISIVE_OUTCOMES and not sealed:
        raise EvidenceMissing(
            f"{finding.rule_id} decided {finding.outcome.value} with no qualified operand. A verdict "
            "with nothing behind it cannot be defended to the vendor it is sent to."
        )

    run = CheckRun(
        package_revision_id=package_revision_id,
        rule_snapshot_id=snapshot_row.id,
        engine_version=finding.engine_version,
        defaults_set_id=defaults_set_id,
        defaults_canonical_json=defaults_canonical_json,
    )
    session.add(run)
    session.flush()

    for name, operand in sealed.items():
        exact = _exact(operand.value)
        if exact is None:
            # A qualified operand whose value is not a single exact number — a `many` selector's
            # tuple, or text. The trace records it; `verdict_inputs` holds one rational per slot and
            # has nowhere to put it. Skipped rather than coerced, because a tuple flattened into one
            # number would be a different calculation wearing the same name.
            continue
        session.add(
            VerdictInput(
                check_run_id=run.id,
                operand_name=name,
                value_numerator=exact.numerator,
                value_denominator=exact.denominator,
                unit=_unit_of(operand.value),
                evidence_status=operand.status.value,
                canonical_observation_id=_observation_id(operand),
                slot_row_review_decision_id=_row_review_decision_id(operand),
            )
        )

    row = FindingRow(
        check_run_id=run.id,
        # Explicit, and the composite foreign key checks it against the run's own.
        package_revision_id=package_revision_id,
        scope_item_id=scope_item_id,
        scope_row_candidate_id=scope_row_candidate_id,
        scope_label=scope_label,
        outcome=finding.outcome.value,
        severity=finding.severity.value,
        trace=(
            calculation_trace(finding.trace, outcome=finding.outcome)
            if finding.trace is not None
            else abstention_trace(
                finding.outcome,
                cause=_cause_for(finding.outcome),
                reason=finding.reason or missing_operand_reason(missing or {}),
            )
        ),
        parameter_set_versions=dict(parameter_set_ids),
        # **The four the engine carried and this table used to drop (#521).**
        #
        # `finding.reason` is written for a decision as well as an abstention. The abstention path
        # also puts its reason inside the trace, which is where it has always been read from; this
        # column is what makes a decision's reason readable at all.
        reason=finding.reason or None,
        # Exact, in three columns, never a float. A delta is a measurement and ADR-0001 applies to it
        # exactly as it does to an operand.
        delta_numerator=None if finding.delta is None else finding.delta.exact.numerator,
        delta_denominator=None if finding.delta is None else finding.delta.exact.denominator,
        delta_unit=None if finding.delta is None else finding.delta.unit.value,
        variant=finding.variant or None,
        # `[]` where the check produced no notes, not `NULL`. The null means "written before this
        # column existed", and a check that ran and had nothing to add is a different fact.
        notes=list(finding.notes),
    )
    session.add(row)
    session.flush()
    return row


def _paired_architect(
    session: Session,
    observation: CanonicalObservation,
    row_candidate: ObservationCandidate,
    pairing: EffectivePairing | None,
) -> None:
    """Refuse an architect operand unless it is exactly what this row's pairing names (#1054).

    The observation must rest on one candidate, the one the row's effective pairing names, on the
    row's own page of the same document version (so the same package revision), qualified only by
    its drawn-length witness, and still an architect's value the check may use: read by code from
    the architect's own text, unheld, inside a drawing confirmed as the architect's
    (`architect_candidate_refusal`, the same test the check itself applies).
    """
    if pairing is None:
        raise EvidenceMissing(
            "an architect's value supports a countertop row only through that row's pairing"
        )
    supporters = list(
        session.scalars(
            select(ObservationCandidate)
            .join(
                EvidenceSupportingCandidate,
                EvidenceSupportingCandidate.candidate_id == ObservationCandidate.id,
            )
            .where(EvidenceSupportingCandidate.canonical_observation_id == observation.id)
        )
    )
    named = {pair.architect_candidate_id for pair in pairing.pairs}
    if len(supporters) != 1 or supporters[0].id not in named:
        raise EvidenceMissing("architect evidence is not the dimension this row's pairing names")
    (supporter,) = supporters
    lanes = set(
        session.scalars(
            select(EvidenceCorroborationLane.lane).where(
                EvidenceCorroborationLane.canonical_observation_id == observation.id
            )
        )
    )
    if (
        supporter.page_id != row_candidate.page_id
        or supporter.document_version_id != row_candidate.document_version_id
        or supporter.value_numerator != observation.value_numerator
        or supporter.value_denominator != observation.value_denominator
        or lanes != {CorroborationLane.DRAWN_LENGTH.value}
    ):
        raise EvidenceMissing("architect evidence does not belong to this row's sheet")
    refusal = architect_candidate_refusal(session, None, supporter)
    if refusal is not None:
        raise EvidenceMissing(f"the paired architect dimension cannot be used: {refusal}")


def _cause_for(outcome: Outcome) -> str:
    """A machine-readable cause for an abstention, from the outcome that produced it.

    Kept narrow: three outcomes can abstain and each abstains for one reason, so a free-text cause
    would be three sentences that drift. The reviewer-facing explanation is `reason`.
    """
    return {
        Outcome.NOT_FOUND: "operand_missing",
        Outcome.REVIEW_REQUIRED: "needs_review",
        Outcome.NO_APPLICABLE_RULE: "no_applicable_rule",
    }.get(outcome, "unknown")


def _exact(value: object) -> Fraction | None:
    """The single exact rational behind an operand value, or `None` when there is not one."""
    if isinstance(value, Measurement):
        return value.exact
    if isinstance(value, Fraction):
        return value
    return None


def _observation_id(operand: VerdictOperand) -> UUID | None:
    """Return the explicit evidence identity carried across the workflow boundary.

    A reviewer-entered run parameter has no canonical observation and therefore returns ``None``.
    Evidence-derived operands must carry a real UUID; accepting malformed provenance would create a
    decisive finding that cannot be traced back to the reading it used.
    """
    if operand.evidence_observation_id is None:
        return None
    try:
        return UUID(operand.evidence_observation_id)
    except (AttributeError, TypeError, ValueError) as error:
        raise EvidenceMissing(
            f"operand {operand.name!r} has an invalid canonical observation reference"
        ) from error


def _row_review_decision_id(operand: VerdictOperand) -> UUID | None:
    if operand.row_review_decision_id is None:
        return None
    if operand.evidence_observation_id is not None:
        raise EvidenceMissing("an operand cannot cite both a drawing observation and a row review")
    try:
        return UUID(operand.row_review_decision_id)
    except (AttributeError, TypeError, ValueError) as error:
        raise EvidenceMissing(
            f"operand {operand.name!r} has an invalid row review decision reference"
        ) from error


def _unit_of(value: object) -> str:
    """The stored unit for a sealed operand.

    A bare `Fraction` operand — a literal or a count — carries no unit of its own and is stored as
    inches, which is the arithmetic unit every V1 rule declares (Q12). Stated here rather than
    defaulted silently, because the column has a check constraint and a wrong answer would be a
    dimension recorded in the wrong system.
    """
    if isinstance(value, Measurement):
        return value.unit.value
    return "in"


def sealed_operand_names(operands: Mapping[str, VerdictOperand]) -> Sequence[str]:
    """Which operands would be written, for a caller that wants to report before committing."""
    return sorted(
        name for name, operand in operands.items() if operand.status in QUALIFIED_STATUSES
    )
