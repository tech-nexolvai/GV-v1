"""A reading the AI budget stops: its live activity counts what the stored reading keeps (#1238).

The #1206 fixture: page 1 is read in full, then the budget refuses the next question, so pages 2
and 3 are listed unread with the budget named. The events must say exactly that, per agent, as
Pariksha's `gate_tally` counts the stored reading: the page sorter stopped pages 2 and 3, the line
finder passed only page 1's line, and the number reader's events are page 1's only. Synthetic
drawings and fake readers only; no call leaves the test.
"""

from __future__ import annotations

from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.runs import ReadingEvent
from storage.local import LocalStore
from tests.workflow.test_budget_stop import _stopped_reading, session, store
from workflow.pariksha_gates import Agent
from workflow.pariksha_run_check import gate_tally

pytest_plugins = ("tests.app.postgres_fixture",)

__all__ = ["session", "store"]  # fixtures, imported for this module's tests


def test_a_budget_stop_is_counted_per_agent_exactly_as_the_stored_reading(
    session: Session, store: LocalStore
) -> None:
    revision, _readers = _stopped_reading(session, store)

    rows = session.scalars(
        select(ReadingEvent).where(
            ReadingEvent.package_revision_id == revision.id, ReadingEvent.kind != "finished"
        )
    ).all()
    told = Counter((row.agent, row.kind) for row in rows)
    tally = {(count.agent.value, count.gate): count for count in gate_tally(session, revision.id)}

    for agent in (Agent.BALA, Agent.REKHA, Agent.AKSHARA):
        stops = sum(count.stopped for (who, _gate), count in tally.items() if who == agent.value)
        assert told[(agent.value, "stopped")] == stops, agent
    line = tally[("Rekha", "row-has-printed-numbers")]
    assert told[("Rekha", "passed")] == line.passed + line.recorded
    assert told[("Akshara", "passed")] == tally[("Akshara", "seal")].passed
    budget = sorted(
        row.page_number for row in rows if row.agent == "Bala" and row.code == "budget-reached"
    )
    assert budget == [2, 3], "each unread page is told once, with the budget named"
    assert {row.page_number for row in rows if row.agent == "Akshara"} == {1}
    assert {row.page_number for row in rows if (row.agent, row.kind) == ("Rekha", "passed")} == {1}


def test_the_committed_reading_is_marked_finished_once_and_named_on_the_progress_row(
    session: Session, store: LocalStore
) -> None:
    from app.models.runs import ReadingProgress

    revision, _readers = _stopped_reading(session, store)

    readings = set(
        session.scalars(
            select(ReadingEvent.reading_id).where(ReadingEvent.package_revision_id == revision.id)
        )
    )
    finished = session.scalars(
        select(ReadingEvent).where(
            ReadingEvent.package_revision_id == revision.id, ReadingEvent.kind == "finished"
        )
    ).all()
    progress = session.scalar(
        select(ReadingProgress).where(ReadingProgress.package_revision_id == revision.id)
    )
    assert len(readings) == 1
    assert [row.reading_id for row in finished] == list(readings)
    assert progress is not None and progress.reading_id in readings
