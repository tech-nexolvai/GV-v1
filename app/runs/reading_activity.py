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
* `reading_activity_for` is the reader the API calls. It shows only the revision's newest reading
  (one id per stage attempt), also after that reading has ended.

**Codes only.** An event stores a page number, an agent, a code question id (`p11:row-choice`), a
kind and a short code (`readers-differ`), never drawing text; the database refuses anything else
(`app.models.runs.ReadingEvent`). The plain sentence a person reads is made here from the codes,
naming each agent by its plain role ("the line finder", "the number reader"); the agent's own name
is a separate field the screen shows on hover (Anant, 2026-10-14, phase 3 decision 1).

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

from sqlalchemy import Engine, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.base import utc_now
from app.models.runs import (
    READING_EVENT_CODE_MAX,
    READING_EVENT_CODE_PATTERN,
    READING_EVENT_ITEM_MAX,
    READING_EVENT_ITEM_PATTERN,
    READING_EVENT_MAX_ATTEMPTS,
    ReadingEvent,
)
from app.runs.reading_progress import wait_briefly_for_locks
from workflow.pariksha_gates import Agent

__all__ = [
    "ACTIVITY_ITEMS_SHOWN",
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
    """Phase 3, PR B: the item is asked again, with what changed (`change`)."""
    TRY_AGAIN_RESULT = "try_again_result"
    """Phase 3, PR B: `helped`, `not-helped` or `allowance-used`."""


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

#: How many items the API lists: the newest that are not passes (passes are only counted).
ACTIVITY_ITEMS_SHOWN: Final = 20

_ITEM = re.compile(READING_EVENT_ITEM_PATTERN)
_CODE = re.compile(READING_EVENT_CODE_PATTERN)


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


@dataclass(frozen=True, slots=True)
class ActivityEvent:
    """One event as the reader emits it. Refuses anything that is not a code.

    A try-again event (`trying_again`, `try_again_result`) names its try (`attempt`, 1 to 3); a
    `trying_again` event also names what changed, one of `TRY_AGAIN_CHANGES` for its agent, and a
    result's code is one of `TRY_AGAIN_RESULTS`. Other events carry neither.
    """

    page_number: int
    agent: Agent
    item: str
    """A code question id: `p<page index>:` then lower-case letters, digits, `:` and `-`."""
    kind: EventKind
    code: str
    change: str | None = None
    attempt: int | None = None

    def __post_init__(self) -> None:
        if self.page_number < 1:
            raise ValueError("an event's page number starts at 1")
        if len(self.item) > READING_EVENT_ITEM_MAX or not _ITEM.fullmatch(self.item):
            raise ValueError("an event's item is a code question id, never text")
        for value in (self.code, self.change):
            if value is not None and (
                len(value) > READING_EVENT_CODE_MAX or not _CODE.fullmatch(value)
            ):
                raise ValueError("an event's code and change are short codes, never text")
        trying = self.kind in (EventKind.TRYING_AGAIN, EventKind.TRY_AGAIN_RESULT)
        if not trying:
            if self.change is not None or self.attempt is not None:
                raise ValueError("only a try-again event names a change or a try")
            return
        if self.attempt is None or not 1 <= self.attempt <= READING_EVENT_MAX_ATTEMPTS:
            raise ValueError(f"a try-again event names its try, 1 to {READING_EVENT_MAX_ATTEMPTS}")
        if self.kind is EventKind.TRYING_AGAIN:
            if self.change not in TRY_AGAIN_CHANGES.get(self.agent, {}):
                raise ValueError("a try names what it changed, one of the agent's known changes")
        elif self.code not in TRY_AGAIN_RESULTS or self.change is not None:
            raise ValueError("a try's result is one of the known results")


class ActivityHook(Protocol):
    """Told each batch of events as it happens. Watching only: it never changes a reading."""

    def record(self, events: Sequence[ActivityEvent]) -> None: ...


def record_events(
    engine: Engine | None,
    package_revision_id: UUID,
    reading_id: UUID,
    events: Sequence[ActivityEvent],
) -> bool:
    """Write one batch of events in its own committed transaction. Never raises.

    Returns whether the batch was written. A failure is logged by type only (a database error can
    quote the row it refused) and swallowed: the reading goes on whether or not it can be watched.
    """
    if engine is None or not events:
        return False
    try:
        now = utc_now()
        rows = [
            {
                "id": uuid4(),
                "created_at": now,
                "package_revision_id": package_revision_id,
                "reading_id": reading_id,
                "position": position,
                "page_number": event.page_number,
                "agent": Agent(event.agent).value,
                "item": event.item,
                "kind": EventKind(event.kind).value,
                "code": event.code,
                "change": event.change,
                "attempt": event.attempt,
            }
            for position, event in enumerate(events)
        ]
        with Session(engine) as side, side.begin():
            wait_briefly_for_locks(side)
            side.execute(insert(ReadingEvent), rows)
    except Exception as error:  # noqa: BLE001 - watching is never allowed to stop a reading
        logger.warning("reading activity not recorded: %s", type(error).__name__)
        return False
    return True


class ActivityRecorder:
    """The hook a reading stage hands the reader: one revision, one reading id. Never raises."""

    def __init__(self, engine: Engine | None, package_revision_id: UUID, reading_id: UUID) -> None:
        self._engine = engine
        self._revision = package_revision_id
        self.reading_id = reading_id

    def record(self, events: Sequence[ActivityEvent]) -> None:
        record_events(self._engine, self._revision, self.reading_id, events)


# --- the words -------------------------------------------------------------------------------------

#: Why the line finder's inspector stopped a page, after "Page N: ".
_LINE_STOPS: Final[dict[str, str]] = {
    "readers-differ": "the line finder's two AIs picked different lines.",
    "no-printed-numbers": "the line finder's chosen line has no printed numbers.",
    "no-countertop": "the line finder found no countertop line.",
    "reader-unsure": "the line finder was not sure which line is the countertop.",
    "not-among-lines": "the line finder saw a countertop that none of the lines measures.",
    "no-answer": "the line finder got no answer from one of its AIs.",
    "not-asked": "the line finder was not asked: the AI budget was reached.",
}

#: Why the number reader's inspector held a number, after "Page N: ".
_NUMBER_STOPS: Final[dict[str, str]] = {
    "readers-differ": "the number reader's two AIs read a number differently.",
    "unsure": "the number reader was not sure a number belongs to its piece.",
    "unreadable": "the number reader could not read a number clearly.",
    "one-reader-missing": "only one of the number reader's AIs read a number.",
    "not-asked": "a number was not read.",
    "no-label": "the number reader found no number for a piece.",
    "two-labels": "the number reader found more than one number for a piece.",
    "row-held": "a number was read, but its line waits for you.",
    "not-offered": "a number was read, but it was not offered to the form.",
    "not-sealed": "a number was not settled by both AIs.",
    "drawn-length": "the number reader's two AIs agreed on a number that does not fit its drawn length.",
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
        known = (
            _LINE_STOPS if agent is Agent.REKHA else _NUMBER_STOPS if agent is Agent.AKSHARA else {}
        )
        return page + known.get(code, f"{role}'s inspector stopped it; it waits for you.")
    if kind is EventKind.TRYING_AGAIN:
        which = "" if attempt is None else f" (try {attempt} of {READING_EVENT_MAX_ATTEMPTS})"
        how = TRY_AGAIN_CHANGES.get(agent, {}).get(change or "", "")
        return f"{page}{role} is trying again{which}{how}."
    return page + _result_line(code, attempt, role)


# --- the reader ------------------------------------------------------------------------------------

ItemState = Literal["answered", "passed", "stopped", "trying_again", "helped", "waiting"]


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
        return f"Tried again on {_items(tried_again)}: {helped} now agree; {_waits(waiting)}."
    if waiting:
        return f"From the reading, {_waits(waiting)}."
    return "From the reading, nothing waits for you."


def reading_activity_for(
    session: Session, package_revision_id: UUID, *, reading: bool
) -> ActivitySnapshot | None:
    """The newest reading's activity, or `None` when the revision has none.

    Only the newest reading (the reading id of the newest event) is shown; an older attempt's
    events stay stored and are never mixed in. `reading` says whether the revision is being read
    now: while it is, `now` is the newest item's sentence; after, it is the reading's summary.
    """
    newest = session.execute(
        select(ReadingEvent.reading_id)
        .where(ReadingEvent.package_revision_id == package_revision_id)
        .order_by(ReadingEvent.created_at.desc(), ReadingEvent.position.desc())
        .limit(1)
    ).scalar_one_or_none()
    if newest is None:
        return None
    rows = session.scalars(
        select(ReadingEvent)
        .where(
            ReadingEvent.package_revision_id == package_revision_id,
            ReadingEvent.reading_id == newest,
        )
        .order_by(ReadingEvent.created_at, ReadingEvent.position)
    ).all()
    latest: dict[tuple[str, str], ReadingEvent] = {}
    stopped: set[tuple[str, str]] = set()
    tried: set[tuple[str, str]] = set()
    for row in rows:
        key = (row.agent, row.item)
        latest.pop(key, None)  # re-inserted, so `latest` keeps the newest-event order
        latest[key] = row
        if row.kind == EventKind.STOPPED.value:
            stopped.add(key)
        elif row.kind == EventKind.TRYING_AGAIN.value:
            tried.add(key)
    items: list[ActivityItem] = []
    for row in latest.values():
        agent, kind = Agent(row.agent), EventKind(row.kind)
        items.append(
            ActivityItem(
                page=row.page_number,
                agent=agent.value,
                role=ROLE_NAMES[agent],
                item=row.item,
                state=_state(kind, row.code),
                code=row.code,
                change=row.change,
                attempt=row.attempt,
                line=event_line(agent, kind, row.code, row.change, row.page_number, row.attempt),
                at=row.created_at,
            )
        )
    states = [item.state for item in items]
    helped = states.count("helped")
    waiting = sum(state in ("stopped", "waiting") for state in states)
    summary = ActivitySummary(
        passed=states.count("passed"),
        stopped=len(stopped),
        tried_again=len(tried),
        helped=helped,
        waiting=waiting,
        sentence=summary_sentence(tried_again=len(tried), helped=helped, waiting=waiting),
    )
    shown = [item for item in reversed(items) if item.state != "passed"]
    if reading:
        now = shown[0].line if shown else items[-1].line
    else:
        now = summary.sentence
    return ActivitySnapshot(
        reading_id=newest,
        updated_at=rows[-1].created_at,
        now=now,
        items=tuple(shown[:ACTIVITY_ITEMS_SHOWN]),
        summary=summary,
    )
