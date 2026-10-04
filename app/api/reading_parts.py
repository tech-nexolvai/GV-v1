"""Which reading is each part's width: suggested by the computer, decided by a person (#913).

Once a person has confirmed a vendor drawing's parts (#882) and its readings (#530), these endpoints
list each confirmed part with the confirmed reading the computer suggests is its width, the readings
a person may pick instead, and what a person decided; and they record a person's answer for one
part: confirm the suggestion, confirm another reading instead, or take the link back. **Only a
person's decision writes a link**, through `workflow/reading_parts.py`; listing never does, and there
is deliberately no endpoint that decides more than one part's link.

The rules about which readings may be linked, and to what, are `app/evidence/reading_parts.py`'s and
`workflow/reading_parts.py`'s.

Source: issue #913; #748 plan, step 6. Verification: tests/api/test_reading_parts.py.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from fractions import Fraction
from typing import Annotated, Final
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.dependencies import get_session
from app.api.drawing_views import NOT_FOUND_DETAIL, _revision
from app.auth import Principal, require_action, require_project_access
from app.auth.roles import Action
from app.db.session import is_unique_violation
from app.evidence.reading_parts import (
    NO_TOLERANCE,
    DrawingLinks,
    LinkRefusalReason,
    LinkRefused,
    ListedLink,
    ListedPart,
    ListedReading,
    confirm_listed_link,
    revision_links,
    withdraw_listed_link,
)
from app.models import ReadingPart
from units.imperial import format_inches
from vocabulary.part_kinds import PartKind

router = APIRouter(tags=["reading parts"])

#: What a person is told when someone else decided on the same reading at the same moment. The
#: database keeps one live link per reading, so the second to arrive is refused.
_DECIDED_AT_ONCE: Final = (
    "Someone else decided on this reading at the same moment. Reload the page to see their decision."
)


class LinkReadingOut(BaseModel):
    """A confirmed reading on the drawing, which a person may pick as a part's width."""

    reading_id: UUID
    value: str = Field(description="The reading's exact value, as `25 1/2 in`.")
    semantic_type: str = Field(description="What a person confirmed the reading is.")
    placed_by: str | None = Field(
        description=(
            "`line` when a reading behind it was attached to a dimension line, `region` when none "
            "was and the box it is printed in places it; null when neither places it."
        )
    )
    left: str | None = Field(description="Its span across the page, exact; null with no span.")
    right: str | None
    unplaced: str | None = Field(description="Why it spans no part, in plain English, or null.")
    linked_to: UUID | None = Field(
        description="The part it is linked to now, while that part still stands."
    )
    linked_to_number: int | None


class SuggestedLinkOut(BaseModel):
    """The reading the computer suggests is a part's width. Nothing is written until a person
    confirms it."""

    reading_id: UUID | None = Field(
        description="Null when no single reading spans this part alone; `said` says why."
    )
    said: str = Field(description="Why this reading, or why none, in plain English.")
    spanning: list[UUID] = Field(
        description="Every confirmed reading on the drawing whose line or region spans the part."
    )
    edge_tolerance: str


class LinkOut(BaseModel):
    """A reading a person linked to the part."""

    reading_id: UUID
    decided_by: str
    decided_at: datetime
    signal: str = Field(description="Why the reading belongs to the part, in plain English.")
    read: bool = Field(description="Whether a reader reads this link.")
    why_not_read: str | None


class PartLinkOut(BaseModel):
    """One confirmed part: the reading suggested as its width, and what a person decided."""

    item_id: UUID
    number: int | None = Field(
        description="Its number in 'Parts of each drawing', 1 for the leftmost; null if not listed."
    )
    kind: PartKind
    code: str | None
    left: str = Field(description="Its left end across the page, exact.")
    right: str
    suggestion: SuggestedLinkOut | None = Field(
        description="Null when no tolerance is stated, so nothing can be suggested."
    )
    links: list[LinkOut] = Field(
        description=(
            "The readings linked to this part now: one, or none. Two only when two people linked "
            "different readings at the same moment, and then neither is read."
        )
    )


class LinkDrawingOut(BaseModel):
    view_id: UUID
    page_index: int
    tag: str
    can_confirm: bool
    """Whether a reading may be linked here: only on a drawing confirmed as the vendor's."""
    why_not: str | None
    parts: list[PartLinkOut]
    readings: list[LinkReadingOut] = Field(
        description="The confirmed readings on the drawing, which a link may name, left to right."
    )


class ReadingPartsOut(BaseModel):
    can_suggest: bool
    why_not: str | None
    drawings: list[LinkDrawingOut]


class ConfirmLinkIn(BaseModel):
    reading_id: UUID = Field(
        description="The confirmed reading that is this part's width: the suggestion or another."
    )


def _tolerance(request: Request) -> Decimal | None:
    tolerance: Decimal | None = request.app.state.settings.run_edge_tolerance
    return tolerance


def _reading_out(listed: ListedReading, numbers: dict[UUID, int | None]) -> LinkReadingOut:
    observation, placed = listed.observation, listed.placed
    value = Fraction(observation.value_numerator, observation.value_denominator)
    return LinkReadingOut(
        reading_id=observation.id,
        value=f"{format_inches(value)} {observation.unit}",
        semantic_type=observation.semantic_type,
        placed_by=None if placed.geometry is None else placed.geometry.value,
        left=None if placed.left is None else str(placed.left),
        right=None if placed.right is None else str(placed.right),
        unplaced=placed.unplaced,
        linked_to=listed.linked_to,
        linked_to_number=None if listed.linked_to is None else numbers.get(listed.linked_to),
    )


def _link_out(listed: ListedLink) -> LinkOut:
    row = listed.row
    return LinkOut(
        reading_id=row.canonical_observation_id,
        decided_by=row.confirmed_by,
        decided_at=row.created_at,
        signal=row.signal,
        read=listed.read,
        why_not_read=listed.why_not_read,
    )


def _part_out(listed: ListedPart) -> PartLinkOut:
    suggestion = listed.suggestion
    return PartLinkOut(
        item_id=listed.part.item_id,
        number=listed.number,
        kind=listed.part.kind,
        code=listed.code,
        left=str(listed.part.left),
        right=str(listed.part.right),
        suggestion=(
            None
            if suggestion is None
            else SuggestedLinkOut(
                reading_id=(
                    None if suggestion.reading is None else suggestion.reading.observation_id
                ),
                said=suggestion.said,
                spanning=[reading.observation_id for reading in suggestion.spanning],
                edge_tolerance=str(suggestion.edge_tolerance),
            )
        ),
        links=[_link_out(link) for link in listed.links],
    )


def _drawing_out(drawing: DrawingLinks) -> LinkDrawingOut:
    numbers = {listed.part.item_id: listed.number for listed in drawing.parts}
    return LinkDrawingOut(
        view_id=drawing.view.id,
        page_index=drawing.page_index,
        tag=drawing.view.tag,
        can_confirm=drawing.refusal is None,
        why_not=None if drawing.refusal is None else drawing.refusal.detail,
        parts=[_part_out(listed) for listed in drawing.parts],
        readings=[_reading_out(listed, numbers) for listed in drawing.readings],
    )


def _listed(
    session: Session, revision_id: UUID, item_id: UUID, tolerance: Decimal | None
) -> PartLinkOut:
    """The part as the list now shows it, read back after a decision was committed."""
    for drawing in revision_links(session, revision_id, edge_tolerance=tolerance):
        for listed in drawing.parts:
            if listed.part.item_id == item_id:
                return _part_out(listed)
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)


def _refuse(refused: LinkRefused) -> HTTPException:
    if refused.reason is LinkRefusalReason.NO_SUCH_PART:
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)
    if refused.reason is LinkRefusalReason.DRAWING_NOT_VENDORS:
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=refused.detail)
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=refused.detail)


def _decide(
    session: Session,
    decide: Callable[[], ReadingPart | tuple[ReadingPart, ...] | LinkRefused],
) -> None:
    """Run one decision and commit it; refuse one that was not recorded. A second decision on the
    same reading at the same moment is a conflict: the database keeps one live link per reading,
    and the loser is told so."""
    try:
        outcome = decide()
    except IntegrityError as error:
        session.rollback()
        if is_unique_violation(error, "supersedes_id", "first_link"):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=_DECIDED_AT_ONCE
            ) from error
        raise
    if isinstance(outcome, LinkRefused):
        session.rollback()
        raise _refuse(outcome)
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise


@router.get(
    "/projects/{project_id}/packages/{package_id}/reading-parts",
    response_model=ReadingPartsOut,
    summary="Each confirmed part, the reading suggested as its width, and what a person decided",
)
def list_reading_parts(
    _access: Annotated[Principal, Depends(require_project_access)],
    session: Annotated[Session, Depends(get_session)],
    request: Request,
    project_id: UUID,
    package_id: UUID,
) -> ReadingPartsOut:
    """Read only: a suggestion is worked out on each call and never stored."""
    revision = _revision(session, project_id, package_id)
    tolerance = _tolerance(request)
    return ReadingPartsOut(
        can_suggest=tolerance is not None,
        why_not=None if tolerance is not None else NO_TOLERANCE,
        drawings=[
            _drawing_out(drawing)
            for drawing in revision_links(session, revision.id, edge_tolerance=tolerance)
        ],
    )


@router.post(
    "/projects/{project_id}/packages/{package_id}/reading-parts/{item_id}/confirm",
    response_model=PartLinkOut,
    status_code=status.HTTP_201_CREATED,
    summary="Say which confirmed reading is one part's width",
)
def confirm_reading_part_endpoint(
    principal: Annotated[Principal, Depends(require_project_access)],
    _action: Annotated[Principal, Depends(require_action(Action.CONFIRM_EVIDENCE))],
    session: Annotated[Session, Depends(get_session)],
    request: Request,
    project_id: UUID,
    package_id: UUID,
    item_id: UUID,
    body: ConfirmLinkIn,
) -> PartLinkOut:
    """Record a person's link, as suggested or corrected. Audited, and correctable by confirming
    another reading: the latest decision is the one that counts, and every earlier one is kept."""
    revision = _revision(session, project_id, package_id)
    tolerance = _tolerance(request)
    _decide(
        session,
        lambda: confirm_listed_link(
            session,
            package_revision_id=revision.id,
            item_id=item_id,
            observation_id=body.reading_id,
            edge_tolerance=tolerance,
            actor=principal.id,
        ),
    )
    return _listed(session, revision.id, item_id, tolerance)


@router.post(
    "/projects/{project_id}/packages/{package_id}/reading-parts/{item_id}/withdraw",
    response_model=PartLinkOut,
    status_code=status.HTTP_201_CREATED,
    summary="Take back the link between one part and its reading",
)
def withdraw_reading_part_endpoint(
    principal: Annotated[Principal, Depends(require_project_access)],
    _action: Annotated[Principal, Depends(require_action(Action.CONFIRM_EVIDENCE))],
    session: Annotated[Session, Depends(get_session)],
    request: Request,
    project_id: UUID,
    package_id: UUID,
    item_id: UUID,
) -> PartLinkOut:
    """Record that the reading is not the part's width. Links nothing; the link stops being read."""
    revision = _revision(session, project_id, package_id)
    _decide(
        session,
        lambda: withdraw_listed_link(
            session, package_revision_id=revision.id, item_id=item_id, actor=principal.id
        ),
    )
    return _listed(session, revision.id, item_id, _tolerance(request))
