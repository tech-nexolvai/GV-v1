"""A reading's live activity: what each agent's AIs answered and what its inspector did (#1238).

**Why.** While a package is read the screen said only "page k of N". The progress row is overwritten
in place, and the inspectors' results (#1228, Pariksha's gates) are written only when the stage
commits, so a reviewer could not see, while it happened, that an agent's two AIs split on a page or
that its inspector stopped an item. Phase 3 adds a smart "try again" (PR B), which needs this too:
"trying again" and how it went must be visible as they happen.

**Two halves**, like `app.runs.reading_progress`:

* `record_events` is the writer the worker calls (through `ActivityRecorder`, the hook
  `workflow.slot_reader.read_slot_pages` is handed). Each batch of events is one short side
  transaction that never raises: a line that could not be written only means the screen says less.
  `mark_reading_finished` writes the reading's `finished` marker in the reading stage's own
  transaction, so it exists only when that attempt committed.
* `reading_activity_for` is the reader the API calls. While the revision is read it shows only the
  running attempt (named on the progress row); after, only the newest reading, and only when that
  reading finished. A reading that failed is never shown as if it were the result.

**Codes only.** An event stores a page number, an agent, a code question id (`p11:row-choice`), a
kind and a short code (`readers-differ`), never drawing text; the database refuses anything else
(`app.models.runs.ReadingEvent`). The plain sentence a person reads is made here from the codes,
naming each agent by its plain role ("the line finder", "the number reader"); the agent's own name
is a separate field the screen shows on hover (Anant, 2026-10-14, phase 3 decision 1). The backend
owns every sentence: the screen shows `line`, `now` and `summary.sentence` as they are.

Nothing here decides anything about a drawing. It only reports what the reading did.

Source: issue #1238 · Verification: `tests/workflow/test_reading_activity.py`,
`tests/api/test_package_reading_activity.py`, `tests/db/test_reading_events_migration.py`
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final, Literal, Protocol
from uuid import UUID, uuid4

from sqlalchemy import Engine, func, select, text
from sqlalchemy.dialects.postgresql import distinct_on, insert
from sqlalchemy.orm import Session

from app.db.base import utc_now
from app.models.runs import (
    READING_EVENT_CODE_MAX,
    READING_EVENT_CODE_PATTERN,
    READING_EVENT_ITEM_MAX,
    READING_EVENT_ITEM_PATTERN,
    READING_EVENT_MAX_ATTEMPTS,
    READING_FINISHED_KIND,
    ReadingEvent,
)
from app.runs.reading_progress import wait_briefly_for_locks
from workflow.pariksha_gates import Agent

__all__ = [
    "ACTIVITY_ITEMS_SHOWN",
    "DID_NOT_FINISH",
    "ROLE_NAMES",
    "TRY_AGAIN_CHANGES",
    "TRY_AGAIN_RESULTS",
    "ActivityEvent",
    "ActivityHook",
    "ActivityItem",
    "ActivityRecorder",
    "ActivitySnapshot",
    "ActivitySummary",
    "EventKind",
    "event_line",
    "mark_reading_finished",
    "reading_activity_for",
    "record_events",
    "summary_sentence",
]

logger = logging.getLogger("gv.runs.reading_activity")


class EventKind(StrEnum):
    """What one event says happened to an item (`READING_EVENT_KINDS`)."""

    ANSWERED = "answered"
    """The agent's AIs answered (one event per page per agent)."""
    PASSED = "passed"
    """The agent's inspector passed the item on. Counted, never listed."""
    STOPPED = "stopped"
    """The agent's inspector stopped the item: it waits for a person."""
    TRYING_AGAIN = "trying_again"
    """Phase 3, PR B: the item is asked again, with what changed (`change`) and which try."""
    TRY_AGAIN_RESULT = "try_again_result"
    """Phase 3, PR B: how a try went (`TRY_AGAIN_RESULTS`)."""
    FINISHED = READING_FINISHED_KIND
    """The reading's marker, written only in the reading stage's own transaction; never an item."""


#: Each agent's plain role, the words the reviewer reads (phase 3 decision 1). The agent's own name
#: is shown only on hover.
ROLE_NAMES: Final[dict[Agent, str]] = {
    Agent.BALA: "the page sorter",
    Agent.REKHA: "the line finder",
    Agent.AKSHARA: "the number reader",
    Agent.VASTU: "the architect reader",
    Agent.MILAN: "the matcher",
    Agent.KAUTILYA: "the planner",
    Agent.NINA: "the checker",
    Agent.VANI: "the writer",
}

#: How many items the API lists: the newest that need the reviewer's eye (passes are only counted,
#: and pages whose AIs only answered fill the list only when fewer remain).
ACTIVITY_ITEMS_SHOWN: Final = 20

#: What a try-again may change, per agent, and the words for it (Anant, 2026-10-14, phase 3
#: decision 4: up to three tries per item, each one changing something). PR B emits these codes.
TRY_AGAIN_CHANGES: Final[dict[Agent, dict[str, str]]] = {
    Agent.AKSHARA: {
        "closer-look-2x": " with a 2× close-up",
        "closer-look-3x-upright": " with a 3× close-up, turned upright",
        "tighter-crop": " with a tighter crop",
    },
    Agent.REKHA: {
        "tie-break-disputed-lines": ", with a closer look at the lines its two AIs picked",
        "other-numbered-lines": ", showing the other lines that carry printed numbers",
        "each-line-alone": ", showing each line on its own",
    },
}

#: How a try went (`EventKind.TRY_AGAIN_RESULT` codes).
TRY_AGAIN_RESULTS: Final = ("helped", "not-helped", "allowance-used", "cannot-zoom")

_ITEM = re.compile(READING_EVENT_ITEM_PATTERN)
_CODE = re.compile(READING_EVENT_CODE_PATTERN)


def _check(event: ActivityEvent) -> None:
    """Raise `ValueError` unless the event holds only codes, as the table requires."""
    if event.kind is EventKind.FINISHED:
        raise ValueError("the finished marker is written by `mark_reading_finished` only")
    if event.page_number < 1:
        raise ValueError("an event's page number starts at 1")
    if len(event.item) > READING_EVENT_ITEM_MAX or not _ITEM.fullmatch(event.item):
        raise ValueError("an event's item is a code question id, never text")
    for value in (event.code, event.change):
        if value is not None and (
            len(value) > READING_EVENT_CODE_MAX or not _CODE.fullmatch(value)
        ):
            raise ValueError("an event's code and change are short codes, never text")
    trying = event.kind in (EventKind.TRYING_AGAIN, EventKind.TRY_AGAIN_RESULT)
    if not trying:
        if event.change is not None or event.attempt is not None:
            raise ValueError("only a try-again event names a change or a try")
        return
    if event.attempt is None or not 1 <= event.attempt <= READING_EVENT_MAX_ATTEMPTS:
        raise ValueError(f"a try-again event names its try, 1 to {READING_EVENT_MAX_ATTEMPTS}")
    if event.kind is EventKind.TRYING_AGAIN:
        if event.change not in TRY_AGAIN_CHANGES.get(event.agent, {}):
            raise ValueError("a try names what it changed, one of the agent's known changes")
    elif event.code not in TRY_AGAIN_RESULTS or event.change is not None:
        raise ValueError("a try's result is one of the known results")


@dataclass(frozen=True, slots=True)
class ActivityEvent:
    """One event as the reader emits it. Refuses anything that is not a code.

    A try-again event (`trying_again`, `try_again_result`) names its try (`attempt`, 1 to 3); a
    `trying_again` event also names what changed, one of `TRY_AGAIN_CHANGES` for its agent, and a
    result's code is one of `TRY_AGAIN_RESULTS`. Other events carry neither. `document_version_id`
    is the file the page is in, so two shop files' page 3 are two items.
    """

    page_number: int
    agent: Agent
    item: str
    """A code question id: `p<page index>:` then lower-case letters, digits, `:` and `-`."""
    kind: EventKind
    code: str
    change: str | None = None
    attempt: int | None = None
    document_version_id: UUID | None = None

    def __post_init__(self) -> None:
        _check(self)


class ActivityHook(Protocol):
    """Told each batch of events as it happens. Watching only: it never changes a reading."""

    def record(self, events: Sequence[ActivityEvent]) -> None: ...


#: How long one side write of events may run. The insert is a few rows; anything slower is a
#: surprise, and the reading must not wait on it.
_STATEMENT_WAIT: Final = "5s"


def _row(event: ActivityEvent) -> dict[str, object]:
    _check(event)  # again: an event can be built without its own check (`object.__new__`)
    return {
        "page_number": event.page_number,
        "agent": Agent(event.agent).value,
        "item": event.item,
        "kind": EventKind(event.kind).value,
        "code": event.code,
        "change": event.change,
        "attempt": event.attempt,
        "document_version_id": event.document_version_id,
    }


def record_events(
    engine: Engine | None,
    package_revision_id: UUID,
    reading_id: UUID,
    events: Sequence[ActivityEvent],
) -> bool:
    """Write one batch of events in its own committed transaction. Never raises.

    Each event is checked on its own: one that is not all codes is skipped and logged, and the rest
    are written. Returns whether anything was written. A database failure is logged by type only (a
    database error can quote the row it refused) and swallowed: the reading goes on whether or not
    it can be watched.
    """
    if engine is None or not events:
        return False
    rows: list[dict[str, object]] = []
    for event in events:
        try:
            rows.append(_row(event))
        except Exception as error:  # noqa: BLE001 - one bad event never drops the batch
            logger.warning("reading activity event skipped: %s", type(error).__name__)
    if not rows:
        return False
    try:
        now = utc_now()
        for position, row in enumerate(rows):
            row |= {
                "id": uuid4(),
                "created_at": now,
                "package_revision_id": package_revision_id,
                "reading_id": reading_id,
                "position": position,
            }
        with Session(engine) as side, side.begin():
            wait_briefly_for_locks(side)
            side.execute(text(f"SET LOCAL statement_timeout = '{_STATEMENT_WAIT}'"))
            side.execute(insert(ReadingEvent), rows)
    except Exception as error:  # noqa: BLE001 - watching is never allowed to stop a reading
        logger.warning("reading activity not recorded: %s", type(error).__name__)
        return False
    return True


def mark_reading_finished(session: Session, package_revision_id: UUID, reading_id: UUID) -> None:
    """Add the reading's `finished` marker in the caller's own transaction (the reading stage's).

    It commits with the stage's readings or not at all, so a marker means the attempt's results are
    the ones stored.
    """
    session.add(
        ReadingEvent(
            package_revision_id=package_revision_id,
            reading_id=reading_id,
            position=0,
            kind=EventKind.FINISHED.value,
        )
    )


class ActivityRecorder:
    """The hook a reading stage hands the reader: one revision, one reading id. Never raises."""

    def __init__(self, engine: Engine | None, package_revision_id: UUID, reading_id: UUID) -> None:
        self._engine = engine
        self._revision = package_revision_id
        self.reading_id = reading_id
        self.wrote = False
        """Whether any batch was written, so the stage knows to mark the reading finished."""

    def record(self, events: Sequence[ActivityEvent]) -> None:
        if record_events(self._engine, self._revision, self.reading_id, events):
            self.wrote = True

    def finished(self, session: Session) -> None:
        """Mark the reading finished in the stage's transaction, when it recorded anything."""
        if self.wrote:
            mark_reading_finished(session, self._revision, self.reading_id)


# --- the words -------------------------------------------------------------------------------------

#: Why the page sorter stopped a page, after "Page N: " (`workflow.slot_row_scope.NO_ROW_CODES`).
_PAGE_STOPS: Final[dict[str, str]] = {
    "budget-reached": "not read: the AI budget was reached before this page.",
    "no-drawing": "the page sorter found no drawing on this page.",
    "no-vendor-drawing": "the page sorter found no vendor drawing on this page.",
    "reference-only": "the page sorter found only a reference drawing on this page.",
    "no-row-found": "the page sorter found no countertop line to read on this page.",
    "unreadable": "the page sorter could not read this page.",
    "blank-page": "the page sorter found this page blank.",
}

#: Why the line finder's inspector stopped a page, after "Page N: ".
_LINE_STOPS: Final[dict[str, str]] = {
    "readers-differ": "the line finder's two AIs picked different lines.",
    "no-printed-numbers": "the line finder's chosen line has no printed numbers.",
    "no-countertop": "the line finder found no countertop line.",
    "reader-unsure": "the line finder was not sure which line is the countertop.",
    "not-among-lines": "the line finder saw a countertop that none of the lines measures.",
    "no-answer": "the line finder got no answer from one of its AIs.",
}

#: Why the number reader's inspector held a number, after "Page N: ". Every reason code a reading
#: can be held with has its words here (a test keeps it so).
_NUMBER_STOPS: Final[dict[str, str]] = {
    "readers-differ": "the number reader's two AIs read a number differently.",
    "unsure": "the number reader was not sure a number belongs to its piece.",
    "unreadable": "the number reader could not read a number clearly.",
    "one-reader-missing": "only one of the number reader's AIs read a number.",
    "not-asked": "a number was not read.",
    "no-label": "the number reader found no number for a piece.",
    "no-dimension": "the number reader found only words, no number, for a piece.",
    "two-labels": "the number reader found more than one number for a piece.",
    "row-held": "a number was read, but its line waits for you.",
    "not-offered": "a number was read, but it is held back for you to check.",
    "not-sealed": "a number was not settled by both AIs.",
    "drawn-length": (
        "the number reader's two AIs agreed on a number that does not fit its drawn length."
    ),
    "crowded": "a number sits too close to another label to be read alone.",
    "edge": "a number is cut by the drawing's edge.",
    "reviewer-markup": "a number is covered by reviewer markup.",
    "ink-unchecked": "a number could not be checked for reviewer markup.",
    "stacked": "a number is a stacked fraction.",
    "not-plain": "a number has words or a sum in it.",
    "fraction-not-drawn": "a number has a fraction no drawing prints.",
    "mm-inch-disagree": "a number's millimetres and inches disagree.",
    "row-ambiguous": "the number reader was not sure which line is the countertop.",
    "slot-ambiguous": "a number sits between two pieces.",
    "vif": "a number is marked VIF (verify in field), so it waits for you.",
    "field-cut-included": "a number already includes the field cut, so it waits for you.",
    "counter-break": "the line includes a tall appliance or range bay, so it waits for you.",
}


def _result_line(code: str, attempt: int | None, role: str) -> str:
    try_ = f"try {attempt}" if attempt is not None else "trying again"
    if code == "helped":
        return f"{try_} helped; {role}'s two AIs now agree."
    if code == "not-helped":
        if attempt is not None and attempt < READING_EVENT_MAX_ATTEMPTS:
            return f"{try_} didn't help."
        return f"{try_} didn't help; it waits for you."
    if code == "allowance-used":
        return "not tried again: the try-again allowance for this set is used up. It waits for you."
    if code == "cannot-zoom":
        return (
            "not tried again: a bigger close-up would be too large for the AIs. It waits for you."
        )
    return f"{try_} finished."


def event_line(
    agent: Agent,
    kind: EventKind,
    code: str,
    change: str | None,
    page_number: int,
    attempt: int | None = None,
) -> str:
    """The plain sentence for one event, made from its codes. Never names an agent, only a role."""
    role = ROLE_NAMES[agent]
    page = f"Page {page_number}: "
    if kind is EventKind.ANSWERED:
        return f"{page}{role}'s AIs answered."
    if kind is EventKind.PASSED:
        if agent is Agent.REKHA:
            return f"{page}{role} found the countertop line."
        if agent is Agent.AKSHARA:
            return f"{page}{role} read a number and both AIs agreed."
        return f"{page}{role}'s inspector passed it."
    if kind is EventKind.STOPPED:
        known = {Agent.BALA: _PAGE_STOPS, Agent.REKHA: _LINE_STOPS, Agent.AKSHARA: _NUMBER_STOPS}
        return page + known.get(agent, {}).get(
            code, f"{role}'s inspector stopped it; it waits for you."
        )
    if kind is EventKind.TRYING_AGAIN:
        which = "" if attempt is None else f" (try {attempt} of {READING_EVENT_MAX_ATTEMPTS})"
        how = TRY_AGAIN_CHANGES.get(agent, {}).get(change or "", "")
        return f"{page}{role} is trying again{which}{how}."
    return page + _result_line(code, attempt, role)


# --- the reader ------------------------------------------------------------------------------------

ItemState = Literal["answered", "passed", "stopped", "trying_again", "helped", "waiting"]

#: What `now` and the summary say when the newest reading did not finish (its stage failed, and
#: nothing is reading now): its events are not the stored result, so none is shown.
DID_NOT_FINISH: Final = "The last reading did not finish, so what it did is not shown."


@dataclass(frozen=True, slots=True)
class ActivityItem:
    """One item's state, from its newest event."""

    page: int
    agent: str
    """The agent's own name, for the screen's hover."""
    role: str
    """The agent's plain role, the words the sentences use."""
    item: str
    state: ItemState
    code: str
    change: str | None
    attempt: int | None
    """Which try (1 to 3) when the newest event is a try-again event; null otherwise."""
    line: str
    at: datetime


@dataclass(frozen=True, slots=True)
class ActivitySummary:
    passed: int
    """Items whose newest event is a pass."""
    stopped: int
    """Items an inspector stopped (whatever happened next)."""
    tried_again: int
    helped: int
    waiting: int
    """Items that wait for a person: tried and not helped, plus stopped and never tried."""
    sentence: str
    """The reading's summary in one plain sentence."""


@dataclass(frozen=True, slots=True)
class ActivitySnapshot:
    reading_id: UUID
    finished: bool
    """Whether this reading's stage committed (its results are the stored ones)."""
    updated_at: datetime
    now: str
    items: tuple[ActivityItem, ...]
    summary: ActivitySummary


def _state(kind: EventKind, code: str) -> ItemState:
    if kind is EventKind.TRY_AGAIN_RESULT:
        return "helped" if code == "helped" else "waiting"
    return kind.value  # type: ignore[return-value]


def _items(count: int) -> str:
    return f"{count} item{'s' * (count != 1)}"


def _waits(count: int) -> str:
    return f"{_items(count)} wait{'s' * (count == 1)} for you"


def summary_sentence(*, tried_again: int, helped: int, waiting: int) -> str:
    """The reading's summary in one plain sentence (`ActivitySummary.sentence`)."""
    if tried_again:
        agree = "agrees" if helped == 1 else "agree"
        return f"Tried again on {_items(tried_again)}: {helped} now {agree}; {_waits(waiting)}."
    if waiting:
        return f"From the reading, {_waits(waiting)}."
    return "From the reading, nothing waits for you."


def _newest_reading(session: Session, package_revision_id: UUID) -> UUID | None:
    return session.execute(
        select(ReadingEvent.reading_id)
        .where(ReadingEvent.package_revision_id == package_revision_id)
        .order_by(ReadingEvent.created_at.desc(), ReadingEvent.position.desc())
        .limit(1)
    ).scalar_one_or_none()


def reading_activity_for(
    session: Session,
    package_revision_id: UUID,
    *,
    reading: bool,
    running: UUID | None = None,
) -> ActivitySnapshot | None:
    """What one reading did, or `None` when there is nothing to show.

    * **While the revision is read** (`reading`), only the running attempt is shown: `running`,
      the reading id on the progress row, and `now` is its newest item that is not a pass. With none named, or no event of it yet, `None`: an
      earlier attempt's events are never shown as the running one's.
    * **After**, the newest reading. If its stage committed (its `finished` marker), it is shown,
      and `now` is its summary. If not (the attempt failed and nothing reads now), its events are
      not the stored result: it is returned with no item, `finished` false and `DID_NOT_FINISH`.

    Each item's state is its newest event, found in SQL (`DISTINCT ON` the agent, the file and the
    item); the counts are made there too, and only the items shown are read out. Four small
    queries per poll, all on `(reading_id, created_at, position)`.
    """
    if reading:
        chosen = running
    else:
        chosen = _newest_reading(session, package_revision_id)
    if chosen is None:
        return None
    of_reading = (
        ReadingEvent.package_revision_id == package_revision_id,
        ReadingEvent.reading_id == chosen,
    )
    counts = session.execute(
        select(
            func.max(ReadingEvent.created_at),
            func.count().filter(ReadingEvent.kind == EventKind.FINISHED.value),
            func.count(
                func.distinct(
                    func.concat(
                        ReadingEvent.agent,
                        "|",
                        ReadingEvent.document_version_id,
                        "|",
                        ReadingEvent.item,
                    )
                )
            ).filter(ReadingEvent.kind == EventKind.STOPPED.value),
            func.count(
                func.distinct(
                    func.concat(
                        ReadingEvent.agent,
                        "|",
                        ReadingEvent.document_version_id,
                        "|",
                        ReadingEvent.item,
                    )
                )
            ).filter(ReadingEvent.kind == EventKind.TRYING_AGAIN.value),
        ).where(*of_reading)
    ).one()
    updated, finished_marks, stopped, tried = counts
    if updated is None:
        return None
    finished = bool(finished_marks)
    if not reading and not finished:
        return ActivitySnapshot(
            reading_id=chosen,
            finished=False,
            updated_at=updated,
            now=DID_NOT_FINISH,
            items=(),
            summary=ActivitySummary(0, 0, 0, 0, 0, DID_NOT_FINISH),
        )
    newest = (
        select(
            ReadingEvent.agent,
            ReadingEvent.item,
            ReadingEvent.kind,
            ReadingEvent.code,
            ReadingEvent.change,
            ReadingEvent.attempt,
            ReadingEvent.page_number,
            ReadingEvent.created_at,
            ReadingEvent.position,
        )
        .where(*of_reading, ReadingEvent.kind != EventKind.FINISHED.value)
        .ext(distinct_on(ReadingEvent.agent, ReadingEvent.document_version_id, ReadingEvent.item))
        .order_by(
            ReadingEvent.agent,
            ReadingEvent.document_version_id,
            ReadingEvent.item,
            ReadingEvent.created_at.desc(),
            ReadingEvent.position.desc(),
        )
        .subquery()
    )
    helped_code = newest.c.code == "helped"
    tallies = session.execute(
        select(
            func.count().filter(newest.c.kind == EventKind.PASSED.value),
            func.count().filter((newest.c.kind == EventKind.TRY_AGAIN_RESULT.value) & helped_code),
            func.count().filter(
                (newest.c.kind == EventKind.STOPPED.value)
                | ((newest.c.kind == EventKind.TRY_AGAIN_RESULT.value) & ~helped_code)
            ),
        )
    ).one()
    passed, helped, waiting = (int(value) for value in tallies)
    summary = ActivitySummary(
        passed=passed,
        stopped=int(stopped),
        tried_again=int(tried),
        helped=helped,
        waiting=waiting,
        sentence=summary_sentence(tried_again=int(tried), helped=helped, waiting=waiting),
    )

    def newest_of(*kinds: str, limit: int) -> list[ActivityItem]:
        rows = session.execute(
            select(newest)
            .where(newest.c.kind.in_(kinds))
            .order_by(newest.c.created_at.desc(), newest.c.position.desc())
            .limit(limit)
        ).all()
        found: list[ActivityItem] = []
        for row in rows:
            agent, kind = Agent(row.agent), EventKind(row.kind)
            found.append(
                ActivityItem(
                    page=row.page_number,
                    agent=agent.value,
                    role=ROLE_NAMES[agent],
                    item=row.item,
                    state=_state(kind, row.code),
                    code=row.code,
                    change=row.change,
                    attempt=row.attempt,
                    line=event_line(
                        agent, kind, row.code, row.change, row.page_number, row.attempt
                    ),
                    at=row.created_at,
                )
            )
        return found

    # Newest first: the items that need the reviewer's eye, then (only if room is left) pages whose
    # AIs answered and nothing more has happened on them yet. Passes are only counted.
    needs_eye = newest_of(
        EventKind.STOPPED.value,
        EventKind.TRYING_AGAIN.value,
        EventKind.TRY_AGAIN_RESULT.value,
        limit=ACTIVITY_ITEMS_SHOWN,
    )
    answered = newest_of(EventKind.ANSWERED.value, limit=ACTIVITY_ITEMS_SHOWN)
    shown = [*needs_eye, *answered[: ACTIVITY_ITEMS_SHOWN - len(needs_eye)]]
    if reading:
        latest = sorted([*needs_eye[:1], *answered[:1]], key=lambda item: item.at, reverse=True)
        now = latest[0].line if latest else summary.sentence
    else:
        now = summary.sentence
    return ActivitySnapshot(
        reading_id=chosen,
        finished=finished,
        updated_at=updated,
        now=now,
        items=tuple(shown),
        summary=summary,
    )
