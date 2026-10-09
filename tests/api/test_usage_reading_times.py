"""A real reading time per drawing set, from the package state events (#1071).

`package_reading_times` spans the saved AI call timestamps, and the calls of one reading are saved
together when it finishes, so that span is about zero. These tests pin the replacement: the time from
the first `EXTRACTING` event to the revision reaching review, or to the stop that ended it.
All values are synthetic.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event
from sqlalchemy.orm import Session

from alembic import command
from app.api.dependencies import get_session
from app.auth import Principal, Role, authenticate
from app.config import Settings
from app.db.session import session_factory
from app.main import API_PREFIX, create_app
from app.models import (
    ModelInvocation,
    Package,
    PackageRevision,
    PackageStateEvent,
    Project,
)
from app.schemas.visual_ui import UsageOut
from tests.app.postgres_fixture import alembic_config

pytest_plugins = ("tests.app.postgres_fixture",)

T0 = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)


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


def _client(session: Session, project_id: UUID) -> TestClient:
    app = create_app(Settings(database_url="postgresql+psycopg://gv:gv@localhost:5433/gv"))
    app.dependency_overrides[authenticate] = lambda: Principal(
        id="reviewer", roles=frozenset({Role.REVIEWER}), projects=frozenset({project_id})
    )
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


def _project(session: Session) -> UUID:
    project = Project(name="Synthetic project")
    session.add(project)
    session.flush()
    return project.id


def _package(session: Session, project_id: UUID) -> UUID:
    package = Package(project_id=project_id, vendor="Synthetic vendor", product_type="countertop")
    session.add(package)
    session.flush()
    return package.id


def _revision(
    session: Session,
    package_id: UUID,
    walk: list[tuple[str, timedelta]],
    *,
    number: int = 1,
    supersedes: UUID | None = None,
) -> UUID:
    """A revision whose history is `walk`: (state entered, minutes after T0), after CREATED."""
    revision = PackageRevision(
        package_id=package_id,
        revision_number=number,
        state=walk[-1][0] if walk else "CREATED",
        supersedes_id=supersedes,
    )
    session.add(revision)
    session.flush()
    previous: str | None = None
    for sequence, (state, offset) in enumerate([("CREATED", timedelta(0)), *walk]):
        session.add(
            PackageStateEvent(
                package_revision_id=revision.id,
                sequence=sequence,
                from_state=previous,
                to_state=state,
                actor="synthetic test",
                created_at=T0 + offset,
            )
        )
        previous = state
    session.flush()
    return revision.id


def _minutes(value: float) -> timedelta:
    return timedelta(minutes=value)


UPLOADED = [("UPLOADING", _minutes(-10)), ("UPLOADED", _minutes(-9)), ("INGESTING", _minutes(-1))]
PIPELINE_TAIL = [
    ("MATCHING", _minutes(2)),
    ("VALIDATING_EVIDENCE", _minutes(2.5)),
    ("RUNNING_CHECKS", _minutes(2.7)),
    ("GENERATING_OUTPUTS", _minutes(2.9)),
]


def _usage(session: Session, project_id: UUID, query: str = "") -> dict[str, object]:
    response = _client(session, project_id).get(f"{API_PREFIX}/projects/{project_id}/usage{query}")
    assert response.status_code == 200, response.text
    body: dict[str, object] = response.json()
    return body


def _readings(session: Session, project_id: UUID, query: str = "") -> list[dict[str, object]]:
    readings = _usage(session, project_id, query)["reading_times"]
    assert isinstance(readings, list)
    return readings


def _at(value: object) -> datetime:
    assert isinstance(value, str)
    return datetime.fromisoformat(value)


def test_a_three_minute_reading_reports_three_minutes_from_state_events_not_call_times(
    session: Session,
) -> None:
    project_id = _project(session)
    package_id = _package(session, project_id)
    revision_id = _revision(
        session,
        package_id,
        [
            *UPLOADED,
            ("EXTRACTING", _minutes(0)),
            *PIPELINE_TAIL,
            ("AWAITING_REVIEW", _minutes(3)),
            # A later re-check is not a reading: it never passes through EXTRACTING.
            ("RUNNING_CHECKS", _minutes(30)),
            ("AWAITING_REVIEW", _minutes(31)),
        ],
    )
    # Every call of the reading saved in one go, at one instant: the old measure says ~0.
    saved_together = T0 + _minutes(3)
    session.add_all(
        ModelInvocation(
            package_revision_id=revision_id,
            extraction_run_id=None,
            model_id="synthetic-model",
            prompt_id="synthetic-prompt",
            template_id="synthetic-template",
            input_tokens=1,
            output_tokens=1,
            cost_micros=1,
            latency_ms=1,
            outcome="ok",
            created_at=saved_together,
        )
        for _ in range(3)
    )
    session.flush()

    [reading] = _readings(session, project_id)

    assert reading["package_id"] == str(package_id)
    assert reading["revision_id"] == str(revision_id)
    assert reading["revision_number"] == 1
    assert _at(reading["started_at"]) == T0
    assert _at(reading["finished_at"]) == T0 + _minutes(3)
    assert reading["outcome"] == "finished"
    assert reading["end_state"] == "AWAITING_REVIEW"
    assert reading["duration_ms"] == 180_000


def test_a_revision_still_reading_has_no_finish_time(session: Session) -> None:
    project_id = _project(session)
    package_id = _package(session, project_id)
    _revision(
        session,
        package_id,
        [*UPLOADED, ("EXTRACTING", _minutes(0)), ("MATCHING", _minutes(2))],
    )

    [reading] = _readings(session, project_id)

    assert _at(reading["started_at"]) == T0
    assert reading["finished_at"] is None
    assert reading["outcome"] == "reading"
    assert reading["end_state"] is None
    assert reading["duration_ms"] is None


def test_a_failed_reading_is_reported_as_failed_at_the_failure(session: Session) -> None:
    project_id = _project(session)
    package_id = _package(session, project_id)
    _revision(
        session,
        package_id,
        [*UPLOADED, ("EXTRACTING", _minutes(0)), ("FAILED_PERMANENT", _minutes(1.5))],
    )

    [reading] = _readings(session, project_id)

    assert reading["outcome"] == "failed"
    assert reading["end_state"] == "FAILED_PERMANENT"
    assert _at(reading["finished_at"]) == T0 + _minutes(1.5)
    assert reading["duration_ms"] == 90_000


def test_a_reading_handed_to_a_person_for_input_is_finished_there(session: Session) -> None:
    project_id = _project(session)
    package_id = _package(session, project_id)
    _revision(
        session,
        package_id,
        [
            *UPLOADED,
            ("EXTRACTING", _minutes(0)),
            ("MATCHING", _minutes(2.5)),
            ("VALIDATING_EVIDENCE", _minutes(2.6)),
            ("NEEDS_INPUT", _minutes(3)),
            # The reviewer types the values an hour later and runs the checks: not reading time.
            ("RUNNING_CHECKS", _minutes(60)),
            ("GENERATING_OUTPUTS", _minutes(60.5)),
            ("AWAITING_REVIEW", _minutes(61)),
        ],
    )

    [reading] = _readings(session, project_id)

    assert reading["outcome"] == "finished"
    assert reading["end_state"] == "NEEDS_INPUT"
    assert _at(reading["finished_at"]) == T0 + _minutes(3)
    assert reading["duration_ms"] == 180_000


def test_a_failure_keeps_its_time_when_the_revision_is_later_superseded(
    session: Session,
) -> None:
    project_id = _project(session)
    package_id = _package(session, project_id)
    _revision(
        session,
        package_id,
        [
            *UPLOADED,
            ("EXTRACTING", _minutes(0)),
            ("FAILED_PERMANENT", _minutes(2)),
            ("SUPERSEDED", _minutes(90)),
        ],
    )

    [reading] = _readings(session, project_id)

    assert reading["outcome"] == "failed"
    assert reading["end_state"] == "FAILED_PERMANENT"
    assert reading["duration_ms"] == 120_000


def test_a_retried_reading_that_reaches_review_is_finished_and_counts_the_whole_wait(
    session: Session,
) -> None:
    project_id = _project(session)
    package_id = _package(session, project_id)
    _revision(
        session,
        package_id,
        [
            *UPLOADED,
            ("EXTRACTING", _minutes(0)),
            ("FAILED_RETRYABLE", _minutes(1)),
            ("EXTRACTING", _minutes(5)),
            ("MATCHING", _minutes(7)),
            ("VALIDATING_EVIDENCE", _minutes(7.2)),
            ("RUNNING_CHECKS", _minutes(7.4)),
            ("GENERATING_OUTPUTS", _minutes(7.6)),
            ("AWAITING_REVIEW", _minutes(8)),
        ],
    )

    [reading] = _readings(session, project_id)

    assert reading["outcome"] == "finished"
    assert _at(reading["started_at"]) == T0
    assert reading["duration_ms"] == 8 * 60_000


def test_a_reread_after_supersede_reports_each_revision_on_its_own(session: Session) -> None:
    project_id = _project(session)
    package_id = _package(session, project_id)
    first = _revision(
        session,
        package_id,
        [*UPLOADED, ("EXTRACTING", _minutes(0)), ("SUPERSEDED", _minutes(1))],
    )
    second = _revision(
        session,
        package_id,
        [
            *UPLOADED,
            ("EXTRACTING", _minutes(20)),
            ("MATCHING", _minutes(22)),
            ("VALIDATING_EVIDENCE", _minutes(22.5)),
            ("RUNNING_CHECKS", _minutes(22.7)),
            ("GENERATING_OUTPUTS", _minutes(22.9)),
            ("AWAITING_REVIEW", _minutes(24)),
        ],
        number=2,
        supersedes=first,
    )

    readings = {reading["revision_id"]: reading for reading in _readings(session, project_id)}

    assert set(readings) == {str(first), str(second)}
    assert readings[str(first)]["outcome"] == "failed"
    assert readings[str(first)]["end_state"] == "SUPERSEDED"
    assert readings[str(second)]["outcome"] == "finished"
    assert readings[str(second)]["revision_number"] == 2
    assert readings[str(second)]["duration_ms"] == 4 * 60_000


def test_a_revision_never_read_has_no_reading_row(session: Session) -> None:
    project_id = _project(session)
    package_id = _package(session, project_id)
    _revision(session, package_id, UPLOADED)

    assert _readings(session, project_id) == []


def test_readings_are_project_scoped(session: Session) -> None:
    mine = _project(session)
    theirs = _project(session)
    _revision(
        session,
        _package(session, theirs),
        [*UPLOADED, ("EXTRACTING", _minutes(0)), ("AWAITING_REVIEW", _minutes(3))],
    )

    assert _readings(session, mine) == []
    other = _client(session, mine).get(f"{API_PREFIX}/projects/{theirs}/usage")
    assert other.status_code == 404
    unknown = uuid4()
    assert _client(session, mine).get(f"{API_PREFIX}/projects/{unknown}/usage").status_code == 404


def test_the_time_window_selects_readings_by_their_start(session: Session) -> None:
    project_id = _project(session)
    package_id = _package(session, project_id)
    _revision(
        session,
        package_id,
        [*UPLOADED, ("EXTRACTING", _minutes(0)), ("AWAITING_REVIEW", _minutes(3))],
    )
    inside = f"?from={(T0 - _minutes(1)).isoformat()}&to={(T0 + _minutes(1)).isoformat()}"
    after = f"?from={(T0 + _minutes(1)).isoformat()}"

    assert len(_readings(session, project_id, inside.replace("+", "%2B"))) == 1
    assert _readings(session, project_id, after.replace("+", "%2B")) == []


def _statements_for_usage(session: Session, project_id: UUID) -> int:
    statements = 0

    def count(*_args: object) -> None:
        nonlocal statements
        statements += 1

    client = _client(session, project_id)
    event.listen(session.bind, "before_cursor_execute", count)
    try:
        response = client.get(f"{API_PREFIX}/projects/{project_id}/usage")
    finally:
        event.remove(session.bind, "before_cursor_execute", count)
    assert response.status_code == 200, response.text
    return statements


def test_reading_times_query_count_does_not_grow_with_revisions(session: Session) -> None:
    project_id = _project(session)
    walk = [
        *UPLOADED,
        ("EXTRACTING", _minutes(0)),
        *PIPELINE_TAIL,
        ("AWAITING_REVIEW", _minutes(3)),
    ]
    _revision(session, _package(session, project_id), walk)
    one = _statements_for_usage(session, project_id)
    for _ in range(6):
        _revision(session, _package(session, project_id), walk)
    seven = _statements_for_usage(session, project_id)

    assert len(_readings(session, project_id)) == 7
    assert seven == one


def test_the_schema_says_what_package_reading_times_really_measures() -> None:
    old = UsageOut.model_fields["package_reading_times"].description or ""
    new = UsageOut.model_fields["reading_times"].description or ""

    assert "not the reading time" in old
    assert "state events" in new


def test_a_reused_stored_answer_is_not_a_call(session: Session) -> None:
    """#1112: a re-run that reuses a stored answer records it with no tokens and no cost, and
    marks it `reused_from`; usage counts only the calls that were made."""
    project_id = _project(session)
    revision_id = _revision(
        session,
        _package(session, project_id),
        [*UPLOADED, ("EXTRACTING", _minutes(0)), ("AWAITING_REVIEW", _minutes(3))],
    )
    asked = ModelInvocation(
        package_revision_id=revision_id,
        extraction_run_id=None,
        model_id="synthetic-model",
        prompt_id="synthetic-prompt",
        template_id="synthetic-template",
        input_tokens=10,
        output_tokens=5,
        cost_micros=7,
        latency_ms=1,
        outcome="ok",
        reader_question_packet={"packet_sha256": "a" * 64},
        created_at=T0,
    )
    session.add(asked)
    session.flush()
    session.add(
        ModelInvocation(
            package_revision_id=revision_id,
            extraction_run_id=None,
            model_id="synthetic-model",
            prompt_id="synthetic-prompt",
            template_id="synthetic-template",
            input_tokens=0,
            output_tokens=0,
            cost_micros=0,
            latency_ms=0,
            outcome="ok",
            reader_question_packet={"packet_sha256": "a" * 64, "reused_from": str(asked.id)},
            created_at=T0 + _minutes(1),
        )
    )
    session.flush()

    totals = _usage(session, project_id)["totals"]

    assert isinstance(totals, dict)
    assert totals["calls"] == 1
    assert (totals["input_tokens"], totals["output_tokens"]) == (10, 5)
    assert totals["cost_usd"] == "0.000007"
