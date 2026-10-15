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
from app.models import DrawingView, ItemViewLink, ObservationCandidate
from app.models.runs import ModelInvocation, TaskRun, WorkflowRun
from extraction.depth.read import Asked, DepthReading, ReaderSays, ValueReading, ViewReading
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
    assert "clearly inside one vendor drawing" in placed.reason


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


def test_a_view_lies_in_one_drawing_only_when_nearly_all_of_it_does() -> None:
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
    # 63% in one drawing and 37% in another: in neither (review of #1294).
    assert drawings_holding(drawings, view("0.315", "0.2", "0.815", "0.4")) == []
    # Straddling two drawings evenly: in neither.
    assert drawings_holding(drawings, view("0.3", "0.2", "0.7", "0.4")) == []
    # Mostly outside: in none.
    assert drawings_holding(drawings, view("0.6", "0.65", "0.9", "0.99")) == []
    assert drawings_holding([], view("0.1", "0.1", "0.2", "0.2")) is None


def test_a_view_on_a_drawing_with_no_countertop_is_held_on_the_page_s_countertops() -> None:
    drawing, countertop = uuid4(), uuid4()
    placed = placement_of([drawing], {}, [countertop])
    assert placed.proposal_id is None and placed.among == (countertop,)
    assert "has no countertop" in placed.reason


# -- the real `_depth_pass`, with the reading itself made up (review of #1294) ---------------------


class _Refusing:
    def converse(self, **_: object) -> object:
        from extraction.slot_reader.claude_output import PictureWouldBeResized

        raise PictureWouldBeResized("a test makes no call")


class _Clients:
    def for_current_thread(self) -> _Refusing:
        return _Refusing()


def _claude_stages(store: LocalStore, ceiling: Decimal | None = None) -> DatabaseStages:
    from app.runs.rates import load_model_rates
    from tests.workflow.test_slot_reader import FRACTION_ENV
    from workflow.form_reader import FormReaderRuntime
    from workflow.slot_reader import (
        E2_CROP_SETTINGS,
        MEASURED_SETTINGS,
        SlotReaderRuntime,
        fraction_bar_from_environment,
    )

    form = FormReaderRuntime(
        reader_ids=CLAUDE_PAIR,
        clients=_Clients(),  # type: ignore[arg-type]
        rates=load_model_rates(
            Path(__file__).resolve().parents[2] / "deploy" / "model_rates.us-east-1.json"
        ),
        calls_per_minute=dict.fromkeys(CLAUDE_PAIR, 6000),
        max_concurrent_calls=1,
        max_tokens=3000,
        max_throttle_retries=0,
        retry_backoff_seconds=0.001,
        prompt=None,  # type: ignore[arg-type]
    )
    slot = SlotReaderRuntime(
        form=form,
        crop_settings=E2_CROP_SETTINGS,
        row_settings=MEASURED_SETTINGS,
        fraction_bar=fraction_bar_from_environment(FRACTION_ENV),
        allow_stacked=True,
        claude_row_reader=True,
        spend_cap_usd=ceiling,
    )
    return DatabaseStages(store, form_reader=form, slot_reader=slot, depth_reader=True)


def _match_stage_run(session: Session, sheet: _Set) -> None:
    from workflow.idempotency import stage_idempotency_key
    from workflow.review import ENGINE_VERSION

    task = session.get_one(TaskRun, sheet.run.task_run_id)
    session.add(
        TaskRun(
            workflow_run_id=task.workflow_run_id,
            idempotency_key=stage_idempotency_key(
                package_revision_id=sheet.revision.id, stage="match", engine_version=ENGINE_VERSION
            ),
            task_type="match",
            attempt=1,
            outcome="RUNNING",
        )
    )
    session.flush()


def _fake_reading(
    monkeypatch: pytest.MonkeyPatch, *, fail: Exception | None = None, rounds: int = 3
) -> list[int]:
    """The page is 'prepared' and 'read' by stand-ins; `ask` is the real asker, asked `rounds`
    times. Returns how many times the reading ran."""
    import extraction.depth.read as read_module

    ran: list[int] = []

    def prepare(*_: object, **__: object) -> SimpleNamespace:
        return SimpleNamespace(views=[])

    def read(pages: object, *, ask: object, **_: object) -> DepthReading:
        ran.append(1)
        for number in range(rounds):
            question = SimpleNamespace(pictures=[b"png"], prompt_id="view-kind-v1")
            ask(  # type: ignore[operator]
                [Asked(f"p0:v1.{number}:kind", CLAUDE_PAIR[0], 0, question, {})]  # type: ignore[arg-type]
            )
        if fail is not None:
            raise fail
        return DepthReading(views=(), questions_asked=rounds)

    monkeypatch.setattr(read_module, "prepare_pdf_page", prepare)
    monkeypatch.setattr(read_module, "read_depth", read)
    return ran


def test_the_three_rounds_share_one_spend_guard_capped_by_what_the_set_has_left(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    import workflow.depth_reader as reader_module

    sheet = _Set(session, store)
    _match_stage_run(session, sheet)
    session.add(
        ModelInvocation(
            extraction_run_id=sheet.run.id,
            model_id=CLAUDE_PAIR[0],
            prompt_id="claude-slot-span-v5",
            template_id="claude-slot-span-v5",
            input_tokens=1,
            output_tokens=1,
            latency_ms=1,
            outcome="ok",
            cost_micros=500_000,
        )
    )
    session.flush()
    guards: list[object] = []

    def capture(jobs: object, **kwargs: object) -> dict[object, object]:
        guards.append(kwargs["spend_guard"])
        assert kwargs.get("spend_cap_usd") is None
        return {}

    monkeypatch.setattr(reader_module, "read_crops_parallel", capture)
    _fake_reading(monkeypatch)
    stages = _claude_stages(store)

    result = stages._depth_pass(session, sheet.revision.id)

    assert result["ran"] is True
    assert len(guards) == 3 and len({id(guard) for guard in guards}) == 1, "one guard, three rounds"
    budget = stages._ai_budget.for_pages(1)
    assert guards[0]._maximum_usd == budget - Decimal("0.5")  # type: ignore[attr-defined]


def test_a_failed_reading_is_marked_and_the_earlier_good_one_still_counts(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.workflow.test_depth_evidence import _rule
    from workflow.depth_evidence import depth_operands

    sheet = _Set(session, store)
    sheet.reading("CT010", 24, QUALIFIED, link=sheet.link())
    _match_stage_run(session, sheet)
    _fake_reading(monkeypatch, fail=RuntimeError("the reader broke"))

    result = _claude_stages(store)._depth_pass(session, sheet.revision.id)

    assert result["ran"] is False and "the reader broke" in str(result["reason"])
    found = depth_operands(session, sheet.revision.id, _rule("CT-DEPTH-001"), sheet.item)
    assert found.operands["countertop_depth"].value.exact == 24  # type: ignore[union-attr]
    assert any("failed" in note for note in found.notes)


def test_a_reader_that_never_read_well_says_it_failed_on_each_countertop(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.workflow.test_depth_evidence import _rule
    from workflow.depth_evidence import depth_operands

    sheet = _Set(session, store)
    session.delete(sheet.run)
    session.flush()
    workflow_run = WorkflowRun(package_revision_id=sheet.revision.id, engine_run_id=str(uuid4()))
    session.add(workflow_run)
    session.flush()
    sheet.run = SimpleNamespace(task_run_id=None)  # type: ignore[assignment]
    from workflow.idempotency import stage_idempotency_key
    from workflow.review import ENGINE_VERSION

    session.add(
        TaskRun(
            workflow_run_id=workflow_run.id,
            idempotency_key=stage_idempotency_key(
                package_revision_id=sheet.revision.id, stage="match", engine_version=ENGINE_VERSION
            ),
            task_type="match",
            attempt=1,
            outcome="RUNNING",
        )
    )
    session.flush()
    _fake_reading(monkeypatch, fail=RuntimeError("the reader broke"))

    _claude_stages(store)._depth_pass(session, sheet.revision.id)

    found = depth_operands(session, sheet.revision.id, _rule("CT-DEPTH-001"), sheet.item)
    assert found.missing is not None and "the reader broke" in found.missing


def test_a_stage_run_already_recorded_is_not_read_again(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    sheet = _Set(session, store)
    _match_stage_run(session, sheet)
    ran = _fake_reading(monkeypatch, rounds=0)
    stages = _claude_stages(store)

    first = stages._depth_pass(session, sheet.revision.id)
    again = stages._depth_pass(session, sheet.revision.id)

    assert first["ran"] is True and len(ran) == 1
    assert "already recorded" in str(again["reason"]) and len(ran) == 1


def test_a_page_code_cannot_prepare_is_held_on_its_countertops_with_why(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    import extraction.depth.read as read_module
    from tests.workflow.test_depth_evidence import _rule
    from workflow.depth_evidence import depth_operands

    sheet = _Set(session, store)
    _match_stage_run(session, sheet)
    _fake_reading(monkeypatch, rounds=0)

    def refuse(*_: object, **__: object) -> object:
        raise ValueError("the page is too large")

    monkeypatch.setattr(read_module, "prepare_pdf_page", refuse)

    result = _claude_stages(store)._depth_pass(session, sheet.revision.id)

    assert result["pages_not_prepared"] == 1
    found = depth_operands(session, sheet.revision.id, _rule("CT-DEPTH-001"), sheet.item)
    assert found.missing is not None and "the page is too large" in found.missing


def test_readings_that_cannot_be_saved_mark_the_reading_failed_and_never_stop_match(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    import workflow.depth_reader as reader_module

    sheet = _Set(session, store)
    _match_stage_run(session, sheet)
    _fake_reading(monkeypatch, rounds=0)

    def broken(*_: object, **__: object) -> object:
        raise RuntimeError("a row would not save")

    monkeypatch.setattr(reader_module, "persist_depth_reading", broken)

    result = _claude_stages(store).match(session, sheet.revision.id)

    depth = result["depth"]
    assert isinstance(depth, dict) and depth["ran"] is False
    assert "a row would not save" in str(depth["reason"])
    failed = session.scalars(
        select(ObservationCandidate).where(
            ObservationCandidate.ambiguity_flags.contains(["state:run-failed"])
        )
    ).all()
    assert len(failed) == 1 and f"unplaced-among:{sheet.proposal}" in failed[0].ambiguity_flags


# -- re-review of #1294 ----------------------------------------------------------------------------


def test_a_view_also_a_tenth_inside_another_drawing_is_in_neither() -> None:
    from evidence.coordinates import StoredPoint
    from workflow.depth_reader import drawings_holding

    first, second = uuid4(), uuid4()
    drawings = [
        (first, (Decimal(0), Decimal(0), Decimal(1), Decimal(1))),
        (second, (Decimal("0.4"), Decimal(0), Decimal(1), Decimal(1))),
    ]
    view = [
        StoredPoint(Decimal("0.3"), Decimal("0.1")),
        StoredPoint(Decimal("0.5"), Decimal("0.2")),
    ]
    # Wholly inside the first, and half inside the second, which overlaps it: in neither.
    assert drawings_holding(drawings, view) == []
    small = [
        StoredPoint(Decimal("0.1"), Decimal("0.1")),
        StoredPoint(Decimal("0.3"), Decimal("0.2")),
    ]
    assert drawings_holding(drawings, small) == [first]


def test_a_person_s_link_wins_when_the_view_is_read_again(
    session: Session, store: LocalStore
) -> None:
    sheet = _Set(session, store)
    read = _Reading(sheet)
    read.view((_value("CT010", 24),))
    read.persist()
    (code_link,) = session.scalars(select(ItemViewLink)).all()
    second = DrawingView(page_id=sheet.page.id, tag="SECOND", region=sheet.view.region)
    session.add(second)
    session.flush()
    other, _item = _confirmed(session, second, PartKind.COUNTERTOP, "0.20", "0.80")
    persons = ItemViewLink(
        part_proposal_id=other,
        page_id=sheet.page.id,
        region=code_link.region,
        view_kind="plan",
        judged_by="person",
        supersedes_id=code_link.id,
        linked_by="reviewer@example.com",
    )
    session.add(persons)
    session.flush()

    again = _Reading(sheet)
    again.view((_value("CT010", 24),))
    again.persist()

    newest = [row for row in _rows(session, sheet) if SUPPORT_FLAG not in row.ambiguity_flags][-1]
    assert f"item:{other}" in newest.ambiguity_flags
    assert f"link:{persons.id}" in newest.ambiguity_flags


def test_the_cap_is_never_above_the_configured_claude_ceiling(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    import workflow.depth_reader as reader_module

    sheet = _Set(session, store)
    _match_stage_run(session, sheet)
    guards: list[object] = []

    def capture(jobs: object, **kwargs: object) -> dict[object, object]:
        guards.append(kwargs["spend_guard"])
        return {}

    monkeypatch.setattr(reader_module, "read_crops_parallel", capture)
    _fake_reading(monkeypatch, rounds=1)

    _claude_stages(store, ceiling=Decimal("0.2"))._depth_pass(session, sheet.revision.id)

    assert guards[0]._maximum_usd == Decimal("0.2")  # type: ignore[attr-defined]


def test_a_failed_reading_in_the_same_stage_run_is_tried_again(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    sheet = _Set(session, store)
    _match_stage_run(session, sheet)
    _fake_reading(monkeypatch, fail=RuntimeError("the reader broke"), rounds=0)
    stages = _claude_stages(store)
    assert stages._depth_pass(session, sheet.revision.id)["ran"] is False

    ran = _fake_reading(monkeypatch, rounds=0)
    again = stages._depth_pass(session, sheet.revision.id)

    assert again["ran"] is True and len(ran) == 1
    assert "already recorded" not in str(again.get("reason", ""))
    assert stages._depth_pass(session, sheet.revision.id)["reason"] == (
        "this stage's depth reading is already recorded"
    )


def test_nothing_the_depth_pass_does_can_stop_match(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    import extraction.depth.read as read_module

    sheet = _Set(session, store)
    _match_stage_run(session, sheet)
    _fake_reading(monkeypatch, rounds=0)

    def odd(*_: object, **__: object) -> object:
        raise KeyError("an odd page")

    monkeypatch.setattr(read_module, "prepare_pdf_page", odd)

    def broken(*_: object, **__: object) -> None:
        raise RuntimeError("the ledger is down")

    monkeypatch.setattr(DatabaseStages, "_record_reader_attempts", broken)

    result = _claude_stages(store).match(session, sheet.revision.id)

    depth = result["depth"]
    assert isinstance(depth, dict) and depth["ran"] is False
    assert "the ledger is down" in str(depth["reason"])
