"""The page stage suggests each vendor drawing's parts and writes no item (#868).

Verification for `DatabaseStages._propose_page_parts` in `workflow/stages.py` and
`workflow/parts.py:record_part_proposal`. The suggesting itself is
`tests/extraction/model/test_part_proposals.py`; this is about what reaches the database.

**The one outcome that matters most is a count of zero:** after the stage has suggested every part,
`drawing_items` and `item_identifiers` are still empty. A suggestion is not a part (#852), and the
guard in `tests/db/test_drawing_models.py` fails if any code but a person's confirmation writes
either table.

The sheet is hand-built, as in `tests/workflow/test_association.py`: one vendor drawing under the
sheet's own `VENDOR'S SHOP DRAWING ELEVATION` label, holding two dimensions drawn end to end, an
overall above them, and a code printed over the first. A reviewer's note carrying a code sits over the
second. **Both codes are invented.**
"""

from __future__ import annotations

import tempfile
import zlib
from collections.abc import Iterator, Sequence
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from alembic import command
from app.db.session import session_factory
from app.models import (
    DrawingItem,
    DrawingView,
    ItemIdentifier,
    ObservationCandidate,
    PackageRevision,
    PartProposal,
    ViewRole,
)
from app.models.runs import ExtractionRun
from extraction.model.part_proposals import PROPOSER_SOURCE, PROPOSER_VERSION
from storage.local import LocalStore
from tests.app.postgres_fixture import alembic_config
from tests.extraction.test_annotations import _free_text, _pdf, _stamp
from tests.extraction.test_reader import MISSING_SPACE
from tests.workflow.test_association import SETTINGS
from tests.workflow.test_association import _revision as _stored_revision
from tests.workflow.test_markup_route import _SilentOcr
from workflow.association import AssociationSettings
from workflow.review import PageResult
from workflow.stages import MARKUP_EXTRACTOR, STAMP_TEXT_EXTRACTOR, DatabaseStages
from workflow.view_roles import confirm_view_role

pytest_plugins = ("tests.app.postgres_fixture",)

HELVETICA = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"

#: The vendor's drawing, in the stamp's own space: `/BBox [100 500 400 700]` placed by `/Matrix` and
#: `/Rect [50 50 350 250]`, so appearance `(x, y)` is page `(x - 50, y - 450)`.
#:
#: * Two dimensions drawn end to end at appearance `y = 550`, from `x = 150` to `250` and `250` to
#:   `350`, their ends crossed by 60-point witness lines: a chain, so two cabinets.
#: * An overall at `y = 640` from `x = 150` to `350`, with witness lines of its own that do not reach
#:   the chain's: a countertop, whose box is its own.
#: * `XQ24` printed at appearance `(170, 600)`, which is over the first cabinet only.
#:
#: Every witness line is 60 points long because `SETTINGS.line_minimum_pt` is 50: a shorter stroke is
#: not line-work, and the dimension it bounds would never be found.
DRAWING = (
    b"1 w 150 550 m 250 550 l S\n"
    b"1 w 250 550 m 350 550 l S\n"
    b"1 w 150 520 m 150 580 l S\n"
    b"1 w 250 520 m 250 580 l S\n"
    b"1 w 350 520 m 350 580 l S\n"
    b"1 w 150 640 m 350 640 l S\n"
    b"1 w 150 610 m 150 670 l S\n"
    b"1 w 350 610 m 350 670 l S\n"
    b"BT /F1 12 Tf 170 600 Td (XQ24) Tj ET\n"
)


#: The same drawing with two things the suggester must not count (#868, admin, 2026-10-03): the base
#: row drawn a second time one point lower down the stamp (`y = 551`, about 0.003 of the page away,
#: inside `SETTINGS.witness_tolerance`), and a row of wall cabinets above everything at `y = 660`,
#: crossed by its own 60-point witness lines.
DRAWING_TWICE_UNDER_WALL_CABINETS = DRAWING + (
    b"1 w 150 551 m 250 551 l S\n"
    b"1 w 250 551 m 350 551 l S\n"
    b"1 w 150 660 m 250 660 l S\n"
    b"1 w 250 660 m 350 660 l S\n"
    b"1 w 150 640 m 150 700 l S\n"
    b"1 w 250 640 m 250 700 l S\n"
    b"1 w 350 640 m 350 700 l S\n"
)


def _drawing_appearance(font_object: int, drawing: bytes = DRAWING) -> bytes:
    compressed = zlib.compress(drawing)
    return (
        b"<< /Type /XObject /Subtype /Form /FormType 1 /BBox [100 500 400 700] "
        b"/Matrix [1 0 0 1 -100 -500] /Resources << /Font << /F1 "
        + str(font_object).encode()
        + b" 0 R >> >> /Filter /FlateDecode /Length "
        + str(len(compressed)).encode()
        + b" >>\nstream\n"
        + compressed
        + b"\nendstream"
    )


def _sheet(drawing: bytes = DRAWING) -> bytes:
    """The label above the drawing, the reviewer's `XQ30L` over the second cabinet (page x
    220..260), and the stamp. Objects 5, 6 and 7 are the annotations, 8 the appearance, 9 its font.
    """
    return _pdf(
        annotations=[
            _free_text("VENDOR'S SHOP DRAWING ELEVATION ", rect=b"[60 262 340 280]"),
            _free_text("XQ30L", rect=b"[220 140 260 150]"),
            _stamp(appearance_object=8),
        ],
        extra_objects=[_drawing_appearance(9, drawing), HELVETICA],
    )


SHEET = _sheet()


def _upgrade(engine: Engine) -> None:
    config = alembic_config()
    config.attributes["database_url"] = engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    _upgrade(postgres_engine)
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
def store() -> Iterator[LocalStore]:
    with tempfile.TemporaryDirectory() as directory:
        yield LocalStore(root=Path(directory), ticket_secret=b"a secret only this test knows")


def _run(
    session: Session,
    store: LocalStore,
    revision: PackageRevision,
    *,
    association: AssociationSettings | None = SETTINGS,
) -> Sequence[PageResult]:
    result = DatabaseStages(
        store=store,
        dpi=150,
        ocr_engine=_SilentOcr(),  # type: ignore[arg-type]
        association=association,
        missing_space=MISSING_SPACE,
    ).extract_pages(session, revision.id)
    session.commit()
    return result


def _extract(
    session: Session,
    store: LocalStore,
    *,
    association: AssociationSettings | None = SETTINGS,
    data: bytes = SHEET,
) -> tuple[PackageRevision, Sequence[PageResult]]:
    revision = _stored_revision(session, store, data=data)
    session.commit()
    return revision, _run(session, store, revision, association=association)


def _proposals(session: Session) -> list[PartProposal]:
    """Cabinets left to right, then the countertop."""
    return sorted(session.scalars(select(PartProposal)), key=lambda row: (row.kind, _xs(row)))


def _count(session: Session, model: type) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


def _xs(row: PartProposal) -> tuple[Decimal, Decimal]:
    xs = [Decimal(x) for x, _ in row.extent["points"]]  # type: ignore[union-attr, misc]
    return min(xs), max(xs)


def _ys(row: PartProposal) -> tuple[Decimal, Decimal]:
    ys = [Decimal(y) for _, y in row.extent["points"]]  # type: ignore[union-attr, misc]
    return min(ys), max(ys)


def _line_y(row: PartProposal) -> Decimal:
    """How far down the page the line that defined a part is drawn."""
    assert row.defining_line is not None
    return Decimal(row.defining_line["points"][0][1])  # type: ignore[index]


def _extractor_of(session: Session, candidate_id: object) -> str:
    candidate = session.get_one(ObservationCandidate, candidate_id)
    run = session.get_one(ExtractionRun, candidate.extraction_run_id)
    return run.extractor


def test_the_stage_suggests_the_vendor_drawings_parts_and_writes_no_item(
    session: Session, store: LocalStore
) -> None:
    """**Done when, 6.** Outcome: two cabinets and a countertop suggested, each on the vendor's
    drawing, by this suggester; and not one `drawing_items` or `item_identifiers` row."""
    _, result = _extract(session, store)

    rows = _proposals(session)
    view = session.scalars(select(DrawingView)).one()

    assert [row.kind for row in rows] == ["cabinet", "cabinet", "countertop"]
    assert {row.drawing_view_id for row in rows} == {view.id}
    assert {(row.source, row.source_version) for row in rows} == {
        (PROPOSER_SOURCE, PROPOSER_VERSION)
    }
    assert all(row.reason.strip() for row in rows)
    assert _count(session, DrawingItem) == 0
    assert _count(session, ItemIdentifier) == 0
    assert [page.payload["part_proposals"] for page in result] == [
        {"cabinets": 2, "countertops": 1, "with_code": 1, "nested_views": 0}
    ]


def test_a_row_drawn_twice_under_wall_cabinets_gives_the_same_suggestions(
    session: Session, store: LocalStore
) -> None:
    """The base row drawn twice, and a row of wall cabinets above it. Outcome: exactly what the
    plain sheet gives, two cabinets on the first stroke of the base row and one countertop, with
    the code still attached: the copy is not a second row and the wall cabinets get nothing."""
    _, result = _extract(session, store, data=_sheet(DRAWING_TWICE_UNDER_WALL_CABINETS))

    first, second, top = _proposals(session)
    rows = {_line_y(first), _line_y(second)}

    assert [page.payload["part_proposals"] for page in result] == [
        {"cabinets": 2, "countertops": 1, "with_code": 1, "nested_views": 0}
    ]
    # One row, and it is the base row: lower on the page (larger stored y) than the countertop's
    # dimension, which is itself below the wall cabinets' row.
    assert len(rows) == 1 and rows.pop() > _line_y(top)
    assert first.code_as_printed == "XQ24"
    assert "Drawn as 2 strokes" in first.reason and "Drawn as 2 strokes" in second.reason
    assert _count(session, DrawingItem) == 0


def test_the_vendors_printed_code_is_kept_with_the_reading_it_came_from(
    session: Session, store: LocalStore
) -> None:
    """The code over the first cabinet is the vendor's, read from the drawing's own text."""
    _extract(session, store)

    first, _, top = _proposals(session)

    assert first.code_as_printed == "XQ24"
    assert first.code_candidate_id is not None
    candidate = session.get_one(ObservationCandidate, first.code_candidate_id)
    assert candidate.raw_text == "XQ24"
    assert _extractor_of(session, first.code_candidate_id) == STAMP_TEXT_EXTRACTOR
    assert top.code_as_printed is None and top.code_candidate_id is None


def test_a_reviewers_note_is_never_a_parts_code(session: Session, store: LocalStore) -> None:
    """The reviewer's `XQ30L` sits over the second cabinet alone, which is exactly where a code
    attaches. It was read, as markup, and still the cabinet has no code: a reviewer's note is a
    reviewer's word about the drawing, and the vendor's drawing is what is being checked."""
    _extract(session, store)

    _, second, _ = _proposals(session)
    notes = session.scalars(
        select(ObservationCandidate).where(ObservationCandidate.raw_text == "XQ30L")
    ).all()

    assert [_extractor_of(session, note.id) for note in notes] == [MARKUP_EXTRACTOR]
    assert second.code_as_printed is None and second.code_candidate_id is None


def test_the_countertop_keeps_its_own_extent(session: Session, store: LocalStore) -> None:
    """Its height is its own witness lines', which do not reach the cabinets', and its line is its
    own: never the cabinets' outline."""
    _extract(session, store)

    first, second, top = _proposals(session)

    assert _xs(top) == (_xs(first)[0], _xs(second)[1])  # it reaches both ends, as drawn
    assert _ys(top) != (min(_ys(first)[0], _ys(second)[0]), max(_ys(first)[1], _ys(second)[1]))
    assert set(_ys(top)).isdisjoint(set(_ys(first)))
    assert top.defining_line is not None and first.defining_line is not None
    assert top.defining_line["points"] != first.defining_line["points"]


def test_the_rows_are_stored_in_stored_space_exactly(session: Session, store: LocalStore) -> None:
    _extract(session, store)

    for row in _proposals(session):
        assert row.extent["space"] == "stored"
        assert len(row.extent["points"]) == 4  # type: ignore[arg-type]
        assert row.defining_line is not None and row.defining_line["space"] == "stored"
        assert len(row.defining_line["points"]) == 2  # type: ignore[arg-type]
        points: list[list[object]] = row.extent["points"]  # type: ignore[assignment]
        assert all(isinstance(value, str) for point in points for value in point)


def test_running_the_stage_twice_suggests_each_part_once(
    session: Session, store: LocalStore
) -> None:
    """A redelivery finds the suggestions it already made rather than asking a person twice."""
    revision, _ = _extract(session, store)
    first = [row.id for row in _proposals(session)]

    _run(session, store, revision)

    assert [row.id for row in _proposals(session)] == first


def test_without_settings_the_step_does_not_run(session: Session, store: LocalStore) -> None:
    """`None` rather than zero: nobody asked for dimensions to be found, which is not "none found"."""
    _, result = _extract(session, store, association=None)

    assert _proposals(session) == []
    assert [page.payload["part_proposals"] for page in result] == [None]


def test_a_person_confirming_the_drawing_is_the_architects_outranks_the_label(
    session: Session, store: LocalStore
) -> None:
    """The label suggests the vendor's; a person said otherwise. Outcome: a re-read suggests nothing
    for it, and still writes no item."""
    revision, _ = _extract(session, store)
    view = session.scalars(select(DrawingView)).one()
    confirm_view_role(session, view=view, role=ViewRole.ARCH, actor="reviewer@example.com")
    session.commit()

    result = _run(session, store, revision)

    assert [page.payload["part_proposals"] for page in result] == [
        {"cabinets": 0, "countertops": 0, "with_code": 0, "nested_views": 0}
    ]
    assert _count(session, DrawingItem) == 0
