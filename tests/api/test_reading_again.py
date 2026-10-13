"""A person runs the reading again after the AI account or the AI budget stopped it (#1206).

Verification for `app/api/reading_again.py`, its outbox work, the local worker's parking of an
out-of-credit reading (`workflow/outbox.py`, `scripts/drain_outbox.py`) and the package's
`stop_reason` / `pages_waiting_for_budget`. Synthetic packages and fake readers; no network, no model.

The review of #1209 found that neither recovery worked in the product: after a 402 the package sat
in `FAILED_RETRYABLE` with nothing to resume it (and the local worker redelivered it every two
seconds), and after a budget stop nothing could read the pages it left.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.api.dependencies import get_session
from app.audit.events import AuditEvent
from app.auth import Principal, Role, authenticate
from app.config import Settings
from app.db.session import session_factory
from app.lifecycle.side_states import OUT_OF_CREDIT, FailureClass, enter_failure, enter_needs_input
from app.lifecycle.states import begin, transition
from app.main import API_PREFIX, create_app
from app.models import OutboxEntry, Package, PackageRevision, PackageState, Project
from extraction.slot_reader.anthropic import ProviderAccountRefused
from tests.app.postgres_fixture import alembic_config
from workflow.outbox import OutboxDispatchError, dispatch_committed, enqueue

pytest_plugins = ("tests.app.postgres_fixture",)


@pytest.fixture
def factory(postgres_engine: Engine) -> sessionmaker[Session]:
    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")
    return session_factory(postgres_engine)


@pytest.fixture
def session(factory: sessionmaker[Session]) -> Iterator[Session]:
    opened = factory()
    try:
        yield opened
    finally:
        opened.close()


def _client(session: Session, project_id: UUID, roles: frozenset[Role]) -> TestClient:
    app = create_app(Settings(database_url="postgresql+psycopg://gv:gv@localhost:5433/gv"))
    app.dependency_overrides[authenticate] = lambda: Principal(
        id="synthetic reviewer", roles=roles, projects=frozenset({project_id})
    )
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app, raise_server_exceptions=False)


def _ask(session: Session, revision: PackageRevision, roles: frozenset[Role] | None = None) -> Any:
    package = session.get_one(Package, revision.package_id)
    client = _client(session, package.project_id, roles or frozenset({Role.REVIEWER}))
    return client.post(
        f"{API_PREFIX}/projects/{package.project_id}/packages/{package.id}/reading-again"
    )


def _uploaded(session: Session) -> PackageRevision:
    project = Project(name="reading again")
    session.add(project)
    session.flush()
    package = Package(project_id=project.id, vendor="Synthetic vendor")
    session.add(package)
    session.flush()
    revision = PackageRevision(package_id=package.id, revision_number=1, state=PackageState.CREATED)
    session.add(revision)
    session.flush()
    begin(session, revision.id, actor="reviewer")
    transition(session, revision.id, PackageState.UPLOADING, actor="reviewer")
    transition(session, revision.id, PackageState.UPLOADED, actor="reviewer")
    session.commit()
    return revision


def _out_of_credit(session: Session) -> PackageRevision:
    revision = _uploaded(session)
    transition(session, revision.id, PackageState.INGESTING, actor="the worker")
    transition(session, revision.id, PackageState.EXTRACTING, actor="the worker")
    enter_failure(
        session,
        revision.id,
        actor="the extract pages worker",
        failure_class=FailureClass.RETRYABLE,
        reason=OUT_OF_CREDIT,
    )
    session.commit()
    return revision


def _rows(session: Session, workflow: str) -> list[OutboxEntry]:
    return list(session.scalars(select(OutboxEntry).where(OutboxEntry.workflow == workflow)))


# --- out of credit ------------------------------------------------------------------------------


def test_out_of_credit_offers_run_the_reading_again_and_audits_who_asked(session: Session) -> None:
    revision = _out_of_credit(session)

    answer = _ask(session, revision)

    assert answer.status_code == 202, answer.text
    body = answer.json()
    assert body["action"] == "run_reading_again" and body["pages"] == []
    (row,) = _rows(session, "extract_package")
    assert str(row.id) == body["accepted_id"]
    assert row.payload["package_revision_id"] == str(revision.id)
    audit = session.scalars(select(AuditEvent).where(AuditEvent.target_id == row.id)).one()
    assert (audit.actor, audit.category, audit.target_type) == (
        "synthetic reviewer",
        "REVIEW_ACTION",
        "reading_again:run_reading_again",
    )
    # Asked twice, it is queued once.
    assert _ask(session, revision).status_code == 409
    assert len(_rows(session, "extract_package")) == 1


def test_only_someone_who_may_start_a_review_may_ask(session: Session) -> None:
    revision = _out_of_credit(session)

    answer = _ask(session, revision, frozenset({Role.RULE_ADMIN}))

    # Refused as a 404 that says nothing, as every authorisation refusal is (`app/auth`).
    assert answer.status_code == 404
    assert _rows(session, "extract_package") == []


def test_a_reading_that_failed_for_anything_else_is_not_run_again_from_here(
    session: Session,
) -> None:
    revision = _uploaded(session)
    transition(session, revision.id, PackageState.INGESTING, actor="the worker")
    enter_failure(session, revision.id, actor="the worker", error=TimeoutError("synthetic"))
    session.commit()

    answer = _ask(session, revision)

    assert answer.status_code == 409
    assert "did not stop for the AI provider account" in answer.json()["message"]


def test_a_finished_reading_with_nothing_waiting_is_refused(session: Session) -> None:
    revision = _uploaded(session)
    transition(session, revision.id, PackageState.INGESTING, actor="the worker")
    transition(session, revision.id, PackageState.EXTRACTING, actor="the worker")
    enter_needs_input(session, revision.id, actor="the worker", needed="values to confirm")
    session.commit()

    answer = _ask(session, revision)

    assert answer.status_code == 409
    assert _rows(session, "read_unread_pages") == []


# --- through the local worker: parked, not redelivered; then run again --------------------------


class _Stages:
    """A reading whose page stage is refused for the account, or (`works`) reads nothing."""

    def __init__(self, *, works: bool) -> None:
        self.works = works
        self.extracted = 0

    def ingest(self, session: Session, package_revision_id: object) -> dict[str, object]:
        return {"implemented": True}

    def extract_pages(self, session: Session, package_revision_id: object) -> tuple[object, ...]:
        self.extracted += 1
        if not self.works:
            raise ProviderAccountRefused(api="OpenRouter Messages API")
        return ()

    def match(self, session: Session, package_revision_id: object) -> dict[str, object]:
        return {"proposed": 0}

    def validate_evidence(self, session: Session, package_revision_id: object) -> dict[str, object]:
        return {"crops": 0}


def test_the_worker_parks_an_out_of_credit_reading_and_a_person_runs_it_again(
    factory: sessionmaker[Session], session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The local worker's own consumer, through the outbox. Before: the row was redelivered every
    poll for ever. Now it is parked once with the plain reason; the package says so; "Run the
    reading again" queues the reading once more and, the account topped up, it finishes."""
    import scripts.drain_outbox as worker

    revision = _uploaded(session)
    enqueue(
        session,
        workflow="extract_package",
        payload={"package_revision_id": str(revision.id), "project_id": str(uuid4())},
    )
    session.commit()
    stages = _Stages(works=False)
    monkeypatch.setattr(worker, "_stages", lambda **_: stages)

    def start(*, workflow: str, payload: Mapping[str, object], idempotency_key: str) -> None:
        with factory() as own:
            worker._consume(
                own, workflow=workflow, payload=payload, idempotency_key=idempotency_key
            )
            own.commit()

    with pytest.raises(OutboxDispatchError):
        dispatch_committed(factory, start)
    assert dispatch_committed(factory, start) == 0, "parked, not redelivered"
    assert dispatch_committed(factory, start) == 0
    assert stages.extracted == 1
    session.expire_all()
    (parked,) = _rows(session, "extract_package")
    assert (parked.attempts, parked.abandoned_reason) == (1, OUT_OF_CREDIT)
    assert session.get_one(PackageRevision, revision.id).state == PackageState.FAILED_RETRYABLE
    package = session.get_one(Package, revision.package_id)
    shown = _client(session, package.project_id, frozenset({Role.REVIEWER})).get(
        f"{API_PREFIX}/projects/{package.project_id}/packages/{package.id}"
    )
    assert shown.json()["stop_reason"] == OUT_OF_CREDIT

    # The account is topped up; the person runs the reading again.
    assert _ask(session, revision).status_code == 202
    stages.works = True
    assert dispatch_committed(factory, start) == 1
    session.expire_all()
    assert session.get_one(PackageRevision, revision.id).state == PackageState.NEEDS_INPUT
    assert stages.extracted == 2
