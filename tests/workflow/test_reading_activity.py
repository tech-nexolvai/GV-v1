"""A reading's live activity (#1238): the coded events the reader emits, and their safe writer.

Synthetic sheets and values only. The events are emitted by the real slot reader with fake readers;
the database half writes them with `record_events` and checks that the table refuses anything but
codes, refuses edits, and that the stopped events equal Pariksha's own count of stops.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator, Sequence
from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

import workflow.slot_reader as slot_reader_module
from app.models.runs import READING_EVENT_AGENTS, READING_EVENT_KINDS, READING_STAGES, ReadingEvent
from app.runs.reading_activity import (
    ROLE_NAMES,
    TRY_AGAIN_CHANGES,
    TRY_AGAIN_RESULTS,
    ActivityEvent,
    ActivityRecorder,
    EventKind,
    event_line,
    record_events,
)
from app.runs.reading_progress import READING_STAGE_LABELS
from tests.extraction.slot_reader import sheets
from tests.workflow.test_pariksha_gates import _claude, _number_sheet, _tag_sheet
from tests.workflow.test_slot_reader import (
    OPUS,
    SONNET,
    FakeReaders,
    _scaffold,
    four_piece_sheet,
    slot_page,
)
from workflow.pariksha_gates import Agent
from workflow.slot_reader import PageSlotResult, SlotPage, read_slot_pages

pytest_plugins = ("tests.app.postgres_fixture",)


class Collect:
    """An activity hook that keeps every batch it is told."""

    def __init__(self) -> None:
        self.batches: list[tuple[ActivityEvent, ...]] = []

    def record(self, events: Sequence[ActivityEvent]) -> None:
        self.batches.append(tuple(events))

    @property
    def events(self) -> list[ActivityEvent]:
        return [event for batch in self.batches for event in batch]

    def of(self, agent: Agent, kind: EventKind) -> list[ActivityEvent]:
        return [event for event in self.events if event.agent is agent and event.kind is kind]


class Raises:
    """An activity hook that fails on every batch."""

    def __init__(self) -> None:
        self.calls = 0

    def record(self, events: Sequence[ActivityEvent]) -> None:
        self.calls += 1
        raise RuntimeError("the screen is gone")


def _rows(picks: dict[str, int]) -> Any:
    """Each AI's pick; 0 is "no countertop on this sheet". The sheets have one numbered line."""
    return lambda model: {
        "row": picks[model],
        "kind": "row" if picks[model] else "no_countertop",
        "also": [],
        "why": "synthetic",
    }


def _readers(*, read: Any = None, rows: Any = None) -> FakeReaders:
    return FakeReaders(
        read or (lambda _model, _png: '12"'),
        row=rows or _rows({OPUS: 1, SONNET: 1}),
    )


def _watch(page: SlotPage, readers: FakeReaders, activity: Any = None) -> PageSlotResult:
    (result,) = read_slot_pages(
        [page],
        runtime=_claude(readers),
        record_attempt=lambda _attempt: None,
        activity=activity,
    )
    return result


# --- the events the reader emits --------------------------------------------------------------------


def test_a_page_the_two_line_finder_ais_split_on_is_one_stopped_event() -> None:
    seen = Collect()

    result = _watch(
        slot_page(_number_sheet()), _readers(rows=_rows({OPUS: 1, SONNET: 0})), activity=seen
    )

    assert result.plan.row is None
    (stopped,) = seen.of(Agent.REKHA, EventKind.STOPPED)
    assert (stopped.page_number, stopped.item, stopped.code) == (
        1,
        "p0:row-choice",
        "readers-differ",
    )
    assert [event.code for event in seen.of(Agent.REKHA, EventKind.ANSWERED)] == ["all-answered"]
    assert seen.of(Agent.REKHA, EventKind.PASSED) == []
    assert seen.of(Agent.AKSHARA, EventKind.STOPPED) == [], "nothing was read on the page"


def test_a_line_rekhas_gate_stops_is_a_no_printed_numbers_event() -> None:
    seen = Collect()

    result = _watch(slot_page(_tag_sheet()), _readers(), activity=seen)

    assert result.row_gate is not None and result.row_gate.stopped
    (stopped,) = seen.of(Agent.REKHA, EventKind.STOPPED)
    assert stopped.code == "no-printed-numbers"


def test_a_line_both_ais_found_is_passed_and_its_numbers_are_counted() -> None:
    seen = Collect()

    result = _watch(slot_page(_number_sheet()), _readers(), activity=seen)

    assert result.plan.row is not None
    (passed,) = seen.of(Agent.REKHA, EventKind.PASSED)
    assert passed.code == "printed-numbers", "the gate's own code"
    assert seen.of(Agent.REKHA, EventKind.STOPPED) == []
    owners = len(result.slots) + (result.overall is not None)
    numbers = seen.of(Agent.AKSHARA, EventKind.PASSED) + seen.of(Agent.AKSHARA, EventKind.STOPPED)
    assert len(numbers) == owners, "one event per piece and the overall"
    assert len(seen.of(Agent.AKSHARA, EventKind.ANSWERED)) == 1, "one per page per agent"
    assert all(event.item.startswith("p0:") for event in seen.events)


def test_numbers_the_two_ais_read_differently_are_held_as_stopped_events() -> None:
    seen = Collect()

    result = _watch(
        slot_page(_number_sheet()),
        _readers(read=lambda model, _png: '12"' if model == OPUS else '13"'),
        activity=seen,
    )

    assert result.plan.row is not None
    stopped = seen.of(Agent.AKSHARA, EventKind.STOPPED)
    assert stopped and {event.code for event in stopped} == {"readers-differ"}
    assert seen.of(Agent.AKSHARA, EventKind.PASSED) == []
    assert all(":slot" in event.item or ":overall" in event.item for event in stopped)


def test_the_line_finders_events_are_told_before_the_numbers_are_asked() -> None:
    """Live: the row choice is told as soon as it is made, not when the whole reading ends."""
    readers = _readers()
    told_before: list[int] = []

    class Watch(Collect):
        def record(self, events: Sequence[ActivityEvent]) -> None:
            told_before.append(len(readers.requests))
            super().record(events)

    seen = Watch()
    _watch(slot_page(_number_sheet()), readers, activity=seen)

    first, last = seen.batches
    assert [(event.agent, event.kind) for event in first] == [(Agent.REKHA, EventKind.ANSWERED)]
    assert told_before[0] == 0, "no number question was asked before the row choice was told"
    assert told_before[1] == len(readers.requests)
    assert (last[0].agent, last[0].kind) == (
        Agent.REKHA,
        EventKind.PASSED,
    ), "the line's pass is told once its numbers are read: a budget stop could still take it"
    assert {event.agent for event in last[1:]} == {Agent.AKSHARA}
    assert all(event.document_version_id is not None for event in seen.events)


def test_without_the_row_readers_nothing_is_told() -> None:
    seen = Collect()
    readers = FakeReaders(lambda _model, _png: '12"')
    from tests.workflow.test_slot_reader import runtime

    read_slot_pages(
        [slot_page(_number_sheet())],
        runtime=runtime(readers),
        record_attempt=lambda _attempt: None,
        activity=seen,
    )

    assert seen.of(Agent.REKHA, EventKind.STOPPED) == seen.of(Agent.REKHA, EventKind.PASSED) == []


# --- watching never changes a reading ---------------------------------------------------------------


@pytest.fixture
def same_ids(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """The reader's own ids, made the same on every run, so two results can be compared whole."""

    def reset() -> None:
        counter = itertools.count(1)
        monkeypatch.setattr(slot_reader_module, "uuid4", lambda: UUID(int=next(counter)))

    yield reset


@pytest.mark.parametrize(
    "rows, read",
    [
        (_rows({OPUS: 1, SONNET: 1}), lambda _model, _png: '12"'),
        (_rows({OPUS: 1, SONNET: 0}), lambda _model, _png: '12"'),
        (_rows({OPUS: 1, SONNET: 1}), lambda model, _png: '12"' if model == OPUS else '13"'),
    ],
    ids=["read", "split", "held"],
)
def test_a_hook_that_raises_leaves_the_result_exactly_as_with_no_hook(
    same_ids: Any, rows: Any, read: Any
) -> None:
    page = slot_page(_number_sheet())
    same_ids()
    alone = _watch(page, _readers(rows=rows, read=read))
    same_ids()
    failing = Raises()
    watched = _watch(page, _readers(rows=rows, read=read), activity=failing)

    assert failing.calls >= 1, "the hook was told, and raised"
    assert watched == alone


def test_events_that_cannot_be_built_leave_the_result_exactly_as_with_no_hook(
    same_ids: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = slot_page(_number_sheet())
    same_ids()
    alone = _watch(page, _readers())

    def broken(*_args: object, **_kwargs: object) -> list[ActivityEvent]:
        raise ValueError("an event that is not a code")

    monkeypatch.setattr(slot_reader_module, "_line_events", broken)
    monkeypatch.setattr(slot_reader_module, "_after_reading_events", broken)
    same_ids()
    seen = Collect()
    assert _watch(page, _readers(), activity=seen) == alone
    assert seen.batches == []


# --- the event itself -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "item, code, change",
    [
        ("page twelve", "readers-differ", None),
        ("p11:row choice", "readers-differ", None),
        ("p11:row-choice", "Readers differ", None),
        ("p11:row-choice", '36 1/2"', None),
        ("p11:row-choice", "readers-differ", "a closer look"),
        ("p11:" + "x" * 80, "readers-differ", None),
    ],
)
def test_an_event_refuses_anything_but_codes(item: str, code: str, change: str | None) -> None:
    with pytest.raises(ValueError):
        ActivityEvent(12, Agent.REKHA, item, EventKind.STOPPED, code, change)


def test_the_lists_the_table_checks_are_the_codes_lists() -> None:
    assert set(READING_EVENT_AGENTS) == {agent.value for agent in Agent}
    assert set(READING_EVENT_KINDS) == {kind.value for kind in EventKind}
    assert set(ROLE_NAMES) == set(Agent)
    assert "try_again" in READING_STAGES and "try_again" in READING_STAGE_LABELS


#: Every sentence a person reads, exactly (the screen renders only these).
SENTENCES: list[tuple[Agent, EventKind, str, str | None, int | None, str]] = [
    (Agent.REKHA, EventKind.ANSWERED, "all-answered", None, None, "Page 12: the line finder's AIs answered."),
    (Agent.REKHA, EventKind.PASSED, "printed-numbers", None, None, "Page 12: the line finder found the countertop line."),
    (Agent.REKHA, EventKind.STOPPED, "readers-differ", None, None, "Page 12: the line finder's two AIs picked different lines."),
    (Agent.REKHA, EventKind.STOPPED, "no-printed-numbers", None, None, "Page 12: the line finder's chosen line has no printed numbers."),
    (Agent.REKHA, EventKind.STOPPED, "no-countertop", None, None, "Page 12: the line finder found no countertop line."),
    (Agent.REKHA, EventKind.STOPPED, "reader-unsure", None, None, "Page 12: the line finder was not sure which line is the countertop."),
    (Agent.REKHA, EventKind.STOPPED, "not-among-lines", None, None, "Page 12: the line finder saw a countertop that none of the lines measures."),
    (Agent.REKHA, EventKind.STOPPED, "no-answer", None, None, "Page 12: the line finder got no answer from one of its AIs."),
    (Agent.BALA, EventKind.STOPPED, "budget-reached", None, None, "Page 12: not read: the AI budget was reached before this page."),
    (Agent.BALA, EventKind.STOPPED, "no-drawing", None, None, "Page 12: the page sorter found no drawing on this page."),
    (Agent.BALA, EventKind.STOPPED, "no-vendor-drawing", None, None, "Page 12: the page sorter found no vendor drawing on this page."),
    (Agent.BALA, EventKind.STOPPED, "reference-only", None, None, "Page 12: the page sorter found only a reference drawing on this page."),
    (Agent.BALA, EventKind.STOPPED, "no-row-found", None, None, "Page 12: the page sorter found no countertop line to read on this page."),
    (Agent.BALA, EventKind.STOPPED, "unreadable", None, None, "Page 12: the page sorter could not read this page."),
    (Agent.BALA, EventKind.STOPPED, "blank-page", None, None, "Page 12: the page sorter found this page blank."),
    (Agent.AKSHARA, EventKind.ANSWERED, "some-answered", None, None, "Page 12: the number reader's AIs answered."),
    (Agent.AKSHARA, EventKind.PASSED, "sealed", None, None, "Page 12: the number reader read a number and both AIs agreed."),
    (Agent.AKSHARA, EventKind.STOPPED, "readers-differ", None, None, "Page 12: the number reader's two AIs read a number differently."),
    (Agent.AKSHARA, EventKind.STOPPED, "unsure", None, None, "Page 12: the number reader was not sure a number belongs to its piece."),
    (Agent.AKSHARA, EventKind.STOPPED, "unreadable", None, None, "Page 12: the number reader could not read a number clearly."),
    (Agent.AKSHARA, EventKind.STOPPED, "one-reader-missing", None, None, "Page 12: only one of the number reader's AIs read a number."),
    (Agent.AKSHARA, EventKind.STOPPED, "not-asked", None, None, "Page 12: a number was not read."),
    (Agent.AKSHARA, EventKind.STOPPED, "no-label", None, None, "Page 12: the number reader found no number for a piece."),
    (Agent.AKSHARA, EventKind.STOPPED, "two-labels", None, None, "Page 12: the number reader found more than one number for a piece."),
    (Agent.AKSHARA, EventKind.STOPPED, "row-held", None, None, "Page 12: a number was read, but its line waits for you."),
    (Agent.AKSHARA, EventKind.STOPPED, "not-offered", None, None, "Page 12: a number was read, but it is held back for you to check."),
    (Agent.AKSHARA, EventKind.STOPPED, "no-dimension", None, None, "Page 12: the number reader found only words, no number, for a piece."),
    (Agent.AKSHARA, EventKind.STOPPED, "vif", None, None, "Page 12: a number is marked VIF (verify in field), so it waits for you."),
    (Agent.AKSHARA, EventKind.STOPPED, "field-cut-included", None, None, "Page 12: a number already includes the field cut, so it waits for you."),
    (Agent.AKSHARA, EventKind.STOPPED, "counter-break", None, None, "Page 12: the line includes a tall appliance or range bay, so it waits for you."),
    (Agent.AKSHARA, EventKind.STOPPED, "not-sealed", None, None, "Page 12: a number was not settled by both AIs."),
    (Agent.AKSHARA, EventKind.STOPPED, "drawn-length", None, None, "Page 12: the number reader's two AIs agreed on a number that does not fit its drawn length."),
    (Agent.AKSHARA, EventKind.STOPPED, "crowded", None, None, "Page 12: a number sits too close to another label to be read alone."),
    (Agent.AKSHARA, EventKind.STOPPED, "edge", None, None, "Page 12: a number is cut by the drawing's edge."),
    (Agent.AKSHARA, EventKind.STOPPED, "reviewer-markup", None, None, "Page 12: a number is covered by reviewer markup."),
    (Agent.AKSHARA, EventKind.STOPPED, "ink-unchecked", None, None, "Page 12: a number could not be checked for reviewer markup."),
    (Agent.AKSHARA, EventKind.STOPPED, "stacked", None, None, "Page 12: a number is a stacked fraction."),
    (Agent.AKSHARA, EventKind.STOPPED, "not-plain", None, None, "Page 12: a number has words or a sum in it."),
    (Agent.AKSHARA, EventKind.STOPPED, "fraction-not-drawn", None, None, "Page 12: a number has a fraction no drawing prints."),
    (Agent.AKSHARA, EventKind.STOPPED, "mm-inch-disagree", None, None, "Page 12: a number's millimetres and inches disagree."),
    (Agent.AKSHARA, EventKind.STOPPED, "row-ambiguous", None, None, "Page 12: the number reader was not sure which line is the countertop."),
    (Agent.AKSHARA, EventKind.STOPPED, "slot-ambiguous", None, None, "Page 12: a number sits between two pieces."),
    (Agent.AKSHARA, EventKind.STOPPED, "a-code-not-listed", None, None, "Page 12: the number reader's inspector stopped it; it waits for you."),
    (Agent.AKSHARA, EventKind.TRYING_AGAIN, "readers-differ", "closer-look-2x", 1, "Page 12: the number reader is trying again (try 1 of 3) with a 2× close-up."),
    (Agent.AKSHARA, EventKind.TRYING_AGAIN, "readers-differ", "closer-look-3x-upright", 2, "Page 12: the number reader is trying again (try 2 of 3) with a 3× close-up, turned upright."),
    (Agent.AKSHARA, EventKind.TRYING_AGAIN, "readers-differ", "tighter-crop", 3, "Page 12: the number reader is trying again (try 3 of 3) with a tighter crop."),
    (Agent.REKHA, EventKind.TRYING_AGAIN, "readers-differ", "tie-break-disputed-lines", 1, "Page 12: the line finder is trying again (try 1 of 3), with a closer look at the lines its two AIs picked."),
    (Agent.REKHA, EventKind.TRYING_AGAIN, "no-printed-numbers", "other-numbered-lines", 2, "Page 12: the line finder is trying again (try 2 of 3), showing the other lines that carry printed numbers."),
    (Agent.REKHA, EventKind.TRYING_AGAIN, "readers-differ", "each-line-alone", 3, "Page 12: the line finder is trying again (try 3 of 3), showing each line on its own."),
    (Agent.REKHA, EventKind.TRY_AGAIN_RESULT, "helped", None, 2, "Page 12: try 2 helped; the line finder's two AIs now agree."),
    (Agent.AKSHARA, EventKind.TRY_AGAIN_RESULT, "helped", None, 1, "Page 12: try 1 helped; the number reader's two AIs now agree."),
    (Agent.AKSHARA, EventKind.TRY_AGAIN_RESULT, "not-helped", None, 1, "Page 12: try 1 didn't help."),
    (Agent.AKSHARA, EventKind.TRY_AGAIN_RESULT, "not-helped", None, 3, "Page 12: try 3 didn't help; it waits for you."),
    (Agent.REKHA, EventKind.TRY_AGAIN_RESULT, "allowance-used", None, 1, "Page 12: not tried again: the try-again allowance for this set is used up. It waits for you."),
    (Agent.AKSHARA, EventKind.TRY_AGAIN_RESULT, "cannot-zoom", None, 2, "Page 12: not tried again: a bigger close-up would be too large for the AIs. It waits for you."),
]  # fmt: skip


@pytest.mark.parametrize("agent, kind, code, change, attempt, expected", SENTENCES)
def test_every_sentence_exactly_naming_the_role_never_the_agent(
    agent: Agent, kind: EventKind, code: str, change: str | None, attempt: int | None, expected: str
) -> None:
    ActivityEvent(12, agent, "p11:row-choice", kind, code, change, attempt)  # a valid event
    line = event_line(agent, kind, code, change, 12, attempt)

    assert line == expected
    assert not any(name.value in line for name in Agent)


def test_every_change_and_result_code_has_its_own_sentence() -> None:
    covered = {(agent, change) for agent, _kind, _code, change, _a, _line in SENTENCES if change}
    assert covered == {
        (agent, change) for agent, changes in TRY_AGAIN_CHANGES.items() for change in changes
    }
    assert {
        code for _agent, kind, code, *_rest in SENTENCES if kind is EventKind.TRY_AGAIN_RESULT
    } == set(TRY_AGAIN_RESULTS)
    assert set(TRY_AGAIN_CHANGES[Agent.AKSHARA]) == {
        "closer-look-2x",
        "closer-look-3x-upright",
        "tighter-crop",
    }
    assert set(TRY_AGAIN_CHANGES[Agent.REKHA]) == {
        "tie-break-disputed-lines",
        "other-numbered-lines",
        "each-line-alone",
    }
    assert TRY_AGAIN_RESULTS == ("helped", "not-helped", "allowance-used", "cannot-zoom")


@pytest.mark.parametrize(
    "kind, code, change, attempt",
    [
        (EventKind.TRYING_AGAIN, "readers-differ", "closer-look-2x", None),  # no try number
        (EventKind.TRYING_AGAIN, "readers-differ", "closer-look-2x", 4),  # at most three
        (EventKind.TRYING_AGAIN, "readers-differ", "closer-look-2x", 0),
        (EventKind.TRYING_AGAIN, "readers-differ", None, 1),  # what changed is named
        (EventKind.TRYING_AGAIN, "readers-differ", "each-line-alone", 1),  # the line finder's
        (EventKind.TRYING_AGAIN, "readers-differ", "zoom-a-lot", 1),
        (EventKind.TRY_AGAIN_RESULT, "fixed", None, 1),
        (EventKind.TRY_AGAIN_RESULT, "helped", None, None),
        (EventKind.STOPPED, "readers-differ", "closer-look-2x", None),
        (EventKind.STOPPED, "readers-differ", None, 1),
        (EventKind.FINISHED, "readers-differ", None, None),  # only the stage writes the marker
    ],
)
def test_a_try_again_event_names_its_try_and_a_known_change(
    kind: EventKind, code: str, change: str | None, attempt: int | None
) -> None:
    with pytest.raises(ValueError):
        ActivityEvent(12, Agent.AKSHARA, "p11:slot0:0", kind, code, change, attempt)


# --- the writer -------------------------------------------------------------------------------------

_EVENT = ActivityEvent(12, Agent.REKHA, "p11:row-choice", EventKind.STOPPED, "readers-differ")


def test_with_no_engine_nothing_is_written_and_nothing_raises() -> None:
    assert record_events(None, uuid4(), uuid4(), [_EVENT]) is False
    ActivityRecorder(None, uuid4(), uuid4()).record([_EVENT])


def test_a_database_that_cannot_be_reached_is_logged_and_never_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    nowhere = create_engine(
        "postgresql+psycopg://nobody:nothing@127.0.0.1:9/none", connect_args={"connect_timeout": 1}
    )

    assert record_events(nowhere, uuid4(), uuid4(), [_EVENT]) is False
    ActivityRecorder(nowhere, uuid4(), uuid4()).record([_EVENT])
    assert "reading activity not recorded" in caplog.text
    assert "nobody" not in caplog.text, "logged by type only"


@pytest.fixture
def engine(postgres_engine: Any) -> Any:
    from alembic import command
    from tests.app.postgres_fixture import alembic_config

    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")
    return postgres_engine


def test_a_batch_is_written_in_order_in_its_own_transaction(engine: Any) -> None:
    from sqlalchemy.orm import Session

    revision, reading = uuid4(), uuid4()
    events = [
        ActivityEvent(12, Agent.REKHA, "p11:row-choice", EventKind.ANSWERED, "all-answered"),
        _EVENT,
    ]

    assert record_events(engine, revision, reading, events) is True
    with Session(engine) as session:
        rows = session.scalars(
            select(ReadingEvent)
            .where(ReadingEvent.package_revision_id == revision)
            .order_by(ReadingEvent.position)
        ).all()
    assert [(row.position, row.kind, row.code) for row in rows] == [
        (0, "answered", "all-answered"),
        (1, "stopped", "readers-differ"),
    ]
    assert {row.reading_id for row in rows} == {reading}
    assert {row.agent for row in rows} == {"Rekha"}


def test_an_event_row_is_never_updated_or_deleted(engine: Any) -> None:
    revision = uuid4()
    assert record_events(engine, revision, uuid4(), [_EVENT])

    for statement in (
        "UPDATE reading_events SET code = 'agreed' WHERE package_revision_id = :revision",
        "DELETE FROM reading_events WHERE package_revision_id = :revision",
    ):
        with pytest.raises(DBAPIError, match="append-only"), engine.begin() as connection:
            connection.execute(text(statement), {"revision": revision})


@pytest.mark.parametrize(
    "column, value",
    [
        ("item", "page twelve"),
        ("item", "p11:row choice"),
        ("code", "Readers differ"),
        ("code", '36 1/2"'),
        ("change", "a closer look"),
        ("agent", "Someone"),
        ("kind", "guessed"),
        ("page_number", 0),
        ("attempt", 0),
        ("attempt", 4),
        ("agent", None),  # an item event names its agent, page, item and code
        ("code", None),
        ("kind", "finished"),  # the marker names none of them
    ],
)
def test_the_table_refuses_free_text(engine: Any, column: str, value: object) -> None:
    row: dict[str, object] = {
        "id": uuid4(),
        "revision": uuid4(),
        "reading": uuid4(),
        "position": 0,
        "page_number": 12,
        "agent": "Rekha",
        "item": "p11:row-choice",
        "kind": "stopped",
        "code": "readers-differ",
        "change": None,
        "attempt": None,
    }
    row[column] = value
    insert = text(
        "INSERT INTO reading_events (id, created_at, package_revision_id, reading_id, position, "
        "page_number, agent, item, kind, code, change, attempt) VALUES (:id, now(), :revision, "
        ":reading, :position, :page_number, :agent, :item, :kind, :code, :change, :attempt)"
    )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(insert, row)


def test_an_event_that_is_not_all_codes_is_skipped_and_the_rest_written(
    engine: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """An event that slipped past its own check (built without it) is skipped and logged; the
    rest of the batch is still written (#1238 review: never drop a whole batch for one)."""
    from sqlalchemy.orm import Session

    revision = uuid4()
    bad = object.__new__(ActivityEvent)
    for name, value in (
        ("page_number", 12),
        ("agent", Agent.REKHA),
        ("item", "page twelve"),
        ("kind", EventKind.STOPPED),
        ("code", "readers-differ"),
        ("change", None),
        ("attempt", None),
        ("document_version_id", None),
    ):
        object.__setattr__(bad, name, value)

    assert record_events(engine, revision, uuid4(), [bad, _EVENT]) is True
    with Session(engine) as session:
        rows = session.scalars(
            select(ReadingEvent).where(ReadingEvent.package_revision_id == revision)
        ).all()
    assert [(row.item, row.position) for row in rows] == [("p11:row-choice", 0)]
    assert "reading activity event skipped: ValueError" in caplog.text


def test_every_reason_a_number_can_be_held_with_has_its_own_sentence() -> None:
    """Each reason code the seal, the row holds and the reader can give a held number, read from
    their own source so a new code cannot be added without its words."""
    import re as regex
    from pathlib import Path

    from extraction.slot_reader import labels, seal

    source = Path(seal.__file__).read_text()
    codes = set(regex.findall(r'review\(\s*"([a-z0-9-]+)"', source))
    codes |= set(regex.findall(r'\(\s*"([a-z0-9-]+)",\s*"', source))
    codes |= set(regex.findall(r'reason_code="([a-z0-9-]+)"', source))
    codes |= set(
        regex.findall(r'OwnerOutcome\(\s*LabelState\.REVIEW, None, None, "([a-z0-9-]+)"', source)
    )
    codes |= {labels.FIELD_CUT_INCLUDED[0], labels.VIF[0], labels.COUNTER_BREAK[0]}
    codes |= {seal._ODD_FRACTION, "drawn-length", "row-held", "not-offered", "not-sealed"}
    assert {"readers-differ", "no-label", "two-labels", "crowded", "vif"} <= codes, codes
    generic = event_line(Agent.AKSHARA, EventKind.STOPPED, "a-code-not-listed", None, 12)
    missing = {
        code
        for code in codes
        if event_line(Agent.AKSHARA, EventKind.STOPPED, code, None, 12) == generic
    }
    assert missing == set()


# --- the stopped events equal Pariksha's stops ------------------------------------------------------


def _sheet_with_split_numbers() -> bytes:
    """Three pieces with invented numbers, read differently by the two fake AIs."""
    return sheets.sheet(sheets.text_labels(('11"', '25"', '12"'), '48"'))


@pytest.mark.parametrize(
    "data, rows, read",
    [
        (four_piece_sheet, _rows({OPUS: 1, SONNET: 1}), lambda _model, _png: '12"'),
        (_number_sheet, _rows({OPUS: 1, SONNET: 1}), lambda _model, _png: '12"'),
        (_number_sheet, _rows({OPUS: 1, SONNET: 0}), lambda _model, _png: '12"'),
        (_tag_sheet, _rows({OPUS: 1, SONNET: 1}), lambda _model, _png: '12"'),
        (
            _sheet_with_split_numbers,
            _rows({OPUS: 1, SONNET: 1}),
            lambda model, _png: '12"' if model == OPUS else '13"',
        ),
    ],
    ids=["sealed", "read", "split", "gate-stop", "held-numbers"],
)
def test_the_stopped_events_equal_parikshas_stops(
    engine: Any, data: Any, rows: Any, read: Any
) -> None:
    from sqlalchemy.orm import Session

    from workflow.pariksha_run_check import gate_tally
    from workflow.slot_reader import persist_slot_readings

    with Session(engine) as session:
        revision, version, page_row, run = _scaffold(session)
        page = replace(slot_page(data()), page_id=page_row.id, document_version_id=version.id)
        seen = Collect()
        result = _watch(page, _readers(rows=rows, read=read), activity=seen)
        persist_slot_readings(
            session,
            package_revision_id=revision.id,
            extraction_run_id=run.id,
            reader_ids=(OPUS, SONNET),
            results=[result],
        )
        session.flush()
        tally = {(count.agent, count.gate): count for count in gate_tally(session, revision.id)}
        session.rollback()

    for agent in (Agent.REKHA, Agent.AKSHARA):
        stops = sum(count.stopped for (who, _gate), count in tally.items() if who is agent)
        assert len(seen.of(agent, EventKind.STOPPED)) == stops, agent
    seal = tally.get((Agent.AKSHARA, "seal"))
    assert len(seen.of(Agent.AKSHARA, EventKind.PASSED)) == (0 if seal is None else seal.passed)
    line = tally.get((Agent.REKHA, "row-has-printed-numbers"))
    assert len(seen.of(Agent.REKHA, EventKind.PASSED)) == (
        0 if line is None else line.passed + line.recorded
    ), "one pass per row the reading keeps"


def test_numbers_both_ais_agree_on_are_passes() -> None:
    seen = Collect()

    _watch(slot_page(four_piece_sheet()), _readers(), activity=seen)

    passed = seen.of(Agent.AKSHARA, EventKind.PASSED)
    assert [event.item for event in passed] == [f"p0:slot{index}:0" for index in range(4)]
    assert {event.code for event in passed} == {"sealed"}


def test_an_event_write_is_bounded_in_time_as_well_as_in_lock_waits(engine: Any) -> None:
    """A side write never holds up the reading: it gives up on a lock after a few seconds, and on a
    slow statement too (#1238 review)."""
    from sqlalchemy import event

    statements: list[str] = []

    def seen(_connection: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", seen)
    try:
        assert record_events(engine, uuid4(), uuid4(), [_EVENT])
    finally:
        event.remove(engine, "before_cursor_execute", seen)
    assert any("SET LOCAL lock_timeout" in statement for statement in statements)
    assert any("SET LOCAL statement_timeout" in statement for statement in statements)
