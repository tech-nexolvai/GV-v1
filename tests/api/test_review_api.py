"""Review sessions and actions over HTTP (#229).

The service has done this work since D4.1 and nothing exposed it. This layer adds one thing —
turning a service refusal into a status code — and takes away one thing that matters more: the
reviewer's name never comes from the request body.

That is what most of these assert. An audit trail whose author is client-supplied answers "who says
so?" with "whoever was asked", which is the only question it exists to answer.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from alembic import command
from app.api import review
from app.auth import Principal, Role, authenticate
from app.config import Settings
from app.db.session import session_factory
from app.main import API_PREFIX, create_app
from app.models import (
    CheckRun,
    Finding,
    Package,
    PackageRevision,
    Project,
    RuleDefinition,
    RuleSnapshot,
)
from app.models.package import PackageState
from verdict.outcomes import Outcome, Severity

pytest_plugins = ("tests.app.postgres_fixture",)

DATABASE_URL = "postgresql+psycopg://gv:gv@localhost:5433/gv"
PROJECT_A = uuid4()
PROJECT_B = uuid4()
REVIEWER = "anant"


def _settings() -> Settings:
    return Settings(database_url=DATABASE_URL)  # type: ignore[call-arg]


def _principal(*projects: UUID, name: str = REVIEWER) -> Principal:
    return Principal(id=name, roles=frozenset(Role), projects=frozenset(projects))


def _app(principal: Principal, session: Session) -> FastAPI:
    app = create_app(_settings())
    app.dependency_overrides[authenticate] = lambda: principal
    app.dependency_overrides[review.get_session] = lambda: session
    return app


def _client(session: Session, *projects: UUID, name: str = REVIEWER) -> TestClient:
    return TestClient(
        _app(_principal(*projects, name=name), session), raise_server_exceptions=False
    )


def _upgrade(engine: Engine) -> None:
    config = Config("alembic.ini")
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


def _revision(session: Session, project_id: UUID) -> PackageRevision:
    session.add(Project(id=project_id, name=f"p-{project_id}"))
    session.flush()
    package = Package(project_id=project_id)
    session.add(package)
    session.flush()
    revision = PackageRevision(
        package_id=package.id, revision_number=1, state=PackageState.AWAITING_REVIEW
    )
    session.add(revision)
    session.flush()
    return revision


def _finding(session: Session, revision: PackageRevision) -> Finding:
    definition = RuleDefinition(rule_id=f"CT-WIDTH-{uuid4().hex[:8]}")
    session.add(definition)
    session.flush()
    canonical = json.dumps({"id": definition.rule_id, "version": "1.0.0"}, separators=(",", ":"))
    snapshot = RuleSnapshot(
        rule_definition_id=definition.id,
        snapshot_id=f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}",
        version="1.0.0",
        canonical_json=canonical,
        product_type="countertop",
        check_type="internal",
        unconfirmed_tolerance_count=0,
    )
    session.add(snapshot)
    session.flush()
    run = CheckRun(
        package_revision_id=revision.id,
        rule_snapshot_id=snapshot.id,
        engine_version="verdict-1.2.3",
    )
    session.add(run)
    session.flush()
    finding = Finding(
        check_run_id=run.id,
        package_revision_id=revision.id,
        outcome=Outcome.FAIL,
        severity=Severity.CRITICAL,
        parameter_set_versions={},
        # NOT NULL: a finding without its arithmetic is a verdict nobody can check.
        trace={"operation": "equals", "comparison": "96 == 98 1/2"},
    )
    session.add(finding)
    session.flush()
    return finding


def _open(client: TestClient, project_id: UUID, revision: PackageRevision) -> dict:
    response = client.post(
        f"{API_PREFIX}/projects/{project_id}/packages/{revision.package_id}/review-sessions",
        json={
            "package_revision_id": str(
                revision.package_revision_id
                if hasattr(revision, "package_revision_id")
                else revision.id
            )
        },
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


# ---------------------------------------------------------------------------
# The reviewer is the caller
# ---------------------------------------------------------------------------


def test_the_session_records_the_authenticated_reviewer(session: Session) -> None:
    """**The point of the endpoint.** A body-supplied name would let a caller open a sitting as
    somebody else, and the record of who reviewed what is the thing being kept."""
    revision = _revision(session, PROJECT_A)
    body = _open(_client(session, PROJECT_A, name="keyur"), PROJECT_A, revision)

    assert body["reviewer"] == "keyur"


def test_a_reviewer_cannot_be_named_in_the_body(session: Session) -> None:
    """`extra="forbid"`, so an attempt is a 422 rather than a field quietly ignored. Ignoring it
    would let a caller believe they had recorded somebody else's decision."""
    revision = _revision(session, PROJECT_A)
    response = _client(session, PROJECT_A).post(
        f"{API_PREFIX}/projects/{PROJECT_A}/packages/{revision.package_id}/review-sessions",
        json={"package_revision_id": str(revision.id), "reviewer": "somebody-else"},
    )
    assert response.status_code == 422


def test_the_action_records_the_authenticated_actor(session: Session) -> None:
    """Same rule one level down. An action is the audit trail's unit, and its actor is the caller."""
    revision = _revision(session, PROJECT_A)
    finding = _finding(session, revision)
    client = _client(session, PROJECT_A, name="keyur")
    opened = _open(client, PROJECT_A, revision)

    response = client.post(
        f"{API_PREFIX}/projects/{PROJECT_A}/review-sessions/{opened['id']}/actions",
        json={"finding_id": str(finding.id), "action": "confirm"},
    )

    assert response.status_code == 201, response.text
    assert response.json()["actor"] == "keyur"


def test_an_actor_cannot_be_named_in_the_body(session: Session) -> None:
    revision = _revision(session, PROJECT_A)
    finding = _finding(session, revision)
    client = _client(session, PROJECT_A)
    opened = _open(client, PROJECT_A, revision)

    response = client.post(
        f"{API_PREFIX}/projects/{PROJECT_A}/review-sessions/{opened['id']}/actions",
        json={"finding_id": str(finding.id), "action": "confirm", "actor": "somebody-else"},
    )
    assert response.status_code == 422


def test_the_revision_is_read_off_the_finding_not_the_request(session: Session) -> None:
    """ "An action references a server-side finding revision, never a client-supplied value" — the
    body has no field for it, and the stored action still names the right revision."""
    revision = _revision(session, PROJECT_A)
    finding = _finding(session, revision)
    client = _client(session, PROJECT_A)
    opened = _open(client, PROJECT_A, revision)

    body = client.post(
        f"{API_PREFIX}/projects/{PROJECT_A}/review-sessions/{opened['id']}/actions",
        json={"finding_id": str(finding.id), "action": "confirm"},
    ).json()

    assert body["package_revision_id"] == str(revision.id)


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_another_projects_revision_cannot_be_reviewed(session: Session) -> None:
    """404, not 403. A 403 confirms the revision exists, which is what the boundary hides."""
    _revision(session, PROJECT_A)
    theirs = _revision(session, PROJECT_B)

    response = _client(session, PROJECT_A).post(
        f"{API_PREFIX}/projects/{PROJECT_A}/packages/{theirs.package_id}/review-sessions",
        json={"package_revision_id": str(theirs.id)},
    )
    assert response.status_code == 404


def test_another_projects_session_cannot_be_actioned(session: Session) -> None:
    """The dependency establishes the caller may see *this project*. It says nothing about whether
    the session they named is in it, so the endpoint checks in SQL."""
    theirs = _revision(session, PROJECT_B)
    finding = _finding(session, theirs)
    opened = _open(_client(session, PROJECT_B), PROJECT_B, theirs)
    session.commit()

    response = _client(session, PROJECT_A).post(
        f"{API_PREFIX}/projects/{PROJECT_A}/review-sessions/{opened['id']}/actions",
        json={"finding_id": str(finding.id), "action": "confirm"},
    )
    assert response.status_code == 404


def test_completing_twice_is_refused(session: Session) -> None:
    """Not a no-op. A second attempt means somebody believes they are finishing work that was
    already finished, and telling them otherwise leaves that belief in place."""
    revision = _revision(session, PROJECT_A)
    client = _client(session, PROJECT_A)
    opened = _open(client, PROJECT_A, revision)
    url = f"{API_PREFIX}/projects/{PROJECT_A}/review-sessions/{opened['id']}/complete"

    assert client.post(url).status_code == 200
    assert client.post(url).status_code == 409


def test_a_completed_session_accepts_no_further_actions(session: Session) -> None:
    """The window is what makes the record trustworthy: an action landing after the sitting closed
    was decided at a time nobody wrote down."""
    revision = _revision(session, PROJECT_A)
    finding = _finding(session, revision)
    client = _client(session, PROJECT_A)
    opened = _open(client, PROJECT_A, revision)
    client.post(f"{API_PREFIX}/projects/{PROJECT_A}/review-sessions/{opened['id']}/complete")

    response = client.post(
        f"{API_PREFIX}/projects/{PROJECT_A}/review-sessions/{opened['id']}/actions",
        json={"finding_id": str(finding.id), "action": "confirm"},
    )
    assert response.status_code == 409


# ---------------------------------------------------------------------------
# The list the sidebar reads
# ---------------------------------------------------------------------------


def test_the_list_defaults_to_the_callers_own_sessions(session: Session) -> None:
    """What a reviewer came back for. A list defaulting to everyone's would bury their own on any
    project with more than one reviewer."""
    revision = _revision(session, PROJECT_A)
    _open(_client(session, PROJECT_A, name="anant"), PROJECT_A, revision)
    _open(_client(session, PROJECT_A, name="keyur"), PROJECT_A, revision)
    session.commit()

    mine = _client(session, PROJECT_A, name="anant").get(
        f"{API_PREFIX}/projects/{PROJECT_A}/review-sessions"
    )
    assert mine.status_code == 200
    assert [item["reviewer"] for item in mine.json()["items"]] == ["anant"]


def test_the_list_can_show_everyones(session: Session) -> None:
    """Otherwise the test above passes against an endpoint that can only ever return one reviewer."""
    revision = _revision(session, PROJECT_A)
    _open(_client(session, PROJECT_A, name="anant"), PROJECT_A, revision)
    _open(_client(session, PROJECT_A, name="keyur"), PROJECT_A, revision)
    session.commit()

    everyone = _client(session, PROJECT_A, name="anant").get(
        f"{API_PREFIX}/projects/{PROJECT_A}/review-sessions", params={"mine": "false"}
    )
    assert {item["reviewer"] for item in everyone.json()["items"]} == {"anant", "keyur"}


def test_the_list_does_not_cross_projects(session: Session) -> None:
    _revision(session, PROJECT_A)
    theirs = _revision(session, PROJECT_B)
    _open(_client(session, PROJECT_B), PROJECT_B, theirs)
    session.commit()

    body = (
        _client(session, PROJECT_A)
        .get(f"{API_PREFIX}/projects/{PROJECT_A}/review-sessions", params={"mine": "false"})
        .json()
    )
    assert body["items"] == []


def test_an_open_session_reports_no_completion_time(session: Session) -> None:
    """`completed_at` is the difference between a sitting in progress and one that is finished, and
    the sidebar renders on it."""
    revision = _revision(session, PROJECT_A)
    assert _open(_client(session, PROJECT_A), PROJECT_A, revision)["completed_at"] is None
