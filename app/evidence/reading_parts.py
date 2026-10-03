"""Which reading is each part's width, as a person decides it on the Measure page (#913).

Once a person has confirmed a vendor drawing's parts (#882) and what its readings are (#530), this
module is what the Measure page asks for next to each part: the reading the computer suggests is its
width (`workflow/reading_parts.py:suggest_links`), the readings a person may pick instead, and what a
person decided.

- **Confirming** says which confirmed reading is the part's width: the suggestion, or a correction
  to another confirmed reading on the same drawing.
- **Withdrawing** takes the part's link back.

**Listing writes nothing.** A suggestion is worked out each time the page asks, from the parts and
readings a person stands by at that moment, and is never stored: only a person's decision writes a
`reading_parts` row, through `workflow/reading_parts.py`.

**A reading is linked only on a drawing a person has confirmed as the vendor's**, as a part is
confirmed. Withdrawing is allowed on any drawing of the revision, because it links nothing.

**Without a stated tolerance nothing is suggested**, and the page says so; a person may still pick a
part's reading, and the link records that nothing was suggested (`GV_RUN_EDGE_TOLERANCE`).

**There is no "confirm all".** Each call decides one part's link.

**No extraction imports**, as in `app/evidence/parts.py`: the API reaches this, and
`tests/api/test_no_heavy_work.py` keeps `app/api/` away from anything that reads a PDF.

Source: issue #913; #748 plan, step 6. Verification: tests/api/test_reading_parts.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.evidence.parts import revision_parts
from app.models import CanonicalObservation, DrawingView, ReadingPart, ViewRole
from workflow.parts import PlacedPart, live_parts_on
from workflow.reading_parts import (
    LinkSuggestion,
    PlacedReading,
    confirm_reading_part,
    current_link,
    current_links_to,
    live_reading_ids,
    live_reading_parts,
    readings_on,
    suggest_links,
    withdraw_reading_part,
)

__all__ = [
    "NO_TOLERANCE",
    "DrawingLinks",
    "LinkRefusalReason",
    "LinkRefused",
    "ListedLink",
    "ListedPart",
    "ListedReading",
    "confirm_listed_link",
    "revision_links",
    "withdraw_listed_link",
]

#: What a person is told when no tolerance is stated, so nothing can be suggested.
NO_TOLERANCE: Final = (
    "No reading can be suggested yet: the tolerance for whether a reading's ends meet a part's "
    "(GV_RUN_EDGE_TOLERANCE) has not been set for this system. You can still pick each part's "
    "reading."
)

#: Why a part's link is no longer read, in words, keyed by what changed.
_REPLACED_IN_REVIEW: Final = (
    "The reading was confirmed or corrected in review after it was linked, so this link is no "
    "longer read. Link the reading as it now stands."
)
_TWO_READINGS: Final = (
    "Two readings are linked to this part, so neither is read: a part's width is one reading. "
    "Confirm the right one again, which takes the other back."
)


class LinkRefusalReason(StrEnum):
    """Why a decision on a link was not recorded. Each is something a person can be told."""

    NO_SUCH_PART = "no_such_part"
    #: The part's drawing is not one a person confirmed as the vendor's.
    DRAWING_NOT_VENDORS = "drawing_not_vendors"
    #: The reading named is not a confirmed reading on the part's drawing.
    NOT_A_READING_ON_THE_DRAWING = "not_a_reading_on_the_drawing"
    NOTHING_LINKED = "nothing_linked"


@dataclass(frozen=True, slots=True)
class LinkRefused:
    """A decision that was not recorded, and why, in plain English."""

    reason: LinkRefusalReason
    detail: str


@dataclass(frozen=True, slots=True)
class ListedReading:
    """A confirmed reading on a drawing that a person may pick as a part's width."""

    placed: PlacedReading
    observation: CanonicalObservation
    linked_to: UUID | None
    """The part its current link names, while that part still stands; otherwise `None`."""


@dataclass(frozen=True, slots=True)
class ListedLink:
    """One reading's current link to a part, and whether a reader reads it."""

    row: ReadingPart
    read: bool
    why_not_read: str | None


@dataclass(frozen=True, slots=True)
class ListedPart:
    """One confirmed part: the reading suggested as its width, and what a person decided."""

    part: PlacedPart
    number: int | None
    """Its number in "Parts of each drawing", `1` for the leftmost; `None` if it is not listed."""
    code: str | None
    suggestion: LinkSuggestion | None
    """`None` when no tolerance is stated."""
    links: tuple[ListedLink, ...]
    """The readings whose current link names this part: one, or none. Two only when two people
    linked different readings at the same moment, and then neither is read."""


@dataclass(frozen=True, slots=True)
class DrawingLinks:
    """One drawing with a confirmed part: its parts, and the readings a link may name."""

    view: DrawingView
    page_index: int
    parts: tuple[ListedPart, ...]
    readings: tuple[ListedReading, ...]
    refusal: LinkRefused | None
    """Why no link can be confirmed on this drawing, or `None` when one can."""


def revision_links(
    session: Session, package_revision_id: UUID, *, edge_tolerance: Decimal | None
) -> tuple[DrawingLinks, ...]:
    """Every drawing of the revision with a confirmed part a person stands by.

    In page order; each drawing's parts left to right, and its readings left to right. A drawing no
    longer confirmed as the vendor's is listed with why nothing can be linked there, so a link made
    there before can still be withdrawn. **Reads only.**
    """
    drawings: list[DrawingLinks] = []
    for drawing in revision_parts(session, package_revision_id):
        placed = live_parts_on(session, drawing.view.id)
        if not placed:
            continue
        numbers = {listed.proposal.id: listed.position for listed in drawing.parts}
        decided = {
            listed.decision.drawing_item_id: listed.decision
            for listed in drawing.parts
            if listed.decision is not None and listed.decision.drawing_item_id is not None
        }
        readings = readings_on(session, drawing.view)
        observations = {
            observation.id: observation
            for observation in session.scalars(
                select(CanonicalObservation).where(
                    CanonicalObservation.id.in_([reading.observation_id for reading in readings])
                )
            )
        }
        standing = {part.item_id for part in placed}
        suggestions = (
            {}
            if edge_tolerance is None
            else {
                suggestion.part.item_id: suggestion
                for suggestion in suggest_links(placed, readings, edge_tolerance=edge_tolerance)
            }
        )
        links = {part.item_id: current_links_to(session, part.item_id) for part in placed}
        named = [link.id for found in links.values() for link in found]
        read = set(
            session.scalars(
                live_reading_parts()
                .where(ReadingPart.id.in_(named))
                .with_only_columns(ReadingPart.id)
            )
        )
        live_readings = set(
            session.scalars(
                live_reading_ids().where(
                    CanonicalObservation.id.in_(
                        [
                            link.canonical_observation_id
                            for found in links.values()
                            for link in found
                        ]
                    )
                )
            )
        )
        parts = tuple(
            ListedPart(
                part=part,
                number=(
                    numbers.get(decided[part.item_id].part_proposal_id)
                    if part.item_id in decided
                    else None
                ),
                code=decided[part.item_id].code_as_printed if part.item_id in decided else None,
                suggestion=suggestions.get(part.item_id),
                links=tuple(
                    _listed_link(link, read, live_readings) for link in links[part.item_id]
                ),
            )
            for part in placed
        )
        listed_readings = tuple(
            ListedReading(
                placed=reading,
                observation=observations[reading.observation_id],
                linked_to=_linked_to(session, reading.observation_id, standing),
            )
            for reading in readings
        )
        drawings.append(
            DrawingLinks(
                view=drawing.view,
                page_index=drawing.page_index,
                parts=parts,
                readings=listed_readings,
                refusal=_cannot_link(drawing.view),
            )
        )
    return tuple(drawings)


def confirm_listed_link(
    session: Session,
    *,
    package_revision_id: UUID,
    item_id: UUID,
    observation_id: UUID,
    edge_tolerance: Decimal | None,
    actor: str,
) -> ReadingPart | LinkRefused:
    """A person saying one confirmed reading is the width of one confirmed part of this revision.

    Recorded by `confirm_reading_part`, which takes back any other reading linked to the part and
    audits the decision. Confirming another reading is how a person corrects a link.
    """
    found = _part(session, package_revision_id, item_id)
    if isinstance(found, LinkRefused):
        return found
    part, view = found
    refused = _cannot_link(view)
    if refused is not None:
        return refused
    if observation_id not in {reading.observation_id for reading in readings_on(session, view)}:
        return LinkRefused(
            LinkRefusalReason.NOT_A_READING_ON_THE_DRAWING,
            "A part's width is a reading confirmed on the part's own drawing. This one is not, or "
            "was corrected in review since the page was loaded. Reload the page.",
        )
    return confirm_reading_part(
        session,
        observation_id=observation_id,
        item_id=part.item_id,
        edge_tolerance=edge_tolerance,
        actor=actor,
    )


def withdraw_listed_link(
    session: Session, *, package_revision_id: UUID, item_id: UUID, actor: str
) -> tuple[ReadingPart, ...] | LinkRefused:
    """A person taking back the link to one part of this revision, on whichever drawing it is."""
    found = _part(session, package_revision_id, item_id)
    if isinstance(found, LinkRefused):
        return found
    if not current_links_to(session, item_id):
        return LinkRefused(
            LinkRefusalReason.NOTHING_LINKED,
            "No reading is linked to this part, so there is nothing to take back.",
        )
    return withdraw_reading_part(session, item_id=item_id, actor=actor)


def _cannot_link(view: DrawingView) -> LinkRefused | None:
    if view.role == ViewRole.SHOP.value:
        return None
    return LinkRefused(
        LinkRefusalReason.DRAWING_NOT_VENDORS,
        "This drawing is no longer confirmed as the vendor's, so no reading can be linked to its "
        "parts. A link made here before can still be taken back.",
    )


def _part(
    session: Session, package_revision_id: UUID, item_id: UUID
) -> tuple[PlacedPart, DrawingView] | LinkRefused:
    """The part, if it is a confirmed one a person stands by on a drawing of this revision."""
    for drawing in revision_parts(session, package_revision_id):
        for part in live_parts_on(session, drawing.view.id):
            if part.item_id == item_id:
                return part, drawing.view
    return LinkRefused(LinkRefusalReason.NO_SUCH_PART, "There is no such part on this package.")


def _listed_link(link: ReadingPart, read: set[UUID], live_readings: set[UUID]) -> ListedLink:
    """A current link to a part that still stands: read, or not and why."""
    if link.id in read:
        return ListedLink(row=link, read=True, why_not_read=None)
    return ListedLink(
        row=link,
        read=False,
        why_not_read=(
            _REPLACED_IN_REVIEW
            if link.canonical_observation_id not in live_readings
            else _TWO_READINGS
        ),
    )


def _linked_to(session: Session, observation_id: UUID, standing: set[UUID]) -> UUID | None:
    link = current_link(session, observation_id)
    if link is None or link.drawing_item_id not in standing:
        return None
    return link.drawing_item_id
