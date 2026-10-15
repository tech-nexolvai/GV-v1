"""The depth reader in the worker: what it writes, where it ties each view, and when it runs (#1290).

Verification for `workflow/depth_reader.py` and the call in `DatabaseStages.match`. The reading
itself is `extraction/depth/read.py`'s (tested in `tests/extraction/depth/`); here it is made up, so
no picture is drawn and no model is asked.

Every value here is made up.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.db.session import session_factory
from app.models import ItemViewLink, ObservationCandidate
from extraction.depth.read import DepthReading, ReaderSays, ValueReading, ViewReading
from extraction.depth.view_kind import ViewKind
from extraction.geometry.rows import Box
from extraction.slot_reader.bedrock import CropJob
from extraction.slot_reader.stop import ReadingStop, question_kind
from storage.local import LocalStore
from tests.evidence.test_bridge import _upgrade
from tests.workflow.test_depth_evidence import _Set
from tests.workflow.test_reading_parts import _confirmed
from vocabulary.part_kinds import PartKind
from workflow.depth_evidence import (
    CLAUDE_PAIR,
    CONFIRM_ONLY,
    HELD,
    QUALIFIED,
    SUPPORT_FLAG,
    VIEW_NOTE,
)
from workflow.depth_reader import DepthPlacement, persist_depth_reading, placement_of
from workflow.stages import DatabaseStages

pytest_plugins = ("tests.app.postgres_fixture",)


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    _upgrade(postgres_engine)
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
def store(tmp_path: Path) -> LocalStore:
    return LocalStore(root=tmp_path, ticket_secret=b"synthetic-test-only")


# -- which countertop a view shows -----------------------------------------------------------------


def test_a_view_in_one_drawing_with_one_countertop_is_tied_to_it() -> None:
    drawing, countertop = uuid4(), uuid4()
    assert placement_of([drawing], {drawing: (countertop,)}, [countertop]) == DepthPlacement(
        countertop
    )


def test_a_drawing_with_two_countertops_ties_the_view_to_neither() -> None:
    drawing, first, second = uuid4(), uuid4(), uuid4()
    placed = placement_of([drawing], {drawing: (first, second)}, [first, second])
    assert placed.proposal_id is None and placed.among == (first, second)
    assert "holds 2 countertops" in placed.reason


@pytest.mark.parametrize("holding", [None, [], "two"])
def test_a_view_not_inside_exactly_one_drawing_is_held_on_every_countertop_of_its_page(
    holding: object,
) -> None:
    first, second = uuid4(), uuid4()
    drawings = [uuid4(), uuid4()] if holding == "two" else holding
    placed = placement_of(drawings, {}, [first, second])  # type: ignore[arg-type]
    assert placed.proposal_id is None and placed.among == (first, second)
    assert "exactly one vendor drawing" in placed.reason


# -- what is written -------------------------------------------------------------------------------


def _says(text: str) -> tuple[ReaderSays, ...]:
    return tuple(ReaderSays(model, "value", text) for model in CLAUDE_PAIR)


def _value(
    field: str,
    inches: int | None,
    *,
    usable: bool = True,
    confirmed_by: str | None = "vector-text",
    readers: tuple[ReaderSays, ...] | None = None,
    reason: str = "code agrees",
) -> ValueReading:
    return ValueReading(
        field=field,
        span_number=1,
        readers=readers if readers is not None else _says(f'{inches}"'),
        usable=usable,
        inches=None if inches is None or not usable else Fraction(inches),
        text=None if inches is None else f'{inches}"',
        reason=reason,
        confirmed_by=confirmed_by if usable else None,  # type: ignore[arg-type]
        agreed_inches=None if inches is None else Fraction(inches),
    )


class _Reading:
    """A made-up reading of one page: views at the given boxes, in the page's PDF points."""

    def __init__(self, sheet: _Set) -> None:
        self.sheet = sheet
        self.views: list[tuple[ViewReading, SimpleNamespace]] = []

    def view(
        self,
        values: tuple[ValueReading, ...],
        *,
        kind: ViewKind | None = ViewKind.PLAN,
        box: Box | None = None,
        reason: str = "",
    ) -> None:
        number = len(self.views) + 1
        extent = box or Box(Decimal(100), Decimal(30), Decimal(300), Decimal(70))
        reading = ViewReading(
            page_index=0,
            page_sha256=self.sheet.page.content_hash,
            panel=1,
            view_number=number,
            extent=extent,
            heading="PLAN VIEW",
            code_kind=None,  # type: ignore[arg-type]
            kinds=(),
            kind=kind,
            kind_reason=reason or "both readers and code agree",
            values=values,
            values_reason=reason,
        )
        prepared = SimpleNamespace(
            panel=1,
            view=SimpleNamespace(number=number, extent=extent),
            key=f"p0:v1.{number}",
            label_areas={},
        )
        self.views.append((reading, prepared))

    def persist(self):  # type: ignore[no-untyped-def]
        scale = Decimal(300) / Decimal(72)
        page = SimpleNamespace(
            page_sha256=self.sheet.page.content_hash,
            page_index=0,
            document_version_id=str(self.sheet.page.document_version_id),
            views=[prepared for _reading, prepared in self.views],
            pixels=lambda box: (
                int(box.x0 * scale),
                int(box.top * scale),
                int(box.x1 * scale),
                int(box.bottom * scale),
            ),
        )
        reading = DepthReading(views=tuple(r for r, _p in self.views), questions_asked=0)
        return persist_depth_reading(
            self.sheet.session,
            self.sheet.revision.id,
            self.sheet.run,
            reading,
            [page],  # type: ignore[list-item]
            {(str(self.sheet.page.document_version_id), 0): self.sheet.page},
            dpi=300,
        )


def _rows(session: Session, sheet: _Set) -> list[ObservationCandidate]:
    return list(
        session.scalars(
            select(ObservationCandidate)
            .where(ObservationCandidate.extraction_run_id == sheet.run.id)
            .order_by(ObservationCandidate.created_at)
        )
    )


def _state(row: ObservationCandidate) -> str:
    return next(flag for flag in row.ambiguity_flags if flag.startswith("state:"))


def test_a_value_both_judgments_confirmed_by_printed_text_gets_the_lane_and_two_readers(
    session: Session, store: LocalStore
) -> None:
    sheet = _Set(session, store)
    read = _Reading(sheet)
    read.view((_value("CT010", 24),))

    report = read.persist()

    (link,) = session.scalars(select(ItemViewLink)).all()
    assert link.part_proposal_id == sheet.proposal and link.view_kind == "plan"
    assert link.judged_by == "code+ais" and link.extraction_run_id == sheet.run.id
    parent, *children = _rows(session, sheet)
    assert _state(parent) == QUALIFIED and report.qualified == 1
    assert parent.corroboration_lane == "SECOND_READER" and parent.semantic_guess == "CT010"
    assert (parent.value_numerator, parent.value_denominator, parent.unit) == (24, 1, "in")
    assert {f"item:{sheet.proposal}", f"link:{link.id}", "confirmed-by:vector-text"} <= set(
        parent.ambiguity_flags
    )
    assert len(children) == 2
    assert all(SUPPORT_FLAG in child.ambiguity_flags for child in children)
    assert all(child.corroboration_lane is None for child in children)


def test_drawn_length_alone_is_written_for_a_person_to_confirm(
    session: Session, store: LocalStore
) -> None:
    sheet = _Set(session, store)
    read = _Reading(sheet)
    read.view((_value("CT010", 24, confirmed_by="drawn-length-only"),))

    read.persist()

    (row,) = _rows(session, sheet)
    assert _state(row) == CONFIRM_ONLY and row.corroboration_lane is None
    assert row.value_numerator == 24


def test_a_value_code_held_keeps_no_number_and_its_reason(
    session: Session, store: LocalStore
) -> None:
    sheet = _Set(session, store)
    read = _Reading(sheet)
    read.view((_value("CT007", 4, usable=False, reason="the strip starts at the stone's edge"),))

    read.persist()

    (row,) = _rows(session, sheet)
    assert _state(row) == HELD and row.value_numerator is None and row.corroboration_lane is None
    assert row.review_reason == "the strip starts at the stone's edge"


def test_a_value_only_one_reader_read_gets_no_lane(session: Session, store: LocalStore) -> None:
    sheet = _Set(session, store)
    read = _Reading(sheet)
    read.view((_value("CT010", 24, readers=(ReaderSays(CLAUDE_PAIR[0], "value", '24"'),)),))

    read.persist()

    (row,) = _rows(session, sheet)
    assert _state(row) == CONFIRM_ONLY and row.corroboration_lane is None


def test_a_drawing_with_two_countertops_links_nothing_and_never_gives_a_lane(
    session: Session, store: LocalStore
) -> None:
    sheet = _Set(session, store)
    other, _item = _confirmed(session, sheet.view, PartKind.COUNTERTOP, "0.10", "0.15")
    read = _Reading(sheet)
    read.view((_value("CT010", 24),))

    report = read.persist()

    assert session.scalar(select(func.count()).select_from(ItemViewLink)) == 0
    (row,) = _rows(session, sheet)
    assert _state(row) == CONFIRM_ONLY and row.corroboration_lane is None
    assert {f"unplaced-among:{sheet.proposal}", f"unplaced-among:{other}"} <= set(
        row.ambiguity_flags
    )
    assert "holds 2 countertops" in (row.review_reason or "")
    assert report.unplaced_views == 1


def test_a_view_whose_kind_is_not_settled_says_why(session: Session, store: LocalStore) -> None:
    sheet = _Set(session, store)
    read = _Reading(sheet)
    read.view((), kind=None, reason="the drawing's label says plan; both readers see a front view")

    read.persist()

    (row,) = _rows(session, sheet)
    assert VIEW_NOTE in row.ambiguity_flags and row.value_numerator is None
    assert "both readers see a front view" in (row.review_reason or "")
    assert session.scalar(select(func.count()).select_from(ItemViewLink)) == 0


def test_reading_again_finds_the_link_and_a_changed_kind_ties_to_nothing(
    session: Session, store: LocalStore
) -> None:
    sheet = _Set(session, store)
    first = _Reading(sheet)
    first.view((_value("CT010", 24),))
    first.persist()
    again = _Reading(sheet)
    again.view((_value("CT010", 24),))
    assert again.persist().links_reused == 1

    changed = _Reading(sheet)
    changed.view((_value("CT010", 24),), kind=ViewKind.SECTION)
    changed.persist()

    assert session.scalar(select(func.count()).select_from(ItemViewLink)) == 1
    newest = _rows(session, sheet)[-1]
    assert f"unplaced-among:{sheet.proposal}" in newest.ambiguity_flags
    assert "a person must say which" in (newest.review_reason or "")


# -- when it runs ----------------------------------------------------------------------------------


def test_a_depth_question_is_never_a_page_s_own_reading() -> None:
    job = CropJob("p3:v1.1:kind", CLAUDE_PAIR[0], 3, b"png", depth=object())  # type: ignore[arg-type]
    stop = ReadingStop()
    stop.refused(job, "budget")

    assert question_kind(job) == "depth"
    assert stop.pages_left() == frozenset()


def test_match_reads_depths_once_and_only_when_switched_on(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    sheet = _Set(session, store)
    calls: list[UUID] = []

    def counted(self: DatabaseStages, session: Session, revision_id: UUID) -> dict[str, object]:
        calls.append(revision_id)
        return {"ran": True}

    monkeypatch.setattr(DatabaseStages, "_depth_pass", counted)

    off = DatabaseStages(store).match(session, sheet.revision.id)
    assert calls == [] and "depth" not in off

    on = DatabaseStages(store, depth_reader=True).match(session, sheet.revision.id)
    assert calls == [sheet.revision.id] and on["depth"] == {"ran": True}


def test_switched_on_without_the_claude_readers_it_reads_nothing_and_says_why(
    session: Session, store: LocalStore
) -> None:
    sheet = _Set(session, store)

    result = DatabaseStages(store, depth_reader=True).match(session, sheet.revision.id)

    depth = result["depth"]
    assert isinstance(depth, dict) and depth["ran"] is False
    assert "Claude" in str(depth["reason"])
    assert session.scalar(select(func.count()).select_from(ObservationCandidate)) == 0


def test_the_switch_must_be_a_yes_or_no() -> None:
    with pytest.raises(TypeError):
        DatabaseStages(depth_reader="yes")  # type: ignore[arg-type]


def test_a_view_lies_in_the_drawing_holding_its_centre_and_most_of_it() -> None:
    from evidence.coordinates import StoredPoint
    from workflow.depth_reader import drawings_holding

    def box(x0: str, y0: str, x1: str, y1: str) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        return (Decimal(x0), Decimal(y0), Decimal(x1), Decimal(y1))

    def view(x0: str, y0: str, x1: str, y1: str) -> list[StoredPoint]:
        return [StoredPoint(Decimal(x0), Decimal(y0)), StoredPoint(Decimal(x1), Decimal(y1))]

    vendor, architect = uuid4(), uuid4()
    drawings = [(vendor, box("0.5", "0.1", "0.95", "0.7")), (architect, box("0", "0", "0.5", "1"))]
    # A caption running a hundredth past the drawing's box still lies in it.
    assert drawings_holding(drawings, view("0.78", "0.1", "0.96", "0.5")) == [vendor]
    # Straddling two drawings evenly: in both, so it is placed in neither (`placement_of`).
    assert sorted(drawings_holding(drawings, view("0.3", "0.2", "0.7", "0.4")) or [], key=str) == (
        sorted([vendor, architect], key=str)
    )
    # Mostly outside: in none.
    assert drawings_holding(drawings, view("0.6", "0.65", "0.9", "0.99")) == []
    assert drawings_holding([], view("0.1", "0.1", "0.2", "0.2")) is None
