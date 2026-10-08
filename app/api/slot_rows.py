"""Reviewer decisions for the slot reader's independent countertop rows.

These endpoints expose only the newest slot-reader rows. A saved wall choice or typed value is
anchored to that row candidate, never to the revision, and changing it appends a correction rather
than editing the previous decision.
"""

from __future__ import annotations

from datetime import datetime
from fractions import Fraction
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.dependencies import get_session
from app.api.drawing_views import _revision
from app.audit.events import AuditCategory, emit
from app.auth import Principal, require_action, require_project_access
from app.auth.roles import Action
from app.models import ObservationCandidate, SlotRowReviewDecision
from units.imperial import format_inches
from units.normalise import UnitNormalisationError, normalise_to_inches
from vocabulary.semantic_types import SemanticType
from workflow.countertop_runs import published_wall_layouts
from workflow.slot_row_scope import (
    SlotRow,
    candidate_is_sealed,
    candidate_value,
    latest_row_decision,
    slot_rows,
)

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
    held_reason: str | None
    values: list[SlotRowValueOut]
    wall_proposal: str | None
    wall_source: str | None
    wall_reason: str | None
    wall_layout_choices: list[str]
    decision_id: UUID | None
    confirmed_by: str | None
    decided_at: datetime | None
    wall_config: str | None


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


def _row_out(session: Session, row: SlotRow, layouts: tuple[str, ...]) -> SlotRowOut:
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
                    None if selected_candidate is None else selected_candidate.review_reason
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

    return SlotRowOut(
        row_id=row.anchor.id,
        page_number=row.page_number,
        label=row.label,
        piece_count=row.piece_count,
        held_reason=row.held_reason,
        values=values,
        wall_proposal=wall_proposal_value,
        wall_source=source,
        wall_reason=reason,
        wall_layout_choices=list(layouts),
        decision_id=None if decision is None else decision.id,
        confirmed_by=None if decision is None else decision.confirmed_by,
        decided_at=None if decision is None else decision.created_at,
        wall_config=None if decision is None else decision.wall_config,
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
    return SlotRowsOut(
        rows=[_row_out(session, row, layouts) for row in slot_rows(session, revision.id)]
    )


@router.post(
    "/projects/{project_id}/packages/{package_id}/slot-rows/{row_id}/review",
    response_model=SlotRowOut,
    status_code=status.HTTP_201_CREATED,
    summary="Confirm a wall choice or fill missing widths for one slot-reader row",
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
    if row.held_reason is not None:
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
    existing = {value.key for value in listed.values if not value.needs_value}
    if set(body.measurements) & existing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="A saved width can only be entered for a value this row still needs.",
        )
    allowed_missing = {value.key for value in listed.values if value.needs_value}
    if set(body.measurements) - allowed_missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="A width can only be entered for a missing value in this row.",
        )

    normalized: dict[str, object] = {}
    for key, text in body.measurements.items():
        try:
            measurement = normalise_to_inches(text)
        except (UnitNormalisationError, ValueError) as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Enter a valid width for {key.replace('_', ' ')}.",
            ) from error
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
    return _row_out(session, _current_row(session, revision.id, row_id), layouts)
