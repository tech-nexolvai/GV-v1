"""Structured, read-only countertop results for the visual reviewer."""

from __future__ import annotations

import re
from fractions import Fraction
from typing import Annotated, Any, Final, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_session
from app.auth import Action, Principal, require_action, require_project_access
from app.evidence.sides import ReadingSides
from app.models import (
    CheckRun,
    EvidenceSupportingCandidate,
    Finding,
    Package,
    PackageRevision,
    RuleDefinition,
    RuleSnapshot,
    VerdictInput,
)
from app.review.approval import readiness_and_decisions
from app.review.row_location import RowLocation, architect_locations, row_locations
from app.schemas.visual_ui import (
    AgreementFactsOut,
    ArchitectComparedOut,
    ArchitectResultOut,
    CountertopPieceOut,
    CountertopResultOut,
    CountertopResultsOut,
    ExactValueOut,
    HoldOut,
    ReviewerDecisionOut,
    WallLayoutOut,
)
from units.imperial import format_inches
from verdict.outcomes import Outcome
from vocabulary.check_holds import CHECK_HOLD_REASONS
from vocabulary.reviewer_reasons import reviewer_reason
from workflow.architect_pairing_contract import EffectivePairing
from workflow.architect_row_plan import (
    ARCHITECT_CHECK_RULE_ID,
    Disposition,
    PairingLookup,
    effective_architect_pairings,
    pairing_judgments,
    pairing_source_from_notes,
    plan_architect_row,
)
from workflow.slot_row_scope import SlotRow, candidate_is_sealed, candidate_value, slot_rows

router = APIRouter(tags=["visual reviewer"])


def _exact(value: Fraction | None) -> ExactValueOut | None:
    if value is None:
        return None
    return ExactValueOut(
        numerator=str(value.numerator),
        denominator=str(value.denominator),
        display=f'{format_inches(value)}"',
    )


def _trace_value(trace: dict[str, Any], name: str) -> Fraction | None:
    for operand in trace.get("operands", []):
        if not isinstance(operand, dict) or operand.get("name") != name:
            continue
        raw = operand.get("value")
        if isinstance(raw, str):
            try:
                from units.imperial import parse_imperial

                return parse_imperial(raw.removesuffix(" in").removesuffix('"'))
            except (ValueError, TypeError):
                return None
    return None


def _intermediate(trace: dict[str, Any], name: str) -> Fraction | None:
    for pair in trace.get("intermediates", []):
        if isinstance(pair, list) and len(pair) == 2 and pair[0] == name:
            raw = pair[1]
            if isinstance(raw, str):
                try:
                    from units.imperial import parse_imperial

                    result = re.search(r"(?:^|, )result, (.*?)(?:, rendering,|$)", raw)
                    text = result.group(1) if result else raw
                    return parse_imperial(text.removesuffix(" in").removesuffix('"'))
                except (ValueError, TypeError):
                    return None
    return None


def _scale_inputs(trace: dict[str, Any]) -> tuple[Fraction | None, int | None]:
    """Read the stored field-cut operands from the serialized scale-operation trace."""
    raw = next(
        (
            pair[1]
            for pair in trace.get("intermediates", [])
            if isinstance(pair, list) and len(pair) == 2 and pair[0] == "field_cut_total"
        ),
        None,
    )
    if not isinstance(raw, str):
        return None, None
    value_match = re.search(r"(?:^|, )value, (.*?), result,", raw)
    count_match = re.search(r"(?:^|, )multiplier, (.*?), value,", raw)
    try:
        from units.imperial import parse_imperial

        value = (
            None
            if value_match is None
            else parse_imperial(value_match.group(1).removesuffix(" in"))
        )
        count_value = None if count_match is None else parse_imperial(count_match.group(1))
        return (
            value,
            (
                count_value.numerator
                if count_value is not None and count_value.denominator == 1
                else None
            ),
        )
    except (ValueError, TypeError):
        return None, None


def _source_and_kind(
    candidate: Any, typed: bool
) -> tuple[Literal["sealed", "typed", "missing"], str | None]:
    kind = None
    if candidate is not None:
        kind = next(
            (
                flag.removeprefix("kind:")
                for flag in candidate.ambiguity_flags or ()
                if flag.startswith("kind:")
            ),
            None,
        )
    if typed:
        return "typed", kind
    if candidate is None:
        return "missing", None
    if candidate.corroboration_status == "CORROBORATED":
        return "sealed", kind
    return "missing", None


def _row_values(
    row: Any,
    finding: Finding | None,
    verdict_inputs: dict[str, VerdictInput],
) -> tuple[Fraction | None, list[CountertopPieceOut], Fraction | None, int | None, Fraction | None]:
    candidates: dict[int | None, Any] = {}
    for candidate in row.candidates:
        slot = next(
            (
                flag.removeprefix("slot:")
                for flag in candidate.ambiguity_flags or ()
                if flag.startswith("slot:")
            ),
            None,
        )
        position = None if slot == "overall" else int(slot) if slot and slot.isdigit() else -1
        if position != -1:
            candidates[position] = candidate
    saved = {} if row.decision is None else row.decision.measurements
    trace = {} if finding is None else finding.trace
    overall_input = verdict_inputs.get("countertop_width")
    overall = (
        Fraction(overall_input.value_numerator, overall_input.value_denominator)
        if overall_input is not None
        else _trace_value(trace, "countertop_width") if finding is not None else None
    )
    if overall is None:
        candidate = candidates.get(None)
        overall = (
            candidate_value(candidate)
            if candidate is not None and candidate_is_sealed(candidate)
            else None
        )
    if overall is None and isinstance(saved.get("countertop_width"), dict):
        item = saved["countertop_width"]
        try:
            overall = Fraction(int(item["numerator"]), int(item["denominator"]))
        except (KeyError, ValueError, TypeError, ZeroDivisionError):
            pass
    pieces: list[CountertopPieceOut] = []
    for index in range(row.piece_count):
        recorded_input = verdict_inputs.get(f"piece_widths[{index}]")
        recorded = (
            Fraction(recorded_input.value_numerator, recorded_input.value_denominator)
            if recorded_input is not None
            else _trace_value(trace, f"piece_widths[{index}]") if finding is not None else None
        )
        candidate = candidates.get(index)
        typed = saved.get(f"piece_widths:{index}")
        if recorded is None and isinstance(typed, dict):
            try:
                recorded = Fraction(int(typed["numerator"]), int(typed["denominator"]))
            except (KeyError, ValueError, TypeError, ZeroDivisionError):
                recorded = None
        if recorded is None and candidate is not None and candidate_is_sealed(candidate):
            recorded = candidate_value(candidate)
        source, kind = _source_and_kind(candidate, isinstance(typed, dict) and recorded is not None)
        if recorded_input is not None:
            source = "typed" if recorded_input.evidence_status == "HUMAN_CONFIRMED" else "sealed"
        pieces.append(
            CountertopPieceOut(index=index, value=_exact(recorded), source=source, kind=kind)
        )
    expected = _intermediate(trace, "expected_width") if finding is not None else None
    field_per_end, field_count = _scale_inputs(trace) if finding is not None else (None, None)
    return overall, pieces, field_per_end, field_count, expected


def _wall(row: Any) -> WallLayoutOut:
    decision = row.decision
    candidate = row.wall_candidate
    flags = set(() if candidate is None else candidate.ambiguity_flags or ())
    config = None if decision is None else decision.wall_config
    source: Literal[
        "drawing clues", "both readers", "reviewer", "between panels", "not established"
    ] = ("reviewer" if config is not None else "not established")
    if config is None:
        sealed = next(
            (
                flag.removeprefix("walls-sealed:")
                for flag in flags
                if flag.startswith("walls-sealed:")
            ),
            None,
        )
        config = sealed
        source_flag = next(
            (
                flag.removeprefix("wall-source:")
                for flag in flags
                if flag.startswith("wall-source:")
            ),
            None,
        )
        if source_flag == "vendor-drawing-clues":
            source = "drawing clues"
        elif source_flag == "readers" and sealed is not None:
            source = "both readers"
    if any(
        flag == "check-hold:stone-short-of-ends"
        for candidate in row.candidates
        for flag in candidate.ambiguity_flags or ()
    ):
        source = "between panels"
    labels = {
        "back_left_right": "back wall and both ends",
        "back_only": "back wall only; no field cut at the ends",
        "island": "island; no wall ends",
    }
    return WallLayoutOut(
        config=config, label=None if config is None else labels.get(config, config), source=source
    )


_ARCHITECT_INPUT = re.compile(r"^(architect|vendor)_(overall|piece\[(\d+)\])$")
NOT_CHECKED_YET: Final = (
    "Not checked yet: run the checks to compare this row with the architect's drawing."
)


def _pair_outcomes(trace: dict[str, Any]) -> dict[int, Outcome]:
    """Each compared pair's own outcome, as the engine's stored trace recorded it (`pair[#k]`)."""
    found: dict[int, Outcome] = {}
    for pair in trace.get("intermediates", []):
        if not (isinstance(pair, list) and len(pair) == 2 and isinstance(pair[1], str)):
            continue
        name = pair[0]
        if not (isinstance(name, str) and name.startswith("pair[#") and name.endswith("]")):
            continue
        index = name.removeprefix("pair[#").removesuffix("]")
        if not index.isdigit() or "outcome=<Outcome." not in pair[1]:
            continue
        # The last one: an operand's printed text comes earlier in the record and is not trusted.
        word = pair[1].rsplit("outcome=<Outcome.", 1)[1].split(":", 1)[0]
        if word in Outcome.__members__:
            found[int(index)] = Outcome[word]
    return found


def _architect_positions(
    session: Session, check_run_ids: list[UUID]
) -> dict[UUID, dict[str, RowLocation]]:
    """Where each architect operand the check recorded was read, by check run and operand name.

    Followed through the recorded operand's own evidence (`VerdictInput.canonical_observation_id`
    to its one primary supporting candidate, `workflow/architect_row_evidence.py`), never by
    matching values and never through a pairing made after the check ran. Two statements for any
    number of rows; an operand whose candidate is not exactly one, or cannot be placed, is left out.
    """
    if not check_run_ids:
        return {}
    supporting: dict[tuple[UUID, str], set[UUID]] = {}
    for check_run_id, name, candidate_id in session.execute(
        select(
            VerdictInput.check_run_id,
            VerdictInput.operand_name,
            EvidenceSupportingCandidate.candidate_id,
        )
        .join(
            EvidenceSupportingCandidate,
            EvidenceSupportingCandidate.canonical_observation_id
            == VerdictInput.canonical_observation_id,
        )
        .where(
            VerdictInput.check_run_id.in_(check_run_ids),
            VerdictInput.operand_name.startswith("architect_"),
            EvidenceSupportingCandidate.role == "primary",
        )
    ):
        supporting.setdefault((check_run_id, name), set()).add(candidate_id)
    single = {key: next(iter(ids)) for key, ids in supporting.items() if len(ids) == 1}
    placed = architect_locations(session, set(single.values()))
    positions: dict[UUID, dict[str, RowLocation]] = {}
    for (check_run_id, name), candidate_id in single.items():
        location = placed.get(candidate_id)
        if location is not None:
            positions.setdefault(check_run_id, {})[name] = location
    return positions


def _architect_block(
    session: Session,
    row: SlotRow,
    finding: Finding | None,
    inputs: dict[str, VerdictInput],
    *,
    blocking: set[UUID],
    pairing_lookup: PairingLookup,
    sides: ReadingSides,
    positions: dict[str, RowLocation] | None = None,
) -> ArchitectResultOut:
    """What the architect check recorded for this row, or why nothing was compared."""
    if finding is None:
        pairing = pairing_lookup(session, row.anchor.id)
        plan = plan_architect_row(session, row, pairing, sides=sides)
        return ArchitectResultOut(
            not_compared_reason=(
                plan.reason if plan.disposition is Disposition.NOT_COMPARED else NOT_CHECKED_YET
            ),
            pairing_source=None if pairing is None else pairing.source,
            pairing_judgments=pairing_judgments(None if pairing is None else pairing.source),
        )
    values: dict[tuple[str, int | None], dict[str, Fraction]] = {}
    for name, item in inputs.items():
        match = _ARCHITECT_INPUT.match(name)
        if match is None:
            continue
        side, kind, slot = match.groups()
        key = ("overall", None) if kind == "overall" else ("piece", int(slot))
        values.setdefault(key, {})[side] = Fraction(item.value_numerator, item.value_denominator)
    ordered = sorted(values, key=lambda key: -1 if key[1] is None else key[1])
    source = pairing_source_from_notes(finding.notes)
    decided = finding.outcome in (Outcome.PASS.value, Outcome.FAIL.value)
    pair_outcomes = _pair_outcomes(finding.trace or {}) if decided else {}
    compared: list[ArchitectComparedOut] = []
    for position, (kind, slot) in enumerate(ordered):
        vendor = values[(kind, slot)].get("vendor")
        architect = values[(kind, slot)].get("architect")
        delta = vendor - architect if vendor is not None and architect is not None else None
        vendor_out, architect_out, delta_out = _exact(vendor), _exact(architect), _exact(delta)
        compared.append(
            ArchitectComparedOut(
                kind="overall" if kind == "overall" else "piece",
                vendor_piece=None if slot is None else slot + 1,
                vendor=vendor_out,
                architect=architect_out,
                delta=delta_out,
                vendor_display=None if vendor_out is None else vendor_out.display,
                architect_display=None if architect_out is None else architect_out.display,
                delta_display=None if delta_out is None else delta_out.display,
                outcome=(
                    pair_outcomes.get(position)
                    if decided
                    else Outcome(finding.outcome) if vendor is not None else None
                ),
                architect_location=(positions or {}).get(
                    "architect_overall" if slot is None else f"architect_piece[{slot}]"
                ),
            )
        )
    return ArchitectResultOut(
        outcome=Outcome(finding.outcome),
        finding_id=finding.id,
        reason=finding.reason,
        needs_decision=finding.id in blocking,
        compared=tuple(compared),
        not_compared_reason=None,
        pairing_source=source,
        pairing_judgments=pairing_judgments(source),
    )


@router.get(
    "/projects/{project_id}/packages/{package_id}/countertop-results",
    response_model=CountertopResultsOut,
    summary="Read structured countertop results for the live check run",
)
def countertop_results(
    principal: Annotated[Principal, Depends(require_project_access)],
    _: Annotated[Principal, Depends(require_action(Action.READ_PACKAGE))],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    package_id: UUID,
) -> CountertopResultsOut:
    del principal
    revision = session.execute(
        select(PackageRevision)
        .join(Package, Package.id == PackageRevision.package_id)
        .where(Package.id == package_id, Package.project_id == project_id)
        .order_by(PackageRevision.revision_number.desc())
        .limit(1)
    ).scalar_one_or_none()
    if revision is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return _countertop_results_for_revision(session, package_id, revision)


def _batched_pairings(session: Session, row_anchor_ids: list[UUID]) -> PairingLookup:
    """Every listed row's pairing read at once, answered row by row as a `PairingLookup`."""
    pairings = effective_architect_pairings(session, row_anchor_ids)

    def lookup(_session: Session, row_anchor_id: UUID) -> EffectivePairing | None:
        return pairings.get(row_anchor_id)

    return lookup


def _countertop_results_for_revision(
    session: Session,
    package_id: UUID,
    revision: PackageRevision,
    *,
    pairing_lookup: PairingLookup | None = None,
) -> CountertopResultsOut:
    """Shared projection used by the read API and the signed report writer.

    `pairing_lookup` answers, for a row the architect check wrote nothing for, why nothing was
    compared; by default every such row's pairing record (#1053) is read in one batch
    (`effective_architect_pairings`), so the statement count does not grow with the rows.
    """
    rows = slot_rows(session, revision.id)
    findings = session.execute(
        select(Finding, RuleDefinition.rule_id)
        .join(CheckRun, CheckRun.id == Finding.check_run_id)
        .join(RuleSnapshot, RuleSnapshot.id == CheckRun.rule_snapshot_id)
        .join(RuleDefinition, RuleDefinition.id == RuleSnapshot.rule_definition_id)
        .where(
            Finding.package_revision_id == revision.id,
            CheckRun.superseded_at.is_(None),
            Finding.scope_row_candidate_id.is_not(None),
            RuleDefinition.rule_id.in_(("CT-WIDTH-001", ARCHITECT_CHECK_RULE_ID)),
        )
    ).all()
    finding_by_row = {
        finding.scope_row_candidate_id: finding
        for finding, rule_id in findings
        if rule_id == "CT-WIDTH-001"
    }
    architect_by_row = {
        finding.scope_row_candidate_id: finding
        for finding, rule_id in findings
        if rule_id == ARCHITECT_CHECK_RULE_ID
    }
    lookup = pairing_lookup or _batched_pairings(
        session, [row.anchor.id for row in rows if row.anchor.id not in architect_by_row]
    )
    sides = ReadingSides(session)
    verdict_inputs_by_finding: dict[UUID, dict[str, VerdictInput]] = {}
    if findings:
        for input_row in session.scalars(
            select(VerdictInput).where(
                VerdictInput.check_run_id.in_([finding.check_run_id for finding, _ in findings])
            )
        ).all():
            verdict_inputs_by_finding.setdefault(input_row.check_run_id, {})[
                input_row.operand_name
            ] = input_row
    positions = _architect_positions(
        session, [finding.check_run_id for finding in architect_by_row.values()]
    )
    row_ids = tuple(row.anchor.id for row in rows)
    locations = row_locations(session, row_ids)
    # The decision standing on each result: the reviewer's own latest, or the one it carried over an
    # unchanged re-run (#1073). Read once, with sign-off readiness, so the two cannot disagree.
    readiness, records = readiness_and_decisions(session, revision.id)
    decisions = records.decisions
    need_ids = set(readiness.blocking_finding_ids)
    items: list[CountertopResultOut] = []
    for row in rows:
        finding = finding_by_row.get(row.anchor.id)
        overall, pieces, field_per_end, field_count, expected = _row_values(
            row,
            finding,
            {} if finding is None else verdict_inputs_by_finding.get(finding.check_run_id, {}),
        )
        decision = None if finding is None else decisions.get(finding.id)
        wall_flags = set(
            () if row.wall_candidate is None else row.wall_candidate.ambiguity_flags or ()
        )
        by_slot = {
            next(
                (
                    flag.removeprefix("slot:")
                    for flag in candidate.ambiguity_flags or ()
                    if flag.startswith("slot:")
                ),
                "",
            ): candidate
            for candidate in row.candidates
        }
        agreement_values: list[bool | None] = []
        for slot in ("overall", *(str(index) for index in range(row.piece_count))):
            candidate = by_slot.get(slot)
            if candidate is None:
                agreement_values.append(None)
                continue
            reader_ids = {
                flag.removeprefix("reader-id:")
                for flag in candidate.ambiguity_flags or ()
                if flag.startswith("reader-id:")
            }
            agreement_values.append(
                candidate.corroboration_status == "CORROBORATED" if len(reader_ids) >= 2 else None
            )
        values_agreed = tuple(agreement_values)
        hold = None
        if row.held_reason:
            flags = {
                flag for candidate in row.candidates for flag in candidate.ambiguity_flags or ()
            }
            code = next(
                (
                    flag.removeprefix("check-hold:")
                    for flag in flags
                    if flag.startswith("check-hold:")
                ),
                next(
                    (
                        flag.removeprefix("row-hold:")
                        for flag in flags
                        if flag.startswith("row-hold:")
                    ),
                    "row-held",
                ),
            )
            hold = HoldOut(
                code=code,
                reason=reviewer_reason(flags, row.held_reason)
                or CHECK_HOLD_REASONS.get(code, code),
            )
        # PASS findings may not persist a delta column. When checked, derive the signed difference
        # only from the immutable recorded operand and recorded expected-width trace above.
        delta = (
            overall - expected
            if finding is not None and overall is not None and expected is not None
            else None
        )
        items.append(
            CountertopResultOut(
                finding_id=None if finding is None else finding.id,
                row_id=row.anchor.id,
                page_number=row.page_number,
                label=row.label,
                row_location=locations.get(row.anchor.id),
                outcome=None if finding is None else Outcome(finding.outcome),
                reviewer_decision=(
                    None
                    if decision is None
                    else ReviewerDecisionOut(
                        action=decision.action.action,
                        note=decision.action.note,
                        actor=decision.action.actor,
                        time=decision.action.created_at,
                        carried_over=decision.carried_over,
                        carried_from_finding_id=decision.carried_from_finding_id,
                    )
                ),
                needs_decision=finding is None or finding.id in need_ids,
                printed_overall=_exact(overall),
                pieces=tuple(pieces),
                field_cut_per_end=_exact(field_per_end),
                field_cut_count=field_count,
                expected_total=_exact(expected),
                delta=_exact(delta),
                wall_layout=_wall(row),
                agreement=AgreementFactsOut(
                    both_readers_agreed_on_row=any(
                        flag.startswith("row-choice:")
                        and flag.removeprefix("row-choice:").isdigit()
                        and int(flag.removeprefix("row-choice:")) > 0
                        for candidate in row.candidates
                        for flag in candidate.ambiguity_flags or ()
                    )
                    or None,
                    values_agreed=values_agreed,
                    code_clue_used=any(flag.startswith("wall-clue:") for flag in wall_flags),
                ),
                hold=hold,
                architect=_architect_block(
                    session,
                    row,
                    architect_by_row.get(row.anchor.id),
                    (
                        {}
                        if (architect := architect_by_row.get(row.anchor.id)) is None
                        else verdict_inputs_by_finding.get(architect.check_run_id, {})
                    ),
                    blocking=need_ids,
                    pairing_lookup=lookup,
                    sides=sides,
                    positions=(
                        None
                        if (architect := architect_by_row.get(row.anchor.id)) is None
                        else positions.get(architect.check_run_id)
                    ),
                ),
            )
        )
    return CountertopResultsOut(package_id=package_id, revision_id=revision.id, items=tuple(items))


__all__ = ["_countertop_results_for_revision", "countertop_results", "router"]
