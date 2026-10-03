"""Which reading is each part's width: the suggestion, and the one writer of a link (#913).

Verification for `workflow/reading_parts.py`. The first half needs no database: `suggest_links`
reads only the parts and readings it is given. The second half places real readings with
`readings_on`, writes through `confirm_reading_part` and `withdraw_reading_part`, and reads back
through `live_reading_parts`, against a real database.

**The outcomes that matter most:** a suggestion is never made between two candidates; a width is
never taken from geometry, only a reading is named; and a link that was withdrawn or corrected, or
whose part or reading was taken back, is not read.

Coordinates are stored page space, where `y` grows down the page. The drawing used throughout is a
countertop across `0.20..0.60` over a filler, a cabinet and a filler drawn end to end beneath it.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterator, Sequence
from decimal import Decimal
from fractions import Fraction
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from alembic import command
from app.audit.events import AuditEvent
from app.auth import Principal, Role
from app.db.session import session_factory
from app.models import (
    CanonicalObservation,
    DrawingView,
    EvidenceSupportingCandidate,
    ObservationAssociation,
    Page,
    PartProposal,
    ReadingPart,
)
from app.review.evidence_actions import confirm_evidence
from evidence.canonical import Authority
from rules.semantic_types import DocumentRole, SemanticType
from tests.app.postgres_fixture import alembic_config
from tests.db.test_drawing_models import _code_reading, _page
from tests.review.test_evidence_actions import link_finding_to_observation
from tests.review.test_ledger import _scenario
from units.measurement import Unit
from verdict.operands import EvidenceStatus
from vocabulary.part_kinds import PartKind
from workflow import reading_parts as links
from workflow.parts import (
    PlacedPart,
    confirm_part,
    live_parts_on,
    record_part_proposal,
    withdraw_part,
)
from workflow.reading_parts import (
    REPLACED,
    WITHDRAWN,
    PlacedReading,
    ReadingGeometry,
    confirm_reading_part,
    current_link,
    live_reading_parts,
    readings_on,
    suggest_links,
    withdraw_reading_part,
)

pytest_plugins = ("tests.app.postgres_fixture",)

TOLERANCE = Decimal("0.004")
DRAWING = uuid4()
PAGE = uuid4()
ACTOR = "reviewer@example.com"


def _part(
    kind: PartKind, left: str, right: str, top: str = "0.42", bottom: str = "0.70"
) -> PlacedPart:
    return PlacedPart(
        item_id=uuid4(),
        kind=kind,
        view_id=DRAWING,
        left=Decimal(left),
        right=Decimal(right),
        top=Decimal(top),
        bottom=Decimal(bottom),
    )


def _placed(
    left: str | None,
    right: str | None,
    *,
    geometry: ReadingGeometry | None = ReadingGeometry.LINE,
    unplaced: str | None = None,
    printed: tuple[str, str] | None = None,
) -> PlacedReading:
    """A placed reading. Unless `printed` says where across the page it is printed, a reading on a
    line is printed at the line's middle, and a region is printed where it spans."""
    if printed is None:
        if left is None or right is None:
            printed = ("0.30", "0.32")
        elif geometry is ReadingGeometry.REGION:
            printed = (left, right)
        else:
            middle = (Decimal(left) + Decimal(right)) / 2
            printed = (str(middle - Decimal("0.001")), str(middle + Decimal("0.001")))
    return PlacedReading(
        observation_id=uuid4(),
        page_id=PAGE,
        region=(Decimal(printed[0]), Decimal("0.38"), Decimal(printed[1]), Decimal("0.40")),
        geometry=geometry,
        left=None if left is None else Decimal(left),
        right=None if right is None else Decimal(right),
        unplaced=unplaced,
    )


def _row() -> list[PlacedPart]:
    """A filler, a cabinet and a filler, end to end."""
    return [
        _part(PartKind.FILLER, "0.20", "0.25"),
        _part(PartKind.CABINET, "0.25", "0.55"),
        _part(PartKind.FILLER, "0.55", "0.60"),
    ]


def _suggested(suggestions: Sequence[links.LinkSuggestion]) -> list[UUID | None]:
    return [
        None if suggestion.reading is None else suggestion.reading.observation_id
        for suggestion in suggestions
    ]


# -- the suggestion: no database -------------------------------------------------------------------


def test_each_part_is_suggested_the_reading_whose_line_spans_it() -> None:
    """Every reading is given to every part; each part is suggested the one whose line meets both
    of its ends, and the sentence says how, with the tolerance."""
    row = _row()
    spans = [("0.20", "0.25"), ("0.25", "0.55"), ("0.55", "0.60")]
    readings = [_placed(left, right) for left, right in spans]

    suggestions = suggest_links(row, list(reversed(readings)), edge_tolerance=TOLERANCE)

    assert [suggestion.part for suggestion in suggestions] == row
    assert _suggested(suggestions) == [reading.observation_id for reading in readings]
    for suggestion in suggestions:
        assert suggestion.said.startswith("Its dimension line runs across the page")
        assert "(0.004)" in suggestion.said and suggestion.part.kind.value in suggestion.said
        assert len(suggestion.said) <= 500  # `reading_parts.signal`
        assert suggestion.edge_tolerance == TOLERANCE


def test_a_reading_with_no_line_is_placed_by_the_region_it_is_printed_in() -> None:
    filler = _row()[0]
    region = _placed("0.2010", "0.2490", geometry=ReadingGeometry.REGION)

    (suggestion,) = suggest_links([filler], [region], edge_tolerance=TOLERANCE)

    assert suggestion.reading == region
    assert suggestion.said.startswith("No reading behind it was attached to a dimension line.")
    assert "region it is printed in" in suggestion.said


@pytest.mark.parametrize(
    ("offset", "suggested"),
    [("0.004", True), ("0.0041", False)],
    ids=["within-the-tolerance", "past-the-tolerance"],
)
@pytest.mark.parametrize("end", ["left", "right"])
def test_the_tolerance_decides_each_end(offset: str, suggested: bool, end: str) -> None:
    """A reading whose end lies exactly the tolerance from the part's is suggested; a hair more, at
    either end, and it is not."""
    cabinet = _part(PartKind.CABINET, "0.25", "0.55")
    left = Decimal("0.25") - Decimal(offset) if end == "left" else Decimal("0.25")
    right = Decimal("0.55") + Decimal(offset) if end == "right" else Decimal("0.55")
    reading = _placed(str(left), str(right))

    (suggestion,) = suggest_links([cabinet], [reading], edge_tolerance=TOLERANCE)

    assert (suggestion.reading is not None) is suggested
    assert bool(suggestion.spanning) is suggested


def test_a_part_a_person_added_by_two_ends_is_placed_as_a_box_is() -> None:
    """**A person-added part has a two-point outline** (#882): its top is its bottom. Only the ends
    across the page are compared, so it is suggested its reading exactly as a box would be."""
    added = PlacedPart.from_extent(
        item_id=uuid4(),
        kind=PartKind.FILLER,
        view_id=DRAWING,
        extent={"space": "stored", "points": [["0.25", "0.45"], ["0.20", "0.45"]]},
    )
    assert added.top == added.bottom
    reading = _placed("0.2015", "0.2490")

    (suggestion,) = suggest_links([added], [reading], edge_tolerance=TOLERANCE)

    assert suggestion.reading == reading


def test_two_readings_spanning_one_part_are_left_for_a_person() -> None:
    """A width is never chosen between candidates by the computer: both are offered, neither is
    suggested, and the sentence says why."""
    cabinet = _part(PartKind.CABINET, "0.25", "0.55")
    readings = [_placed("0.25", "0.55"), _placed("0.251", "0.549")]

    (suggestion,) = suggest_links([cabinet], readings, edge_tolerance=TOLERANCE)

    assert suggestion.reading is None
    assert set(suggestion.spanning) == set(readings)
    assert "2 confirmed readings on this drawing span this cabinet" in suggestion.said


def test_one_reading_spanning_two_parts_is_suggested_for_neither() -> None:
    """A countertop over one cabinet with no fillers has the cabinet's ends: the one reading spans
    both, and which it measures is a person's call."""
    top = _part(PartKind.COUNTERTOP, "0.25", "0.55", "0.40", "0.42")
    cabinet = _part(PartKind.CABINET, "0.25", "0.55")
    reading = _placed("0.25", "0.55")

    suggestions = suggest_links([top, cabinet], [reading], edge_tolerance=TOLERANCE)

    assert _suggested(suggestions) == [None, None]
    assert all(suggestion.spanning == (reading,) for suggestion in suggestions)
    assert all("spans another confirmed part" in suggestion.said for suggestion in suggestions)


def test_a_reading_placed_nowhere_spans_nothing() -> None:
    """A reading on a line running down the page, or behind two lines, has no span: it is never
    suggested, however narrow the part."""
    narrow = _part(PartKind.FILLER, "0.300", "0.301")
    readings = [
        _placed(None, None, unplaced="Its dimension line runs down the page."),
        _placed(None, None, geometry=None, unplaced="attached to 2 different dimension lines"),
    ]

    (suggestion,) = suggest_links([narrow], readings, edge_tolerance=TOLERANCE)

    assert suggestion.reading is None and suggestion.spanning == ()
    assert suggestion.said.startswith("No confirmed reading on this drawing")


@pytest.mark.parametrize(
    ("printed", "suggested"),
    [
        (("0.246", "0.30"), True),
        (("0.50", "0.554"), True),
        (("0.2459", "0.30"), False),
        (("0.50", "0.5541"), False),
        (("0.10", "0.12"), False),
    ],
    ids=["left-edge-within", "right-edge-within", "left-edge-past", "right-edge-past", "beside"],
)
def test_a_reading_printed_beyond_its_own_line_is_set_aside(
    printed: tuple[str, str], suggested: bool
) -> None:
    """**Measured on AI_Set_2 page 7**: the association stage attached the number in a note printed
    beside the drawing to the nearest cabinet's dimension line. The line meets the cabinet's ends,
    but the number is printed beyond them, so the line may not be its own: it is set aside, and the
    part says so. Printed within the tolerance of the line's ends, it still counts."""
    cabinet = _part(PartKind.CABINET, "0.25", "0.55")
    reading = _placed("0.25", "0.55", printed=printed)

    (suggestion,) = suggest_links([cabinet], [reading], edge_tolerance=TOLERANCE)

    assert (suggestion.reading == reading) is suggested
    assert ("1 reading was set aside" in suggestion.said) is not suggested


def test_a_region_is_never_set_aside_for_where_it_is_printed() -> None:
    """A reading with no line is placed by where it is printed, so it is always printed over it."""
    filler = _part(PartKind.FILLER, "0.20", "0.25")
    region = _placed("0.2010", "0.2490", geometry=ReadingGeometry.REGION)

    (suggestion,) = suggest_links([filler], [region], edge_tolerance=TOLERANCE)

    assert suggestion.reading == region


def test_the_suggestion_never_carries_a_width() -> None:
    """**Widths always come from readings, never from geometry.** A suggestion names a reading and
    nothing else: there is no field a width could be read from."""
    fields = set(links.LinkSuggestion.__dataclass_fields__)
    assert fields == {"part", "reading", "spanning", "said", "edge_tolerance"}
    assert not {"width", "value", "numerator"} & set(PlacedReading.__dataclass_fields__)


@pytest.mark.parametrize(
    "tolerance",
    [Decimal("NaN"), Decimal("Infinity"), Decimal("-0.001"), 0.004],
    ids=["nan", "infinity", "negative", "float"],
)
def test_a_tolerance_that_removes_the_test_is_refused(tolerance: object) -> None:
    with pytest.raises((ValueError, TypeError)):
        suggest_links(_row(), [], edge_tolerance=tolerance)  # type: ignore[arg-type]


def test_the_tolerance_has_no_default() -> None:
    """Stated configuration, never a number written here."""
    for function in (suggest_links, confirm_reading_part):
        parameter = inspect.signature(function).parameters["edge_tolerance"]
        assert parameter.default is inspect.Parameter.empty
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


# -- placing a reading -----------------------------------------------------------------------------


def _observation_row(polygon: list[list[str]]) -> CanonicalObservation:
    return CanonicalObservation(
        id=uuid4(),
        page_id=PAGE,
        polygon=polygon,
    )


def test_a_reading_is_placed_by_its_one_line_drawn_either_way_round() -> None:
    row = _observation_row([["0.3", "0.38"], ["0.32", "0.40"]])
    line = ((Decimal("0.25"), Decimal("0.41")), (Decimal("0.55"), Decimal("0.41")))

    placed = PlacedReading.from_observation(row, [line, line])

    assert placed is not None and placed.geometry is ReadingGeometry.LINE
    assert (placed.left, placed.right) == (Decimal("0.25"), Decimal("0.55"))
    assert placed.region == (Decimal("0.3"), Decimal("0.38"), Decimal("0.32"), Decimal("0.40"))


def test_a_reading_behind_two_lines_or_on_a_vertical_one_is_placed_nowhere() -> None:
    row = _observation_row([["0.3", "0.38"], ["0.32", "0.40"]])
    across = ((Decimal("0.25"), Decimal("0.41")), (Decimal("0.55"), Decimal("0.41")))
    other = ((Decimal("0.25"), Decimal("0.45")), (Decimal("0.55"), Decimal("0.45")))
    down = ((Decimal("0.25"), Decimal("0.10")), (Decimal("0.25"), Decimal("0.41")))

    two = PlacedReading.from_observation(row, [across, other])
    vertical = PlacedReading.from_observation(row, [down])

    assert two is not None and two.geometry is None and two.left is None
    assert "2 different dimension lines" in (two.unplaced or "")
    assert vertical is not None and vertical.left is None
    assert "height, not a width" in (vertical.unplaced or "")


@pytest.mark.parametrize(
    "polygon",
    [[], [["0.3"]], [["0.3", "NaN"]], [["0.3", "a quarter"]], "not a list"],
    ids=["empty", "one-coordinate", "nan", "not-a-number", "not-a-list"],
)
def test_a_reading_whose_region_is_not_a_box_is_on_no_drawing(polygon: object) -> None:
    row = _observation_row(polygon)  # type: ignore[arg-type]
    assert PlacedReading.from_observation(row, []) is None


# -- the decision: against a real database ---------------------------------------------------------


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


#: The drawing's region on its page.
REGION = {
    "space": "stored",
    "points": [["0.10", "0.10"], ["0.90", "0.10"], ["0.90", "0.90"], ["0.10", "0.90"]],
}


def _box(left: str, right: str, top: str, bottom: str) -> list[tuple[Decimal, Decimal]]:
    return [
        (Decimal(left), Decimal(top)),
        (Decimal(right), Decimal(top)),
        (Decimal(right), Decimal(bottom)),
        (Decimal(left), Decimal(bottom)),
    ]


def _confirmed(
    session: Session, view: DrawingView, kind: PartKind, left: str, right: str
) -> tuple[UUID, UUID]:
    """A part a person confirmed: its suggestion and its item."""
    box = _box(left, right, "0.42", "0.70")
    proposal = record_part_proposal(
        session,
        drawing_view_id=view.id,
        kind=kind,
        extent=box,
        defining_line=(box[0], box[1]),
        code_as_printed=None,
        code_candidate_id=None,
        reason="a dimension on the vendor's drawing",
        source="test",
        source_version="1",
    )
    confirmation = confirm_part(session, proposal=proposal, kind=kind, code=None, actor=ACTOR)
    assert confirmation.drawing_item_id is not None
    return proposal.id, confirmation.drawing_item_id


def _reading(
    session: Session,
    page: Page,
    *,
    lines: Sequence[tuple[str, str, str, str]] = (),
    polygon: tuple[str, str, str, str] = ("0.30", "0.38", "0.34", "0.40"),
    role: DocumentRole = DocumentRole.SHOP,
    conflicting: tuple[str, str, str, str] | None = None,
) -> UUID:
    """A reading a person confirmed, each of `lines` attached to a reading behind it, and a
    `conflicting` reading attached to a line of its own."""
    left, top, right, bottom = polygon
    value = Fraction(18)
    observation = CanonicalObservation(
        document_version_id=page.document_version_id,
        page_id=page.id,
        document_role=role.value,
        polygon=[[left, top], [right, top], [right, bottom], [left, bottom]],
        coordinate_space="stored",
        semantic_type=SemanticType.CABINET_WIDTH.value,
        value_numerator=value.numerator,
        value_denominator=value.denominator,
        unit=Unit.INCH.value,
        status=EvidenceStatus.HUMAN_CONFIRMED.value,
        authority=Authority.AUTHORITATIVE.value,
        evidence_crop_uri=None,
    )
    session.add(observation)
    session.flush()
    behind = [("primary", line) for line in lines]
    if conflicting is not None:
        behind.append(("conflicting", conflicting))
    for role_behind, (start_x, start_y, end_x, end_y) in behind:
        candidate = _code_reading(session, page, '18"')
        session.add(
            EvidenceSupportingCandidate(
                canonical_observation_id=observation.id, candidate_id=candidate.id, role=role_behind
            )
        )
        session.add(
            ObservationAssociation(
                candidate_id=candidate.id,
                extraction_run_id=candidate.extraction_run_id,
                start_x=start_x,
                start_y=start_y,
                end_x=end_x,
                end_y=end_y,
                signals=["the nearest dimension line"],
            )
        )
    session.flush()
    return observation.id


class Drawing:
    """The drawing above on a real page: its parts confirmed, a reading on each one's line, and a
    reading of the countertop's overall."""

    def __init__(self, session: Session) -> None:
        self.page = _page(session)
        self.view = DrawingView(page_id=self.page.id, tag="D", region=REGION)
        session.add(self.view)
        session.flush()
        self.top_proposal, self.top = _confirmed(
            session, self.view, PartKind.COUNTERTOP, "0.20", "0.60"
        )
        spans = [("0.20", "0.25"), ("0.25", "0.55"), ("0.55", "0.60")]
        kinds = [PartKind.FILLER, PartKind.CABINET, PartKind.FILLER]
        confirmed = [
            _confirmed(session, self.view, kind, left, right)
            for kind, (left, right) in zip(kinds, spans, strict=True)
        ]
        self.row_proposals = [proposal for proposal, _ in confirmed]
        self.row = [item for _, item in confirmed]
        self.readings = [
            _reading(
                session,
                self.page,
                lines=[(left, "0.70", right, "0.70")],
                polygon=(left, "0.66", str(Decimal(left) + Decimal("0.01")), "0.68"),
            )
            for left, right in spans
        ]
        self.overall = _reading(
            session,
            self.page,
            lines=[("0.60", "0.35", "0.20", "0.35")],
            polygon=("0.38", "0.31", "0.42", "0.33"),
        )


def _read(session: Session) -> list[ReadingPart]:
    return list(session.scalars(live_reading_parts().order_by(ReadingPart.created_at)))


def _count(session: Session, model: type) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


def _confirm(session: Session, reading: UUID, item: UUID) -> ReadingPart:
    return confirm_reading_part(
        session, observation_id=reading, item_id=item, edge_tolerance=TOLERANCE, actor=ACTOR
    )


def test_readings_are_placed_by_their_lines_on_their_own_drawing(session: Session) -> None:
    """The four readings are found, each by its line, and every part is suggested its own; the
    overall is the countertop's, though it is drawn reversed."""
    drawing = Drawing(session)

    placed = readings_on(session, drawing.view)
    parts = live_parts_on(session, drawing.view.id)
    suggestions = {
        suggestion.part.item_id: suggestion
        for suggestion in suggest_links(parts, placed, edge_tolerance=TOLERANCE)
    }

    assert {reading.observation_id for reading in placed} == {*drawing.readings, drawing.overall}
    assert all(reading.geometry is ReadingGeometry.LINE for reading in placed)
    for item, reading in zip(drawing.row, drawing.readings, strict=True):
        found = suggestions[item].reading
        assert found is not None and found.observation_id == reading
    top = suggestions[drawing.top].reading
    assert top is not None and top.observation_id == drawing.overall


def test_only_vendor_readings_printed_inside_the_drawing_are_on_it(session: Session) -> None:
    """Another page's reading, the architect's reading, and one printed past each of the drawing's
    four edges are not on it; nor is a reading a review replaced."""
    drawing = Drawing(session)
    other_page = _page(session, index=1)
    past_each_edge = [
        ("0.05", "0.40", "0.12", "0.42"),
        ("0.85", "0.40", "0.95", "0.42"),
        ("0.30", "0.05", "0.34", "0.12"),
        ("0.30", "0.88", "0.34", "0.95"),
    ]
    elsewhere = [
        _reading(session, other_page),
        _reading(session, drawing.page, role=DocumentRole.ARCH),
        *(_reading(session, drawing.page, polygon=polygon) for polygon in past_each_edge),
    ]
    replaced = _reading(session, drawing.page)
    _replace_in_review(session, replaced)

    found = {reading.observation_id for reading in readings_on(session, drawing.view)}

    assert not set(elsewhere) & found
    assert replaced not in found
    assert {*drawing.readings, drawing.overall} <= found


def test_a_conflicting_readings_line_does_not_place_it(session: Session) -> None:
    """A reading that disagreed with this one says nothing about what it measures: the reading is
    placed by its own line alone."""
    drawing = Drawing(session)
    reading = _reading(
        session,
        drawing.page,
        lines=[("0.25", "0.75", "0.55", "0.75")],
        conflicting=("0.20", "0.75", "0.60", "0.75"),
    )

    (placed,) = (
        found for found in readings_on(session, drawing.view) if found.observation_id == reading
    )

    assert (placed.left, placed.right) == (Decimal("0.25"), Decimal("0.55"))


def test_only_a_confirmation_writes_a_link_with_the_suggestions_sentence(session: Session) -> None:
    """**Done when, 1.** Suggesting writes nothing; confirming writes one row, naming the person,
    with the suggestion's sentence, and audits it."""
    drawing = Drawing(session)
    parts = live_parts_on(session, drawing.view.id)
    suggest_links(parts, readings_on(session, drawing.view), edge_tolerance=TOLERANCE)
    assert _count(session, ReadingPart) == 0

    link = _confirm(session, drawing.readings[1], drawing.row[1])

    assert _read(session) == [link]
    assert (link.drawing_item_id, link.confirmed_by, link.supersedes_id) == (
        drawing.row[1],
        ACTOR,
        None,
    )
    assert link.signal.startswith("Its dimension line runs across the page")
    audited = session.scalars(select(AuditEvent).where(AuditEvent.target_id == link.id)).one()
    assert (audited.actor, audited.target_type) == (ACTOR, "reading_part")


def test_a_withdrawn_link_is_not_read(session: Session) -> None:
    """**Done when, 2.** Its row stays; the withdrawal names no part and replaces it."""
    drawing = Drawing(session)
    link = _confirm(session, drawing.readings[1], drawing.row[1])

    (withdrawal,) = withdraw_reading_part(session, item_id=drawing.row[1], actor=ACTOR)

    assert _read(session) == []
    assert (withdrawal.drawing_item_id, withdrawal.supersedes_id) == (None, link.id)
    assert withdrawal.signal == WITHDRAWN
    assert current_link(session, drawing.readings[1]) == withdrawal
    assert _count(session, ReadingPart) == 2


def test_a_corrected_link_is_read_and_the_one_it_replaced_is_not(session: Session) -> None:
    """**Done when, 2.** The cabinet was linked to the wrong reading; the person confirms the right
    one. A part's width is one reading, so the first is taken back in the same decision."""
    drawing = Drawing(session)
    wrong = _confirm(session, drawing.overall, drawing.row[1])
    assert wrong.signal.startswith("Picked by a person. The suggestion did not hold this reading")

    right = _confirm(session, drawing.readings[1], drawing.row[1])

    assert _read(session) == [right]
    taken_back = current_link(session, drawing.overall)
    assert taken_back is not None and taken_back.drawing_item_id is None
    assert (taken_back.supersedes_id, taken_back.signal) == (wrong.id, REPLACED)


def test_a_reading_moved_to_another_part_leaves_the_first(session: Session) -> None:
    """One live link per reading: linking it to the countertop replaces its link to the cabinet."""
    drawing = Drawing(session)
    first = _confirm(session, drawing.overall, drawing.row[1])

    moved = _confirm(session, drawing.overall, drawing.top)

    assert _read(session) == [moved]
    assert moved.supersedes_id == first.id


@pytest.mark.parametrize("taken_back", ["withdrawn", "corrected"])
def test_a_link_whose_part_was_taken_back_is_not_read(session: Session, taken_back: str) -> None:
    """**Done when, 2.** The part's item stays, and the link to it with it; neither is read."""
    drawing = Drawing(session)
    _confirm(session, drawing.readings[1], drawing.row[1])
    proposal = session.get_one(PartProposal, drawing.row_proposals[1])

    if taken_back == "withdrawn":
        withdraw_part(session, proposal=proposal, actor=ACTOR)
    else:
        confirm_part(session, proposal=proposal, kind=PartKind.FILLER, code=None, actor=ACTOR)

    assert _read(session) == []


def _replace_in_review(session: Session, observation: UUID) -> None:
    """A reviewer confirms the reading in review, which makes a new reading in its place."""
    scenario = _scenario(session)
    link_finding_to_observation(session, scenario.finding_id, observation)
    confirm_evidence(
        session,
        principal=Principal("anant", frozenset({Role.REVIEWER})),
        review_session_id=scenario.review_session_id,
        finding_id=scenario.finding_id,
        observation_id=observation,
    )


def test_a_link_whose_reading_was_replaced_in_review_is_not_read(session: Session) -> None:
    """**Done when, 2.** Once a review replaced the reading, perhaps with a corrected value, a link
    to the old one is not read, and the old one can no longer be linked."""
    drawing = Drawing(session)
    _confirm(session, drawing.readings[1], drawing.row[1])

    _replace_in_review(session, drawing.readings[1])

    assert _read(session) == []
    with pytest.raises(ValueError, match="own drawing"):
        _confirm(session, drawing.readings[1], drawing.row[1])


def test_a_part_two_readings_are_linked_to_is_read_as_having_none(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two people linking different readings to one part at the same moment both succeed, since
    the database holds one link per reading, not per part. Neither is read: reading one of two
    would be choosing a width. Simulated by hiding the first link from the second decision."""
    drawing = Drawing(session)
    _confirm(session, drawing.readings[1], drawing.row[1])
    monkeypatch.setattr(links, "current_links_to", lambda *_: ())

    _confirm(session, drawing.overall, drawing.row[1])

    assert _read(session) == []
    monkeypatch.undo()
    assert len(links.current_links_to(session, drawing.row[1])) == 2


def test_without_a_tolerance_a_person_may_still_pick_and_the_row_says_nothing_was_suggested(
    session: Session,
) -> None:
    drawing = Drawing(session)

    link = confirm_reading_part(
        session,
        observation_id=drawing.readings[0],
        item_id=drawing.row[0],
        edge_tolerance=None,
        actor=ACTOR,
    )

    assert "Nothing was suggested" in link.signal and "GV_RUN_EDGE_TOLERANCE" in link.signal
    assert _read(session) == [link]


@pytest.mark.parametrize(
    ("case", "said"),
    [
        ("no-person", "person"),
        ("a-part-taken-back", "confirmed part"),
        ("a-suggestion-not-a-part", "confirmed part"),
        ("another-drawings-reading", "own drawing"),
        ("not-a-reading", "own drawing"),
    ],
)
def test_the_writer_refuses_what_a_person_could_not_have_meant(
    session: Session, case: str, said: str
) -> None:
    drawing = Drawing(session)
    reading, item, actor = drawing.readings[0], drawing.row[0], ACTOR
    if case == "no-person":
        actor = " \t"
    elif case == "a-part-taken-back":
        withdraw_part(
            session, proposal=session.get_one(PartProposal, drawing.row_proposals[0]), actor=ACTOR
        )
    elif case == "a-suggestion-not-a-part":
        item = drawing.row_proposals[0]
    elif case == "another-drawings-reading":
        reading = _reading(session, _page(session, index=1))
    else:
        reading = uuid4()

    with pytest.raises(ValueError, match=said):
        confirm_reading_part(
            session, observation_id=reading, item_id=item, edge_tolerance=TOLERANCE, actor=actor
        )
    assert _count(session, ReadingPart) == 0


def test_withdrawing_a_part_with_no_link_writes_nothing(session: Session) -> None:
    drawing = Drawing(session)

    with pytest.raises(ValueError, match="no link to withdraw"):
        withdraw_reading_part(session, item_id=drawing.row[0], actor=ACTOR)
    with pytest.raises(ValueError, match="person"):
        withdraw_reading_part(session, item_id=drawing.row[0], actor="")
    assert _count(session, ReadingPart) == 0


def test_a_pick_says_whether_the_suggestion_left_it_open_or_placed_it_nowhere(
    session: Session,
) -> None:
    """The sentence on a link a person picked says why it was not the suggestion: two readings
    spanned the part and the person chose, or the reading's lines disagreed so it spans nothing."""
    drawing = Drawing(session)
    twin = _reading(
        session,
        drawing.page,
        lines=[("0.25", "0.72", "0.55", "0.72")],
        polygon=("0.36", "0.66", "0.38", "0.68"),
    )
    torn = _reading(
        session,
        drawing.page,
        lines=[("0.25", "0.74", "0.55", "0.74"), ("0.20", "0.75", "0.60", "0.75")],
        polygon=("0.40", "0.66", "0.42", "0.68"),
    )

    left_open = _confirm(session, twin, drawing.row[1])
    nowhere = _confirm(session, torn, drawing.row[1])

    assert left_open.signal.startswith("Picked by a person, from what the suggestion left open.")
    assert "2 confirmed readings on this drawing span this cabinet" in left_open.signal
    assert nowhere.signal.startswith("Picked by a person. The suggestion places this reading")
    assert "2 different dimension lines" in nowhere.signal
    assert _read(session) == [nowhere]


def test_a_reading_set_aside_can_still_be_picked_and_the_row_says_why(session: Session) -> None:
    drawing = Drawing(session)
    beside = _reading(
        session,
        drawing.page,
        lines=[("0.25", "0.73", "0.55", "0.73")],
        polygon=("0.12", "0.72", "0.14", "0.74"),
    )

    link = _confirm(session, beside, drawing.row[1])

    assert link.signal.startswith("Picked by a person. The suggestion set this reading aside")
    assert _read(session) == [link]


def test_a_drawing_whose_region_is_not_stored_points_has_no_readings(session: Session) -> None:
    """There is no box to place a reading in, so none is on it, rather than every one on the page."""
    drawing = Drawing(session)
    boxless = DrawingView(
        page_id=drawing.page.id, tag="E", region={"space": "pdf_points", "polygon": [0, 0, 1, 1]}
    )
    session.add(boxless)
    session.flush()

    assert readings_on(session, boxless) == ()
    assert len(readings_on(session, drawing.view)) == 4
