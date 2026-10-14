"""Each confirmed countertop's architect comparison, read by countertop (#1273, Milan by item).

One read endpoint: for every live countertop item of a revision, where its row stands with the
architect's drawing, as the countertop results show that row (the recorded result when there is
one), with each compared vendor piece named by its confirmed piece item. The results screen adds
those piece names to the one small line under each countertop's heading.

**Read-only, and it feeds no check.** It writes nothing, runs no check and asks no model; the
lookup it serves (`workflow/milan_items.py`) is imported by nothing else outside the tests.
"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_session
from app.auth import Action, Principal, require_action, require_project_access
from app.models import Package, PackageRevision
from app.schemas.visual_ui import CountertopItemRefOut
from workflow.milan_items import ItemArchitectStatus, architect_statuses_for_revision

router = APIRouter(tags=["countertop architect comparison"])


class ItemArchitectPairOut(BaseModel):
    """One width the check compared with the architect: the overall, or one vendor piece."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["piece", "overall"]
    label: str = Field(description='"overall" or "piece 2", counted from one as the row shows it.')
    vendor_piece: int | None = Field(
        description="The piece's place from the left, 1 first; null for the overall."
    )
    piece_item_id: UUID | None = Field(
        description=(
            "The piece a person confirmed for this place of the countertop's line; null for the "
            "overall, for a piece nobody confirmed, or when the line has no linked pieces."
        )
    )
    piece_item_type: str | None = Field(description="That piece's type; null when there is none.")


class ItemArchitectOut(BaseModel):
    """Where one confirmed countertop stands with the architect's drawing. Read-only."""

    model_config = ConfigDict(frozen=True)

    countertop: CountertopItemRefOut
    row_id: UUID | None = Field(
        description="Its countertop row (`row_id` on the results); null if none."
    )
    state: Literal["compared", "for_reviewer", "not_checked_yet", "not_compared"] = Field(
        description=(
            "As the countertop results show its row: a recorded PASS or FAIL (`compared`), a "
            "recorded result the check could not decide (`for_reviewer`), no result yet for "
            "something a check run would compare (`not_checked_yet`), or nothing comparable "
            "(`not_compared`)."
        )
    )
    reason: str | None = Field(
        description="The recorded result's reason; with no result, why nothing is compared."
    )
    finding_id: UUID | None = Field(description="Its row's architect result; null when none.")
    outcome: str | None = Field(description="That result's outcome; null when none.")
    pairs: tuple[ItemArchitectPairOut, ...] = Field(
        description="The pairs the recorded result compared, overall first; empty with no result."
    )
    pairing_source: str | None = Field(description="Who paired its row; null when nobody did.")
    pairing_judgments: str | None = Field(
        description='Whose judgments the pairing rests on, e.g. "code and both AIs".'
    )


class ItemArchitectListOut(BaseModel):
    """Every confirmed countertop of a revision with its architect comparison, in page order."""

    model_config = ConfigDict(frozen=True)

    package_id: UUID
    revision_id: UUID
    items: tuple[ItemArchitectOut, ...]


def _item_out(found: ItemArchitectStatus) -> ItemArchitectOut:
    countertop = found.countertop
    return ItemArchitectOut(
        countertop=CountertopItemRefOut(
            item_id=countertop.item_id,
            page_number=countertop.page_number,
            number=countertop.number,
            label=countertop.label,
        ),
        row_id=countertop.row_candidate_id,
        state=found.state,
        reason=found.reason,
        finding_id=found.finding_id,
        outcome=None if found.outcome is None else found.outcome.value,
        pairs=tuple(
            ItemArchitectPairOut(
                kind=pair.kind,
                label=pair.label,
                vendor_piece=None if pair.vendor_slot is None else pair.vendor_slot + 1,
                piece_item_id=pair.piece_item_id,
                piece_item_type=pair.piece_item_type,
            )
            for pair in found.pairs
        ),
        pairing_source=found.pairing_source,
        pairing_judgments=found.pairing_judgments,
    )


@router.get(
    "/projects/{project_id}/packages/{package_id}/countertop-items/architect",
    response_model=ItemArchitectListOut,
    summary="Read each confirmed countertop's comparison with the architect's drawing",
)
def countertop_items_architect(
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
) -> ItemArchitectListOut:
    """For each live countertop item, where its row stands with the architect's drawing, as the
    countertop results show that row: its recorded result when there is one, with each compared
    piece named by the piece item a person confirmed (#1273, `workflow/milan_items.py`).

    **Read-only, and it decides nothing**: no check runs, no result changes, and no check reads it.
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
    return ItemArchitectListOut(
        package_id=package_id,
        revision_id=revision.id,
        items=tuple(
            _item_out(found) for found in architect_statuses_for_revision(session, revision.id)
        ),
    )
