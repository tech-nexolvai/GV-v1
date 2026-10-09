"""Structured, read-only countertop results for the visual reviewer."""

from __future__ import annotations

import re
from fractions import Fraction
from typing import Annotated, Any, Final, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, aliased

from app.api.dependencies import get_session
from app.auth import Action, Principal, require_action, require_project_access
from app.evidence.sides import ReadingSides
from app.models import (
    CanonicalObservation,
    CheckRun,
    EvidenceArtifact,
    EvidenceSupportingCandidate,
    Finding,
    ObservationCandidate,
    Package,
    PackageRevision,
    RuleDefinition,
    RuleSnapshot,
    VerdictInput,
)
from app.models.evidence import EvidenceArtifactKind
from app.review.approval import readiness_and_decisions
from app.review.row_location import RowLocation, architect_locations, row_and_slot_locations
from app.schemas.visual_ui import (
    AgreementFactsOut,
    ArchitectComparedOut,
    ArchitectResultOut,
    CountertopPieceOut,
    CountertopResultOut,
    CountertopResultsOut,
    ExactValueOut,
    HoldOut,
    PageWithoutCountertopOut,
    ReviewerDecisionOut,
    RowNotCheckedOut,
    WallLayoutOut,
)
from units.imperial import format_inches
from verdict.outcomes import Outcome
from vocabulary.check_holds import CHECK_HOLD_REASONS, with_no_stone_note
from vocabulary.drawn_length import NO_DRAWN_LENGTH_WITNESS, not_checked_note
from vocabulary.reviewer_reasons import reviewer_reason
from vocabulary.wall_layouts import is_between_panels, wall_layout_words
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
from workflow.part_operands import reviewer_owned_pages
from workflow.slot_row_scope import (
    ROW_CHOICE_SPLIT,
    SlotRow,
    UnchosenRowPage,
    candidate_is_sealed,
    candidate_value,
    slot_rows_and_unchosen_pages,
)

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


#: Evidence statuses a sealed reading's confirmed observation may have.
_ADMISSIBLE_EVIDENCE: Final = frozenset({"CORROBORATED", "HUMAN_CONFIRMED"})


class _StoredCrops:
    """Which sealed readings have a confirmed observation with a stored crop (#1049).

    `by_candidate`: a sealed slot-reader candidate -> its one admissible confirmed observation on
    the same page, when that observation has a stored crop. `recorded`: a recorded operand's
    confirmed observation -> its (page, document version), when it has a stored crop. Display
    only; nothing here is a value or reaches a verdict.
    """

    def __init__(
        self,
        by_candidate: dict[UUID, UUID] | None = None,
        recorded: dict[UUID, tuple[UUID, UUID]] | None = None,
    ) -> None:
        self.by_candidate = by_candidate or {}
        self.recorded = recorded or {}

    def for_reading(
        self, row: SlotRow, candidate: Any, recorded_input: VerdictInput | None
    ) -> UUID | None:
        """The crop's observation for one sealed reading, or `None`; never a neighbour's."""
        if recorded_input is not None:
            if recorded_input.evidence_status == "HUMAN_CONFIRMED":
                return None
            observation = recorded_input.canonical_observation_id
            if observation is None:
                return None
            place = self.recorded.get(observation)
            anchor = (row.anchor.page_id, row.anchor.document_version_id)
            return observation if place == anchor else None
        if candidate is None or not candidate_is_sealed(candidate):
            return None
        return self.by_candidate.get(candidate.id)


def _supported_candidate(flags: Any) -> UUID | None:
    """The candidate a per-reader child supports (`supports:<id>`), from exactly one record."""
    if not isinstance(flags, list) or "slot-reader-support" not in flags:
        return None
    found = [flag.removeprefix("supports:") for flag in flags if str(flag).startswith("supports:")]
    if len(found) != 1:
        return None
    try:
        return UUID(found[0])
    except ValueError:
        return None


def _stored_crops(
    session: Session, rows: tuple[SlotRow, ...], recorded_ids: set[UUID]
) -> _StoredCrops:
    """One statement for every row: the confirmed observations behind the sealed readings and the
    recorded operands, and whether a stored crop exists for each.

    "Has a crop" is the crop endpoint's own rule (`app/api/finding_chain.py:evidence_crop`): a crop
    owned by the observation, or by one of its supporting candidates. A slot candidate's
    observation is found through its supporting links only — the candidate itself, or a per-reader
    child of the same run that records `supports:<candidate>` — never by matching a value. More than
    one admissible observation for a candidate is ambiguous and gives none.
    """
    sealed = {
        candidate.id: candidate
        for row in rows
        for candidate in row.candidates
        if candidate_is_sealed(candidate)
    }
    if not sealed and not recorded_ids:
        return _StoredCrops()
    support = aliased(EvidenceSupportingCandidate)
    has_crop = (
        select(EvidenceArtifact.id)
        .where(
            EvidenceArtifact.kind == EvidenceArtifactKind.CROP.value,
            or_(
                EvidenceArtifact.canonical_observation_id == CanonicalObservation.id,
                EvidenceArtifact.candidate_id.in_(
                    select(support.candidate_id).where(
                        support.canonical_observation_id == CanonicalObservation.id
                    )
                    # Two levels down: without this the inner select takes its own copy of
                    # the table, and "has a crop" becomes "any observation has a crop".
                    .correlate(CanonicalObservation)
                ),
            ),
        )
        .correlate(CanonicalObservation)
        .exists()
    )
    linked = and_(
        ObservationCandidate.extraction_run_id.in_(
            {candidate.extraction_run_id for candidate in sealed.values()}
        ),
        or_(
            ObservationCandidate.id.in_(sealed),
            ObservationCandidate.ambiguity_flags.contains(["slot-reader-support"]),
        ),
    )
    records = session.execute(
        select(
            CanonicalObservation.id,
            CanonicalObservation.page_id,
            CanonicalObservation.document_version_id,
            CanonicalObservation.status,
            has_crop.label("has_crop"),
            ObservationCandidate.id,
            ObservationCandidate.extraction_run_id,
            ObservationCandidate.ambiguity_flags,
        )
        .select_from(CanonicalObservation)
        .outerjoin(
            EvidenceSupportingCandidate,
            EvidenceSupportingCandidate.canonical_observation_id == CanonicalObservation.id,
        )
        .outerjoin(
            ObservationCandidate,
            ObservationCandidate.id == EvidenceSupportingCandidate.candidate_id,
        )
        .where(or_(CanonicalObservation.id.in_(recorded_ids), linked))
    ).all()
    cropped: dict[UUID, tuple[UUID, UUID]] = {}
    links: dict[UUID, set[UUID]] = {}
    for observation, page_id, version_id, state, crop, candidate_id, run_id, flags in records:
        if crop:
            cropped[observation] = (page_id, version_id)
        if candidate_id is None:
            continue
        owner_id = candidate_id if candidate_id in sealed else _supported_candidate(flags)
        owner = None if owner_id is None else sealed.get(owner_id)
        if (
            owner is None
            or run_id != owner.extraction_run_id
            or state not in _ADMISSIBLE_EVIDENCE
            or (page_id, version_id) != (owner.page_id, owner.document_version_id)
        ):
            continue
        links.setdefault(owner.id, set()).add(observation)
    return _StoredCrops(
        by_candidate={
            owner_id: next(iter(observations))
            for owner_id, observations in links.items()
            if len(observations) == 1 and next(iter(observations)) in cropped
        },
        recorded={
            observation: cropped[observation]
            for observation in recorded_ids
            if observation in cropped
        },
    )


def _row_values(
    row: Any,
    finding: Finding | None,
    verdict_inputs: dict[str, VerdictInput],
    slot_locations: dict[UUID, RowLocation] | None = None,
    crops: _StoredCrops | None = None,
) -> tuple[
    Fraction | None,
    list[CountertopPieceOut],
    Fraction | None,
    int | None,
    Fraction | None,
    tuple[RowLocation | None, UUID | None],
]:
    """The row's values, and (last) where its printed overall was read and its crop's observation.

    A slot's location and crop come from the one candidate that claims that slot; a slot two
    candidates claim gets neither.
    """
    slot_locations = slot_locations or {}
    crops = crops or _StoredCrops()
    claims: dict[int | None, int] = {}
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
            claims[position] = claims.get(position, 0) + 1

    def located(position: int | None) -> RowLocation | None:
        candidate = candidates.get(position)
        if candidate is None or claims.get(position) != 1:
            return None
        return slot_locations.get(candidate.id)

    def cropped(position: int | None, recorded_input: VerdictInput | None) -> UUID | None:
        if claims.get(position, 0) > 1:
            return None
        return crops.for_reading(row, candidates.get(position), recorded_input)

    saved = {} if row.decision is None else row.decision.measurements
    trace = {} if finding is None else finding.trace
    overall_input = verdict_inputs.get("countertop_width")
    overall = (
        Fraction(overall_input.value_numerator, overall_input.value_denominator)
        if overall_input is not None
        else _trace_value(trace, "countertop_width") if finding is not None else None
    )
    overall_crop = (
        cropped(None, overall_input)
        if overall_input is not None
        else None if overall is not None else cropped(None, None)
    )
    if overall is None:
        candidate = candidates.get(None)
        overall = (
            candidate_value(candidate)
            if candidate is not None and candidate_is_sealed(candidate)
            else None
        )
        if overall is None:
            overall_crop = None
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
            CountertopPieceOut(
                index=index,
                value=_exact(recorded),
                source=source,
                kind=kind,
                location=located(index),
                canonical_observation_id=(
                    cropped(index, recorded_input) if source == "sealed" else None
                ),
            )
        )
    expected = _intermediate(trace, "expected_width") if finding is not None else None
    field_per_end, field_count = _scale_inputs(trace) if finding is not None else (None, None)
    return overall, pieces, field_per_end, field_count, expected, (located(None), overall_crop)


def _drawn_length_note(row: Any, verdict_inputs: dict[str, VerdictInput]) -> str | None:
    """ "Drawn length not checked (no scale)" for the row's sealed readings the check missed (#1107).

    Only readings: where the recorded check used a value the reviewer typed for that position, the
    reading's missing witness no longer matters and is not named.
    """
    positions: set[int | None] = set()
    for candidate in row.candidates:
        flags = candidate.ambiguity_flags or ()
        if NO_DRAWN_LENGTH_WITNESS not in flags or not candidate_is_sealed(candidate):
            continue
        slot = next((flag.removeprefix("slot:") for flag in flags if flag.startswith("slot:")), "")
        if slot == "overall":
            position: int | None = None
            name = "countertop_width"
        elif slot.isdigit():
            position = int(slot)
            name = f"piece_widths[{position}]"
        else:
            continue
        recorded = verdict_inputs.get(name)
        if recorded is not None and recorded.evidence_status == "HUMAN_CONFIRMED":
            continue
        positions.add(position)
    return not_checked_note(positions)


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
    between_panels = any(
        is_between_panels(candidate.ambiguity_flags or ()) for candidate in row.candidates
    )
    if between_panels:
        source = "between panels"
    return WallLayoutOut(
        config=config,
        label=(
            None if config is None else wall_layout_words(config, between_panels=between_panels)
        ),
        source=source,
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


#: Why a page whose countertop row was not chosen has no architect comparison (#1093).
NO_ROW_CHOSEN: Final = (
    "Not compared: no countertop line was chosen on this page, so nothing was read to compare "
    "with the architect's drawing."
)


def _decision_out(decision: Any) -> ReviewerDecisionOut | None:
    if decision is None:
        return None
    return ReviewerDecisionOut(
        action=decision.action.action,
        note=decision.action.note,
        actor=decision.action.actor,
        time=decision.action.created_at,
        carried_over=decision.carried_over,
        carried_from_finding_id=decision.carried_from_finding_id,
    )


def _split_page_item(
    page: UnchosenRowPage, finding: Finding | None, decision: Any, need_ids: set[UUID]
) -> CountertopResultOut:
    """A page the AIs picked different countertop lines on (#1093): one held item, nothing read.

    No pieces, no walls and no architect comparison, because no row was chosen to read. It needs the
    reviewer until its check result is decided, exactly like a held row.
    """
    return CountertopResultOut(
        finding_id=None if finding is None else finding.id,
        row_id=page.record.id,
        page_number=page.page_number,
        label=page.label,
        row_location=None,
        outcome=None if finding is None else Outcome(finding.outcome),
        reviewer_decision=_decision_out(decision),
        needs_decision=finding is None or finding.id in need_ids,
        printed_overall=None,
        pieces=(),
        field_cut_per_end=None,
        field_cut_count=None,
        expected_total=None,
        delta=None,
        wall_layout=WallLayoutOut(config=None, label=None, source="not established"),
        agreement=AgreementFactsOut(
            both_readers_agreed_on_row=False, values_agreed=(), code_clue_used=False
        ),
        hold=HoldOut(code=ROW_CHOICE_SPLIT, reason=page.reason),
        architect=ArchitectResultOut(not_compared_reason=NO_ROW_CHOSEN),
    )


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
    # With the pages whose countertop row was not chosen (#1093), except where a reviewer-owned run
    # is the source: the check asks nothing there, so neither does this list.
    rows, unchosen = slot_rows_and_unchosen_pages(session, revision.id)
    owned = (
        reviewer_owned_pages(session, revision.id)
        if unchosen or any(row.also for row in rows)
        else set()
    )
    unchosen = tuple(page for page in unchosen if page.page_number - 1 not in owned)
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
    locations, slot_locations = row_and_slot_locations(session, row_ids)
    crops = _stored_crops(
        session,
        rows,
        {
            item.canonical_observation_id
            for finding in finding_by_row.values()
            for name, item in verdict_inputs_by_finding.get(finding.check_run_id, {}).items()
            if item.canonical_observation_id is not None
            and (name == "countertop_width" or name.startswith("piece_widths["))
        },
    )
    # The decision standing on each result: the reviewer's own latest, or the one it carried over an
    # unchanged re-run (#1073). Read once, with sign-off readiness, so the two cannot disagree.
    readiness, records = readiness_and_decisions(session, revision.id)
    decisions = records.decisions
    need_ids = set(readiness.blocking_finding_ids)
    items: list[CountertopResultOut] = []
    for row in rows:
        finding = finding_by_row.get(row.anchor.id)
        overall, pieces, field_per_end, field_count, expected, overall_read = _row_values(
            row,
            finding,
            {} if finding is None else verdict_inputs_by_finding.get(finding.check_run_id, {}),
            slot_locations,
            crops,
        )
        decision = None if finding is None else decisions.get(finding.id)
        drawn_length_note = _drawn_length_note(
            row,
            {} if finding is None else verdict_inputs_by_finding.get(finding.check_run_id, {}),
        )
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
                reason=with_no_stone_note(
                    reviewer_reason(flags, row.held_reason) or CHECK_HOLD_REASONS.get(code, code),
                    flags,
                ),
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
                reviewer_decision=_decision_out(decision),
                needs_decision=finding is None or finding.id in need_ids,
                printed_overall=_exact(overall),
                printed_overall_location=overall_read[0],
                printed_overall_canonical_observation_id=overall_read[1],
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
                drawn_length_note=drawn_length_note,
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
    for page in unchosen:
        if not page.split:
            continue
        finding = finding_by_row.get(page.record.id)
        items.append(
            _split_page_item(
                page, finding, None if finding is None else decisions.get(finding.id), need_ids
            )
        )
    return CountertopResultsOut(
        package_id=package_id,
        revision_id=revision.id,
        # Stable: rows on one page keep their order; a split page sits among them by page number.
        items=tuple(sorted(items, key=lambda item: item.page_number)),
        pages_without_countertop=tuple(
            PageWithoutCountertopOut(page_number=page.page_number, reason=page.reason)
            for page in unchosen
            if not page.split
        ),
        rows_not_checked=_rows_not_checked(rows, owned),
    )


def _rows_not_checked(rows: tuple[SlotRow, ...], owned: set[int]) -> tuple[RowNotCheckedOut, ...]:
    """One entry per vendor page where an AI named a second countertop row (#1108), in page order.

    V1 reads one countertop row per page, so the second is listed, never checked and never
    blocking. A page a reviewer-owned run is the source on is left out, as for the pages above.
    """
    named: dict[int, dict[int, set[str]]] = {}
    for row in rows:
        if row.page_number - 1 in owned:
            continue
        for model, number in row.also:
            named.setdefault(row.page_number, {}).setdefault(number, set()).add(model)
    return tuple(
        RowNotCheckedOut(
            page_number=page_number,
            reason=(
                "An AI found a second countertop on this page ("
                + "; ".join(
                    f"numbered line {number}, named by {' and '.join(sorted(models))}"
                    for number, models in sorted(boxes.items())
                )
                + "). Only one countertop line per page is read, so it was not checked. Check it "
                "on the drawing."
            ),
        )
        for page_number, boxes in sorted(named.items())
    )


__all__ = ["_countertop_results_for_revision", "countertop_results", "router"]
