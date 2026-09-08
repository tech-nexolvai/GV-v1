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
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alembic import command
from app.api import review
from app.auth import Principal, Role, authenticate
from app.config import Settings
from app.db.session import session_factory
from app.main import API_PREFIX, create_app
from app.models import (
    CanonicalObservation,
    CheckRun,
    CorrectionLedgerEntry,
    Finding,
    FindingEvidence,
    Package,
    PackageRevision,
    Project,
    RuleDefinition,
    RuleSnapshot,
)
from app.models.package import PackageState
from app.models.review import ReviewAction
from evidence.canonical import EvidenceStatus
from tests.review.test_ledger import Scenario, _scenario
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


# ---------------------------------------------------------------------------
# The way into the exceptions table, which had none (#547)
# ---------------------------------------------------------------------------


def _tomorrow() -> str:
    return (datetime.now(UTC) + timedelta(days=1)).isoformat()


def _grant(
    client: TestClient,
    project_id: UUID,
    session_id: str,
    finding: Finding,
    **overrides: object,
) -> Any:
    body: dict[str, object] = {
        "finding_id": str(finding.id),
        "scope": "finding",
        "scope_id": str(finding.id),
        "reason": "the vendor confirmed the site dimension by phone",
        "expires_at": _tomorrow(),
    }
    body.update(overrides)
    return client.post(
        f"{API_PREFIX}/projects/{project_id}/review-sessions/{session_id}/exceptions", json=body
    )


def test_granting_an_exception_records_it_with_its_terms(session: Session) -> None:
    """**Nothing could grant one of these before.** Outcome: a row with scope, reason and expiry.

    `ReviewException` appeared only in the models and the tests: the table, its required expiry and
    the exact scope matching were all built with no way in, so the control existed and did nothing.
    """
    project_id = uuid4()
    revision = _revision(session, project_id)
    finding = _finding(session, revision)
    session.commit()
    client = _client(session, project_id)
    opened = _open(client, project_id, revision)

    response = _grant(client, project_id, opened["id"], finding)

    assert response.status_code == 201, response.text
    granted = response.json()
    assert granted["scope"] == "finding"
    assert granted["scope_id"] == str(finding.id)
    assert granted["reason"] == "the vendor confirmed the site dimension by phone"
    assert granted["approved_by"] == REVIEWER
    assert granted["expires_at"]


def test_the_approver_is_the_caller_and_not_a_field(session: Session) -> None:
    """Input: an `approved_by` in the body. Outcome: refused as an unknown field.

    `AGENTS.md` §2.6 calls an anonymous exception nobody's decision, and a client-supplied approver
    answers "who says so?" with "whoever was asked" — the one question the record exists to answer.
    """
    project_id = uuid4()
    revision = _revision(session, project_id)
    finding = _finding(session, revision)
    session.commit()
    client = _client(session, project_id)
    opened = _open(client, project_id, revision)

    response = _grant(client, project_id, opened["id"], finding, approved_by="somebody else")

    assert response.status_code == 422, response.text


def test_an_exception_with_no_expiry_is_refused(session: Session) -> None:
    """**The control, at the outermost layer.** Outcome: 422 for a missing `expires_at`.

    A permanent silent exception is not representable anywhere: the column is `NOT NULL`,
    `ExceptionGrant` has no default, the schema requires it, and `grant_exception` refuses one
    already past. An exception with no end date is how a check gets switched off and nobody
    remembers.
    """
    project_id = uuid4()
    revision = _revision(session, project_id)
    finding = _finding(session, revision)
    session.commit()
    client = _client(session, project_id)
    opened = _open(client, project_id, revision)

    response = client.post(
        f"{API_PREFIX}/projects/{project_id}/review-sessions/{opened['id']}/exceptions",
        json={
            "finding_id": str(finding.id),
            "scope": "finding",
            "scope_id": str(finding.id),
            "reason": "because",
        },
    )

    assert response.status_code == 422, response.text


def test_an_expiry_already_past_is_refused(session: Session) -> None:
    """Input: an expiry in the past. Outcome: 422 saying it would cover nothing.

    The database can only check `expires_at > created_at` — a clock comparison in a CHECK is not
    immutable — so this half is enforced in the service, where a clock is allowed. Accepting it
    would let a reviewer believe they had granted cover they had not.
    """
    project_id = uuid4()
    revision = _revision(session, project_id)
    finding = _finding(session, revision)
    session.commit()
    client = _client(session, project_id)
    opened = _open(client, project_id, revision)

    yesterday = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    response = _grant(client, project_id, opened["id"], finding, expires_at=yesterday)

    assert response.status_code == 422, response.text
    # `message`, not `detail`: this API answers every error in one envelope — `app/errors.py`
    # builds `{error, message, request_id}` so a caller has one shape to read and an id to quote.
    assert "already over" in response.json()["message"]


def test_an_expiry_with_no_timezone_is_refused(session: Session) -> None:
    """Input: a naive datetime. Outcome: 422.

    Guessing the zone is how an exception ends up living hours longer than it was granted for, and
    in the worst case forever — `app/review/exceptions.py` refuses a naive datetime for the same
    reason, and this is the same refusal at the edge.
    """
    project_id = uuid4()
    revision = _revision(session, project_id)
    finding = _finding(session, revision)
    session.commit()
    client = _client(session, project_id)
    opened = _open(client, project_id, revision)

    response = _grant(client, project_id, opened["id"], finding, expires_at="2099-01-01T00:00:00")

    assert response.status_code == 422, response.text


def test_an_exception_with_no_reason_is_refused(session: Session) -> None:
    """Input: whitespace. Outcome: 422. An exception nobody explained is one nobody can review."""
    project_id = uuid4()
    revision = _revision(session, project_id)
    finding = _finding(session, revision)
    session.commit()
    client = _client(session, project_id)
    opened = _open(client, project_id, revision)

    response = _grant(client, project_id, opened["id"], finding, reason="   ")

    assert response.status_code == 422, response.text


def test_a_finding_scoped_exception_must_name_the_finding(session: Session) -> None:
    """Input: `finding` scope naming a different finding. Outcome: 422.

    The one scope where naming something else is certainly a mistake. Item and package scopes are
    *meant* to name something larger, so they are not second-guessed — `decide` refuses to widen at
    read time instead, by matching scope and id exactly.
    """
    project_id = uuid4()
    revision = _revision(session, project_id)
    finding = _finding(session, revision)
    other = _finding(session, revision)
    session.commit()
    client = _client(session, project_id)
    opened = _open(client, project_id, revision)

    response = _grant(client, project_id, opened["id"], finding, scope_id=str(other.id))

    assert response.status_code == 422, response.text
    assert "must name the finding" in response.json()["message"]


def test_an_exception_records_the_action_that_authorised_it(session: Session) -> None:
    """Outcome: a `review_actions` row of kind `except`, carrying the reason as its note.

    Two rows, and both are needed: the action says a reviewer did something, the exception says what
    they accepted and until when. The composite foreign key ties the second to the first *and* to
    its kind, so an exception cannot hang off a `confirm`.
    """
    project_id = uuid4()
    revision = _revision(session, project_id)
    finding = _finding(session, revision)
    session.commit()
    client = _client(session, project_id)
    opened = _open(client, project_id, revision)

    granted = _grant(client, project_id, opened["id"], finding).json()

    action = session.get(ReviewAction, UUID(granted["review_action_id"]))
    assert action is not None
    assert action.action == "except"
    assert action.note == "the vendor confirmed the site dimension by phone"
    assert action.actor == REVIEWER


def test_an_exception_cannot_be_granted_in_a_closed_session(session: Session) -> None:
    """Outcome: 409. Every refusal `record_action` makes applies here, because it makes the row."""
    project_id = uuid4()
    revision = _revision(session, project_id)
    finding = _finding(session, revision)
    session.commit()
    client = _client(session, project_id)
    opened = _open(client, project_id, revision)
    client.post(f"{API_PREFIX}/projects/{project_id}/review-sessions/{opened['id']}/complete")

    response = _grant(client, project_id, opened["id"], finding)

    assert response.status_code == 409, response.text


def test_a_session_in_another_project_is_not_found(session: Session) -> None:
    """Outcome: 404 in the same words as absent. Project scope is an isolation boundary."""
    project_id = uuid4()
    revision = _revision(session, project_id)
    finding = _finding(session, revision)
    session.commit()
    client = _client(session, project_id)
    opened = _open(client, project_id, revision)

    outsider = _client(session, uuid4(), name="someone-else")
    response = _grant(outsider, uuid4(), opened["id"], finding)

    assert response.status_code in (403, 404), response.text


# ---------------------------------------------------------------------------
# The way into the correction ledger, which had none (#547)
# ---------------------------------------------------------------------------


def _correctable(session: Session) -> tuple[UUID, Scenario]:
    """A finding with a real corroborated reading behind it, and the project it belongs to.

    `_scenario` from the ledger tests builds the whole chain, and its docstring says why reusing it
    beats rebuilding it: *"a `CORROBORATED` observation with no supporting candidates is a row whose
    columns are all valid and whose combination is not"*, and that rule lives in a trigger where
    model introspection cannot see it. This test met that trigger once already.

    The project id is read back through the revision, because `Scenario` does not carry it and the
    route needs it in the path.
    """
    scenario = _scenario(session)
    # **`_scenario` does not link the finding to the observation**, which is why
    # `tests/review/test_evidence_actions.py` has its own helper for it. `_decide` refuses an
    # observation that is not linked: a reviewer confirming a reading the verdict never used would
    # be confirming something that had nothing to do with the check. Without this the route answers
    # 404 and the test reads as a routing bug.
    session.add(
        FindingEvidence(
            finding_id=scenario.finding_id,
            canonical_observation_id=scenario.observation_id,
            role="operand",
        )
    )
    session.flush()
    project_id = session.execute(
        select(Package.project_id)
        .join(PackageRevision, PackageRevision.package_id == Package.id)
        .where(PackageRevision.id == scenario.package_revision_id)
    ).scalar_one()
    return project_id, scenario


def _decide(client: TestClient, project_id: UUID, session_id: str, body: dict[str, object]) -> Any:
    return client.post(
        f"{API_PREFIX}/projects/{project_id}/review-sessions/{session_id}/evidence", json=body
    )


def test_correcting_a_reading_writes_the_ledger(session: Session) -> None:
    """**The ledger had no way in.** Outcome: an entry with the original beside the correction.

    `correct_evidence` writes it in the same transaction as its action row and had no route, so no
    reviewer could ever create one — and `D5.4`'s correction rate measured an empty table, which
    reads as "no corrections were needed".
    """
    project_id, scenario = _correctable(session)
    session.commit()
    client = _client(session, project_id)

    response = _decide(
        client,
        project_id,
        str(scenario.review_session_id),
        {
            "finding_id": str(scenario.finding_id),
            "observation_id": str(scenario.observation_id),
            "action": "correct",
            "corrected_value": '98 1/2"',
        },
    )

    assert response.status_code == 201, response.text
    entries = list(session.execute(select(CorrectionLedgerEntry)).scalars())
    assert len(entries) == 1
    assert "1/3" in entries[0].original_value
    assert "197/2" in entries[0].corrected_value


def test_a_correction_never_edits_the_reading_it_corrects(session: Session) -> None:
    """Outcome: the original observation is untouched and a new one is written beside it.

    Which is what keeps "what did we get wrong?" answerable — and the ledger's whole purpose.
    """
    project_id, scenario = _correctable(session)
    session.commit()
    client = _client(session, project_id)

    body = _decide(
        client,
        project_id,
        str(scenario.review_session_id),
        {
            "finding_id": str(scenario.finding_id),
            "observation_id": str(scenario.observation_id),
            "action": "correct",
            "corrected_value": '98 1/2"',
        },
    ).json()

    session.expire_all()
    original = session.get(CanonicalObservation, scenario.observation_id)
    assert original is not None
    assert (original.value_numerator, original.value_denominator) == (1, 3)
    assert body["resulting_observation_id"] != body["original_observation_id"]
    resulting = session.get(CanonicalObservation, UUID(body["resulting_observation_id"]))
    assert resulting is not None
    assert (resulting.value_numerator, resulting.value_denominator) == (197, 2)
    assert resulting.status == EvidenceStatus.HUMAN_CONFIRMED.value


def test_a_correction_with_no_unit_is_refused(session: Session) -> None:
    """Input: a bare number. Outcome: 422 asking for the unit.

    Inherited from `normalise_to_inches` on purpose: a `984` whose `mm` had been lost by tokenisation
    was once stored as 984 inches — 82 feet (#483). A reviewer who omits the mark is told.
    """
    project_id, scenario = _correctable(session)
    session.commit()
    client = _client(session, project_id)

    response = _decide(
        client,
        project_id,
        str(scenario.review_session_id),
        {
            "finding_id": str(scenario.finding_id),
            "observation_id": str(scenario.observation_id),
            "action": "correct",
            "corrected_value": "98.5",
        },
    )

    assert response.status_code == 422, response.text
    assert "unit" in response.json()["message"]


def test_a_correction_without_a_value_is_refused(session: Session) -> None:
    """**Input: `correct` with no corrected value. Outcome: 422.**

    This is the gap the UI had: it sent `action: 'correct'` to the actions route, which takes a kind
    and a note and has nowhere for a value. Recording that something was corrected without saying to
    what leaves the ledger with no correction in it.
    """
    project_id, scenario = _correctable(session)
    session.commit()
    client = _client(session, project_id)

    response = _decide(
        client,
        project_id,
        str(scenario.review_session_id),
        {
            "finding_id": str(scenario.finding_id),
            "observation_id": str(scenario.observation_id),
            "action": "correct",
        },
    )

    assert response.status_code == 422, response.text


def test_a_confirmation_carrying_a_value_is_refused(session: Session) -> None:
    """Input: `confirm` with a corrected value. Outcome: 422.

    A confirmation that accepted a value would be indistinguishable from a correction that happened
    to agree, and the correction rate counts the difference.
    """
    project_id, scenario = _correctable(session)
    session.commit()
    client = _client(session, project_id)

    response = _decide(
        client,
        project_id,
        str(scenario.review_session_id),
        {
            "finding_id": str(scenario.finding_id),
            "observation_id": str(scenario.observation_id),
            "action": "confirm",
            "corrected_value": '96"',
        },
    )

    assert response.status_code == 422, response.text


def test_confirming_writes_no_ledger_entry(session: Session) -> None:
    """Outcome: a `HUMAN_CONFIRMED` copy and an empty ledger.

    A confirmation is not a correction. Writing one would inflate the correction rate with the cases
    where the system was right, which is the opposite of what the metric is for.
    """
    project_id, scenario = _correctable(session)
    session.commit()
    client = _client(session, project_id)

    response = _decide(
        client,
        project_id,
        str(scenario.review_session_id),
        {
            "finding_id": str(scenario.finding_id),
            "observation_id": str(scenario.observation_id),
            "action": "confirm",
        },
    )

    assert response.status_code == 201, response.text
    assert list(session.execute(select(CorrectionLedgerEntry)).scalars()) == []


def test_dismissing_is_not_an_evidence_decision(session: Session) -> None:
    """Input: `dismiss` on this route. Outcome: 422.

    Confirming and correcting act on a *reading*; dismissing and excepting act on a *finding* and
    have their own routes. One route that accepted all four would let a reviewer dismiss a finding
    by naming an observation, and the record would say they had confirmed evidence.
    """
    project_id, scenario = _correctable(session)
    session.commit()
    client = _client(session, project_id)

    response = _decide(
        client,
        project_id,
        str(scenario.review_session_id),
        {
            "finding_id": str(scenario.finding_id),
            "observation_id": str(scenario.observation_id),
            "action": "dismiss",
        },
    )

    assert response.status_code == 422, response.text
