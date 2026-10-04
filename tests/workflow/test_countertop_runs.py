"""The run suggested beneath a countertop, and the one writer of a confirmed run (#893).

Verification for `workflow/countertop_runs.py`. The first half needs no database: `propose_run` reads
only the parts it is given. The second half writes through `confirm_countertop_run` and
`withdraw_countertop_run` and reads back through `live_run_rows`, against a real database.

**The outcomes that matter most:** a wall cabinet drawn above the countertop is never suggested; the
order is the drawing's, never the order parts were given in; and a run that was withdrawn, corrected,
or lost a part a person took back is not read at all, not partly.

Coordinates are stored page space, where `y` grows down the page. The drawing used throughout is a
countertop across `0.20..0.60` with its top at `0.40`, three parts beneath it end to end, and a wall
cabinet above it.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from alembic import command
from app.audit.events import AuditEvent
from app.db.session import session_factory
from app.models import (
    CountertopRun,
    CountertopRunDecision,
    DrawingView,
    PartProposal,
)
from app.verdicts.rulebook import snapshot_store
from tests.app.postgres_fixture import alembic_config
from tests.db.test_drawing_models import _page, _view
from tests.workflow.test_stages import _publish_rulebook
from vocabulary.part_kinds import PartKind
from workflow import countertop_runs as runs
from workflow.countertop_runs import (
    RUN_PROPOSAL_SOURCE,
    PlacedPart,
    confirm_countertop_run,
    current_run_decision,
    live_run_rows,
    propose_run,
    withdraw_countertop_run,
)
from workflow.parts import confirm_part, record_part_proposal, withdraw_part

pytest_plugins = ("tests.app.postgres_fixture",)

TOLERANCE = Decimal("0.004")
DRAWING = uuid4()
ACTOR = "reviewer@example.com"


def _part(
    kind: PartKind,
    left: str,
    right: str,
    top: str,
    bottom: str,
    *,
    view: UUID = DRAWING,
    item: UUID | None = None,
) -> PlacedPart:
    return PlacedPart(
        item_id=item or uuid4(),
        kind=kind,
        view_id=view,
        left=Decimal(left),
        right=Decimal(right),
        top=Decimal(top),
        bottom=Decimal(bottom),
    )


def _top() -> PlacedPart:
    return _part(PartKind.COUNTERTOP, "0.20", "0.60", "0.40", "0.42")


def _base_row() -> list[PlacedPart]:
    """A filler, a cabinet and a filler, end to end beneath the countertop."""
    return [
        _part(PartKind.FILLER, "0.20", "0.25", "0.42", "0.70"),
        _part(PartKind.CABINET, "0.25", "0.55", "0.42", "0.70"),
        _part(PartKind.FILLER, "0.55", "0.60", "0.42", "0.70"),
    ]


def _wall_cabinet() -> PlacedPart:
    """Confirmed as a cabinet, and drawn wholly above the countertop."""
    return _part(PartKind.CABINET, "0.25", "0.55", "0.10", "0.30")


def _members(proposal: runs.RunProposal) -> list[UUID]:
    return [member.part.item_id for member in proposal.members]


# -- the suggestion: no database -------------------------------------------------------------------


def test_a_wall_cabinet_above_the_top_is_never_suggested() -> None:
    """**Done when, 2.** The wall cabinet lies along the countertop and is a confirmed cabinet, so
    only the below-the-top filter keeps it out. It is listed as left out, with why."""
    top, row, wall = _top(), _base_row(), _wall_cabinet()

    proposal = propose_run(top, [wall, *row], edge_tolerance=TOLERANCE)

    assert _members(proposal) == [part.item_id for part in row]
    assert wall.item_id not in _members(proposal)
    assert [entry.part.item_id for entry in proposal.left_out] == [wall.item_id]
    assert "higher up the page than the countertop's top" in proposal.left_out[0].reason
    assert proposal.warnings == ()


def test_the_run_is_ordered_across_the_drawing_whatever_order_the_parts_come_in() -> None:
    """`CAB-FILLER-001` compares runs position by position, and click order was a false PASS
    (#794). Every order of the same parts gives the same run."""
    top, row = _top(), _base_row()
    orders = [row, list(reversed(row)), [row[1], row[2], row[0]]]

    proposals = [propose_run(top, order, edge_tolerance=TOLERANCE) for order in orders]

    assert {tuple(_members(proposal)) for proposal in proposals} == {
        tuple(part.item_id for part in row)
    }
    assert [member.position for member in proposals[0].members] == [0, 1, 2]


def test_each_member_says_why_it_was_suggested() -> None:
    proposal = propose_run(_top(), _base_row(), edge_tolerance=TOLERANCE)

    for member in proposal.members:
        assert member.part.kind.value in member.signal
        assert "same drawing" in member.signal
        assert "not drawn above the top" in member.signal
        assert len(member.signal) <= 500  # `countertop_runs.signal`


def test_only_cabinets_and_fillers_on_the_same_drawing_along_the_countertop_are_suggested() -> None:
    """Another drawing's parts, another countertop, and a part beyond the countertop's ends by more
    than the tolerance are none of them in the run."""
    top, row = _top(), _base_row()
    elsewhere = _part(PartKind.CABINET, "0.25", "0.55", "0.42", "0.70", view=uuid4())
    second_top = _part(PartKind.COUNTERTOP, "0.20", "0.60", "0.45", "0.46")
    beyond = _part(PartKind.FILLER, "0.60", "0.65", "0.42", "0.70")

    proposal = propose_run(
        top, [*row, elsewhere, second_top, beyond, top], edge_tolerance=TOLERANCE
    )

    assert _members(proposal) == [part.item_id for part in row]
    assert proposal.left_out == ()


@pytest.mark.parametrize(
    ("offset", "suggested"),
    [("0.004", True), ("0.0041", False)],
    ids=["within-the-tolerance", "past-the-tolerance"],
)
def test_the_tolerance_decides_both_the_ends_and_the_top(offset: str, suggested: bool) -> None:
    """A part past the countertop's right end, and a part reaching above its top, by exactly the
    tolerance are suggested; a hair more and neither is."""
    top = _top()
    past_the_end = _part(
        PartKind.FILLER, "0.55", str(Decimal("0.60") + Decimal(offset)), "0.42", "0.70"
    )
    above_the_top = _part(
        PartKind.CABINET, "0.20", "0.55", str(Decimal("0.40") - Decimal(offset)), "0.70"
    )

    ends = propose_run(top, [past_the_end], edge_tolerance=TOLERANCE)
    tops = propose_run(top, [above_the_top], edge_tolerance=TOLERANCE)

    assert bool(ends.members) is suggested
    assert bool(tops.members) is suggested
    # Past the end is not along the countertop at all; above the top is along it and left out.
    assert ends.left_out == ()
    assert bool(tops.left_out) is not suggested


def test_a_part_drawn_from_the_same_extension_lines_as_the_countertop_is_below_it() -> None:
    """On the one AI_Set_2 drawing with a countertop suggested over a row, the end fillers' outlines
    start at the countertop's own top: their extension lines are the same strokes. Equal is below.
    """
    top = _top()
    filler = _part(PartKind.FILLER, "0.20", "0.25", "0.40", "0.42")

    assert _members(propose_run(top, [filler], edge_tolerance=TOLERANCE)) == [filler.item_id]


def test_an_overall_drawn_beneath_its_row_suggests_too_few_never_too_many() -> None:
    """The filter's known failure, stated in the module: when the countertop's dimension is drawn
    below the row, the row reaches higher up the page and is left out. The suggestion is short and
    says so; nothing is added to it."""
    top = _part(PartKind.COUNTERTOP, "0.20", "0.60", "0.80", "0.82")
    row = _base_row()

    proposal = propose_run(top, row, edge_tolerance=TOLERANCE)

    assert proposal.members == ()
    assert {entry.part.item_id for entry in proposal.left_out} == {part.item_id for part in row}
    assert proposal.warnings and "No confirmed cabinet or filler" in proposal.warnings[0]


def test_the_countertop_keeps_its_own_extent_and_a_short_run_is_said_to_be_short() -> None:
    """The run is compared against the countertop's extent, which nothing widens or narrows: the
    suggestion holds the countertop exactly as given, and says where the run falls short."""
    top = _top()
    middle_only = [_base_row()[1]]

    proposal = propose_run(top, middle_only, edge_tolerance=TOLERANCE)

    assert proposal.countertop == top
    assert len(proposal.warnings) == 2
    assert "short of the countertop's left end" in proposal.warnings[0]
    assert "short of the countertop's right end" in proposal.warnings[1]


def test_a_gap_and_an_overlap_are_said_and_never_repaired() -> None:
    top = _top()
    gap = [
        _part(PartKind.FILLER, "0.20", "0.25", "0.42", "0.70"),
        _part(PartKind.CABINET, "0.30", "0.60", "0.42", "0.70"),
    ]
    overlap = [
        _part(PartKind.CABINET, "0.20", "0.45", "0.42", "0.70"),
        _part(PartKind.CABINET, "0.40", "0.60", "0.42", "0.70"),
    ]

    with_gap = propose_run(top, gap, edge_tolerance=TOLERANCE)
    with_overlap = propose_run(top, overlap, edge_tolerance=TOLERANCE)

    assert len(with_gap.members) == 2 and len(with_gap.warnings) == 1
    assert "gap between parts 1 and 2" in with_gap.warnings[0]
    assert len(with_overlap.members) == 2 and len(with_overlap.warnings) == 1
    assert "overlap" in with_overlap.warnings[0]


def test_a_run_is_suggested_only_beneath_a_countertop() -> None:
    with pytest.raises(ValueError, match="not one"):
        propose_run(_base_row()[1], _base_row(), edge_tolerance=TOLERANCE)


@pytest.mark.parametrize(
    "tolerance",
    [Decimal("NaN"), Decimal("Infinity"), Decimal("-0.001"), 0.004],
    ids=["nan", "infinity", "negative", "float"],
)
def test_a_tolerance_that_removes_the_tests_is_refused(tolerance: object) -> None:
    with pytest.raises((ValueError, TypeError)):
        propose_run(_top(), _base_row(), edge_tolerance=tolerance)  # type: ignore[arg-type]


def test_the_tolerance_has_no_default() -> None:
    """Stated configuration, never a number written here (#893)."""
    for function in (propose_run, confirm_countertop_run):
        parameter = inspect.signature(function).parameters["edge_tolerance"]
        assert parameter.default is inspect.Parameter.empty
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


@pytest.mark.parametrize(
    "extent",
    [
        {"space": "pdf_points", "points": [["0.1", "0.1"]]},
        {"points": [["0.1", "0.1"]]},
        {"space": "stored", "points": []},
        {"space": "stored", "points": [["0.1", "NaN"]]},
        {"space": "stored", "points": [["0.1", "a quarter"]]},
        {"space": "stored", "points": [["0.1"]]},
    ],
    ids=["other-space", "no-space", "no-points", "nan", "not-a-number", "one-coordinate"],
)
def test_an_outline_that_places_a_part_nowhere_is_refused(extent: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="outline"):
        PlacedPart.from_extent(
            item_id=uuid4(), kind=PartKind.CABINET, view_id=DRAWING, extent=extent
        )


def test_an_outline_is_read_exactly() -> None:
    placed = PlacedPart.from_extent(
        item_id=uuid4(),
        kind=PartKind.FILLER,
        view_id=DRAWING,
        extent={"space": "stored", "points": [["0.3", "0.71"], ["0.1", "0.7000000001"]]},
    )
    assert (placed.left, placed.right) == (Decimal("0.1"), Decimal("0.3"))
    assert (placed.top, placed.bottom) == (Decimal("0.7000000001"), Decimal("0.71"))


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


def _box(left: str, right: str, top: str, bottom: str) -> list[tuple[Decimal, Decimal]]:
    return [
        (Decimal(left), Decimal(top)),
        (Decimal(right), Decimal(top)),
        (Decimal(right), Decimal(bottom)),
        (Decimal(left), Decimal(bottom)),
    ]


def _confirmed(
    session: Session, view: DrawingView, kind: PartKind, box: list[tuple[Decimal, Decimal]]
) -> tuple[PartProposal, UUID]:
    """A part a person confirmed, through the one function that may make its item."""
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
    return proposal, confirmation.drawing_item_id


class Drawing:
    """The drawing above, confirmed part by part, on a real view."""

    def __init__(self, session: Session) -> None:
        self.view = _view(session, _page(session))
        self.top_proposal, self.top = _confirmed(
            session, self.view, PartKind.COUNTERTOP, _box("0.20", "0.60", "0.40", "0.42")
        )
        boxes = [("0.20", "0.25"), ("0.25", "0.55"), ("0.55", "0.60")]
        kinds = [PartKind.FILLER, PartKind.CABINET, PartKind.FILLER]
        confirmed = [
            _confirmed(session, self.view, kind, _box(left, right, "0.42", "0.70"))
            for kind, (left, right) in zip(kinds, boxes, strict=True)
        ]
        self.row_proposals = [proposal for proposal, _ in confirmed]
        self.row = [item for _, item in confirmed]
        _, self.wall = _confirmed(
            session, self.view, PartKind.CABINET, _box("0.25", "0.55", "0.10", "0.30")
        )


def _read(session: Session) -> list[CountertopRun]:
    return list(
        session.scalars(live_run_rows().order_by(CountertopRun.run_id, CountertopRun.position))
    )


def _count(session: Session, model: type) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


def _confirm(session: Session, drawing: Drawing, members: list[UUID]) -> CountertopRunDecision:
    if snapshot_store(session).latest("CT-WIDTH-001") is None:
        _publish_rulebook(session)
    return confirm_countertop_run(
        session,
        countertop_item_id=drawing.top,
        member_item_ids=members,
        edge_tolerance=TOLERANCE,
        actor=ACTOR,
        wall_config="back_left_right",
    )


def test_a_confirmed_run_is_written_in_the_drawings_order_with_its_tolerance(
    session: Session,
) -> None:
    """Named in reverse, written left to right; every row carries the tolerance, the source and the
    person, each suggested member its sentence; the decision is audited."""
    drawing = Drawing(session)

    decision = _confirm(session, drawing, list(reversed(drawing.row)))

    rows = _read(session)
    assert [row.member_item_id for row in rows] == drawing.row
    assert [row.position for row in rows] == [0, 1, 2]
    assert {row.run_id for row in rows} == {decision.run_id}
    assert {row.edge_tolerance for row in rows} == {TOLERANCE}
    assert {row.proposal_source for row in rows} == {RUN_PROPOSAL_SOURCE}
    assert {row.confirmed_by for row in rows} == {ACTOR}
    assert all("not drawn above the top" in row.signal for row in rows)
    audited = session.scalars(select(AuditEvent).where(AuditEvent.target_id == decision.id)).one()
    assert (audited.actor, audited.target_type) == (ACTOR, "countertop_run_decision")


def test_a_part_the_suggestion_left_out_is_written_as_a_persons_addition(session: Session) -> None:
    """A person may correct the suggestion, even to hold a part the filter left out; the row says
    the person added it and why the suggestion had not."""
    drawing = Drawing(session)

    _confirm(session, drawing, [drawing.wall, *drawing.row])

    signals = {row.member_item_id: row.signal for row in _read(session)}
    assert signals[drawing.wall].startswith("Added to the run by a person.")
    assert "higher up the page" in signals[drawing.wall]


def test_a_withdrawn_run_is_not_read(session: Session) -> None:
    """**Done when, 3.** Its rows stay; nothing reads them."""
    drawing = Drawing(session)
    _confirm(session, drawing, drawing.row)

    withdrawn = withdraw_countertop_run(session, countertop_item_id=drawing.top, actor=ACTOR)

    assert _read(session) == []
    assert _count(session, CountertopRun) == 3
    assert withdrawn.run_id is None
    assert current_run_decision(session, drawing.top) == withdrawn


def test_a_corrected_run_is_read_and_the_run_it_replaced_is_not(session: Session) -> None:
    """**Done when, 3.** Confirming again replaces the run; only the latest is read."""
    drawing = Drawing(session)
    first = _confirm(session, drawing, drawing.row)

    second = _confirm(session, drawing, drawing.row[:2])

    rows = _read(session)
    assert {row.run_id for row in rows} == {second.run_id}
    assert [row.member_item_id for row in rows] == drawing.row[:2]
    assert second.supersedes_id == first.id
    assert _count(session, CountertopRun) == 5


@pytest.mark.parametrize("taken_back", ["withdrawn", "corrected"])
def test_a_run_whose_part_was_taken_back_is_not_read_at_all(
    session: Session, taken_back: str
) -> None:
    """**Done when, 3.** Not the other two members either: a run one part short summed as if it were
    whole is short by that part."""
    drawing = Drawing(session)
    _confirm(session, drawing, drawing.row)

    middle = drawing.row_proposals[1]
    if taken_back == "withdrawn":
        withdraw_part(session, proposal=middle, actor=ACTOR)
    else:
        confirm_part(session, proposal=middle, kind=PartKind.FILLER, code=None, actor=ACTOR)

    assert _read(session) == []


@pytest.mark.parametrize("taken_back", ["withdrawn", "corrected"])
def test_a_run_beneath_a_countertop_taken_back_is_not_read(
    session: Session, taken_back: str
) -> None:
    """Corrected, the countertop is a new item with no run of its own: the run confirmed beneath
    the old one is not carried over to it unseen."""
    drawing = Drawing(session)
    _confirm(session, drawing, drawing.row)

    if taken_back == "withdrawn":
        withdraw_part(session, proposal=drawing.top_proposal, actor=ACTOR)
    else:
        corrected = confirm_part(
            session, proposal=drawing.top_proposal, kind=PartKind.COUNTERTOP, code=None, actor=ACTOR
        )
        assert corrected.drawing_item_id is not None
        assert current_run_decision(session, corrected.drawing_item_id) is None

    assert _read(session) == []


def test_two_countertops_runs_are_read_independently(session: Session) -> None:
    """Withdrawing one countertop's run leaves the other's read."""
    drawing = Drawing(session)
    _, other_top = _confirmed(
        session, drawing.view, PartKind.COUNTERTOP, _box("0.20", "0.60", "0.05", "0.06")
    )
    _confirm(session, drawing, drawing.row)
    kept = confirm_countertop_run(
        session,
        countertop_item_id=other_top,
        member_item_ids=[drawing.wall],
        edge_tolerance=TOLERANCE,
        actor=ACTOR,
        wall_config="back_left_right",
    )

    withdraw_countertop_run(session, countertop_item_id=drawing.top, actor=ACTOR)

    assert [row.run_id for row in _read(session)] == [kept.run_id]


def _other_drawing_cabinet(session: Session) -> UUID:
    """A cabinet beneath the same place on the page, but on another drawing."""
    view = _view(session, _page(session, index=1))
    return _confirmed(session, view, PartKind.CABINET, _box("0.25", "0.55", "0.42", "0.70"))[1]


@pytest.mark.parametrize(
    ("case", "said"),
    [
        ("no-parts", "at least one part"),
        ("a-part-twice", "once"),
        ("beneath-a-cabinet", "confirmed countertop"),
        ("the-countertop-in-its-own-run", "cabinet or filler"),
        ("another-drawing", "cabinet or filler"),
        ("a-part-taken-back", "cabinet or filler"),
        ("no-person", "person"),
    ],
)
def test_the_writer_refuses_what_a_person_could_not_have_meant(
    session: Session, case: str, said: str
) -> None:
    drawing = Drawing(session)
    _publish_rulebook(session)
    countertop, members, actor = drawing.top, list(drawing.row), ACTOR
    if case == "no-parts":
        members = []
    elif case == "a-part-twice":
        members = [drawing.row[0], drawing.row[0]]
    elif case == "beneath-a-cabinet":
        countertop = drawing.row[1]
    elif case == "the-countertop-in-its-own-run":
        members = [drawing.top]
    elif case == "another-drawing":
        members = [_other_drawing_cabinet(session)]
    elif case == "a-part-taken-back":
        withdraw_part(session, proposal=drawing.row_proposals[0], actor=ACTOR)
    else:
        actor = "  "

    with pytest.raises(ValueError, match=said):
        confirm_countertop_run(
            session,
            countertop_item_id=countertop,
            member_item_ids=members,
            edge_tolerance=TOLERANCE,
            actor=actor,
            wall_config="back_left_right",
        )
    assert _count(session, CountertopRunDecision) == 0
    assert _count(session, CountertopRun) == 0
