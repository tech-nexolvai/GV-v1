"""Reviewer decisions for the slot reader's independent countertop rows.

These endpoints expose only the newest slot-reader rows. A saved wall choice or typed value is
anchored to that row candidate, never to the revision, and changing it appends a correction rather
than editing the previous decision.

The architect pairing (#1053) is anchored the same way: `GET .../architect-pairing` shows the row's
current pairing and every architect dimension on its page; `POST` records a reviewer's pairing (or
"nothing on the architect's drawing is comparable") as a new record superseding the latest. A
centre-line or held architect dimension is refused, whoever asks.
"""

from __future__ import annotations

from datetime import datetime
from fractions import Fraction
from typing import Annotated, Final, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.dependencies import get_session
from app.api.drawing_views import _revision
from app.api.signed_off import open_revision
from app.audit.events import AuditCategory, emit
from app.auth import Principal, require_action, require_project_access
from app.auth.roles import Action
from app.lifecycle.carry_forward import wall_answer_origins
from app.models import ObservationCandidate, SlotRowReviewDecision
from app.models.verdicts import Finding
from app.review.row_location import RowLocation, architect_locations, row_location
from app.review.sizes import NOT_ABOVE_ZERO, typed_negative
from app.review.wall_cases import (
    current_field_cut,
    field_cut_changed,
    field_cut_text,
    latest_width_findings,
    unavailable_reason,
    wall_cases_out,
)
from app.schemas.visual_ui import WallCasesOut
from units.imperial import format_inches
from units.normalise import UnitNormalisationError, normalise_to_inches
from verdict.outcomes import Outcome
from vocabulary.reviewer_reasons import reviewer_reason
from vocabulary.semantic_types import SemanticType
from vocabulary.wall_layouts import (
    AUTOMATIC_WALL_SOURCE,
    BETWEEN_PANELS_WORDS,
    is_between_panels,
)
from workflow.architect_pairing_records import (
    DecidedPair,
    ReviewerPairingRefused,
    ReviewerPairingStale,
    architect_spans_for_row,
    latest_architect_pairing,
    latest_record,
    record_reviewer_pairing,
)
from workflow.countertop_runs import published_wall_layouts
from workflow.layout_proposals import WALL_INK_FLAG, WALL_WORDS_FLAG
from workflow.slot_row_scope import (
    SlotRow,
    candidate_is_sealed,
    candidate_value,
    latest_row_decision,
    slot_rows,
)
from workflow.wall_cases import is_every_wall_case

router = APIRouter(tags=["slot-reader rows"])


class SlotRowValueOut(BaseModel):
    key: str
    label: str
    position: int | None
    value: str | None
    suggestion: str | None
    source: str
    review_reason: str | None
    needs_value: bool


class SlotRowOut(BaseModel):
    row_id: UUID
    page_number: int
    label: str
    piece_count: int
    row_location: RowLocation | None = None
    held_reason: str | None
    wall_confirmation_allowed: bool
    values: list[SlotRowValueOut]
    wall_proposal: str | None
    wall_source: str | None
    wall_reason: str | None
    wall_layout_choices: list[str]
    decision_id: UUID | None
    confirmed_by: str | None
    decided_at: datetime | None
    wall_config: str | None
    carried_from_revision: int | None = Field(
        default=None,
        description=(
            "When this row's wall answer was carried from an identical row of an earlier revision "
            "(#1180): the revision it was given on. `confirmed_by` is then who gave it."
        ),
    )
    carried_given_at: datetime | None = Field(
        default=None, description="When the carried answer was originally given."
    )
    wall_cases: WallCasesOut = Field(
        default_factory=WallCasesOut,
        description=(
            "Each wall choice's result (#1202): the overall it needs and PASS or FAIL against the "
            "vendor's printed overall, as the width check recorded them for every wall case. "
            "Empty with `unavailable_reason` when there is no such result. Nothing is chosen."
        ),
    )
    walls_needed: bool = Field(
        default=True,
        description=(
            "False when the row's recorded width result is wrong (or right) for every wall case "
            "(#1202): the walls do not change it, so no wall answer is asked for."
        ),
    )
    field_cut_in_force: str | None = Field(
        default=None,
        description=(
            'The field cut per wall end a check run would use now, e.g. `1"` (#1202): the '
            "run's, else the project's, else GV's default, else the rulebook's. Each choice's "
            "result is hidden when it differs from the one the recorded result used."
        ),
    )
    wall_evidence: list[str] = Field(
        default_factory=list,
        description=(
            "What code saw at the row's ends, in plain words (#1202): wall ink (a hatch running up "
            "the end, a solid band, a tall wall line) and the vendor's words there, which are "
            "evidence only and never decide the walls."
        ),
    )


class SlotRowsOut(BaseModel):
    rows: list[SlotRowOut]


class SlotRowReviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    wall_config: str | None = Field(default=None)
    measurements: dict[str, str] = Field(default_factory=dict, max_length=64)

    @field_validator("measurements")
    @classmethod
    def _only_row_width_fields(cls, values: dict[str, str]) -> dict[str, str]:
        allowed = {"countertop_width", *(f"piece_widths:{index}" for index in range(64))}
        if set(values) - allowed:
            raise ValueError("enter only a missing width for this countertop row")
        return values


def _wall_evidence(flags: set[str]) -> list[str]:
    """Code's wall ink and the vendor's words at the row's ends, as plain lines (#1202)."""
    lines: list[str] = []
    ink = next(
        (flag.removeprefix(WALL_INK_FLAG) for flag in flags if flag.startswith(WALL_INK_FLAG)), ""
    )
    ends = [side for side in ("left", "right") if f"{side}=True" in ink]
    if ends:
        lines.append(
            "Code saw wall ink at both ends."
            if len(ends) == 2
            else f"Code saw wall ink at the {ends[0]} end only."
        )
    words = next(
        (flag.removeprefix(WALL_WORDS_FLAG) for flag in flags if flag.startswith(WALL_WORDS_FLAG)),
        "",
    )
    worded = [side for side in ("left", "right") if f"{side}=True" in words]
    if worded:
        where = "both ends" if len(worded) == 2 else f"the {worded[0]} end"
        lines.append(
            f"The vendor's words at {where} (filler, field cut or wall) are evidence only; "
            "they never decide the walls."
        )
    return lines


def _row_out(
    session: Session,
    row: SlotRow,
    layouts: tuple[str, ...],
    finding: Finding | None = None,
    field_cut: Fraction | None = None,
) -> SlotRowOut:
    proposal_by_candidate = row.proposals
    candidate_by_position: dict[int | None, ObservationCandidate] = {}
    for candidate in row.candidates:
        flags = candidate.ambiguity_flags or []
        slot = next(
            (flag.removeprefix("slot:") for flag in flags if flag.startswith("slot:")), None
        )
        position = None if slot == "overall" else int(slot) if slot and slot.isdigit() else -1
        if position == -1:
            continue
        proposal = proposal_by_candidate.get(candidate.id)
        if proposal is None or proposal.field_key not in {
            "SHOP:countertop_overall_width",
            "SHOP:countertop_piece_width",
            "SHOP:cabinet_width",
            "SHOP:filler_width",
        }:
            continue
        candidate_by_position[position] = candidate

    decision = row.decision
    saved = {} if decision is None else decision.measurements
    values: list[SlotRowValueOut] = []
    for position, key, label in (
        (None, "countertop_width", "Overall countertop width"),
        *(
            (index, f"piece_widths:{index}", f"Piece {index + 1} width")
            for index in range(row.piece_count)
        ),
    ):
        selected_candidate: ObservationCandidate | None = candidate_by_position.get(position)
        saved_value = saved.get(key)
        proposed = candidate_value(selected_candidate) if selected_candidate is not None else None
        saved_exact = None
        if isinstance(saved_value, dict):
            try:
                if saved_value.get("unit") == "in":
                    saved_exact = Fraction(
                        int(saved_value["numerator"]), int(saved_value["denominator"])
                    )
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                saved_exact = None
        sealed = (
            selected_candidate is not None
            and candidate_is_sealed(selected_candidate)
            and proposed is not None
        )
        exact = saved_exact if saved_exact is not None else proposed if sealed else None
        values.append(
            SlotRowValueOut(
                key=key,
                label=label,
                position=position,
                value=None if exact is None else f"{format_inches(exact)} in",
                suggestion=(
                    f"{format_inches(proposed)} in"
                    if proposed is not None and not sealed and saved_exact is None
                    else None
                ),
                source="reviewer" if saved_exact is not None else "slot reader" if sealed else "",
                review_reason=(
                    None
                    if selected_candidate is None
                    else reviewer_reason(
                        selected_candidate.ambiguity_flags or (), selected_candidate.review_reason
                    )
                ),
                needs_value=exact is None,
            )
        )

    wall_candidate = row.wall_candidate
    wall_flags = set(() if wall_candidate is None else wall_candidate.ambiguity_flags or ())
    sealed_flag = next((flag for flag in wall_flags if flag.startswith("walls-sealed:")), None)
    source_flag = next((flag for flag in wall_flags if flag.startswith("wall-source:")), None)
    held_flag = next((flag for flag in wall_flags if flag.startswith("walls-held:")), None)
    wall_proposal_value = None if sealed_flag is None else sealed_flag.removeprefix("walls-sealed:")
    source = None if source_flag is None else source_flag.removeprefix("wall-source:")
    reason = None if wall_candidate is None else wall_candidate.review_reason
    if held_flag is not None:
        wall_proposal_value = None
        reason = reason or "The readers did not agree on this row's wall layout."

    if row.wall_confirmation_allowed and any(
        is_between_panels(candidate.ambiguity_flags or ()) for candidate in row.candidates
    ):
        # `back_only` is how the rule says "no field cut"; for this row it says nothing about walls.
        wall_proposal_value = "back_only"
        source = "between-panels"
        reason = (
            "The stone sits between side panels, so the panels take the field cut — "
            f"confirm '{BETWEEN_PANELS_WORDS}'."
        )

    if (
        source == AUTOMATIC_WALL_SOURCE
        and wall_proposal_value == "back_left_right"
        and held_flag is None
    ):
        reason = reason or "Seen on the drawing at both ends and confirmed by both AIs."

    between_panels = any(
        is_between_panels(candidate.ambiguity_flags or ()) for candidate in row.candidates
    )
    saved_after = (
        finding is not None
        and decision is not None
        and decision.created_at is not None
        and finding.created_at is not None
        and decision.created_at > finding.created_at
    )
    # The project's field cut changed since this result: its numbers are out of date (#1202).
    stale = field_cut_changed(finding, field_cut)
    why_not = unavailable_reason(finding, saved_after=saved_after, field_cut_changed=stale)
    walls_needed = not (
        finding is not None
        and not saved_after
        and not stale
        and is_every_wall_case(finding.trace)
        and finding.outcome in (Outcome.PASS.value, Outcome.FAIL.value)
    )
    origin = wall_answer_origins(session, (decision,)).get(decision.id) if decision else None
    return SlotRowOut(
        row_id=row.anchor.id,
        page_number=row.page_number,
        label=row.label,
        row_location=row_location(session, row.anchor.id),
        piece_count=row.piece_count,
        held_reason=row.held_reason,
        wall_confirmation_allowed=row.wall_confirmation_allowed,
        values=values,
        wall_proposal=wall_proposal_value,
        wall_source=source,
        wall_reason=reason,
        wall_layout_choices=list(layouts),
        decision_id=None if decision is None else decision.id,
        confirmed_by=None if decision is None else decision.confirmed_by,
        decided_at=None if decision is None else decision.created_at,
        carried_from_revision=None if origin is None else origin.revision_number,
        carried_given_at=None if origin is None else origin.given_at,
        wall_config=None if decision is None else decision.wall_config,
        wall_cases=wall_cases_out(
            None if finding is None else finding.trace,
            between_panels=between_panels,
            unavailable_reason=why_not,
        ),
        walls_needed=walls_needed,
        field_cut_in_force=field_cut_text(field_cut),
        wall_evidence=_wall_evidence(wall_flags),
    )


def _current_row(session: Session, revision_id: UUID, row_id: UUID) -> SlotRow:
    row = next(
        (
            candidate
            for candidate in slot_rows(session, revision_id)
            if candidate.anchor.id == row_id
        ),
        None,
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return row


@router.get(
    "/projects/{project_id}/packages/{package_id}/slot-rows",
    response_model=SlotRowsOut,
    summary="List each current slot-reader countertop row and its reviewer-only gaps",
)
def list_slot_rows(
    _access: Annotated[Principal, Depends(require_project_access)],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    package_id: UUID,
) -> SlotRowsOut:
    revision = _revision(session, project_id, package_id)
    layouts = published_wall_layouts(session)
    rows = slot_rows(session, revision.id)
    findings = latest_width_findings(session, revision.id, [row.anchor.id for row in rows])
    field_cut = current_field_cut(session, revision.id, project_id=project_id) if rows else None
    return SlotRowsOut(
        rows=[
            _row_out(session, row, layouts, findings.get(row.anchor.id), field_cut) for row in rows
        ]
    )


@router.post(
    "/projects/{project_id}/packages/{package_id}/slot-rows/{row_id}/review",
    response_model=SlotRowOut,
    status_code=status.HTTP_201_CREATED,
    summary="Confirm a wall choice or fill missing widths for one slot-reader row",
    dependencies=[Depends(open_revision)],
)
def review_slot_row(
    principal: Annotated[Principal, Depends(require_project_access)],
    _action: Annotated[Principal, Depends(require_action(Action.CONFIRM_EVIDENCE))],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    package_id: UUID,
    row_id: UUID,
    body: SlotRowReviewIn,
) -> SlotRowOut:
    revision = _revision(session, project_id, package_id)
    row = _current_row(session, revision.id, row_id)
    if row.held_reason is not None and not (
        row.wall_confirmation_allowed and body.wall_config is not None and not body.measurements
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This row cannot be checked until the hold is resolved: {row.held_reason}",
        )
    layouts = published_wall_layouts(session)
    if body.wall_config is not None and body.wall_config not in layouts:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Choose one of the published wall layouts for this countertop row.",
        )
    current = latest_row_decision(session, row_id)
    listed = _row_out(session, row, layouts)
    locked = {
        value.key for value in listed.values if not value.needs_value and value.source != "reviewer"
    }
    if set(body.measurements) & locked:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="A reader-sealed width cannot be replaced here; correct only a reviewer-entered width.",
        )
    allowed_editable = {
        value.key for value in listed.values if value.needs_value or value.source == "reviewer"
    }
    if set(body.measurements) - allowed_editable:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="A width can only be entered for a missing value or corrected reviewer value in this row.",
        )

    normalized: dict[str, object] = {}
    for key, text in body.measurements.items():
        if typed_negative(text):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"{key.replace('_', ' ').capitalize()}: {NOT_ABOVE_ZERO}",
            )
        try:
            measurement = normalise_to_inches(text)
        except (UnitNormalisationError, ValueError) as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Enter a valid width for {key.replace('_', ' ')}.",
            ) from error
        if measurement.exact <= 0:
            # Every value on a row is a width; zero or less is a mistake that could pass (#1182).
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"{key.replace('_', ' ').capitalize()}: {NOT_ABOVE_ZERO}",
            )
        normalized[key] = {
            "numerator": measurement.exact.numerator,
            "denominator": measurement.exact.denominator,
            "unit": "in",
            "display": text,
        }

    if (
        body.wall_config is None
        and SemanticType.WALL_CONFIG.value not in body.model_fields_set
        and current is not None
    ):
        wall_config = current.wall_config
    else:
        wall_config = body.wall_config
    if not body.measurements and SemanticType.WALL_CONFIG.value not in body.model_fields_set:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Choose this row's wall layout or enter a missing width before saving.",
        )

    decision = SlotRowReviewDecision(
        id=uuid4(),
        row_candidate_id=row_id,
        supersedes_id=None if current is None else current.id,
        wall_config=wall_config,
        measurements=normalized if current is None else {**current.measurements, **normalized},
        confirmed_by=principal.id,
    )
    session.add(decision)
    emit(
        session,
        category=AuditCategory.REVIEW_ACTION,
        actor=principal.id,
        target_id=decision.id,
        target_type="slot_row_review_decision",
    )
    try:
        session.commit()
    except IntegrityError as error:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This row was just updated. Reload it before confirming again.",
        ) from error
    findings = latest_width_findings(session, revision.id, [row_id])
    return _row_out(
        session,
        _current_row(session, revision.id, row_id),
        layouts,
        findings.get(row_id),
        current_field_cut(session, revision.id, project_id=project_id),
    )


# --- the architect pairing (#1053) ----------------------------------------------------------------


class ArchitectPairIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["piece", "overall"]
    architect_candidate_id: UUID
    vendor_slot_indices: list[int] = Field(default_factory=list, max_length=64)
    """The row's piece positions (0 = the first piece), next to each other; empty for the overall."""


class ArchitectPairingIn(BaseModel):
    """A reviewer's pairing for one row. `pairs: []` states that nothing on the architect's drawing
    is comparable with this row."""

    model_config = ConfigDict(extra="forbid")

    pairs: list[ArchitectPairIn] = Field(max_length=64)
    note: str | None = Field(default=None, max_length=500)
    expected_record_id: UUID | None = None
    """The `current.record_id` the reviewer was shown (#1088). When given and the row's pairing has
    moved on since, the request is refused with 409 and nothing is recorded. Optional, so a client
    that does not send it keeps today's behaviour."""


#: Independent automatic judgments behind each pairing source (#1053): code by drawn position, and
#: both AIs by what each dimension measures.
_JUDGMENTS: Final = {"code+ais": 2, "code": 1, "both-ais": 1}


class ArchitectPairOut(BaseModel):
    kind: str
    architect_candidate_id: UUID
    vendor_slot_indices: list[int]


class ArchitectPairingRecordOut(BaseModel):
    record_id: UUID
    source: str
    status: str
    pairs: list[ArchitectPairOut]
    reasons: list[str]
    note: str | None
    decided_by: str | None
    decided_at: datetime
    supersedes_id: UUID | None


class ArchitectPairingEffectiveOut(BaseModel):
    """The pairing that counts, re-checked against the architect's dimensions as they stand."""

    record_id: UUID
    source: str
    status: str
    pairs: list[ArchitectPairOut]
    reasons: list[str]
    judgments: int
    """How many independent automatic judgments the pairing rests on: 2 for `code+ais` (code's
    drawn position and both AIs agree), 1 for `code` or `both-ais`, 0 for a reviewer's pairing or
    none."""
    needs_confirmation: bool
    """One judgment only (`code` or `both-ais`) with pairs: any result resting on it, PASS or FAIL,
    waits for a reviewer's confirmation of the pairing. `code+ais` and `reviewer` stand."""


class ArchitectSpanOut(BaseModel):
    candidate_id: UUID
    printed: str
    inches: str | None
    on_outline: bool | None
    held_reason: str | None
    row: int | None
    slot: int | None
    can_pair: bool
    refusal: str | None
    location: RowLocation | None = Field(
        default=None,
        description=(
            "Where this dimension is on the drawing: its line from tick to tick with its printed "
            "label, in the same stored space as `row_location` (#1066). Null when the position the "
            "architect reader stored cannot be used exactly."
        ),
    )


class ArchitectPairingOut(BaseModel):
    row_id: UUID
    piece_count: int
    current: ArchitectPairingRecordOut | None
    effective: ArchitectPairingEffectiveOut | None
    spans: list[ArchitectSpanOut]


def _pairs_out(pairs: object) -> list[ArchitectPairOut]:
    found: list[ArchitectPairOut] = []
    for raw in pairs if isinstance(pairs, list) else []:
        if isinstance(raw, dict):
            found.append(
                ArchitectPairOut(
                    kind=str(raw.get("kind")),
                    architect_candidate_id=UUID(str(raw.get("architect_candidate_id"))),
                    vendor_slot_indices=[int(i) for i in raw.get("vendor_slot_indices") or []],
                )
            )
    return found


def _architect_pairing_out(session: Session, row: SlotRow) -> ArchitectPairingOut:
    current = latest_record(session, row.anchor.id)
    effective = latest_architect_pairing(session, row.anchor.id)
    spans = architect_spans_for_row(session, row.anchor, current)
    locations = architect_locations(session, [span.candidate.id for span in spans])
    reasons = [] if current is None else current.details.get("reasons")
    note = None if current is None else current.details.get("note")
    return ArchitectPairingOut(
        row_id=row.anchor.id,
        piece_count=row.piece_count,
        current=(
            None
            if current is None
            else ArchitectPairingRecordOut(
                record_id=current.id,
                source=current.source,
                status=current.status,
                pairs=_pairs_out(current.pairs),
                reasons=[str(reason) for reason in reasons] if isinstance(reasons, list) else [],
                note=note if isinstance(note, str) else None,
                decided_by=current.decided_by,
                decided_at=current.created_at,
                supersedes_id=current.supersedes_id,
            )
        ),
        effective=(
            None
            if effective is None
            else ArchitectPairingEffectiveOut(
                record_id=effective.record_id,
                source=effective.source,
                status=effective.status,
                pairs=[
                    ArchitectPairOut(
                        kind=pair.kind,
                        architect_candidate_id=pair.architect_candidate_id,
                        vendor_slot_indices=list(pair.vendor_slot_indices),
                    )
                    for pair in effective.pairs
                ],
                reasons=list(effective.reasons),
                judgments=_JUDGMENTS.get(effective.source, 0),
                needs_confirmation=effective.source in {"code", "both-ais"}
                and bool(effective.pairs),
            )
        ),
        spans=[
            ArchitectSpanOut(
                candidate_id=span.candidate.id,
                printed=span.candidate.raw_text,
                inches=None if span.inches is None else f"{format_inches(span.inches)} in",
                on_outline=span.on_outline,
                held_reason=span.held_reason,
                row=span.row,
                slot=span.slot,
                can_pair=span.comparable,
                refusal=None if span.comparable else span.refusal(),
                location=locations.get(span.candidate.id),
            )
            for span in spans
        ],
    )


@router.get(
    "/projects/{project_id}/packages/{package_id}/slot-rows/{row_id}/architect-pairing",
    response_model=ArchitectPairingOut,
    summary="Show which architect dimension pairs with which vendor piece on one countertop row",
)
def get_architect_pairing(
    _access: Annotated[Principal, Depends(require_project_access)],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    package_id: UUID,
    row_id: UUID,
) -> ArchitectPairingOut:
    revision = _revision(session, project_id, package_id)
    return _architect_pairing_out(session, _current_row(session, revision.id, row_id))


@router.post(
    "/projects/{project_id}/packages/{package_id}/slot-rows/{row_id}/architect-pairing",
    response_model=ArchitectPairingOut,
    status_code=status.HTTP_201_CREATED,
    summary="Pair the architect's dimensions with one countertop row (or say none is comparable)",
    dependencies=[Depends(open_revision)],
)
def pair_architect_dimensions(
    principal: Annotated[Principal, Depends(require_project_access)],
    _action: Annotated[Principal, Depends(require_action(Action.CONFIRM_EVIDENCE))],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    package_id: UUID,
    row_id: UUID,
    body: ArchitectPairingIn,
) -> ArchitectPairingOut:
    revision = _revision(session, project_id, package_id)
    row = _current_row(session, revision.id, row_id)
    try:
        record = record_reviewer_pairing(
            session,
            anchor=row.anchor,
            package_revision_id=revision.id,
            piece_count=row.piece_count,
            pairs=[
                DecidedPair(
                    kind=pair.kind,
                    architect_candidate_id=pair.architect_candidate_id,
                    vendor_slot_indices=tuple(pair.vendor_slot_indices),
                )
                for pair in body.pairs
            ],
            note=body.note,
            actor=principal.id,
            expected_record_id=body.expected_record_id,
        )
    except ReviewerPairingStale as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except ReviewerPairingRefused as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)
        ) from error
    try:
        # Flushed first: two reviewers superseding the same record collide on its unique index here.
        session.flush()
        emit(
            session,
            category=AuditCategory.REVIEW_ACTION,
            actor=principal.id,
            target_id=record.id,
            target_type="architect_pairing_record",
        )
        session.commit()
    except IntegrityError as error:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This row's pairing was just updated. Reload it before pairing again.",
        ) from error
    return _architect_pairing_out(session, _current_row(session, revision.id, row_id))
