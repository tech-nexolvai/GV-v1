"""Where the architect's dimension is on the drawing (#1066).

`architect_location` (app/review/row_location.py) outlines one architect span the architect reader
stored (`workflow/architect_reader.py`): from its left tick to its right tick, with its printed
label and the band the reader accepts the label in around its line, in the stored space of the
candidate's own page. The countertop results (`architect.compared[].architect_location`) and the
reviewer's pairing picker (`spans[].location`) show it. Read-only; null whenever the stored geometry
cannot be used exactly. Synthetic values only (`tests/extraction/architect/combined_sheet.py`).
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event, select
from sqlalchemy.orm import Session

from alembic import command
from app.api import visual_countertops
from app.api.dependencies import get_session
from app.api.slot_rows import get_architect_pairing
from app.auth import Principal, Role, authenticate
from app.config import Settings
from app.db.session import session_factory
from app.evidence.sides import reading_transform
from app.main import API_PREFIX, create_app
from app.models import ObservationCandidate, Page, ReviewAction
from app.models.evidence import ArchitectPairingRecord
from app.models.runs import ExtractionRun
from app.models.verdicts import Finding, VerdictInput
from app.review.row_location import (
    ARCHITECT_LABEL_REACH_PT,
    RowLocation,
    architect_location,
    architect_locations,
)
from evidence.coordinates import ImagePoint, PdfPoint, StoredPoint
from extraction.architect.reader import MEASURED_ARCHITECT_SETTINGS
from storage.local import LocalStore
from tests.api.test_architect_pairing import Row
from tests.app.postgres_fixture import alembic_config
from tests.extraction.architect.combined_sheet import (
    ARCH_RECT,
    LEFT,
    MIDDLE,
    OUTLET,
    RIGHT,
    combined_sheet,
)
from tests.workflow.test_architect_reader import _architect_rows, _by_text, _extract, _upload
from tests.workflow.test_architect_row_evidence import (
    _architect_drawing,
    _architect_value,
    _overall,
    _pairing,
    _piece,
    _run,
    _sealed_rows,
)

pytest_plugins = ("tests.app.postgres_fixture",)


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


@pytest.fixture
def store() -> Iterator[LocalStore]:
    with tempfile.TemporaryDirectory() as directory:
        yield LocalStore(root=Path(directory), ticket_secret=b"a secret only this test knows")


def _corners(location: RowLocation) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    xs = [Decimal(x) for x, _y in location.polygon]
    ys = [Decimal(y) for _x, y in location.polygon]
    return min(xs), min(ys), max(xs), max(ys)


def _covers(location: RowLocation, point: StoredPoint) -> bool:
    left, top, right, bottom = _corners(location)
    return left <= point.x <= right and top <= point.y <= bottom


def _context(session: Session, candidate: ObservationCandidate) -> tuple[Page, ExtractionRun]:
    return (
        session.get_one(Page, candidate.page_id),
        session.get_one(ExtractionRun, candidate.extraction_run_id),
    )


def _stored_pdf(session: Session, candidate: ObservationCandidate, x: Decimal, y: Decimal):  # type: ignore[no-untyped-def]
    page, run = _context(session, candidate)
    transform = reading_transform(page, run)
    assert transform is not None
    return transform.to_stored(transform.to_image(PdfPoint(x, y)))


def _stored_label(session: Session, candidate: ObservationCandidate) -> list[StoredPoint]:
    page, run = _context(session, candidate)
    transform = reading_transform(page, run)
    assert transform is not None
    return [transform.to_stored(ImagePoint(int(x), int(y))) for x, y in candidate.polygon]


def test_the_reach_is_the_architect_readers_own() -> None:
    """The control plane may not import the reader, so the reach is repeated; this keeps it equal."""
    assert ARCHITECT_LABEL_REACH_PT == MEASURED_ARCHITECT_SETTINGS.label_reach_pt


# --- the outline, on the real reader's stored spans ------------------------------------------------

#: The combined sheet's rows on the page, in PDF space: the drawing is pasted at 1:1 at ARCH_RECT.
_X, _Y = Decimal(ARCH_RECT[0]), Decimal(ARCH_RECT[1])
_LINES = {
    "3' - 4\"": (_Y + 40, LEFT, MIDDLE),
    "2' - 2\"": (_Y + 40, MIDDLE, RIGHT),
    "1' - 5\"": (_Y + 25, LEFT, OUTLET),
    "2' - 7\"": (_Y + 10, 200, 236),
}


@pytest.mark.parametrize("text", sorted(_LINES))
def test_a_stored_span_is_outlined_from_tick_to_tick_over_its_line_and_label(
    session: Session, store: LocalStore, text: str
) -> None:
    _extract(session, store, _upload(session, store, combined_sheet()))
    candidate = _by_text(_architect_rows(session))[text]
    line_y, left_tick, right_tick = _LINES[text]

    location = architect_locations(session, (candidate.id,))[candidate.id]

    assert location.page_id == candidate.page_id
    assert location.document_version_id == candidate.document_version_id
    assert location.page_number == session.get_one(Page, candidate.page_id).index + 1
    assert location.coordinate_space == "stored"
    for x in (_X + Decimal(str(left_tick)), _X + Decimal(str(right_tick))):
        assert _covers(location, _stored_pdf(session, candidate, x, line_y)), (text, x)
    for corner in _stored_label(session, candidate):
        assert _covers(location, corner), (text, corner)
    # Tick to tick, not the whole drawing: nothing left of the left tick or right of the label.
    left, _top, right, _bottom = _corners(location)
    outside_left = _stored_pdf(session, candidate, _X + Decimal(str(left_tick)) - 3, line_y)
    assert outside_left.x < left
    assert right < _stored_pdf(session, candidate, _X + Decimal(str(right_tick)) + 30, line_y).x


def test_an_outline_stays_off_the_row_below(session: Session, store: LocalStore) -> None:
    """Row 1 (`3' - 4"`) is 15 pt above row 2: its outline holds its own line, not the next one."""
    _extract(session, store, _upload(session, store, combined_sheet()))
    candidate = _by_text(_architect_rows(session))["3' - 4\""]

    location = architect_locations(session, (candidate.id,))[candidate.id]

    below = _stored_pdf(session, candidate, _X + LEFT + 5, _Y + 25)
    assert not _covers(location, below)


# --- null, never guessed --------------------------------------------------------------------------


def _stored_span(session: Session) -> ObservationCandidate:
    package_id, anchors = _sealed_rows(session)
    del package_id
    run = _architect_drawing(session, anchors[0])
    return _architect_value(session, run, anchors[0], "3' - 7\"")


def _variant(candidate: ObservationCandidate, **changes: object) -> ObservationCandidate:
    """An unsaved copy with some stored fields changed: stored candidates are append-only."""
    fields = {
        "id": candidate.id,
        "document_version_id": candidate.document_version_id,
        "page_id": candidate.page_id,
        "extraction_run_id": candidate.extraction_run_id,
        "raw_text": candidate.raw_text,
        "polygon": candidate.polygon,
        "coordinate_space": candidate.coordinate_space,
        "ambiguity_flags": list(candidate.ambiguity_flags or ()),
    }
    fields.update(changes)
    return ObservationCandidate(**fields)


def _without_ticks(candidate: ObservationCandidate, *flags: str) -> list[str]:
    return [
        flag for flag in candidate.ambiguity_flags or () if not flag.startswith("arch-ticks:")
    ] + list(flags)


def test_a_synthetic_span_is_placed_on_its_page(session: Session) -> None:
    candidate = _stored_span(session)

    location = architect_location(candidate, *_context(session, candidate))

    assert location is not None
    # `_architect_value`: ticks at 110 and 170 pt; the label's box (300, 200)-(400, 230) at 150 dpi.
    for corner in _stored_label(session, candidate):
        assert _covers(location, corner)
    label_top = Decimal(792) - Decimal(200) * 72 / 150
    for x in (Decimal(110), Decimal(170)):
        assert _covers(location, _stored_pdf(session, candidate, x, label_top))


@pytest.mark.parametrize(
    "flags",
    [
        pytest.param((), id="no ticks"),
        pytest.param(("arch-ticks:abc:170",), id="not a number"),
        pytest.param(("arch-ticks:110",), id="one tick"),
        pytest.param(("arch-ticks:110:170:200",), id="three ticks"),
        pytest.param(("arch-ticks:170:110",), id="reversed"),
        pytest.param(("arch-ticks:110:110",), id="no width"),
        pytest.param(("arch-ticks:NaN:170",), id="not finite"),
        pytest.param(("arch-ticks:-Infinity:170",), id="infinite"),
        pytest.param(("arch-ticks:110:170", "arch-ticks:120:180"), id="two tick records"),
        pytest.param(("arch-ticks:110:99999",), id="off the page"),
    ],
)
def test_malformed_ticks_give_no_location(session: Session, flags: tuple[str, ...]) -> None:
    candidate = _stored_span(session)
    page, run = _context(session, candidate)

    changed = _variant(candidate, ambiguity_flags=_without_ticks(candidate, *flags))

    assert architect_location(changed, page, run) is None


@pytest.mark.parametrize(
    ("polygon", "space"),
    [
        pytest.param(None, "image", id="no polygon"),
        pytest.param([[300, 200], [400, 200], [400, 230]], "image", id="three corners"),
        pytest.param([[300, 200], [400, 200], [400, 230], ["x", 230]], "image", id="not a number"),
        pytest.param([[300.5, 200], [400, 200], [400, 230], [300, 230]], "image", id="not pixels"),
        pytest.param([[300, 200], [300, 200], [300, 200], [300, 200]], "image", id="a point"),
        pytest.param([[0, 0], [1, 0], [1, 1], [0, 1]], "stored", id="another space"),
    ],
)
def test_a_malformed_label_box_gives_no_location(
    session: Session, polygon: object, space: str
) -> None:
    candidate = _stored_span(session)
    page, run = _context(session, candidate)

    changed = _variant(candidate, polygon=polygon, coordinate_space=space)

    assert architect_location(changed, page, run) is None


def test_no_recorded_transform_gives_no_location(session: Session) -> None:
    candidate = _stored_span(session)
    page, run = _context(session, candidate)
    no_dpi = ExtractionRun(id=run.id, extractor=run.extractor, dpi=None)
    no_crop = Page(
        id=page.id,
        document_version_id=page.document_version_id,
        index=page.index,
        rotation=page.rotation,
        media_box=page.media_box,
        crop_box=None,
    )

    assert architect_location(candidate, page, no_dpi) is None
    assert architect_location(candidate, no_crop, run) is None


def test_a_candidate_from_another_route_gives_no_location(session: Session) -> None:
    candidate = _stored_span(session)
    page, run = _context(session, candidate)
    slot_route = ExtractionRun(id=run.id, extractor="slot-reader", dpi=run.dpi)

    assert architect_location(candidate, page, slot_route) is None


def test_a_candidate_on_another_page_gives_no_location(session: Session) -> None:
    candidate = _stored_span(session)
    page, run = _context(session, candidate)
    other = session.scalars(select(Page).where(Page.id != page.id)).first()
    assert other is not None

    assert architect_location(candidate, other, run) is None


# --- the pairing picker ---------------------------------------------------------------------------


def test_the_pairing_picker_places_each_span_it_offers(session: Session) -> None:
    row = Row(session)
    # `Row` stores its spans with no ticks (and one-pixel labels): none of them can be placed.
    shown = get_architect_pairing(
        row.principal, session, row.project_id, row.package_id, row.anchor.id
    )
    assert [span.location for span in shown.spans] == [None, None, None]

    placed = _variant(
        row.cabinet,
        id=None,
        raw_text="3' - 0\"",
        value_numerator=36,
        value_denominator=1,
        unit="in",
        polygon=[[300, 200], [400, 200], [400, 230], [300, 230]],
        ambiguity_flags=[
            *(f for f in row.cabinet.ambiguity_flags if not f.startswith("arch-slot:")),
            "arch-slot:3",
            "arch-ticks:110:170",
        ],
    )
    session.add(placed)
    session.commit()

    shown = get_architect_pairing(
        row.principal, session, row.project_id, row.package_id, row.anchor.id
    )
    spans = {span.candidate_id: span for span in shown.spans}
    location = spans[placed.id].location
    assert location is not None
    assert location == architect_location(placed, *_context(session, placed))
    assert location.page_id == row.anchor.page_id
    assert spans[row.cabinet.id].location is None


# --- the countertop results -----------------------------------------------------------------------


def _client(session: Session, project_id: UUID) -> TestClient:
    app = create_app(Settings(database_url="postgresql+psycopg://gv:gv@localhost:5433/gv"))
    app.dependency_overrides[authenticate] = lambda: Principal(
        id="reviewer", roles=frozenset({Role.REVIEWER}), projects=frozenset({project_id})
    )
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


def _project_of(session: Session, package_id: UUID) -> UUID:
    from app.models import Package

    return session.get_one(Package, package_id).project_id


def _results(session: Session, package_id: UUID) -> dict[str, dict]:  # type: ignore[type-arg]
    project = _project_of(session, package_id)
    response = _client(session, project).get(
        f"{API_PREFIX}/projects/{project}/packages/{package_id}/countertop-results"
    )
    assert response.status_code == 200, response.text
    return {item["row_id"]: item for item in response.json()["items"]}


def test_a_compared_dimension_is_placed_where_the_check_read_it(
    session: Session, tmp_path: Path
) -> None:
    """Two spans print the same value in different places: the compared one is outlined, found
    through the check's recorded operand, never by matching values."""
    package_id, anchors = _sealed_rows(session)
    run = _architect_drawing(session, anchors[0])
    elsewhere = _architect_value(
        session, run, anchors[0], "3' - 7\"", slot=1, box=(700, 500, 800, 530)
    )
    overall = _architect_value(session, run, anchors[0], "3' - 7\"", slot=0)
    _run(session, package_id, tmp_path, {anchors[0]: _pairing(_overall(overall))})

    compared = _results(session, package_id)[str(anchors[0])]["architect"]["compared"]

    assert len(compared) == 1
    expected = architect_location(overall, *_context(session, overall))
    assert expected is not None
    assert compared[0]["architect_location"] == expected.model_dump(mode="json")
    assert expected != architect_location(elsewhere, *_context(session, elsewhere))


def test_a_compared_dimension_with_no_stored_ticks_has_no_location(
    session: Session, tmp_path: Path
) -> None:
    package_id, anchors = _sealed_rows(session)
    run = _architect_drawing(session, anchors[0])
    stored = _architect_value(session, run, anchors[0], "3' - 6\"", slot=1)
    overall = _variant(
        stored,
        id=None,
        value_numerator=43,
        value_denominator=1,
        unit="in",
        raw_text="3' - 7\"",
        ambiguity_flags=_without_ticks(stored),
    )
    session.add(overall)
    session.flush()
    _run(session, package_id, tmp_path, {anchors[0]: _pairing(_overall(overall))})

    compared = _results(session, package_id)[str(anchors[0])]["architect"]["compared"]

    assert [pair["architect_location"] for pair in compared] == [None]


def _snapshot(session: Session) -> tuple[object, ...]:
    session.expire_all()
    return (
        sorted((str(f.id), f.outcome, f.reason) for f in session.scalars(select(Finding))),
        session.query(VerdictInput).count(),
        session.query(ArchitectPairingRecord).count(),
        session.query(ReviewAction).count(),
        sorted(
            (str(c.id), tuple(c.ambiguity_flags or ()), c.value_numerator)
            for c in session.scalars(select(ObservationCandidate))
        ),
    )


def test_reading_the_compared_locations_changes_no_outcome_or_decision(
    session: Session, tmp_path: Path
) -> None:
    package_id, anchors = _sealed_rows(session)
    run = _architect_drawing(session, anchors[0])
    overall = _architect_value(session, run, anchors[0], "3' - 7\"")
    _run(session, package_id, tmp_path, {anchors[0]: _pairing(_overall(overall))})
    session.commit()
    before = _snapshot(session)
    first = _results(session, package_id)

    assert _results(session, package_id) == first
    assert _snapshot(session) == before


def test_reading_the_offered_locations_changes_no_pairing(session: Session) -> None:
    row = Row(session, source="code+ais", status="paired")
    before = _snapshot(session)

    for _ in range(2):
        get_architect_pairing(row.principal, session, row.project_id, row.package_id, row.anchor.id)

    assert _snapshot(session) == before


def _statements(session: Session, package_id: UUID) -> int:
    count = 0

    def counted(*_args: object) -> None:
        nonlocal count
        count += 1

    event.listen(session.bind, "before_cursor_execute", counted)
    try:
        _results(session, package_id)
    finally:
        event.remove(session.bind, "before_cursor_execute", counted)
    return count


@pytest.mark.parametrize("compared_rows", [1, 2])
def test_placing_the_compared_dimensions_costs_two_statements_whatever_the_rows(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compared_rows: int
) -> None:
    package_id, anchors = _sealed_rows(session)
    first = _architect_drawing(session, anchors[0])
    second = _architect_drawing(session, anchors[1])
    pairings = {
        anchors[0]: _pairing(_piece(_architect_value(session, first, anchors[0], "1' - 8\""), 0)),
        anchors[1]: _pairing(_piece(_architect_value(session, second, anchors[1], "1' - 9\""), 0)),
    }
    _run(session, package_id, tmp_path, dict(list(pairings.items())[:compared_rows]))
    items = _results(session, package_id)
    placed = [
        pair["architect_location"]
        for anchor in anchors.values()
        for pair in items[str(anchor)]["architect"]["compared"]
    ]
    assert len(placed) == compared_rows and None not in placed
    with_locations = _statements(session, package_id)

    monkeypatch.setattr(visual_countertops, "_architect_positions", lambda _session, _ids: {})

    assert with_locations - _statements(session, package_id) == 2
