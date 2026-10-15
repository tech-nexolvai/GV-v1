"""Structured, read-only countertop results for the visual reviewer."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from fractions import Fraction
from typing import Annotated, Any, Final, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, aliased

from app.api.dependencies import get_session
from app.api.signed_off import open_revision
from app.audit.events import AuditCategory, emit
from app.auth import Action, Principal, require_action, require_project_access
from app.evidence.sides import ReadingSides, has_separate_architect_file
from app.lifecycle.carry_forward import CarriedOrigin, wall_answer_origins
from app.models import (
    CanonicalObservation,
    CheckRun,
    EvidenceArtifact,
    EvidenceSupportingCandidate,
    Finding,
    NoCountertopAnswer,
    ObservationCandidate,
    Package,
    PackageRevision,
    PackageRevisionDocument,
    Page,
    RuleDefinition,
    RuleSnapshot,
    VerdictInput,
)
from app.models.evidence import NO_COUNTERTOP_NOTE_MAX_LENGTH, EvidenceArtifactKind
from app.review.approval import readiness_and_decisions
from app.review.architect_view_match import (
    SeparateFileMatches,
    compared_view,
    match_out,
    separate_file_views,
)
from app.review.cabinet_reading import results_note
from app.review.drawings_not_read import drawings_not_read
from app.review.no_countertop import (
    PageAnswer,
    answered_pages,
    blocking_pages,
    countertops_checked,
    pages_words,
    reading_answer,
)
from app.review.row_location import RowLocation, architect_locations, row_and_slot_locations
from app.review.wall_cases import (
    FIELD_CUT_CHANGED,
    current_field_cut,
    field_cut_changed,
    field_cut_text,
    wall_cases_out,
)
from app.schemas.visual_ui import (
    AgreementFactsOut,
    ArchitectComparedOut,
    ArchitectResultOut,
    BlankPageProofOut,
    CheckPlanOut,
    CountertopItemRefOut,
    CountertopPieceOut,
    CountertopResultOut,
    CountertopResultsOut,
    ExactValueOut,
    HoldOut,
    ItemPlanOut,
    NoCountertopConfirmationOut,
    PageWithoutCountertopOut,
    PlannedFindingOut,
    PlannedRuleOut,
    ReviewerDecisionOut,
    RowNotCheckedOut,
    WallCasesOut,
    WallLayoutOut,
)
from units.imperial import format_inches
from verdict.outcomes import Outcome
from vocabulary.check_holds import CHECK_HOLD_REASONS, with_no_stone_note
from vocabulary.drawn_length import NO_DRAWN_LENGTH_WITNESS, not_checked_note
from vocabulary.reviewer_reasons import reviewer_reason
from vocabulary.wall_layouts import (
    AUTOMATIC_WALL_SOURCE,
    AUTOMATIC_WALLS_LINE,
    NOT_NEEDED_FAIL_LINE,
    NOT_NEEDED_PASS_LINE,
    READERS_WALL_SOURCE,
    chosen_by_line,
    is_between_panels,
    wall_layout_words,
)
from workflow.architect_match_records import effective_architect_matches
from workflow.architect_pairing_contract import EffectivePairing

# The batch form of `architect_views` (#1167), private to its module: the countertop results ask
# it once per revision instead of once per row.
from workflow.architect_pairing_records import _architect_views_by_page
from workflow.architect_row_plan import (
    ARCHITECT_CHECK_RULE_ID,
    PAIR_BY_REVIEWER,
    Disposition,
    PairingLookup,
    architect_file_indexed,
    architect_matches_waiting_for_run,
    effective_architect_pairings,
    pairing_judgments,
    pairing_source_from_notes,
    plan_architect_row,
)
from workflow.item_plan import LiveCountertop, PlannedFinding, check_plan_for, countertops_by_row
from workflow.pariksha_gates import stopped_by_rekha
from workflow.part_operands import reviewer_owned_pages
from workflow.slot_row_scope import (
    ROW_CHOICE_SPLIT,
    SlotRow,
    UnchosenRowPage,
    candidate_is_sealed,
    candidate_value,
    slot_rows_and_unchosen_pages,
    unchosen_page,
)
from workflow.wall_cases import is_every_wall_case, scale_inputs, stored_wall_cases

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
    """Read the stored field-cut operands from the serialized scale-operation trace.

    The engine writes the inputs as `value, 1 in, multiplier, 2`; the earlier reader expected the
    other order and so never found them (#1202 found it). `scale_inputs` reads either order.
    """
    raw = next(
        (
            pair[1]
            for pair in trace.get("intermediates", [])
            if isinstance(pair, list) and len(pair) == 2 and pair[0] == "field_cut_total"
        ),
        None,
    )
    return scale_inputs(raw)


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

    # A width once typed on the row's decision fills nothing since #1339 (confirm-only), so it is
    # never shown as a value: only what the check used, or a sealed reading, is.
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
    pieces: list[CountertopPieceOut] = []
    for index in range(row.piece_count):
        recorded_input = verdict_inputs.get(f"piece_widths[{index}]")
        recorded = (
            Fraction(recorded_input.value_numerator, recorded_input.value_denominator)
            if recorded_input is not None
            else _trace_value(trace, f"piece_widths[{index}]") if finding is not None else None
        )
        candidate = candidates.get(index)
        if recorded is None and candidate is not None and candidate_is_sealed(candidate):
            recorded = candidate_value(candidate)
        source, kind = _source_and_kind(candidate, False)
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
    if finding is not None and field_per_end is None:
        # Checked for every wall case (#1202): the field cut per end the cases used; no one count.
        field_per_end = next(
            (case.field_cut_per_end for case in stored_wall_cases(trace) if case.field_cut_per_end),
            None,
        )
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


def _wall(
    row: Any,
    origins: Mapping[UUID, CarriedOrigin] | None = None,
    finding: Finding | None = None,
) -> WallLayoutOut:
    """The walls the row's result used, who set them, and the results line saying so (#1202)."""
    decision = row.decision
    origin = None if decision is None or origins is None else origins.get(decision.id)
    candidate = row.wall_candidate
    flags = set(() if candidate is None else candidate.ambiguity_flags or ())
    config = None if decision is None else decision.wall_config
    source: Literal[
        "drawing clues",
        "both readers",
        "reviewer",
        "between panels",
        "not established",
        "code ink and both AIs",
        "not needed",
    ] = (
        "reviewer" if config is not None else "not established"
    )
    between_panels = any(
        is_between_panels(candidate.ambiguity_flags or ()) for candidate in row.candidates
    )
    line: str | None = None
    if config is not None:
        who = origin.given_by if origin is not None and origin.given_by else decision.confirmed_by
        line = chosen_by_line(config, who, between_panels=between_panels)
    elif (
        finding is not None
        and is_every_wall_case(finding.trace)
        and finding.outcome in (Outcome.FAIL.value, Outcome.PASS.value)
    ):
        # Wrong (or right) for every wall case: no walls were used, and none are asked for.
        return WallLayoutOut(
            config=None,
            label=None,
            source="not needed",
            line=(
                NOT_NEEDED_FAIL_LINE
                if finding.outcome == Outcome.FAIL.value
                else NOT_NEEDED_PASS_LINE
            ),
        )
    wall_hold = getattr(row, "wall_hold", None)
    if config is None and wall_hold is not None:
        # A reviewer's earlier wall answer could not be carried to this reading (#1241): no walls
        # were used, so the sealed proposal is never shown as this result's walls.
        line = f"Walls asked again: {wall_hold}"
    elif config is None:
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
        held = any(flag.startswith("walls-held:") for flag in flags)
        if (
            source_flag == AUTOMATIC_WALL_SOURCE
            and sealed == "back_left_right"
            and not held
            and not between_panels
        ):
            source = "code ink and both AIs"
            line = AUTOMATIC_WALLS_LINE
        elif source_flag == "vendor-drawing-clues":
            # Before #1202 code alone settled these; now only a proposal for the reviewer.
            source = "drawing clues"
        elif source_flag in (READERS_WALL_SOURCE, "drawing-and-readers") and sealed is not None:
            source = "both readers"
    # "Between panels" says where the stone stops, so it is the source the row shows; whether a
    # reviewer chose the walls is kept apart, so their choice never reads as a proposal (#1332).
    chosen_by_reviewer = source == "reviewer"
    if between_panels:
        source = "between panels"
    return WallLayoutOut(
        config=config,
        label=(
            None if config is None else wall_layout_words(config, between_panels=between_panels)
        ),
        source=source,
        line=line,
        carried_from_revision=(
            None if origin is None or source != "reviewer" else origin.revision_number
        ),
        carried_by=None if origin is None or source != "reviewer" else origin.given_by,
        carried_given_at=None if origin is None or source != "reviewer" else origin.given_at,
        chosen_by_reviewer=chosen_by_reviewer,
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
    separate_architect_file: bool = False,
    matches: SeparateFileMatches | None = None,
) -> ArchitectResultOut:
    """What the architect check recorded for this row, or why nothing was compared.

    `separate_architect_file` (#1161): the revision's architect drawings came as their own file.
    `matches` (#1168): when that file was indexed, every row's match with one of its views, read
    once for the revision; the row then says its match state and the view it was compared with.
    `None` on a combined sheet: the block is exactly as before (every new field null).
    """
    view_match = None if matches is None else match_out(matches, row.anchor.id)
    row_match = None if matches is None else matches.matches.get(row.anchor.id)
    if finding is None:
        pairing = pairing_lookup(session, row.anchor.id)
        plan = plan_architect_row(
            session,
            row,
            pairing,
            sides=sides,
            separate_architect_file=separate_architect_file,
            architect_file_indexed=matches is not None,
            match=row_match,
            match_waits_for_run=(
                matches is not None
                and row_match is not None
                and row_match.record_id in matches.waiting
            ),
            # Read once per revision for the rows with no match (#1168): no query per row.
            own_architect_view=(
                None if matches is None else row.anchor.page_id in matches.own_view_pages
            ),
        )
        return ArchitectResultOut(
            not_compared_reason=(
                plan.reason if plan.disposition is Disposition.NOT_COMPARED else NOT_CHECKED_YET
            ),
            pairing_source=None if pairing is None else pairing.source,
            pairing_judgments=pairing_judgments(None if pairing is None else pairing.source),
            match=view_match,
            can_pair=None if matches is None else False,
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
    # Named only when the row's own result shows it used the view (#1168).
    view, view_text = (
        (None, None)
        if matches is None
        else compared_view(
            matches,
            row.anchor.id,
            locations=[pair.architect_location for pair in compared],
            finding_created_at=finding.created_at,
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
        match=view_match,
        # A pairing is offered only where the check's reason asks for one (#1168); otherwise a
        # matched row is compared by hand or waits for a run.
        can_pair=(
            None
            if matches is None
            else finding.id in blocking and PAIR_BY_REVIEWER in (finding.reason or "")
            # A view chosen after the run: the pairing waits for that run, never offered now.
            and (row_match is None or row_match.record_id not in matches.waiting)
        ),
        compared_with=view,
        compared_with_text=view_text,
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
    revision_number: Annotated[
        int | None,
        Query(
            alias="revision",
            ge=1,
            description=(
                "An earlier revision's number, to read its results as they were (#1180). "
                "Default: the newest revision."
            ),
        ),
    ] = None,
) -> CountertopResultsOut:
    del principal
    # The package is loaded with its revision, so its product (#1205) costs no statement of its own.
    chosen = select(PackageRevision, Package).join(
        Package, Package.id == PackageRevision.package_id
    )
    chosen = chosen.where(Package.id == package_id, Package.project_id == project_id)
    if revision_number is not None:
        chosen = chosen.where(PackageRevision.revision_number == revision_number)
    found = session.execute(
        chosen.order_by(PackageRevision.revision_number.desc()).limit(1)
    ).first()
    if found is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return _countertop_results_for_revision(session, package_id, found[0])


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

    A page whose line both AIs chose but Rekha's gate stopped (no printed numbers, #1228) is the
    same held item with the same hold code (the screen shows it as a page to pick the row on), and
    says truthfully that both readers agreed on the line.
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
            both_readers_agreed_on_row=stopped_by_rekha(page.record.ambiguity_flags),
            values_agreed=(),
            code_clue_used=False,
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
    When the architect's drawings are their own indexed file (#1168), every row's match with one
    of its views is read once per revision, with the matches waiting for a run and the views.

    **A package whose checks include no countertop** (cabinets, #1205) gets no countertop item,
    listed page, second row or unread drawing: none is a question for its review. Its results carry
    the product, `countertops_checked=False` and the one plain line instead, and every reader of
    this projection — the screen, the review assistant, the frozen signed files — takes them from
    here, so none of them can list a countertop page for a cabinet review.
    """
    # The package's product (#1205). Read from the identity map when the caller already loaded the
    # package (the read endpoint does).
    package = session.get(Package, package_id)
    product = None if package is None else package.product_type
    if not countertops_checked(product):
        return CountertopResultsOut(
            package_id=package_id,
            revision_id=revision.id,
            items=(),
            product_type=product,
            countertops_checked=False,
            reading_note=results_note(session, revision.id, product),
        )
    # With the pages whose countertop row was not chosen (#1093), except where a reviewer-owned run
    # is the source: the check asks nothing there, so neither does this list.
    rows, unchosen = slot_rows_and_unchosen_pages(session, revision.id)
    owned = (
        reviewer_owned_pages(session, revision.id)
        if unchosen or any(row.also for row in rows)
        else set()
    )
    unchosen = tuple(page for page in unchosen if page.page_number - 1 not in owned)
    # A reviewer's answer on a page listed with no countertop (#1191): "There is a countertop" makes
    # it blocking like a split page; "Confirm: no countertop here" is shown on its listed entry.
    unchosen, page_answers = answered_pages(session, revision, unchosen)
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
        session,
        [row.anchor.id for row in rows if row.anchor.id not in architect_by_row],
    )
    sides = ReadingSides(session)
    # Asked once per revision (#1161): whether the architect's drawings came as their own file.
    separate_architect = bool(rows) and has_separate_architect_file(session, revision.id)
    # ... and, when that file was indexed, every row's match with one of its views (#1168), read
    # once. A combined sheet asks nothing more, and its architect blocks are as before.
    indexed = separate_architect and architect_file_indexed(session, revision.id)
    row_anchor_ids = [row.anchor.id for row in rows]
    # The live countertop confirmed from each row (#1245): the screen groups the row's results under
    # it. One statement, or a fixed few after a re-read. Asked only when there are rows.
    countertop_by_row = countertops_by_row(session, revision.id) if rows else {}
    matches = effective_architect_matches(session, row_anchor_ids) if indexed else {}
    waiting = architect_matches_waiting_for_run(
        session, revision.id, {match.record_id for match in matches.values() if match is not None}
    )
    # Which vendor pages hold their own architect drawing, for the rows with no match (one
    # statement, only when such a row exists): `plan_architect_row` would ask once per row.
    unmatched_pages = {row.anchor.page_id for row in rows if matches.get(row.anchor.id) is None}
    own_view_pages = (
        frozenset(_architect_views_by_page(session, unmatched_pages))
        if indexed and unmatched_pages
        else frozenset()
    )
    view_matches = (
        separate_file_views(session, package_id, matches, waiting, own_view_pages=own_view_pages)
        if indexed
        else None
    )
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
    pages_blocking = blocking_pages(
        unchosen, page_answers, {row for row in finding_by_row if row is not None}
    )
    readiness, records = readiness_and_decisions(session, revision.id, pages=pages_blocking)
    decisions = records.decisions
    need_ids = set(readiness.blocking_finding_ids)
    # Wall answers carried from an earlier revision say so (#1180).
    wall_origins = wall_answer_origins(session, (row.decision for row in rows))
    # The field cut the newest results used, for the field-cut explanation (#1202): read from the
    # results already loaded, so the statement count does not change.
    current = current_field_cut(session, revision.id) if rows else None
    in_force = field_cut_text(current)
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
                wall_layout=_wall(row, wall_origins, finding),
                wall_cases=(
                    WallCasesOut(unavailable_reason=FIELD_CUT_CHANGED)
                    if finding is not None
                    and is_every_wall_case(finding.trace)
                    and field_cut_changed(finding, current)
                    else (
                        wall_cases_out(
                            finding.trace,
                            between_panels=any(
                                is_between_panels(candidate.ambiguity_flags or ())
                                for candidate in row.candidates
                            ),
                        )
                        if finding is not None and is_every_wall_case(finding.trace)
                        else WallCasesOut()
                    )
                ),
                field_cut_in_force=in_force,
                countertop_item=_countertop_ref(countertop_by_row.get(row.anchor.id)),
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
                    code_clue_used=any(
                        flag.startswith(("wall-clue:", "wall-ink:")) for flag in wall_flags
                    ),
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
                    separate_architect_file=separate_architect,
                    matches=view_matches,
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
        product_type=product,
        countertops_checked=True,
        reading_note=None,
        pages_without_countertop=tuple(
            PageWithoutCountertopOut(
                page_number=page.page_number,
                reason=page.reason,
                record_id=page.record.id,
                confirmation=_confirmation_out(page_answers.get(page.record.id)),
                older_reading=page.kind == "older",
                needs_answer=page.needs_answer,
                blank_page=None if page.needs_answer else _blank_page_out(page),
                doubtful=page.doubtful,
                readers_said_none=page.readers_said_none,
            )
            for page in unchosen
            if not page.split
        ),
        rows_not_checked=_rows_not_checked(rows, owned),
        drawings_not_read=drawings_not_read(
            session,
            revision.id,
            {row.page_number for row in rows}
            | {page.page_number for page in unchosen if not page.found_by_code},
        ),
    )


def _countertop_ref(countertop: LiveCountertop | None) -> CountertopItemRefOut | None:
    if countertop is None:
        return None
    return CountertopItemRefOut(
        item_id=countertop.item_id,
        page_number=countertop.page_number,
        number=countertop.number,
        label=countertop.label,
    )


@router.get(
    "/projects/{project_id}/packages/{package_id}/countertop-items/plan",
    response_model=CheckPlanOut,
    summary="Read each confirmed countertop's check plan: its rules, its results, what they wait on",
)
def countertop_item_plan(
    principal: Annotated[Principal, Depends(require_project_access)],
    _: Annotated[Principal, Depends(require_action(Action.READ_PACKAGE))],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    package_id: UUID,
    revision_number: Annotated[
        int | None,
        Query(
            alias="revision",
            ge=1,
            description="An earlier revision's number. Default: the newest revision.",
        ),
    ] = None,
) -> CheckPlanOut:
    """For each live countertop item (one whose current decision confirms it), the published rules
    for the package's product and where each stands, the results its row or the item itself has,
    and what each rule's whole-package result is still waiting on (#1245, `workflow/item_plan.py`).

    **Read-only, and it decides nothing**: no check runs, no result changes. A countertop with no
    countertop row linked to it says so in `note`.
    """
    del principal
    chosen = (
        select(PackageRevision)
        .join(Package, Package.id == PackageRevision.package_id)
        .where(Package.id == package_id, Package.project_id == project_id)
    )
    if revision_number is not None:
        chosen = chosen.where(PackageRevision.revision_number == revision_number)
    revision = session.scalars(
        chosen.order_by(PackageRevision.revision_number.desc()).limit(1)
    ).first()
    if revision is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    plan = check_plan_for(session, revision.id)

    def found(entry: PlannedFinding) -> PlannedFindingOut:
        return PlannedFindingOut(
            finding_id=entry.finding_id,
            rule_id=entry.rule_id,
            outcome=entry.outcome,
            scope="item" if entry.scope == "item" else "row",
        )

    return CheckPlanOut(
        package_id=package_id,
        revision_id=revision.id,
        items=tuple(
            ItemPlanOut(
                countertop=CountertopItemRefOut(
                    item_id=item.countertop.item_id,
                    page_number=item.countertop.page_number,
                    number=item.countertop.number,
                    label=item.countertop.label,
                ),
                row_id=item.countertop.row_candidate_id,
                rules=tuple(
                    PlannedRuleOut(
                        rule_id=rule.rule_id,
                        resolved=rule.resolved,
                        waits_for=rule.waits_for,
                        version=rule.version,
                        variant=rule.variant,
                        findings=tuple(found(entry) for entry in rule.findings),
                        missing=rule.missing,
                    )
                    for rule in item.rules
                ),
                findings=tuple(found(entry) for entry in item.findings),
                note=item.note,
            )
            for item in plan.items
        ),
    )


def _answer_lock_key(revision_id: UUID) -> int:
    """A stable signed 64-bit key for PostgreSQL's advisory lock on one revision's page answers."""
    digest = hashlib.sha256(f"no-countertop-answer:{revision_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def _confirmation_out(answer: PageAnswer | None) -> NoCountertopConfirmationOut | None:
    """A listed page's confirmation in force; a "There is a countertop" never reaches the list."""
    if answer is None or answer.answer != "no_countertop":
        return None
    return NoCountertopConfirmationOut(
        confirmed_by=answer.answered_by,
        confirmed_at=answer.answered_at,
        from_revision=answer.from_revision,
        note=answer.note,
    )


def _blank_page_out(page: UnchosenRowPage) -> BlankPageProofOut | None:
    """What code checked to prove the page blank (#1195), or `None` when it did not."""
    proof = page.blank_proof
    if proof is None:
        return None
    return BlankPageProofOut(
        rule=proof["rule"],
        stamps=proof["stamps"],
        other_annotations=proof["other-annotations"],
        images=proof["images"],
        drawn_paths=proof["drawn-paths"],
        frames=proof["frames"],
        white_paint=proof["white-paint"],
        typed_texts=proof["typed-texts"],
        typed_characters=proof["typed-characters"],
    )


def _note(text: str | None) -> str | None:
    """A note as stored: trimmed, and none at all when only spaces were typed."""
    trimmed = (text or "").strip()
    return trimmed or None


class NoCountertopAnswerIn(BaseModel):
    """One reviewer click on a page listed with no countertop (#1191). Nothing is pre-selected."""

    model_config = ConfigDict(extra="forbid")

    answer: Literal["no_countertop", "countertop"] = Field(
        description=(
            '"no_countertop": "Confirm: no countertop here". "countertop": "There is a '
            'countertop": the page then needs the reviewer like a split page, and its result comes '
            "with the next check run."
        )
    )
    note: str | None = Field(
        default=None,
        max_length=NO_COUNTERTOP_NOTE_MAX_LENGTH,
        description="An optional note kept with the answer (#1195).",
    )


class NoCountertopPageIn(BaseModel):
    """One page the reviewer confirms in "Confirm these N pages" (#1195): its number and the
    reading record the screen showed for it, so a page read again since is refused, not guessed."""

    model_config = ConfigDict(extra="forbid")

    page_number: int = Field(ge=1)
    expected_record_id: UUID = Field(
        description="The page's `record_id` as the screen showed it (`pages_without_countertop`)."
    )


class NoCountertopPagesIn(BaseModel):
    """ "Confirm these N pages" (#1195): "Confirm: no countertop here" on each page named, exactly
    as if each were clicked on its own, with one optional note for all of them. Pages left out
    (unticked) are not answered and keep waiting."""

    model_config = ConfigDict(extra="forbid")

    pages: list[NoCountertopPageIn] = Field(min_length=1, max_length=500)
    note: str | None = Field(
        default=None,
        max_length=NO_COUNTERTOP_NOTE_MAX_LENGTH,
        description="An optional note kept with each page's answer.",
    )

    @model_validator(mode="after")
    def _each_page_once(self) -> NoCountertopPagesIn:
        records = [page.expected_record_id for page in self.pages]
        if len(set(records)) != len(records):
            raise ValueError("each page may be named only once")
        return self


#: Said when the screen answers a page that is no longer listed (read again, or already answered).
PAGE_NOT_LISTED: Final = (
    "This page is no longer listed with no countertop: it was read again or already answered. "
    "Reload the results."
)


def _pages_not_on_list(numbers: list[int]) -> str:
    verb = "is" if len(numbers) == 1 else "are"
    words = pages_words(numbers)
    return (
        f"{words[0].upper()}{words[1:]} {verb} not on the list of pages not checked. Only pages "
        "listed there can be confirmed."
    )


def stale_pages_sentence(numbers: list[int]) -> str:
    """Said when "Confirm these N pages" names pages the list no longer shows as the screen did."""
    words = pages_words(numbers)
    return (
        f"The list of pages not checked changed since you opened it ({words}: read again or "
        "already answered). Nothing was saved. Reload the results and confirm again."
    )


def _newest_revision(session: Session, project_id: UUID, package_id: UUID) -> PackageRevision:
    revision = session.execute(
        select(PackageRevision)
        .join(Package, Package.id == PackageRevision.package_id)
        .where(Package.id == package_id, Package.project_id == project_id)
        .order_by(PackageRevision.revision_number.desc())
        .limit(1)
    ).scalar_one_or_none()
    if revision is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return revision


def _listed_now(
    session: Session, revision: PackageRevision
) -> tuple[tuple[UnchosenRowPage, ...], dict[UUID, PageAnswer]]:
    """The revision's pages as listed right now, with the answers in force, read under the
    revision's answer lock (taken here), so two answers never cross."""
    session.execute(select(func.pg_advisory_xact_lock(_answer_lock_key(revision.id))))
    _rows, unchosen = slot_rows_and_unchosen_pages(session, revision.id)
    owned = reviewer_owned_pages(session, revision.id) if unchosen else set()
    return answered_pages(
        session, revision, tuple(page for page in unchosen if page.page_number - 1 not in owned)
    )


@router.post(
    "/projects/{project_id}/packages/{package_id}/no-countertop-pages/{record_id}/answer",
    response_model=CountertopResultsOut,
    status_code=status.HTTP_201_CREATED,
    summary="Confirm that a listed page has no countertop, or say there is one",
    dependencies=[Depends(open_revision)],
)
def answer_no_countertop_page(
    principal: Annotated[Principal, Depends(require_project_access)],
    _action: Annotated[Principal, Depends(require_action(Action.CONFIRM_EVIDENCE))],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    package_id: UUID,
    record_id: UUID,
    body: NoCountertopAnswerIn,
    response: Response,
) -> CountertopResultsOut:
    """Record one reviewer answer on a page every AI said has no countertop (#1191).

    The page must be listed right now on the newest revision (409 otherwise, with a plain sentence).
    The answer is a new append-only record with who and when; the newest one counts. Answers the
    updated countertop results, so the screen shows the page confirmed, or as a blocking item.

    **One answer at a time per revision.** The newest answer is read under a transaction-scoped
    advisory lock, so a "Confirm" sent at the same moment as a "There is a countertop" waits for it,
    finds the page no longer listed and is refused, instead of silently overriding it. A second
    "Confirm" on a page already confirmed records nothing and answers 200 with the first person and
    time (the answer was already given).
    """
    revision = _newest_revision(session, project_id, package_id)
    listed_now, in_force = _listed_now(session, revision)
    page = next(
        (page for page in listed_now if page.record.id == record_id and not page.split), None
    )
    if page is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=PAGE_NOT_LISTED)
    standing = in_force.get(page.record.id)
    if standing is not None and standing.answer == body.answer:
        # Already confirmed: nothing new to record; the first person and time stand.
        session.rollback()
        response.status_code = status.HTTP_200_OK
        return _countertop_results_for_revision(session, package_id, revision)
    answer = NoCountertopAnswer(
        package_revision_id=revision.id,
        row_choice_candidate_id=page.record.id,
        page_id=page.record.page_id,
        page_number=page.page_number,
        page_content_hash=page.page_content_hash or "",
        reading_answer=reading_answer(page),
        answer=body.answer,
        answered_by=principal.id,
        note=_note(body.note),
    )
    session.add(answer)
    session.flush()
    emit(
        session,
        category=AuditCategory.REVIEW_ACTION,
        actor=principal.id,
        target_id=answer.id,
        target_type="no_countertop_answer",
    )
    session.commit()
    return _countertop_results_for_revision(session, package_id, revision)


#: The audit target type of one "Confirm these N pages" action (#1195); its id is the `batch_id`
#: on each answer it recorded.
NO_COUNTERTOP_BATCH_TARGET: Final = "no_countertop_answer_batch"


@router.post(
    "/projects/{project_id}/packages/{package_id}/no-countertop-pages/confirm",
    response_model=CountertopResultsOut,
    status_code=status.HTTP_201_CREATED,
    summary="Confirm that each named listed page has no countertop, in one action",
    dependencies=[Depends(open_revision)],
)
def confirm_no_countertop_pages(
    principal: Annotated[Principal, Depends(require_project_access)],
    _action: Annotated[Principal, Depends(require_action(Action.CONFIRM_EVIDENCE))],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    package_id: UUID,
    body: NoCountertopPagesIn,
    response: Response,
) -> CountertopResultsOut:
    """ "Confirm these N pages" (#1195): "Confirm: no countertop here" on every page named.

    The same permission, lock and record as the single answer (`answer_no_countertop_page`): each
    page gets its own answer row (who, when, the optional note) and its own audit event; the rows
    share a batch id, which has an audit event of its own.

    **All or nothing.** The request names each page and the reading record the screen showed for it.
    A record that was never a listed page of this revision is refused with 422. If any named page is
    no longer listed as the screen showed it (read again, answered "There is a countertop", or no
    longer listed), nothing is recorded and the request is refused with 409: the screen reloads.
    A page already confirmed records nothing new (its first person and time stand); when every named
    page was already confirmed the answer is 200, else 201. Pages not named keep waiting.
    """
    revision = _newest_revision(session, project_id, package_id)
    listed_now, in_force = _listed_now(session, revision)
    named = {page.expected_record_id: page.page_number for page in body.pages}
    # 422: a record the list never held for that page (not a listed reading of this revision).
    records = {
        record.id: (page_index + 1, unchosen_page(page_index, record).listed)
        for record, page_index in session.execute(
            select(ObservationCandidate, Page.index)
            .join(Page, Page.id == ObservationCandidate.page_id)
            .join(
                PackageRevisionDocument,
                PackageRevisionDocument.document_version_id == Page.document_version_id,
            )
            .where(
                ObservationCandidate.id.in_(named),
                PackageRevisionDocument.package_revision_id == revision.id,
                ObservationCandidate.ambiguity_flags.contains(["slot-reader-row-choice"]),
            )
        ).all()
    }
    unknown = sorted(
        {number for record, number in named.items() if records.get(record) != (number, True)}
    )
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=_pages_not_on_list(unknown)
        )
    # 409: the list changed since the screen loaded it.
    current = {page.record.id: page for page in listed_now if page.listed}
    stale = sorted({number for record, number in named.items() if record not in current})
    if stale:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=stale_pages_sentence(stale)
        )
    batch_id = uuid4()
    note = _note(body.note)
    recorded = 0
    for record_id in sorted(named, key=lambda record: (named[record], str(record))):
        page = current[record_id]
        standing = in_force.get(record_id)
        if standing is not None and standing.answer == "no_countertop":
            continue  # already confirmed: the first person and time stand
        answer = NoCountertopAnswer(
            package_revision_id=revision.id,
            row_choice_candidate_id=record_id,
            page_id=page.record.page_id,
            page_number=page.page_number,
            page_content_hash=page.page_content_hash or "",
            reading_answer=reading_answer(page),
            answer="no_countertop",
            answered_by=principal.id,
            note=note,
            batch_id=batch_id,
        )
        session.add(answer)
        session.flush()
        emit(
            session,
            category=AuditCategory.REVIEW_ACTION,
            actor=principal.id,
            target_id=answer.id,
            target_type="no_countertop_answer",
        )
        recorded += 1
    if not recorded:
        session.rollback()
        response.status_code = status.HTTP_200_OK
        return _countertop_results_for_revision(session, package_id, revision)
    emit(
        session,
        category=AuditCategory.REVIEW_ACTION,
        actor=principal.id,
        target_id=batch_id,
        target_type=NO_COUNTERTOP_BATCH_TARGET,
    )
    session.commit()
    return _countertop_results_for_revision(session, package_id, revision)


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


__all__ = [
    "NO_COUNTERTOP_BATCH_TARGET",
    "_countertop_results_for_revision",
    "answer_no_countertop_page",
    "confirm_no_countertop_pages",
    "countertop_results",
    "router",
]
