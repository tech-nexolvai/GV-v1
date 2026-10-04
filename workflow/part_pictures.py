"""A picture of every suggested part, for a person to look at (#897).

The admin's decision of 2026-10-04: every suggested part gets its own picture on the Measure page,
so a person can decide what it is without opening the PDF. The worker that suggests the parts
(#868) cuts one per suggestion, from the vendor's page, covering the part's outline and a stated
margin; a part a person adds (#882) is cut by the same worker once the person has added it. Each is
recorded in `part_pictures` (`PartPicture`), which holds a pointer and a digest, never the image.

**For a person's eyes only.** Nothing reads a value from a picture, and nothing here writes a part,
a decision or a run: `record_part_picture` writes `part_pictures` and nothing else, and it is the
only code that does (`tests/db/test_drawing_models.py`).

**Whether a picture shows GV's own coloured marks (#921)** is asked when it is cut, by the agreement
gate's own test (`workflow/stages.py:crop_shows_a_gv_mark`, #901), and recorded with it: GV's marks
baked into the vendor's drawing cannot be left out of the render, so the Measure page warns under a
picture that shows them. A picture cut before the check existed, or whose page's coloured markup
could not be read, is "not checked" (`GvMarks`), never taken to be clean.

**Two settings, and neither has a default.** How far past the outline a picture reaches and the
resolution it is cut at are a deployment's to state (`PartPictureSettings`); without them no
picture is cut, and the page says none is stored.

**No extraction imports**, as in `workflow/parts.py`: the API names `CUT_PART_PICTURES_WORKFLOW`
when a person adds a part, and `tests/api/test_no_heavy_work.py` keeps `app/api/` away from
anything that reads a PDF. The cutting itself, which renders the page, is
`workflow/stages.py:DatabaseStages`'s.

Source: issues #897 and #921 and the admin's decisions on them. Verification:
tests/workflow/test_part_pictures.py, tests/db/test_drawing_models.py,
tests/api/test_drawing_parts.py.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.models import DrawingView, Page, PartPicture, PartProposal

__all__ = [
    "CUT_PART_PICTURES_WORKFLOW",
    "PNG",
    "GvMarks",
    "PartPictureSettings",
    "gv_marks",
    "pages_without_pictures",
    "part_picture",
    "pictured",
    "record_part_picture",
    "unpictured_proposals",
]

#: The work a person adding a part asks for: cut the pictures still missing on the revision. Named,
#: not free text, so the API and the worker that consumes it cannot drift apart.
CUT_PART_PICTURES_WORKFLOW: Final = "cut_part_pictures"

#: The one format a picture is stored in, as `evidence/crop.py` encodes it.
PNG: Final = "image/png"


class GvMarks(StrEnum):
    """What a part's picture was found to show of GV's own coloured marks (#921), as the Measure
    page is told it. Read from `PartPicture.shows_gv_marks` by `gv_marks`."""

    #: Markup drawn in colour lies in the picture, wholly or in part: the page warns under it.
    SHOWN = "shown"
    #: The picture was checked and no markup drawn in colour lies in it.
    NOT_SHOWN = "not_shown"
    #: Nobody knows: the picture was cut before the check existed, or its page's coloured markup
    #: could not be read. Never taken to mean the picture is clean.
    NOT_CHECKED = "not_checked"


def gv_marks(shows_gv_marks: bool | None) -> GvMarks:
    """What a recorded answer says, in the Measure page's words: `None` is not checked."""
    if shows_gv_marks is None:
        return GvMarks.NOT_CHECKED
    return GvMarks.SHOWN if shows_gv_marks else GvMarks.NOT_SHOWN


@dataclass(frozen=True, slots=True)
class PartPictureSettings:
    """How a part's picture is cut. **No defaults**: both are stated by the deployment.

    `margin_pt` is how far past the part's outline the picture reaches on every side, in PDF
    points; `dpi` is the resolution the vendor's page is rendered at to cut it. Neither decides
    anything: a picture is for a person to look at. They are still stated rather than chosen here,
    because a number nobody stated is a number nobody can find.
    """

    margin_pt: Decimal
    dpi: int

    def __post_init__(self) -> None:
        if isinstance(self.margin_pt, float) or not isinstance(self.margin_pt, Decimal):
            raise TypeError("margin_pt must be a Decimal, never a float")
        if not self.margin_pt.is_finite() or self.margin_pt <= 0:
            raise ValueError("margin_pt must be a finite number of points greater than zero")
        if isinstance(self.dpi, bool) or not isinstance(self.dpi, int):
            raise TypeError("dpi must be an integer")
        if self.dpi <= 0:
            raise ValueError("dpi must be greater than zero")


def record_part_picture(
    session: Session,
    *,
    proposal: PartProposal,
    storage_key: str,
    sha256: str,
    settings: PartPictureSettings,
    shows_gv_marks: bool | None,
) -> PartPicture:
    """Record where one suggestion's picture is stored. **The only code that writes
    `part_pictures`**, and it writes nothing else.

    `shows_gv_marks` is what the check for GV's coloured marks said of the picture's pixels (#921),
    or `None` where it could not be asked. **Required**, so no caller records a picture without
    saying whether it was checked.

    A suggestion that already has a picture keeps it, and that picture is returned: the first one
    cut stands, so a redelivered job adds no second row.
    """
    if shows_gv_marks is not None and not isinstance(shows_gv_marks, bool):
        raise TypeError("shows_gv_marks must be True, False or None")
    existing = part_picture(session, proposal.id)
    if existing is not None:
        return existing
    picture = PartPicture(
        part_proposal_id=proposal.id,
        storage_key=storage_key,
        sha256=sha256,
        media_type=PNG,
        margin_pt=settings.margin_pt,
        dpi=settings.dpi,
        shows_gv_marks=shows_gv_marks,
    )
    session.add(picture)
    session.flush()
    return picture


def part_picture(session: Session, proposal_id: UUID) -> PartPicture | None:
    """The picture recorded for one suggestion, or `None` while none is."""
    return session.execute(
        select(PartPicture).where(PartPicture.part_proposal_id == proposal_id)
    ).scalar_one_or_none()


def pictured(session: Session, proposal_ids: Sequence[UUID]) -> dict[UUID, GvMarks]:
    """Which of these suggestions have a picture recorded, each with what the check for GV's
    coloured marks said of it (#921). A suggestion with no picture is absent."""
    if not proposal_ids:
        return {}
    return {
        proposal_id: gv_marks(shows)
        for proposal_id, shows in session.execute(
            select(PartPicture.part_proposal_id, PartPicture.shows_gv_marks).where(
                PartPicture.part_proposal_id.in_(list(proposal_ids))
            )
        )
    }


def unpictured_proposals(session: Session, page_id: UUID) -> list[PartProposal]:
    """Every suggestion on the page's drawings with no picture yet, in the order they were filed.

    A person's own additions included: a part added by its two ends is a suggestion too (#882).
    """
    return list(
        session.scalars(
            select(PartProposal)
            .join(DrawingView, DrawingView.id == PartProposal.drawing_view_id)
            .where(
                DrawingView.page_id == page_id,
                ~exists().where(PartPicture.part_proposal_id == PartProposal.id),
            )
            .order_by(PartProposal.created_at, PartProposal.id)
        )
    )


def pages_without_pictures(session: Session, document_version_ids: Sequence[UUID]) -> list[Page]:
    """The pages of these documents with a suggestion that has no picture yet, in page order."""
    if not document_version_ids:
        return []
    return list(
        session.scalars(
            select(Page)
            .where(
                Page.document_version_id.in_(list(document_version_ids)),
                exists()
                .where(DrawingView.page_id == Page.id)
                .where(PartProposal.drawing_view_id == DrawingView.id)
                .where(~exists().where(PartPicture.part_proposal_id == PartProposal.id)),
            )
            .order_by(Page.document_version_id, Page.index)
        )
    )
