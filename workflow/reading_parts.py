"""Which confirmed reading is each confirmed part's width: suggested, then decided by a person (#913).

A person confirms a drawing's cabinets, fillers and countertops one at a time (#882), and what each
reading on the drawing is (#530). This module says which of those readings is the width of which
part (#913, step 6 of the plan on #748). **The computer only suggests a link; a person confirms,
corrects or withdraws it.** Nothing here reads a width: the link says which reading belongs to which
part, and the width stays the reading's own exact value.

## The suggestion (`suggest_links`), which writes nothing

A reading is placed by its own geometry (`PlacedReading`):

- **its dimension line**, when the association stage attached a reading behind it to one
  (`observation_associations`). That line is what the number measures, so its two ends are the
  ends of what it measures. A line that runs down the page measures a height, never a width, and is
  never compared with a part's ends;
- **otherwise its region**, the box it is printed in, for a reading no line was found for (a model
  reader's, say). Its left and right sides are compared exactly as a line's ends are;
- **neither**, when the readings behind it were attached to different lines: which one it measures
  is not known, and guessing would be choosing a width. It is never suggested, and a person may
  still pick it.

A reading **spans** a confirmed part when both ends of its line or region across the page lie within
the stated tolerance of the part's two ends. A part's ends are the sides of its outline: the
dimension that defined it (#868), or the line between the two ends a person gave (#882), which has
no height. Only left and right are compared, so a two-point outline is placed exactly as a box is.

**A line counts only for a reading printed over it**: the box the reading is printed in lies across
the page between the line's ends, give or take the same tolerance. The association stage attaches a
number to the nearest line within a distance, and on `AI_Set_2` page 7 it attached the number in a
note printed beside the drawing to the nearest cabinet's dimension line; taken at its word, that
note was suggested as the cabinet's width. A reading set aside this way spans nothing and is said
to have been set aside; a person may still pick it. It can only make a suggestion rarer.

The suggested reading for a part is the one reading on its drawing that spans it, **when it spans
no other part**. Two readings spanning one part, or one reading spanning two parts (a countertop
over a single cabinet with no fillers has the cabinet's ends), is said in words and left for a
person: a width is never chosen between candidates by the computer (`AGENTS.md` §2.4).

## The decision, which only a person makes

`_write_link` is **the only code that writes a `reading_parts` row**, and only
`confirm_reading_part` and `withdraw_reading_part` call it, each for a person's decision; a guard in
`tests/db/test_drawing_models.py` fails if anything else constructs or inserts into the table, or
calls the writer. Each row names the link it replaces, so the database keeps at most one live link
per reading (`ReadingPart`).

- **Confirming** says a reading is a part's width: the suggestion, or a correction to another
  confirmed reading on the part's drawing. A part's width is one reading, so confirming withdraws
  any other link to the same part in the same transaction, and moves the reading off any part it was
  linked to before.
- **Withdrawing** takes a part's link back. It writes a row naming no part, which the table
  requires to replace a link that existed.

**What a reader reads** (`live_reading_parts`): only a reading's current link, and only while the
part is one a person stands by (`workflow/parts.py:live_part_item_ids`), the reading has not been
replaced by a review action, and no other such link names the same part. A part with two readings
linked is read as having none rather than as having either. **No rule reads links yet** (#748 step
7), and a guard in `tests/db/test_drawing_models.py` keeps them out of everything but the models and
their migration, this module, and the Measure page's listing and endpoints.

**No extraction imports, on purpose**, as in `workflow/parts.py`: the API reaches this, and
`tests/api/test_no_heavy_work.py` keeps `app/api/` away from anything that reads a PDF.

Source: issue #913; #748 plan, step 6. Verification: tests/workflow/test_reading_parts.py,
tests/db/test_drawing_models.py, tests/api/test_reading_parts.py.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Final, cast
from uuid import UUID

from sqlalchemy import ColumnElement, Select, exists, func, select
from sqlalchemy.orm import Session, aliased

from app.audit.events import AuditCategory, emit
from app.models import (
    CanonicalObservation,
    DrawingView,
    EvidenceSupportingCandidate,
    ObservationAssociation,
    ReadingPart,
)
from app.models.evidence import EvidenceCandidateRole
from app.models.review import ReviewAction
from vocabulary.semantic_types import DocumentRole
from workflow.parts import (
    PlacedPart,
    check_edge_tolerance,
    live_part,
    live_part_item_ids,
    live_parts_on,
)

__all__ = [
    "LinkSuggestion",
    "PlacedReading",
    "ReadingGeometry",
    "confirm_reading_part",
    "current_link",
    "current_links_to",
    "live_reading_ids",
    "live_reading_parts",
    "readings_on",
    "suggest_links",
    "withdraw_reading_part",
]

#: How many places the numbers in a sentence for a person are shown to. Display only: every
#: comparison is made on the exact stored values.
_SHOWN: Final = ".4f"

#: The supporting readings whose lines place a canonical reading. A conflicting reading disagreed
#: with it, so where it was attached says nothing about what this reading measures.
_SUPPORTING: Final = (
    EvidenceCandidateRole.PRIMARY.value,
    EvidenceCandidateRole.CORROBORATING.value,
)

#: The sentence on a row that takes a part's link back.
WITHDRAWN: Final = "Withdrawn by a person: this reading is not this part's width."

#: The sentence on a row that takes a link back because a person linked another reading to its part.
REPLACED: Final = (
    "Withdrawn by a person: another reading was confirmed as this part's width, and a part's width "
    "is one reading."
)


class ReadingGeometry(StrEnum):
    """What a reading is placed by on the drawing."""

    LINE = "line"
    """The dimension line a reading behind it was attached to."""
    REGION = "region"
    """The box it is printed in: no reading behind it was attached to a line."""


#: A dimension line as two points in stored page space, the left end first (the upper one when
#: both are level), so one line drawn either way round is one line.
type _Line = tuple[tuple[Decimal, Decimal], tuple[Decimal, Decimal]]


@dataclass(frozen=True, slots=True)
class PlacedReading:
    """A confirmed reading as a link is decided from it: where it is printed, and the span across
    the page it measures, from its own geometry. Stored page space, `y` growing down the page."""

    observation_id: UUID
    page_id: UUID
    region: tuple[Decimal, Decimal, Decimal, Decimal]
    """`(left, top, right, bottom)` of the box it is printed in."""
    geometry: ReadingGeometry | None
    """What placed it, or `None` when nothing could (`unplaced` says why)."""
    left: Decimal | None
    right: Decimal | None
    """Its span across the page: its line's ends, or its region's sides. `None` when it measures no
    width (`unplaced` says why)."""
    unplaced: str | None
    """Why it spans no part whatever the tolerance, in plain English, or `None`."""

    @classmethod
    def from_observation(
        cls, observation: CanonicalObservation, lines: Iterable[_Line]
    ) -> PlacedReading | None:
        """Place a canonical reading by the lines attached to the readings behind it, or else by
        its region. `None` when its region is not a box of finite numbers: such a reading cannot be
        said to be on any drawing."""
        region = _region(observation.polygon)
        if region is None:
            return None
        distinct = set(lines)
        geometry: ReadingGeometry | None = ReadingGeometry.REGION
        left: Decimal | None = region[0]
        right: Decimal | None = region[2]
        unplaced: str | None = None
        if len(distinct) > 1:
            geometry, left, right = None, None, None
            unplaced = (
                f"The readings behind it were attached to {len(distinct)} different dimension "
                "lines, so which one it measures is not known."
            )
        elif distinct:
            (start_x, start_y), (end_x, end_y) = distinct.pop()
            geometry = ReadingGeometry.LINE
            if abs(end_x - start_x) <= abs(end_y - start_y):
                left, right = None, None
                unplaced = (
                    "Its dimension line runs down the page: it measures a height, not a width."
                )
            else:
                left, right = min(start_x, end_x), max(start_x, end_x)
        return cls(
            observation_id=observation.id,
            page_id=observation.page_id,
            region=region,
            geometry=geometry,
            left=left,
            right=right,
            unplaced=unplaced,
        )


@dataclass(frozen=True, slots=True)
class LinkSuggestion:
    """Which reading the computer suggests is one part's width. **A suggestion: nothing writes it.**"""

    part: PlacedPart
    reading: PlacedReading | None
    """The suggested reading, or `None` when no single reading spans this part alone."""
    spanning: tuple[PlacedReading, ...]
    """Every reading whose line or region spans this part, in the order the readings were given
    (`readings_on` gives them left to right)."""
    said: str
    """Why this reading, or why none, in plain English."""
    edge_tolerance: Decimal


def suggest_links(
    parts: Sequence[PlacedPart], readings: Sequence[PlacedReading], *, edge_tolerance: Decimal
) -> tuple[LinkSuggestion, ...]:
    """Suggest a reading for each of `parts` from `readings`, both on the same drawing.

    One suggestion per part, in the order the parts are given. Reads only what it is given and
    writes nothing. The tolerance is required and has no default: it is the stated
    `GV_RUN_EDGE_TOLERANCE`, in stored units.
    """
    tolerance = check_edge_tolerance(edge_tolerance)
    spans = {
        part.item_id: tuple(reading for reading in readings if _spans(reading, part, tolerance))
        for part in parts
    }
    parts_spanned: dict[UUID, int] = {}
    for found in spans.values():
        for each in found:
            parts_spanned[each.observation_id] = parts_spanned.get(each.observation_id, 0) + 1
    suggestions: list[LinkSuggestion] = []
    for part in parts:
        spanning = spans[part.item_id]
        reading: PlacedReading | None = None
        if not spanning:
            aside = sum(
                1
                for each in readings
                if _meets(each, part, tolerance) and not _printed_over(each, tolerance)
            )
            said = _spans_nothing(part, tolerance, aside)
        elif len(spanning) > 1:
            said = (
                f"{len(spanning)} confirmed readings on this drawing span this {part.kind.value}, "
                "so which one is its width is for a person to say."
            )
        elif parts_spanned[spanning[0].observation_id] > 1:
            said = (
                "One confirmed reading spans this "
                f"{part.kind.value}, and it spans another confirmed part on this drawing too, so "
                "which part it measures is for a person to say."
            )
        else:
            reading = spanning[0]
            said = _signal(reading, part, tolerance)
        suggestions.append(
            LinkSuggestion(
                part=part,
                reading=reading,
                spanning=spanning,
                said=said,
                edge_tolerance=tolerance,
            )
        )
    return tuple(suggestions)


def _spans(reading: PlacedReading, part: PlacedPart, tolerance: Decimal) -> bool:
    """Whether the reading spans the part: its line or region meets the part's two ends, and a line
    is one the reading is printed over."""
    return _meets(reading, part, tolerance) and _printed_over(reading, tolerance)


def _meets(reading: PlacedReading, part: PlacedPart, tolerance: Decimal) -> bool:
    """Whether both ends of the reading's line or region across the page lie within the tolerance
    of the part's two ends."""
    if reading.left is None or reading.right is None:
        return False
    return (
        abs(reading.left - part.left) <= tolerance and abs(reading.right - part.right) <= tolerance
    )


def _printed_over(reading: PlacedReading, tolerance: Decimal) -> bool:
    """Whether a reading placed by a line is printed over it: the region it is printed in lies
    across the page between the line's ends, give or take the tolerance. Always true of a region.

    A dimension's number is printed along its own line. One printed beyond the ends of the line it
    was attached to may belong to something else: the attachment takes the nearest line within a
    distance, and a number in a note beside a drawing can be nearest to a cabinet's line."""
    if (
        reading.geometry is not ReadingGeometry.LINE
        or reading.left is None
        or reading.right is None
    ):
        return True
    left, _, right, _ = reading.region
    return left >= reading.left - tolerance and right <= reading.right + tolerance


def _span(left: Decimal, right: Decimal) -> str:
    return f"{left:{_SHOWN}} to {right:{_SHOWN}}"


def _signal(reading: PlacedReading, part: PlacedPart, tolerance: Decimal) -> str:
    assert reading.left is not None and reading.right is not None
    if reading.geometry is ReadingGeometry.LINE:
        placed = (
            f"Its dimension line runs across the page from {_span(reading.left, reading.right)}"
        )
    else:
        placed = (
            "No reading behind it was attached to a dimension line. The region it is printed in "
            f"runs across the page from {_span(reading.left, reading.right)}"
        )
    return (
        f"{placed}, within the stated tolerance ({tolerance}) of this {part.kind.value}'s ends "
        f"({_span(part.left, part.right)}), on the same drawing, and it spans no other confirmed "
        "part."
    )


def _spans_nothing(part: PlacedPart, tolerance: Decimal, aside: int) -> str:
    said = (
        "No confirmed reading on this drawing has a dimension line, or a region where no line was "
        f"found, whose ends lie within the stated tolerance ({tolerance}) of this "
        f"{part.kind.value}'s ends ({_span(part.left, part.right)})."
    )
    if aside:
        said += (
            f" {aside} {'reading was' if aside == 1 else 'readings were'} set aside: attached to a "
            "line that meets these ends, but printed beyond that line's ends, so the line may not "
            "be its own."
        )
    return said


def _region(polygon: object) -> tuple[Decimal, Decimal, Decimal, Decimal] | None:
    """The box around a stored polygon, read exactly, or `None` if it is not one."""
    if not isinstance(polygon, list) or not polygon:
        return None
    points: list[tuple[Decimal, Decimal]] = []
    for point in cast(list[object], polygon):
        if not isinstance(point, list) or len(point) != 2:
            return None
        try:
            x, y = Decimal(str(point[0])), Decimal(str(point[1]))
        except InvalidOperation:
            return None
        if not (x.is_finite() and y.is_finite()):
            return None
        points.append((x, y))
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    return min(xs), min(ys), max(xs), max(ys)


def _view_box(view: DrawingView) -> tuple[Decimal, Decimal, Decimal, Decimal] | None:
    if view.region.get("space") != "stored":
        return None
    return _region(view.region.get("points"))


# ---------------------------------------------------------------------------
# Reading the confirmed readings
# ---------------------------------------------------------------------------


def live_reading_ids() -> Select[tuple[UUID]]:
    """The canonical readings no review action has replaced.

    A reviewer confirming or correcting a reading in review makes a new one and names the old one
    as its original (`app/review/evidence_actions.py`); the old one keeps its row, and a link to it
    must not go on being read, least of all after its value was corrected. Returns a query, so the
    caller filters with it in its own statement.
    """
    replaced = select(ReviewAction.original_observation_id).where(
        ReviewAction.original_observation_id.is_not(None)
    )
    return select(CanonicalObservation.id).where(CanonicalObservation.id.not_in(replaced))


def readings_on(session: Session, view: DrawingView) -> tuple[PlacedReading, ...]:
    """Every confirmed reading on one drawing that a link may name, left to right.

    On the drawing's page, on the vendor's side, not replaced by a review action, and printed wholly
    inside the box around the drawing's region: the box `#868` takes a drawing's dimensions from. A
    drawing whose region is not kept as stored points has no box, and so no readings.
    """
    box = _view_box(view)
    if box is None:
        return ()
    observations = list(
        session.scalars(
            select(CanonicalObservation).where(
                CanonicalObservation.page_id == view.page_id,
                CanonicalObservation.document_role == DocumentRole.SHOP.value,
                CanonicalObservation.id.in_(live_reading_ids()),
            )
        )
    )
    lines = _attached_lines(session, [observation.id for observation in observations])
    placed = (
        PlacedReading.from_observation(observation, lines.get(observation.id, ()))
        for observation in observations
    )
    left, top, right, bottom = box
    return tuple(
        sorted(
            (
                reading
                for reading in placed
                if reading is not None
                and left <= reading.region[0]
                and reading.region[2] <= right
                and top <= reading.region[1]
                and reading.region[3] <= bottom
            ),
            key=lambda reading: (*reading.region, str(reading.observation_id)),
        )
    )


def _attached_lines(
    session: Session, observation_ids: Sequence[UUID]
) -> Mapping[UUID, tuple[_Line, ...]]:
    """Each reading's attached dimension lines, through the readings that support it, as exact
    points with the left end first, so one line drawn either way round is one line."""
    if not observation_ids:
        return {}
    rows = session.execute(
        select(EvidenceSupportingCandidate.canonical_observation_id, ObservationAssociation)
        .join(
            ObservationAssociation,
            ObservationAssociation.candidate_id == EvidenceSupportingCandidate.candidate_id,
        )
        .where(
            EvidenceSupportingCandidate.canonical_observation_id.in_(list(observation_ids)),
            EvidenceSupportingCandidate.role.in_(_SUPPORTING),
            ObservationAssociation.refusal_reason.is_(None),
        )
    )
    lines: dict[UUID, list[_Line]] = {}
    for observation_id, association in rows:
        ends = (association.start_x, association.start_y, association.end_x, association.end_y)
        try:
            start_x, start_y, end_x, end_y = (Decimal(str(value)) for value in ends)
        except InvalidOperation:
            continue  # `attached_or_refused` keeps these present; a non-number places nothing.
        first, second = sorted(((start_x, start_y), (end_x, end_y)))
        lines.setdefault(observation_id, []).append((first, second))
    return {observation_id: tuple(found) for observation_id, found in lines.items()}


# ---------------------------------------------------------------------------
# The decision, and what a reader may read
# ---------------------------------------------------------------------------


def current_link(session: Session, observation_id: UUID) -> ReadingPart | None:
    """The row for this reading that nothing has replaced, or `None` if it was never linked. The
    schema allows at most one, so this asks for one and would raise rather than pick."""
    later = aliased(ReadingPart)
    return session.execute(
        select(ReadingPart).where(
            ReadingPart.canonical_observation_id == observation_id,
            ~exists().where(later.supersedes_id == ReadingPart.id),
        )
    ).scalar_one_or_none()


def current_links_to(session: Session, item_id: UUID) -> tuple[ReadingPart, ...]:
    """Every reading's current row that names this part, oldest first: one, unless two people
    linked different readings to it at the same moment."""
    later = aliased(ReadingPart)
    return tuple(
        session.scalars(
            select(ReadingPart)
            .where(
                ReadingPart.drawing_item_id == item_id,
                ~exists().where(later.supersedes_id == ReadingPart.id),
            )
            .order_by(ReadingPart.created_at, ReadingPart.id)
        )
    )


def _standing(link: type[ReadingPart]) -> list[ColumnElement[bool]]:
    """What makes one row of `link` a link a reader may read, apart from its part's other links."""
    later = aliased(ReadingPart)
    return [
        ~exists().where(later.supersedes_id == link.id),
        link.drawing_item_id.is_not(None),
        link.drawing_item_id.in_(live_part_item_ids()),
        link.canonical_observation_id.in_(live_reading_ids()),
    ]


def live_reading_parts() -> Select[tuple[ReadingPart]]:
    """The `reading_parts` rows a reader may read: each a reading's current link, to a part a person
    still stands by, from a reading no review action has replaced, and the only such link to its
    part.

    A withdrawn or corrected link, a link whose part was taken back or corrected, and a link whose
    reading was replaced are not here. Nor is either link to a part two readings are linked to: a
    part's width is one reading, and reading one of two would be choosing. Returns a query, so the
    caller filters and orders it in its own statement.
    """
    other = aliased(ReadingPart)
    doubled = (
        select(other.drawing_item_id)
        .where(*_standing(other))
        .group_by(other.drawing_item_id)
        .having(func.count() > 1)
    )
    return select(ReadingPart).where(
        *_standing(ReadingPart), ReadingPart.drawing_item_id.not_in(doubled)
    )


def confirm_reading_part(
    session: Session,
    *,
    observation_id: UUID,
    item_id: UUID,
    edge_tolerance: Decimal | None,
    actor: str,
) -> ReadingPart:
    """A person saying this confirmed reading is this confirmed part's width.

    The part must be one a person stands by, and the reading a confirmed reading on the part's own
    drawing (`readings_on`). The row's sentence is the suggestion's when the reading is the one
    suggested under `edge_tolerance`, and otherwise says a person picked it and why the suggestion
    had not. With no tolerance stated nothing was suggested, and the sentence says that.

    A part's width is one reading: any other reading linked to this part has its link withdrawn in
    the same transaction. The reading's own earlier link, to this part or another, is replaced.

    Raises `ValueError` for anything a person could not have meant: no person, a part that is not a
    confirmed part, or a reading that is not a confirmed reading on its drawing.
    """
    _require_actor(actor)
    tolerance = None if edge_tolerance is None else check_edge_tolerance(edge_tolerance)
    part = live_part(session, item_id)
    if part is None:
        raise ValueError("a reading is linked only to a confirmed part a person stands by")
    view = session.get_one(DrawingView, part.view_id)
    readings = readings_on(session, view)
    reading = next((found for found in readings if found.observation_id == observation_id), None)
    if reading is None:
        raise ValueError("a part's width is a confirmed reading on the part's own drawing")
    signal = (
        _NOTHING_SUGGESTED
        if tolerance is None
        else _sentence(reading, part, live_parts_on(session, view.id), readings, tolerance)
    )
    for other in current_links_to(session, item_id):
        if other.canonical_observation_id != observation_id:
            _write_link(
                session,
                observation_id=other.canonical_observation_id,
                item_id=None,
                signal=REPLACED,
                actor=actor,
            )
    return _write_link(
        session, observation_id=observation_id, item_id=item_id, signal=signal, actor=actor
    )


def withdraw_reading_part(
    session: Session, *, item_id: UUID, actor: str
) -> tuple[ReadingPart, ...]:
    """A person taking back the link to this part: every reading's current link that names it.

    Writes one row naming no part for each, so the readings keep their history and are linked to
    nothing. Raises `ValueError` when no reading is linked to the part, since a withdrawal must
    replace a link that existed (`reading_part_withdraws_a_link`).
    """
    _require_actor(actor)
    links = current_links_to(session, item_id)
    if not links:
        raise ValueError("no reading is linked to this part, so there is no link to withdraw")
    return tuple(
        _write_link(
            session,
            observation_id=link.canonical_observation_id,
            item_id=None,
            signal=WITHDRAWN,
            actor=actor,
        )
        for link in links
    )


#: The sentence on a link a person picked while no tolerance was stated.
_NOTHING_SUGGESTED: Final = (
    "Picked by a person. Nothing was suggested: the tolerance for whether a reading's ends meet a "
    "part's (GV_RUN_EDGE_TOLERANCE) is not set."
)


def _sentence(
    reading: PlacedReading,
    part: PlacedPart,
    parts: Sequence[PlacedPart],
    readings: Sequence[PlacedReading],
    tolerance: Decimal,
) -> str:
    """The sentence a confirmed link is written with: the suggestion's, when this reading is the
    one suggested; otherwise that a person picked it, and why the suggestion had not."""
    (suggestion,) = (
        found
        for found in suggest_links(parts, readings, edge_tolerance=tolerance)
        if found.part.item_id == part.item_id
    )
    if (
        suggestion.reading is not None
        and suggestion.reading.observation_id == reading.observation_id
    ):
        return suggestion.said
    if reading.unplaced is not None:
        return f"Picked by a person. The suggestion places this reading nowhere: {reading.unplaced}"
    if reading in suggestion.spanning:
        return f"Picked by a person, from what the suggestion left open. {suggestion.said}"
    assert reading.left is not None and reading.right is not None
    if _meets(reading, part, tolerance):
        return (
            "Picked by a person. The suggestion set this reading aside: its dimension line meets "
            f"this {part.kind.value}'s ends, but it is printed beyond that line's ends "
            f"({_span(reading.left, reading.right)}), so the line may not be its own."
        )
    return (
        "Picked by a person. The suggestion did not hold this reading: its "
        f"{'dimension line' if reading.geometry is ReadingGeometry.LINE else 'region'} runs across "
        f"the page from {_span(reading.left, reading.right)}, and its ends do not both lie within "
        f"the stated tolerance ({tolerance}) of this {part.kind.value}'s "
        f"({_span(part.left, part.right)})."
    )


def _require_actor(actor: str) -> None:
    if not isinstance(actor, str) or not actor.strip():
        raise ValueError("a decision on a reading's part needs the person who made it")


def _write_link(
    session: Session, *, observation_id: UUID, item_id: UUID | None, signal: str, actor: str
) -> ReadingPart:
    """**The only code that writes a `reading_parts` row.** Replaces the reading's current row, so
    the database's one-live-link rule holds, and audits the decision in the same transaction."""
    replaced = current_link(session, observation_id)
    row = ReadingPart(
        canonical_observation_id=observation_id,
        drawing_item_id=item_id,
        supersedes_id=None if replaced is None else replaced.id,
        signal=signal,
        confirmed_by=actor,
    )
    session.add(row)
    session.flush()
    # Like every reviewer action: which reading is a part's width is a person's call, and the audit
    # trail is where somebody looks for who made it.
    emit(
        session,
        category=AuditCategory.REVIEW_ACTION,
        actor=actor,
        target_id=row.id,
        target_type="reading_part",
    )
    session.flush()
    return row
