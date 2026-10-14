"""`GET .../packages/{id}` says what each agent did in the newest reading (#1238).

Synthetic events only, written with the worker's own writer: one older reading and one newer one.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.models import PackageRevision, PackageState
from app.runs.reading_activity import ActivityEvent, EventKind, record_events
from tests.api.test_package_reading_progress import _revision, _walk
from tests.api.test_packages import PROJECT, _client, _new_package, session, store
from workflow.pariksha_gates import Agent

pytest_plugins = ("tests.app.postgres_fixture",)

__all__ = ["session", "store"]  # fixtures, imported for this module's tests


def _write(session: Session, revision_id: UUID, reading: UUID, *events: ActivityEvent) -> None:
    assert record_events(session.get_bind(), revision_id, reading, events)  # type: ignore[arg-type]


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

    activity = _client(session, store).get(f"/api/v1/projects/{PROJECT}/packages/{package_id}")
    body = activity.json()["activity"]

    assert body["reading_id"] == str(newer)
    assert [
        (item["page"], item["item"], item["state"], item["code"]) for item in body["items"]
    ] == [
        (13, "p12:slot2:0", "stopped", "readers-differ"),
        (13, "p12:labels", "answered", "all-answered"),
        (12, "p11:row-choice", "stopped", "no-printed-numbers"),
    ], "newest first; passes are never listed; the older reading and other revisions never"
    first = body["items"][0]
    assert first["line"] == "Page 13: the number reader's two AIs read a number differently."
    assert first["agent"] == "Akshara" and first["role"] == "the number reader"
    assert first["change"] is None and first["at"]
    assert body["items"][2]["line"] == (
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
    _two_readings(session, revision.id)
    stored = session.get(PackageRevision, revision.id)
    assert stored is not None
    stored.state = PackageState.AWAITING_REVIEW
    session.commit()

    answer = _client(session, store).get(f"/api/v1/projects/{PROJECT}/packages/{package_id}")
    body = answer.json()

    assert body["state"] == "AWAITING_REVIEW"
    assert body["reading"] is None
    assert body["activity"] is not None
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
    )

    body = _client(session, store).get(f"/api/v1/projects/{PROJECT}/packages/{package_id}").json()

    assert len(body["activity"]["items"]) == 20
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
        "sentence": "Tried again on 2 items: 1 now agree; 1 item waits for you.",
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
        "Tried again on 2 items: 1 now agree; 2 items wait for you."
    )
