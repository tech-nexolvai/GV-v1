"""The parts of each vendor drawing, as a person confirms them on the Measure page (#882).

The computer suggests each vendor drawing's parts (#868), and a suggestion is not a part (#852): a
`drawing_items` row exists only once a person confirms one, through `workflow/parts.py:confirm_part`.
This module is what the Measure page asks for: each drawing's suggestions, left to right, and a
person's answer to one of them at a time.

- **Confirming** says what a suggestion is (a cabinet, a filler or a countertop) and which code is
  printed on it. The code is kept exactly as the person sent it, never trimmed and never decoded.
- **Withdrawing** says it is not a part.
- **Adding** files the person's own suggestion between the two ends they gave, and confirms it in
  the same transaction: a part a person adds is one they have already decided on.

**A part is confirmed only on a drawing a person has confirmed as the vendor's.** The suggester also
aims at drawings whose label merely suggests the vendor's (#868). A label is enough to aim a
suggestion, but not to make a part: an unconfirmed drawing may yet turn out to be the architect's.
The architect's drawing gets no parts here at all. **Withdrawing is allowed on any drawing in the
revision.** It makes nothing, and a part confirmed before its drawing's role changed must stay
removable.

**There is no "confirm all".** Each call decides one suggestion, because each decision is a person
looking at one part.

**Each suggestion has its own picture (#897)**, cut by the worker from the vendor's drawing: the
part's outline and a stated margin around it (`workflow/part_pictures.py`). A part a person adds is
cut once the worker reaches it, and until then, or where the page could not be rendered, it has
none and `has_picture` says so rather than offering some other region. **A suggestion with a code
also has the crop of the reading its code came from**, cut by the evidence stage as every reading's
is (`has_crop`), so a person can check the code itself.

**No extraction imports**, as in `workflow/view_roles.py`: the API reaches this, and
`tests/api/test_no_heavy_work.py` keeps `app/api/` away from anything that reads a PDF.

Source: issues #882 and #897; #748 plan, step 4. Verification: tests/api/test_drawing_parts.py.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Final, cast
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.orm import Session, aliased

from app.models import (
    DrawingView,
    PackageRevisionDocument,
    Page,
    PartConfirmation,
    PartProposal,
    ViewRole,
)
from app.models.evidence import EvidenceArtifact, EvidenceArtifactKind
from vocabulary.part_kinds import PartKind
from workflow.part_pictures import pictured
from workflow.parts import confirm_part, record_part_proposal, withdraw_part
from workflow.view_roles import revision_views

__all__ = [
    "ADDED_BY_A_PERSON",
    "DrawingParts",
    "ListedPart",
    "PartRefusalReason",
    "PartRefused",
    "add_part",
    "confirm_listed_part",
    "defining_ends",
    "listed_proposal",
    "revision_parts",
    "withdraw_listed_part",
]

#: The `part_proposals.source` of a part a person added, so it is never mistaken for the computer's.
ADDED_BY_A_PERSON: Final = "added-by-a-person"

#: Its version, which `part_proposals.source_version` requires. One way of adding exists.
_ADDED_VERSION: Final = "1"

#: The reason filed with a part a person added. It names no person: who added the part is the
#: confirmation's `confirmed_by`, and keeping it out of the suggestion means the same part added
#: twice is found again rather than filed as a second suggestion.
_ADDED_REASON: Final = "Added by a person: the suggestions for this drawing did not include it."


class PartRefusalReason(StrEnum):
    """Why a decision on a part was not recorded. Each is something a person can be told and act on."""

    NO_SUCH_PART = "no_such_part"
    NO_SUCH_DRAWING = "no_such_drawing"
    #: Nobody has said whose drawing this is yet.
    DRAWING_NOT_CONFIRMED = "drawing_not_confirmed"
    #: A person said it is the architect's drawing.
    ARCHITECTS_DRAWING = "architects_drawing"
    CODE_BLANK = "code_blank"
    #: An end is not a number, or lies outside the box around the drawing.
    END_NOT_ON_DRAWING = "end_not_on_drawing"
    #: The two ends are one above the other, so they span nothing from left to right.
    ENDS_NOT_APART = "ends_not_apart"


@dataclass(frozen=True, slots=True)
class PartRefused:
    """A decision that was not recorded, and why, in plain English."""

    reason: PartRefusalReason
    detail: str


@dataclass(frozen=True, slots=True)
class ListedPart:
    """One suggestion on a drawing, with what a person last said about it."""

    proposal: PartProposal
    position: int
    """Its place on its drawing, `1` for the leftmost."""
    decision: PartConfirmation | None
    """The decision nothing has replaced, or `None` while nobody has decided."""
    has_picture: bool
    """Whether its own picture is stored (#897)."""
    has_crop: bool
    """Whether the crop of the reading its code came from is stored (the module docstring says
    why both)."""


@dataclass(frozen=True, slots=True)
class DrawingParts:
    """One drawing of the revision and its suggestions, left to right."""

    view: DrawingView
    page_index: int
    parts: tuple[ListedPart, ...]
    refusal: PartRefused | None
    """Why no part can be confirmed or added on this drawing, or `None` when one can."""


def revision_parts(session: Session, package_revision_id: UUID) -> tuple[DrawingParts, ...]:
    """Every drawing of the revision that has a suggestion or is confirmed as the vendor's.

    In page order, each drawing's suggestions left to right. **Reads only.** A vendor's drawing with
    no suggestion is listed too, so a person can add the parts the suggester missed.
    """
    views = revision_views(session, package_revision_id)
    proposals = list(
        session.scalars(
            select(PartProposal).where(
                PartProposal.drawing_view_id.in_([entry.view.id for entry in views])
            )
        )
    )
    decisions = _current_decisions(session, [proposal.id for proposal in proposals])
    pictures = pictured(session, [proposal.id for proposal in proposals])
    cropped = _cropped_candidates(session, proposals)
    by_view: dict[UUID, list[PartProposal]] = {}
    for proposal in proposals:
        by_view.setdefault(proposal.drawing_view_id, []).append(proposal)

    drawings: list[DrawingParts] = []
    for entry in views:
        on_view = sorted(by_view.get(entry.view.id, []), key=_left_to_right)
        if not on_view and entry.view.role != ViewRole.SHOP.value:
            continue
        drawings.append(
            DrawingParts(
                view=entry.view,
                page_index=entry.page_index,
                parts=tuple(
                    ListedPart(
                        proposal=proposal,
                        position=index,
                        decision=decisions.get(proposal.id),
                        has_picture=proposal.id in pictures,
                        has_crop=proposal.code_candidate_id in cropped,
                    )
                    for index, proposal in enumerate(on_view, start=1)
                ),
                refusal=_cannot_make_parts(entry.view),
            )
        )
    return tuple(drawings)


def defining_ends(proposal: PartProposal) -> tuple[tuple[str, str], tuple[str, str]] | None:
    """The two ends of the line that defined a suggestion, left first, as the exact stored text.

    Text, so a person picking one as an end of a part they add sends back exactly this point.
    `None` when no line defined the suggestion.
    """
    if proposal.defining_line is None:
        return None
    first, second = (
        (str(x), str(y)) for x, y in cast(list[list[str]], proposal.defining_line["points"])
    )
    return (first, second) if Decimal(first[0]) <= Decimal(second[0]) else (second, first)


def listed_proposal(
    session: Session, package_revision_id: UUID, proposal_id: UUID
) -> PartProposal | None:
    """The suggestion, if it is on one of this revision's drawings; `None` for any other id."""
    return session.execute(
        select(PartProposal)
        .join(DrawingView, DrawingView.id == PartProposal.drawing_view_id)
        .join(Page, Page.id == DrawingView.page_id)
        .join(
            PackageRevisionDocument,
            PackageRevisionDocument.document_version_id == Page.document_version_id,
        )
        .where(
            PartProposal.id == proposal_id,
            PackageRevisionDocument.package_revision_id == package_revision_id,
        )
    ).scalar_one_or_none()


def confirm_listed_part(
    session: Session,
    *,
    package_revision_id: UUID,
    proposal_id: UUID,
    kind: PartKind,
    code: str | None,
    actor: str,
) -> PartConfirmation | PartRefused:
    """A person saying one suggestion is a part of this kind, with this code or none.

    Recorded by `confirm_part`, which makes the item and audits the decision. A confirmation replaces
    whichever decision was current, so confirming again is how a person corrects one.
    """
    proposal = listed_proposal(session, package_revision_id, proposal_id)
    if proposal is None:
        return PartRefused(PartRefusalReason.NO_SUCH_PART, "There is no such part on this package.")
    refused = _cannot_make_parts(session.get_one(DrawingView, proposal.drawing_view_id))
    if refused is not None:
        return refused
    if code is not None and not code.strip():
        return _blank_code()
    return confirm_part(session, proposal=proposal, kind=kind, code=code, actor=actor)


def withdraw_listed_part(
    session: Session, *, package_revision_id: UUID, proposal_id: UUID, actor: str
) -> PartConfirmation | PartRefused:
    """A person saying one suggestion is not a part, on whichever drawing of the revision it is."""
    proposal = listed_proposal(session, package_revision_id, proposal_id)
    if proposal is None:
        return PartRefused(PartRefusalReason.NO_SUCH_PART, "There is no such part on this package.")
    return withdraw_part(session, proposal=proposal, actor=actor)


def add_part(
    session: Session,
    *,
    package_revision_id: UUID,
    view_id: UUID,
    kind: PartKind,
    code: str | None,
    ends: tuple[tuple[str, str], tuple[str, str]],
    actor: str,
) -> PartConfirmation | PartRefused:
    """A person adding a part the suggestions missed, by its two ends on the drawing.

    `ends` are two points in stored page space, as text so they stay exact. They are filed in left
    to right order as a suggestion whose source is `ADDED_BY_A_PERSON`, whose defining line runs
    between them, and whose outline is that same line: nothing here invents a height the person did
    not give. The person's code goes on the confirmation, not the suggestion, because a suggestion's
    code must name the reading it came from and this one came from no reading.
    """
    view = next(
        (
            entry.view
            for entry in revision_views(session, package_revision_id)
            if entry.view.id == view_id
        ),
        None,
    )
    if view is None:
        return PartRefused(
            PartRefusalReason.NO_SUCH_DRAWING, "There is no such drawing on this package."
        )
    refused = _cannot_make_parts(view)
    if refused is not None:
        return refused
    if code is not None and not code.strip():
        return _blank_code()
    points = _points_on(view, ends)
    if isinstance(points, PartRefused):
        return points
    left, right = sorted(points)
    if left[0] == right[0]:
        return PartRefused(
            PartRefusalReason.ENDS_NOT_APART,
            "The two ends are one above the other, so they do not span a part from left to right.",
        )
    proposal = record_part_proposal(
        session,
        drawing_view_id=view.id,
        kind=kind,
        extent=[left, right],
        defining_line=(left, right),
        code_as_printed=None,
        code_candidate_id=None,
        reason=_ADDED_REASON,
        source=ADDED_BY_A_PERSON,
        source_version=_ADDED_VERSION,
    )
    return confirm_part(session, proposal=proposal, kind=kind, code=code, actor=actor)


def _cannot_make_parts(view: DrawingView) -> PartRefused | None:
    if view.role is None:
        return PartRefused(
            PartRefusalReason.DRAWING_NOT_CONFIRMED,
            "Nobody has confirmed whose drawing this is yet. Confirm it is the vendor's drawing "
            "first; its parts can be confirmed after that.",
        )
    if view.role != ViewRole.SHOP.value:
        return PartRefused(
            PartRefusalReason.ARCHITECTS_DRAWING,
            "This was confirmed as the architect's drawing. Parts are confirmed on the vendor's "
            "drawing only.",
        )
    return None


def _blank_code() -> PartRefused:
    return PartRefused(
        PartRefusalReason.CODE_BLANK,
        "A code must be the text printed on the part. Leave it out if none is printed.",
    )


def _points_on(
    view: DrawingView, ends: tuple[tuple[str, str], tuple[str, str]]
) -> list[tuple[Decimal, Decimal]] | PartRefused:
    """Both ends as exact numbers, each inside the box around the drawing's region.

    The box, as the suggester uses it (#868): a drawing's dimensions are the ones lying inside it.
    A drawing whose region is not kept as stored points, as `record_panel_view` keeps every one it
    makes, has no box to place an end in, and is refused rather than guessed at.
    """
    if view.region.get("space") != "stored" or not view.region.get("points"):
        return PartRefused(
            PartRefusalReason.END_NOT_ON_DRAWING,
            "This drawing's outline is not recorded on the page, so no end can be placed on it.",
        )
    region = [(Decimal(x), Decimal(y)) for x, y in cast(list[list[str]], view.region["points"])]
    low_x, high_x = min(x for x, _ in region), max(x for x, _ in region)
    low_y, high_y = min(y for _, y in region), max(y for _, y in region)
    points: list[tuple[Decimal, Decimal]] = []
    for raw_x, raw_y in ends:
        try:
            x, y = Decimal(raw_x), Decimal(raw_y)
        except InvalidOperation:
            x = y = Decimal("NaN")
        if not (x.is_finite() and y.is_finite() and low_x <= x <= high_x and low_y <= y <= high_y):
            return PartRefused(
                PartRefusalReason.END_NOT_ON_DRAWING,
                "An end of the part is not on this drawing.",
            )
        points.append((x, y))
    return points


def _left_to_right(proposal: PartProposal) -> tuple[Decimal, Decimal, Decimal, datetime, UUID]:
    """Left end, then right end, then top, across the page; filing order only breaks exact ties."""
    points = [(Decimal(x), Decimal(y)) for x, y in cast(list[list[str]], proposal.extent["points"])]
    return (
        min(x for x, _ in points),
        max(x for x, _ in points),
        min(y for _, y in points),
        proposal.created_at,
        proposal.id,
    )


def _current_decisions(
    session: Session, proposal_ids: Sequence[UUID]
) -> dict[UUID, PartConfirmation]:
    """Each suggestion's decision that nothing has replaced, as `current_decision` reads one."""
    if not proposal_ids:
        return {}
    later = aliased(PartConfirmation)
    rows = session.scalars(
        select(PartConfirmation).where(
            PartConfirmation.part_proposal_id.in_(list(proposal_ids)),
            ~exists().where(later.supersedes_id == PartConfirmation.id),
        )
    )
    return {row.part_proposal_id: row for row in rows}


def _cropped_candidates(session: Session, proposals: Sequence[PartProposal]) -> set[UUID]:
    """The code readings among these suggestions that have a stored crop."""
    candidate_ids = [p.code_candidate_id for p in proposals if p.code_candidate_id is not None]
    if not candidate_ids:
        return set()
    return {
        candidate_id
        for candidate_id in session.scalars(
            select(EvidenceArtifact.candidate_id).where(
                EvidenceArtifact.candidate_id.in_(candidate_ids),
                EvidenceArtifact.kind == EvidenceArtifactKind.CROP.value,
            )
        )
        if candidate_id is not None
    }
