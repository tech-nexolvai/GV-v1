"""`GET .../packages/{id}` says what each agent did in the reading (#1238).

Synthetic events only, written with the worker's own writer: one older reading and one newer one.
While the revision is read, only the attempt the progress row names is shown; after, only the
newest reading, and only if its stage finished (its `finished` marker).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.lifecycle.side_states import enter_failure
from app.models import PackageRevision, PackageState
from app.runs.reading_activity import (
    DID_NOT_FINISH,
    ActivityEvent,
    EventKind,
    mark_reading_finished,
    record_events,
)
from app.runs.reading_progress import record_progress
from tests.api.test_package_reading_progress import _revision, _walk
from tests.api.test_packages import PROJECT, _client, _new_package, session, store
from workflow.pariksha_gates import Agent

pytest_plugins = ("tests.app.postgres_fixture",)

__all__ = ["session", "store"]  # fixtures, imported for this module's tests


def _write(session: Session, revision_id: UUID, reading: UUID, *events: ActivityEvent) -> None:
    assert record_events(session.get_bind(), revision_id, reading, events)  # type: ignore[arg-type]


def _running(session: Session, revision_id: UUID, reading: UUID) -> None:
    """The progress row names `reading` as the running attempt, as the reading stage's does."""
    assert record_progress(
        session.get_bind(), revision_id, "dimensions", reading_id=reading  # type: ignore[arg-type]
    )


def _finished(session: Session, revision_id: UUID, reading: UUID) -> None:
    """The reading's stage committed, with its `finished` marker."""
    mark_reading_finished(session, revision_id, reading)
    session.commit()


def _get(session: Session, store: Any, package_id: UUID) -> Any:
    return _client(session, store).get(f"/api/v1/projects/{PROJECT}/packages/{package_id}").json()


def _two_readings(session: Session, revision_id: UUID) -> UUID:
    """An older reading that split on page 3, then a newer one; returns the newer reading's id."""
    _write(
        session,
        revision_id,
        uuid4(),
        ActivityEvent(3, Agent.REKHA, "p2:row-choice", EventKind.STOPPED, "readers-differ"),
    )
    newer = uuid4()
    _write(
        session,
        revision_id,
        newer,
        ActivityEvent(12, Agent.REKHA, "p11:row-choice", EventKind.ANSWERED, "all-answered"),
        ActivityEvent(12, Agent.REKHA, "p11:row-choice", EventKind.STOPPED, "no-printed-numbers"),
        ActivityEvent(13, Agent.REKHA, "p12:row-choice", EventKind.ANSWERED, "all-answered"),
        ActivityEvent(13, Agent.REKHA, "p12:row-choice", EventKind.PASSED, "printed-numbers"),
    )
    _write(
        session,
        newer,
        newer,
        ActivityEvent(99, Agent.REKHA, "p98:row-choice", EventKind.STOPPED, "readers-differ"),
    )  # another revision's id: never shown here
    _write(
        session,
        revision_id,
        newer,
        ActivityEvent(13, Agent.AKSHARA, "p12:labels", EventKind.ANSWERED, "all-answered"),
        ActivityEvent(13, Agent.AKSHARA, "p12:slot0:0", EventKind.PASSED, "sealed"),
        ActivityEvent(13, Agent.AKSHARA, "p12:slot1:0", EventKind.PASSED, "sealed"),
        ActivityEvent(13, Agent.AKSHARA, "p12:slot2:0", EventKind.STOPPED, "readers-differ"),
    )
    return newer


def _reading(session: Session) -> tuple[UUID, PackageRevision]:
    package_id = _new_package(session)
    revision = _revision(session, package_id)
    _walk(
        session,
        revision.id,
        PackageState.UPLOADING,
        PackageState.UPLOADED,
        PackageState.INGESTING,
        PackageState.EXTRACTING,
    )
    return package_id, revision


def test_before_any_event_there_is_no_activity(session: Session, store: Any) -> None:
    package_id, _revision_row = _reading(session)

    answer = _client(session, store).get(f"/api/v1/projects/{PROJECT}/packages/{package_id}")

    assert answer.status_code == 200
    assert answer.json()["activity"] is None


def test_while_reading_only_the_newest_reading_is_shown_in_plain_role_words(
    session: Session, store: Any
) -> None:
    package_id, revision = _reading(session)
    newer = _two_readings(session, revision.id)
    _running(session, revision.id, newer)

    body = _get(session, store, package_id)["activity"]

    assert body["reading_id"] == str(newer)
    assert body["finished"] is False, "still running"
    assert [
        (item["page"], item["item"], item["state"], item["code"]) for item in body["items"]
    ] == [
        (13, "p12:slot2:0", "stopped", "readers-differ"),
        (12, "p11:row-choice", "stopped", "no-printed-numbers"),
        (13, "p12:labels", "answered", "all-answered"),
    ], (
        "newest first, the pages whose AIs only answered after; passes are never listed; the "
        "older reading and other revisions never"
    )
    first = body["items"][0]
    assert first["line"] == "Page 13: the number reader's two AIs read a number differently."
    assert first["agent"] == "Akshara" and first["role"] == "the number reader"
    assert first["change"] is None and first["at"]
    assert body["items"][1]["line"] == (
        "Page 12: the line finder's chosen line has no printed numbers."
    )
    assert body["now"] == first["line"]
    assert body["summary"] == {
        "passed": 3,
        "stopped": 2,
        "tried_again": 0,
        "helped": 0,
        "waiting": 2,
        "sentence": "From the reading, 2 items wait for you.",
    }
    assert all(item["attempt"] is None for item in body["items"])
    assert not any(
        name in item["line"] or name in body["now"]
        for item in body["items"]
        for name in ("Rekha", "Akshara")
    ), "the sentences name roles; the agent's name is for hover only"


def test_after_the_reading_the_activity_stays_with_its_summary(
    session: Session, store: Any
) -> None:
    package_id, revision = _reading(session)
    newer = _two_readings(session, revision.id)
    _finished(session, revision.id, newer)
    stored = session.get(PackageRevision, revision.id)
    assert stored is not None
    stored.state = PackageState.AWAITING_REVIEW
    session.commit()

    answer = _client(session, store).get(f"/api/v1/projects/{PROJECT}/packages/{package_id}")
    body = answer.json()

    assert body["state"] == "AWAITING_REVIEW"
    assert body["reading"] is None
    assert body["activity"] is not None
    assert body["activity"]["finished"] is True
    assert body["activity"]["now"] == "From the reading, 2 items wait for you."
    assert len(body["activity"]["items"]) == 3


def test_the_list_shows_at_most_twenty_items_but_counts_them_all(
    session: Session, store: Any
) -> None:
    package_id, revision = _reading(session)
    reading = uuid4()
    _write(
        session,
        revision.id,
        reading,
        *(
            ActivityEvent(page, Agent.REKHA, f"p{page - 1}:row-choice", EventKind.STOPPED, code)
            for page in range(1, 26)
            for code in ("readers-differ",)
        ),
        *(
            ActivityEvent(page, Agent.AKSHARA, f"p{page - 1}:labels", EventKind.ANSWERED, "all-answered")
            for page in range(1, 26)
        ),
    )  # fmt: skip
    _running(session, revision.id, reading)

    body = _get(session, store, package_id)

    assert len(body["activity"]["items"]) == 20
    assert {item["state"] for item in body["activity"]["items"]} == {
        "stopped"
    }, "pages whose AIs only answered never take a place from an item that needs the reviewer"
    assert body["activity"]["items"][0]["page"] == 25
    assert body["activity"]["summary"]["stopped"] == 25
    assert body["activity"]["summary"]["waiting"] == 25


def test_the_package_list_never_carries_activity(session: Session, store: Any) -> None:
    _package_id, revision = _reading(session)
    _two_readings(session, revision.id)

    listed = _client(session, store).get(f"/api/v1/projects/{PROJECT}/packages")

    assert all(item["activity"] is None for item in listed.json()["items"])


def test_trying_again_shows_each_try_and_how_it_went(session: Session, store: Any) -> None:
    """Phase 3, PR B emits these; the shape and the words are fixed here (#1238)."""
    package_id, revision = _reading(session)
    reading = uuid4()
    _running(session, revision.id, reading)
    line, number, other = "p11:row-choice", "p11:slot0:0", "p11:slot1:0"
    _write(
        session,
        revision.id,
        reading,
        ActivityEvent(12, Agent.REKHA, line, EventKind.STOPPED, "readers-differ"),
        ActivityEvent(
            12, Agent.REKHA, line, EventKind.TRYING_AGAIN, "readers-differ",
            "tie-break-disputed-lines", 1,
        ),
        ActivityEvent(12, Agent.REKHA, line, EventKind.TRY_AGAIN_RESULT, "helped", None, 1),
        ActivityEvent(12, Agent.AKSHARA, number, EventKind.STOPPED, "readers-differ"),
        ActivityEvent(
            12, Agent.AKSHARA, number, EventKind.TRYING_AGAIN, "readers-differ",
            "closer-look-2x", 1,
        ),
        ActivityEvent(12, Agent.AKSHARA, number, EventKind.TRY_AGAIN_RESULT, "not-helped", None, 1),
        ActivityEvent(
            12, Agent.AKSHARA, number, EventKind.TRYING_AGAIN, "readers-differ",
            "closer-look-3x-upright", 2,
        ),
        ActivityEvent(12, Agent.AKSHARA, other, EventKind.STOPPED, "unreadable"),
    )  # fmt: skip
    client = _client(session, store)

    body = client.get(f"/api/v1/projects/{PROJECT}/packages/{package_id}").json()["activity"]

    assert [
        (item["item"], item["state"], item["code"], item["change"], item["attempt"])
        for item in body["items"]
    ] == [
        (other, "stopped", "unreadable", None, None),
        (number, "trying_again", "readers-differ", "closer-look-3x-upright", 2),
        (line, "helped", "helped", None, 1),
    ]
    assert body["items"][1]["line"] == (
        "Page 12: the number reader is trying again (try 2 of 3) with a 3× close-up, "
        "turned upright."
    )
    assert body["items"][2]["line"] == "Page 12: try 1 helped; the line finder's two AIs now agree."
    assert body["now"] == "Page 12: the number reader could not read a number clearly."
    assert body["summary"] == {
        "passed": 0,
        "stopped": 3,
        "tried_again": 2,
        "helped": 1,
        "waiting": 1,
        "sentence": "Tried again on 2 items: 1 now agrees; 1 item waits for you.",
    }

    _write(
        session,
        revision.id,
        reading,
        ActivityEvent(12, Agent.AKSHARA, number, EventKind.TRY_AGAIN_RESULT, "not-helped", None, 2),
        ActivityEvent(
            12, Agent.AKSHARA, number, EventKind.TRYING_AGAIN, "readers-differ", "tighter-crop", 3
        ),
        ActivityEvent(12, Agent.AKSHARA, number, EventKind.TRY_AGAIN_RESULT, "not-helped", None, 3),
    )
    body = client.get(f"/api/v1/projects/{PROJECT}/packages/{package_id}").json()["activity"]

    assert body["items"][0]["state"] == "waiting" and body["items"][0]["attempt"] == 3
    assert body["items"][0]["line"] == "Page 12: try 3 didn't help; it waits for you."
    assert body["summary"]["waiting"] == 2
    assert body["summary"]["sentence"] == (
        "Tried again on 2 items: 1 now agrees; 2 items wait for you."
    )


def test_while_reading_a_running_attempt_with_no_event_yet_shows_nothing_of_an_older_one(
    session: Session, store: Any
) -> None:
    """A re-read of the same revision: the earlier reading finished, the new attempt has started
    (named on the progress row) and told nothing yet. Its screen must not show the old events as
    if they were the new attempt's."""
    package_id, revision = _reading(session)
    first = _two_readings(session, revision.id)
    _finished(session, revision.id, first)
    second = uuid4()
    _running(session, revision.id, second)

    assert _get(session, store, package_id)["activity"] is None

    _write(
        session,
        revision.id,
        second,
        ActivityEvent(5, Agent.REKHA, "p4:row-choice", EventKind.STOPPED, "readers-differ"),
    )
    body = _get(session, store, package_id)["activity"]

    assert body["reading_id"] == str(second)
    assert [item["item"] for item in body["items"]] == ["p4:row-choice"]

    _finished(session, revision.id, second)
    stored = session.get(PackageRevision, revision.id)
    assert stored is not None
    stored.state = PackageState.AWAITING_REVIEW
    session.commit()
    after = _get(session, store, package_id)["activity"]

    assert after["reading_id"] == str(second) and after["finished"] is True
    assert after["now"] == "From the reading, 1 item waits for you."


def test_a_reading_whose_stage_failed_is_never_shown_as_the_result(
    session: Session, store: Any
) -> None:
    """The stage rolled back (no `finished` marker) and nothing is reading now: its events are not
    the stored result. The activity says the reading did not finish and lists nothing."""
    package_id, revision = _reading(session)
    attempt = _two_readings(session, revision.id)
    _running(session, revision.id, attempt)
    enter_failure(session, revision.id, actor="the worker", error=TimeoutError("synthetic"))
    session.commit()

    body = _get(session, store, package_id)

    assert body["state"] == "FAILED_RETRYABLE" and body["reading"] is None
    activity = body["activity"]
    assert activity["reading_id"] == str(attempt)
    assert activity["finished"] is False
    assert activity["items"] == []
    assert activity["now"] == DID_NOT_FINISH == activity["summary"]["sentence"]
    assert activity["summary"]["stopped"] == activity["summary"]["waiting"] == 0

    retry = uuid4()
    _running(session, revision.id, retry)  # the retry's progress line, after the failure
    _write(
        session,
        revision.id,
        retry,
        ActivityEvent(7, Agent.REKHA, "p6:row-choice", EventKind.STOPPED, "readers-differ"),
    )
    retrying = _get(session, store, package_id)

    assert retrying["reading"]["retrying"] is True
    assert retrying["activity"]["reading_id"] == str(retry)
    assert [item["item"] for item in retrying["activity"]["items"]] == ["p6:row-choice"]


def test_the_same_page_of_two_files_is_two_items(session: Session, store: Any) -> None:
    """Page indexes start again in every file: an item is the agent, the file and the question."""
    package_id, revision = _reading(session)
    reading = uuid4()
    _running(session, revision.id, reading)
    first, second = uuid4(), uuid4()
    _write(
        session,
        revision.id,
        reading,
        ActivityEvent(
            3, Agent.REKHA, "p2:row-choice", EventKind.STOPPED, "readers-differ",
            document_version_id=first,
        ),
        ActivityEvent(
            3, Agent.REKHA, "p2:row-choice", EventKind.PASSED, "printed-numbers",
            document_version_id=second,
        ),
    )  # fmt: skip

    body = _get(session, store, package_id)["activity"]

    assert [item["state"] for item in body["items"]] == ["stopped"]
    assert body["summary"]["passed"] == 1 and body["summary"]["stopped"] == 1


def test_a_page_code_proved_blank_is_not_counted_as_waiting_for_you(
    session: Session, store: Any
) -> None:
    """#1249: the page sorter's `blank-page` stop is nothing to read; a page it could not read
    still waits for the reviewer, and its line is the one listed."""
    package_id, revision = _reading(session)
    reading = uuid4()
    _write(
        session,
        revision.id,
        reading,
        ActivityEvent(2, Agent.BALA, "p1:page", EventKind.STOPPED, "blank-page"),
        ActivityEvent(5, Agent.BALA, "p4:page", EventKind.STOPPED, "no-row-found"),
        ActivityEvent(7, Agent.REKHA, "p6:row-choice", EventKind.PASSED, "printed-numbers"),
    )
    _finished(session, revision.id, reading)
    stored = session.get(PackageRevision, revision.id)
    assert stored is not None
    stored.state = PackageState.AWAITING_REVIEW
    session.commit()

    activity = _get(session, store, package_id)["activity"]

    assert activity["summary"] == {
        "passed": 2,
        "stopped": 2,
        "tried_again": 0,
        "helped": 0,
        "waiting": 1,
        "sentence": "From the reading, 1 item waits for you.",
    }
    assert activity["now"] == "From the reading, 1 item waits for you."
    assert [(item["page"], item["state"], item["line"]) for item in activity["items"]] == [
        (5, "stopped", "Page 5: the page sorter found no countertop line to read on this page.")
    ]
