"""Read-only projection of a slot-reader run into the CT-WIDTH-001 safety bridge.

The proposal rows are *not* reviewer-confirmed operands. This measures the counterfactual
"accept only the product's sealed proposals, type nothing". A missing proposal, an unverified
reader attempt, or a missing product setting stays unaccounted; none is filled from the key.
Raw provider answers remain in the private database and are never returned by this module.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.document import Page
from app.models.evidence import MeasurementProposal, ObservationCandidate
from app.models.package import Package, PackageRevision
from app.models.parameters import declared_defaults, load_parameter_sets
from app.models.runs import ExtractionRun, ModelInvocation, TaskRun, WorkflowRun
from eval.form_first_safety import (
    SafetyCase,
    SafetyReport,
    WidthInputs,
    evaluate,
    published_ct_width_snapshot,
)
from extraction.form_reader.bedrock import _extract_json_object
from extraction.slot_reader.bedrock import CROP_PROMPT_ID
from extraction.slot_reader.walls import WALL_PROMPT_ID
from rules.parameters import ParameterSet, resolve_all
from units.normalise import UnitNormalisationError, normalise_to_inches
from units.notation import canonical_notation
from workflow.changed_values import layered_parameter_sets
from workflow.layout_proposals import reader_sealed_wall_config
from workflow.measurements import run_parameters_for

OVERALL_FIELD = "SHOP:countertop_overall_width"
PIECE_FIELD = "SHOP:countertop_piece_width"
_KEY_DUAL = re.compile(
    r"^\s*\d+(?:\.\d+)?\s*mm\s*\[\s*(\d+(?:\s+\d+/\d+)?)\s*(?:in|\")\s*\]\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class AttemptAudit:
    """Counts only; private answers never leave the database boundary."""

    attempts: int
    label_attempts: int
    wall_attempts: int
    complete: bool
    reason: str | None


def _answer(raw: str | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        answer = _extract_json_object(raw)
    except (TypeError, ValueError):
        return None
    return answer if isinstance(answer, dict) else None


def _reader_texts(
    candidate: ObservationCandidate, model_ids: tuple[str, ...]
) -> Counter[tuple[str, str]]:
    expected: Counter[tuple[str, str]] = Counter()
    for flag in candidate.ambiguity_flags:
        for model_id in model_ids:
            prefix = f"reader:{model_id}:"
            if flag.startswith(prefix):
                expected[(model_id, flag.removeprefix(prefix))] += 1
                break
    return expected


def audit_saved_attempts(
    attempts: tuple[ModelInvocation, ...],
    candidates: tuple[ObservationCandidate, ...],
    *,
    page_index: int,
    model_ids: tuple[str, ...],
    wall_layout_used: bool,
    wall_candidates: tuple[tuple[int, ObservationCandidate], ...] = (),
) -> AttemptAudit:
    """Verify accepted label texts and the wall decision against stored per-attempt answers.

    An invocation has a page and model but no per-crop key (#985); equal texts are therefore
    compared as a multiset, not assigned to a guessed crop. This is an audit of the persisted
    answers, not a claim that the current schema proves the identity of each image sent.
    """
    relevant = tuple(row for row in attempts if row.prompt_id in {CROP_PROMPT_ID, WALL_PROMPT_ID})
    label_attempts = sum(row.prompt_id == CROP_PROMPT_ID for row in relevant)
    wall_attempts = sum(row.prompt_id == WALL_PROMPT_ID for row in relevant)

    def result(complete: bool, reason: str | None) -> AttemptAudit:
        return AttemptAudit(len(relevant), label_attempts, wall_attempts, complete, reason)

    if not relevant:
        return result(False, "no stored slot-reader attempts")
    if any(
        row.reader_page_index is None
        or row.reader_attempt_number is None
        or row.reader_attempt_number < 1
        or row.model_id not in model_ids
        or (row.outcome in {"ok", "rejected"} and row.private_raw_response is None)
        for row in relevant
    ):
        return result(False, "a stored reader attempt lacks page, model, number, or raw answer")

    expected: Counter[tuple[str, str]] = Counter()
    for candidate in candidates:
        expected.update(_reader_texts(candidate, model_ids))
    if candidates and not expected:
        return result(False, "a sealed proposal has no recorded reader text")
    observed: Counter[tuple[str, str]] = Counter()
    for row in relevant:
        if row.prompt_id != CROP_PROMPT_ID or row.reader_page_index != page_index:
            continue
        if row.outcome != "ok":
            continue
        parsed = _answer(row.private_raw_response)
        if parsed is None or not isinstance(parsed.get("text"), str):
            return result(False, "a successful label attempt cannot be parsed")
        observed[(row.model_id, parsed["text"])] += 1
    if expected - observed:
        return result(False, "a sealed label is not backed by a stored reader answer")

    if wall_layout_used:
        expected_walls: Counter[tuple[int, str, str, str, str, str]] = Counter()
        for wall_page_index, candidate in wall_candidates:
            for flag in candidate.ambiguity_flags:
                for model_id in model_ids:
                    prefix = f"wall-reader:{model_id}:"
                    if not flag.startswith(prefix):
                        continue
                    parts = [part.split("=", 1) for part in flag.removeprefix(prefix).split(",")]
                    if any(len(part) != 2 for part in parts):
                        return result(False, "a stored wall answer flag is malformed")
                    fields = dict(parts)
                    if any(key not in fields for key in ("left", "right", "behind", "view")):
                        return result(False, "a stored wall answer flag is incomplete")
                    expected_walls[
                        (
                            wall_page_index,
                            model_id,
                            fields["left"],
                            fields["right"],
                            fields["behind"],
                            fields["view"],
                        )
                    ] += 1
        if not expected_walls or {entry[1] for entry in expected_walls} != set(model_ids):
            return result(False, "the sealed wall candidates lack both readers")
        observed_walls: Counter[tuple[int, str, str, str, str, str]] = Counter()
        for row in relevant:
            if row.prompt_id != WALL_PROMPT_ID or row.outcome != "ok":
                continue
            parsed = _answer(row.private_raw_response)
            if parsed is None or row.reader_page_index is None:
                return result(False, "a successful wall attempt cannot be parsed")
            observed_walls[
                (
                    row.reader_page_index,
                    row.model_id,
                    str(parsed.get("left")),
                    str(parsed.get("right")),
                    str(parsed.get("behind")),
                    str(parsed.get("view")),
                )
            ] += 1
        if expected_walls - observed_walls:
            return result(False, "a sealed wall is not backed by stored raw answers")
    return result(True, None)


def _field_cut(session: Session, revision_id: UUID, project_id: UUID) -> Fraction | None:
    """Use the same published default and stored GLOBAL/PROJECT/RUN precedence as run_checks."""
    snapshot = published_ct_width_snapshot()
    defaults = declared_defaults([snapshot.rule], when=datetime(2000, 1, 1, tzinfo=UTC))
    stored: list[ParameterSet] = list(load_parameter_sets(session, project_id))
    run = run_parameters_for(session, revision_id)
    if run is not None:
        stored.append(run)
    resolved = resolve_all(*layered_parameter_sets(defaults, tuple(stored)))
    cut = resolved.get("field_cut")
    if cut is None:
        return None
    return cut.value.value.value


def _complete_slot_row(
    candidates: tuple[ObservationCandidate, ...], piece_positions: tuple[int, ...]
) -> bool:
    """A shortened offered chain cannot silently become a complete countertop width."""
    slots: list[int] = []
    overalls = 0
    for candidate in candidates:
        if "slot-reader" not in candidate.ambiguity_flags:
            continue
        for flag in candidate.ambiguity_flags:
            if flag == "slot:overall":
                overalls += 1
            elif flag.startswith("slot:"):
                try:
                    slots.append(int(flag.removeprefix("slot:")))
                except ValueError:
                    return False
    return (
        overalls == 1
        and bool(slots)
        and sorted(slots) == list(range(len(slots)))
        and sorted(piece_positions) == list(range(len(slots)))
    )


def product_case_for_page(
    session: Session,
    *,
    package_revision_id: UUID,
    page_number: int,
    truth: WidthInputs | None,
    case_id: str,
) -> tuple[SafetyCase, AttemptAudit]:
    """Read one page's newest slot proposal, product type, settings, and audit records.

    Does not commit, save measurements, or treat proposals as actual verdict operands. The
    caller supplies key truth independently; product values are never copied into it.
    """
    if page_number < 1:
        raise ValueError("page_number must be one-based")
    revision = session.get(PackageRevision, package_revision_id)
    package = None if revision is None else session.get(Package, revision.package_id)
    if package is None:
        raise ValueError("package revision not found")
    if package.product_type != "countertop":
        empty = AttemptAudit(0, 0, 0, False, "package product is not countertop")
        return SafetyCase(case_id, truth, None, False, empty.reason), empty

    rows = session.execute(
        select(MeasurementProposal, ObservationCandidate, ExtractionRun)
        .join(ObservationCandidate, ObservationCandidate.id == MeasurementProposal.candidate_id)
        .join(ExtractionRun, ExtractionRun.id == ObservationCandidate.extraction_run_id)
        .where(
            MeasurementProposal.package_revision_id == package_revision_id,
            MeasurementProposal.page_number == page_number,
            ExtractionRun.extractor_version.startswith("slot-reader"),
        )
        .order_by(ExtractionRun.created_at.desc(), ExtractionRun.id.desc())
    ).all()
    if not rows:
        run = session.scalar(
            select(ExtractionRun)
            .join(TaskRun, TaskRun.id == ExtractionRun.task_run_id)
            .join(WorkflowRun, WorkflowRun.id == TaskRun.workflow_run_id)
            .join(ModelInvocation, ModelInvocation.extraction_run_id == ExtractionRun.id)
            .where(
                WorkflowRun.package_revision_id == package_revision_id,
                ExtractionRun.extractor_version.startswith("slot-reader"),
                ModelInvocation.reader_page_index == page_number - 1,
                ModelInvocation.prompt_id.in_((CROP_PROMPT_ID, WALL_PROMPT_ID)),
            )
            .order_by(ExtractionRun.created_at.desc(), ExtractionRun.id.desc())
            .limit(1)
        )
        if run is None:
            empty = AttemptAudit(0, 0, 0, False, "no stored slot-reader run for this page")
            return SafetyCase(case_id, truth, None, False, empty.reason), empty
        attempts = tuple(
            session.scalars(
                select(ModelInvocation).where(ModelInvocation.extraction_run_id == run.id)
            )
        )
        model_ids = tuple(sorted({row.model_id for row in attempts}))
        audit = audit_saved_attempts(
            attempts,
            (),
            page_index=page_number - 1,
            model_ids=model_ids,
            wall_layout_used=False,
        )
        return (
            SafetyCase(
                case_id,
                truth,
                None,
                audit.complete,
                "no sealed slot-reader proposal for this page",
            ),
            audit,
        )
    run_id = rows[0][2].id
    same_run = [row for row in rows if row[2].id == run_id]
    proposal_ids = {proposal.proposal_id for proposal, _candidate, _run in same_run}
    if len(proposal_ids) != 1:
        empty = AttemptAudit(0, 0, 0, False, "multiple proposal groups on one page")
        return SafetyCase(case_id, truth, None, False, empty.reason), empty
    group = [
        (proposal, candidate)
        for proposal, candidate, _run in same_run
        if proposal.proposal_id in proposal_ids
    ]
    model_names = {part.strip() for proposal, _ in group for part in proposal.model_id.split(" + ")}
    if len(model_names) != 2:
        empty = AttemptAudit(0, 0, 0, False, "the saved proposal does not name two readers")
        return SafetyCase(case_id, truth, None, False, empty.reason), empty
    model_ids = tuple(sorted(model_names))
    candidates = tuple(candidate for _proposal, candidate in group)
    attempts = tuple(
        session.scalars(select(ModelInvocation).where(ModelInvocation.extraction_run_id == run_id))
    )
    layout = reader_sealed_wall_config(session, package_revision_id)
    wall_layout = None if layout is None else layout.value
    wall_candidates = tuple(
        (page_index, candidate)
        for page_index, candidate in session.execute(
            select(Page.index, ObservationCandidate)
            .select_from(ObservationCandidate)
            .join(Page, Page.id == ObservationCandidate.page_id)
            .where(
                ObservationCandidate.extraction_run_id == run_id,
                ObservationCandidate.raw_text.startswith("walls: "),
            )
        )
    )
    audit = audit_saved_attempts(
        attempts,
        candidates,
        page_index=page_number - 1,
        model_ids=model_ids,
        wall_layout_used=layout is not None and layout.extraction_run_id == run_id,
        wall_candidates=wall_candidates,
    )

    def exact(candidate: ObservationCandidate) -> Fraction | None:
        if (
            candidate.corroboration_status != "CORROBORATED"
            or candidate.value_numerator is None
            or candidate.value_denominator is None
            or candidate.unit != "in"
        ):
            return None
        return Fraction(candidate.value_numerator, candidate.value_denominator)

    overall = [
        exact(candidate) for proposal, candidate in group if proposal.field_key == OVERALL_FIELD
    ]
    pieces = sorted(
        (
            (proposal.position, exact(candidate))
            for proposal, candidate in group
            if proposal.field_key == PIECE_FIELD
        ),
        key=lambda item: item[0],
    )
    all_page_candidates = tuple(
        session.scalars(
            select(ObservationCandidate)
            .join(Page, Page.id == ObservationCandidate.page_id)
            .where(
                ObservationCandidate.extraction_run_id == run_id,
                Page.index == page_number - 1,
            )
        )
    )
    chain = (
        tuple(value for _position, value in pieces)
        if pieces
        and [position for position, _value in pieces] == list(range(len(pieces)))
        and all(value is not None for _position, value in pieces)
        else None
    )
    proposed = WidthInputs(
        overall=overall[0] if len(overall) == 1 else None,
        cabinets=None,
        fillers=None,
        wall_layout=wall_layout,
        field_cut=_field_cut(session, package_revision_id, package.project_id),
        pieces=chain,  # type: ignore[arg-type]  # all members were checked non-null above
    )
    reason = None if audit.complete else audit.reason
    if not _complete_slot_row(all_page_candidates, tuple(position for position, _value in pieces)):
        reason = "saved proposals do not cover every slot of the selected row"
    if layout is not None and layout.extraction_run_id != run_id:
        reason = "wall layout and width proposals cite different extraction runs"
    return SafetyCase(case_id, truth, proposed, audit.complete, reason), audit


def _key_value(entry: object) -> Fraction | None:
    """Parse the human key's printed text, never a model component or client-specific guess."""
    text = entry.get("text") if isinstance(entry, dict) else entry
    if not isinstance(text, str):
        return None
    try:
        notation, _millimetres = canonical_notation(text)
        return normalise_to_inches(notation).exact
    except (ArithmeticError, TypeError, UnitNormalisationError, ValueError):
        # A confirmed key may spell out both units inside the brackets. Only the exact
        # dual-unit shape is accepted; a trailing VIF or explanatory word still abstains.
        dual = _KEY_DUAL.fullmatch(text)
        if dual is None:
            return None
        try:
            return normalise_to_inches(f'{dual.group(1)}"').exact
        except UnitNormalisationError:
            return None


def _key_inputs(
    entry: dict[str, Any], *, wall_layout: str | None, field_cut: Fraction | None
) -> WidthInputs:
    pieces = entry.get("pieces")
    values: tuple[Fraction, ...] | None = None
    if isinstance(pieces, list):
        parsed: list[Fraction] = []
        for piece in pieces:
            # The two local key versions store a piece as {text,kind} or [text,kind].
            token = piece[0] if isinstance(piece, list) and len(piece) == 2 else piece
            value = _key_value(token)
            if value is None:
                parsed = []
                break
            parsed.append(value)
        if parsed:
            values = tuple(parsed)
    return WidthInputs(
        overall=_key_value(entry.get("overall")),
        cabinets=None,
        fillers=None,
        wall_layout=wall_layout,
        field_cut=field_cut,
        pieces=values,
    )


def evaluate_keyed_product_run(
    session: Session,
    *,
    package_revision_id: UUID,
    key_path: Path,
    truth_wall_layouts: dict[int, str],
) -> tuple[SafetyReport, tuple[AttemptAudit, ...]]:
    """Score private confirmed-key pages against saved product proposals; return counts only.

    Wall truth must come independently from the person. If it is absent, that case remains
    unaccounted even when the product proposed a layout. The company field-cut setting is
    shared by both sides because it is a published project input, not a drawing reading.
    """
    payload = json.loads(key_path.read_text(encoding="utf-8"))
    entries = payload.get("countertops") if isinstance(payload, dict) else None
    if not isinstance(entries, list) or not entries:
        raise ValueError("confirmed form key has no countertop cases")
    page_counts = Counter(entry.get("page") for entry in entries if isinstance(entry, dict))
    cases: list[SafetyCase] = []
    audits: list[AttemptAudit] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or not isinstance(entry.get("page"), int):
            raise TypeError("a confirmed key case lacks a numeric page number")
        page = entry["page"]
        case, audit = product_case_for_page(
            session,
            package_revision_id=package_revision_id,
            page_number=page,
            truth=None,
            case_id=f"page-{page}-case-{index + 1}",
        )
        product_cut = None if case.proposed is None else case.proposed.field_cut
        truth = _key_inputs(
            entry,
            wall_layout=truth_wall_layouts.get(page),
            field_cut=product_cut,
        )
        reason = case.unaccounted_reason
        if page_counts[page] != 1:
            reason = "multiple keyed countertops on a page cannot be aligned to one proposal"
        cases.append(
            SafetyCase(case.case_id, truth, case.proposed, case.raw_attempts_complete, reason)
        )
        audits.append(audit)
    return evaluate(tuple(cases), reader_path="slot_crop"), tuple(audits)


__all__ = [
    "AttemptAudit",
    "audit_saved_attempts",
    "evaluate_keyed_product_run",
    "product_case_for_page",
]
