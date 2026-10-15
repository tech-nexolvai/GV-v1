"""A person asks to read the depths again on a drawing set already read (#1308).

Verification for `app/api/depth_again.py` and its outbox work through the local worker
(`scripts/drain_outbox.py` -> `workflow.sutradhar.consume`). Synthetic packages and stand-in stages;
no network, no model.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.api.dependencies import get_session
from app.audit.events import AuditEvent
from app.auth import Principal, Role, authenticate
from app.config import Settings
from app.db.session import session_factory
from app.lifecycle.side_states import enter_needs_input
from app.lifecycle.states import begin, transition
from app.main import API_PREFIX, create_app
from app.models import OutboxEntry, Package, PackageRevision, PackageState, Project
from app.models.runs import ExtractionRun, ModelInvocation, TaskRun, WorkflowRun
from storage.local import LocalStore
from tests.app.postgres_fixture import alembic_config
from tests.workflow.test_read_depths_again import (
    _MustNotBeCalled,
    _read_set,
    _spent_reading_the_set,
    _stages,
)
from tests.workflow.test_slot_reader import FRACTION_ENV
from workflow.depth_again import (
    ALREADY_ASKED,
    BUDGET_USED_UP,
    BUSY,
    NEEDS_CLAUDE,
    NEEDS_REUSE,
    NO_COUNTERTOP,
    READ_DEPTHS_AGAIN_WORKFLOW,
    SWITCHED_OFF,
    UNPRICED,
    DepthsAgainRefused,
)
from workflow.depth_evidence import CLAUDE_PAIR
from workflow.outbox import OutboxDispatchError, dispatch_committed
from workflow.set_spend import depth_cap_usd
from workflow.slot_reader import configured_slot_reader
from workflow.stages import AI_BUDGET_ENV, DatabaseStages

pytest_plugins = ("tests.app.postgres_fixture",)

ON = {"depth_reader": True, "claude_reader_enabled": True}


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


def _settings(switches: Mapping[str, object]) -> Settings:
    # The switches are set past validation: turning the Claude readers on for real needs the form
    # reader's whole configuration and a key, none of which this route reads.
    return Settings(database_url="postgresql+psycopg://gv:gv@localhost:5433/gv").model_copy(
        update=dict(switches)
    )


def _client(
    session: Session, project_id: UUID, roles: frozenset[Role], switches: Mapping[str, object]
) -> TestClient:
    app = create_app(_settings(switches))
    app.dependency_overrides[authenticate] = lambda: Principal(
        id="synthetic reviewer", roles=roles, projects=frozenset({project_id})
    )
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app, raise_server_exceptions=False)


def _url(session: Session, revision: PackageRevision) -> tuple[UUID, str]:
    package = session.get_one(Package, revision.package_id)
    return (
        package.project_id,
        f"{API_PREFIX}/projects/{package.project_id}/packages/{package.id}/read-depths-again",
    )


def _ask(
    session: Session,
    revision: PackageRevision,
    switches: Mapping[str, object] = ON,
    roles: frozenset[Role] | None = None,
) -> Any:
    project, url = _url(session, revision)
    return _client(session, project, roles or frozenset({Role.REVIEWER}), switches).post(url)


def _status(
    session: Session, revision: PackageRevision, switches: Mapping[str, object] = ON
) -> Any:
    project, url = _url(session, revision)
    return _client(session, project, frozenset({Role.REVIEWER}), switches).get(url)


def _read(session: Session, *, product: str | None = "countertop") -> PackageRevision:
    """A set whose reading handed it to the reviewer."""
    project = Project(name=f"depths again {uuid4()}")
    session.add(project)
    session.flush()
    package = Package(project_id=project.id, vendor="Synthetic vendor", product_type=product)
    session.add(package)
    session.flush()
    revision = PackageRevision(package_id=package.id, revision_number=1, state=PackageState.CREATED)
    session.add(revision)
    session.flush()
    begin(session, revision.id, actor="reviewer")
    for state in (
        PackageState.UPLOADING,
        PackageState.UPLOADED,
        PackageState.INGESTING,
        PackageState.EXTRACTING,
    ):
        transition(session, revision.id, state, actor="the worker")
    enter_needs_input(session, revision.id, actor="the worker", needed="values to confirm")
    session.commit()
    return revision


def _rows(session: Session) -> list[OutboxEntry]:
    return list(
        session.scalars(
            select(OutboxEntry).where(OutboxEntry.workflow == READ_DEPTHS_AGAIN_WORKFLOW)
        )
    )


# --- refused, with the plain reason -------------------------------------------------------------


def test_switched_off_it_is_not_offered_and_a_request_is_refused(session: Session) -> None:
    revision = _read(session)

    shown = _status(session, revision, {})
    answer = _ask(session, revision, {})

    assert shown.status_code == 200, shown.text
    assert shown.json()["switched_on"] is False and shown.json()["can_ask"] is False
    assert shown.json()["estimate"] is None
    assert answer.status_code == 409 and answer.json()["message"] == SWITCHED_OFF
    assert _rows(session) == []


@pytest.mark.parametrize(
    ("switches", "reason"),
    [
        ({"depth_reader": True}, NEEDS_CLAUDE),
        ({**ON, "reader_reuse_answers": False}, NEEDS_REUSE),
    ],
)
def test_refused_without_the_claude_readers_or_stored_answers(
    session: Session, switches: Mapping[str, object], reason: str
) -> None:
    revision = _read(session)

    shown = _status(session, revision, switches).json()
    answer = _ask(session, revision, switches)

    assert shown["switched_on"] is True and shown["can_ask"] is False
    assert shown["refused"] == reason
    assert answer.status_code == 409 and answer.json()["message"] == reason
    assert _rows(session) == []


def test_refused_while_the_set_is_being_read(session: Session) -> None:
    project = Project(name=f"depths again {uuid4()}")
    session.add(project)
    session.flush()
    package = Package(project_id=project.id, vendor="Synthetic vendor")
    session.add(package)
    session.flush()
    revision = PackageRevision(package_id=package.id, revision_number=1, state=PackageState.CREATED)
    session.add(revision)
    session.flush()
    begin(session, revision.id, actor="reviewer")
    for state in (PackageState.UPLOADING, PackageState.UPLOADED, PackageState.INGESTING):
        transition(session, revision.id, state, actor="the worker")
    session.commit()

    answer = _ask(session, revision)

    assert answer.status_code == 409 and answer.json()["message"] == BUSY


def test_refused_on_a_set_that_is_not_for_countertops(session: Session) -> None:
    revision = _read(session, product="cabinet")

    answer = _ask(session, revision)

    assert answer.status_code == 409 and "not in scope" in answer.json()["message"]


def test_only_someone_who_may_start_a_review_may_ask(session: Session) -> None:
    revision = _read(session)

    answer = _ask(session, revision, roles=frozenset({Role.RULE_ADMIN}))

    assert answer.status_code == 404
    assert _rows(session) == []


# --- asked ---------------------------------------------------------------------------------------


def test_switched_on_it_shows_an_estimate_queues_once_and_audits_who_asked(
    session: Session,
) -> None:
    revision = _read(session)

    shown = _status(session, revision).json()
    assert shown["switched_on"] is True and shown["can_ask"] is True and shown["refused"] is None
    assert set(shown["estimate"]) >= {"questions_low", "questions_high", "stored_answers", "basis"}
    assert "CT-DEPTH-001" in shown["checks"]
    # No page and nothing spent: the page-scaled budget's minimum is left (the worker's own cap).
    assert shown["budget_left_usd"] == "3.00"

    answer = _ask(session, revision)

    assert answer.status_code == 202, answer.text
    (row,) = _rows(session)
    assert str(row.id) == answer.json()["accepted_id"]
    assert row.payload["package_revision_id"] == str(revision.id)
    audit = session.scalars(select(AuditEvent).where(AuditEvent.target_id == row.id)).one()
    assert (audit.actor, audit.category, audit.target_type) == (
        "synthetic reviewer",
        "REVIEW_ACTION",
        "reading_again:read_depths_again",
    )
    # Asked again while the first waits: refused, never queued twice.
    again = _ask(session, revision)
    assert again.status_code == 409 and again.json()["message"] == ALREADY_ASKED
    assert len(_rows(session)) == 1
    later = _status(session, revision).json()
    assert later["can_ask"] is False and later["last"]["state"] == "waiting"
    # The package's state is not moved by asking.
    session.expire_all()
    assert session.get_one(PackageRevision, revision.id).state == PackageState.NEEDS_INPUT


# --- through the local worker -------------------------------------------------------------------


class _Stages:
    def __init__(self, refuse: str | None) -> None:
        self.refuse = refuse
        self.asked: list[tuple[UUID, str]] = []

    def read_depths_again(
        self, session: Session, revision_id: UUID, request: str
    ) -> dict[str, object]:
        self.asked.append((revision_id, request))
        if self.refuse is not None:
            raise DepthsAgainRefused(self.refuse)
        return {"ran": True, "reason": "Depths read again."}


def _worker(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch, stages: _Stages
) -> Any:
    import scripts.drain_outbox as worker

    monkeypatch.setattr(worker, "_stages", lambda **_: stages)

    def start(*, workflow: str, payload: Mapping[str, object], idempotency_key: str) -> None:
        with factory() as own:
            worker._consume(
                own, workflow=workflow, payload=payload, idempotency_key=idempotency_key
            )
            own.commit()

    return start


def test_the_worker_runs_the_request_once_under_its_own_id(
    factory: sessionmaker[Session], session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    revision = _read(session)
    accepted = _ask(session, revision).json()["accepted_id"]
    stages = _Stages(refuse=None)
    start = _worker(factory, monkeypatch, stages)

    assert dispatch_committed(factory, start) == 1
    assert dispatch_committed(factory, start) == 0

    assert stages.asked == [(revision.id, accepted)]


def test_a_request_the_worker_refuses_is_parked_and_says_why(
    factory: sessionmaker[Session], session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    revision = _read(session)
    assert _ask(session, revision).status_code == 202
    stages = _Stages(refuse=NO_COUNTERTOP)
    start = _worker(factory, monkeypatch, stages)

    with pytest.raises(OutboxDispatchError):
        dispatch_committed(factory, start)
    assert dispatch_committed(factory, start) == 0, "parked, not redelivered"

    session.expire_all()
    (row,) = _rows(session)
    assert row.abandoned_reason == NO_COUNTERTOP
    shown = _status(session, revision).json()
    assert shown["last"]["state"] == "not_read" and shown["last"]["sentence"] == NO_COUNTERTOP
    # Parked, it no longer blocks a new request.
    assert shown["can_ask"] is True
    assert len(stages.asked) == 1


def test_a_call_with_no_price_is_refused_with_the_reason(session: Session) -> None:
    revision = _read(session)
    workflow = WorkflowRun(package_revision_id=revision.id, engine_run_id=str(uuid4()))
    session.add(workflow)
    session.flush()
    task = TaskRun(
        workflow_run_id=workflow.id,
        idempotency_key=f"synthetic-{uuid4()}",
        task_type="extract_pages",
        attempt=1,
        outcome="SUCCEEDED",
    )
    session.add(task)
    session.flush()
    run = ExtractionRun(
        task_run_id=task.id, extractor="synthetic", extractor_version="synthetic", config_hash="x"
    )
    session.add(run)
    session.flush()
    session.add(
        ModelInvocation(
            extraction_run_id=run.id,
            model_id="synthetic-model",
            prompt_id="claude-slot-span-v5",
            template_id="claude-slot-span-v5",
            input_tokens=1,
            output_tokens=1,
            latency_ms=1,
            outcome="ok",
            cost_micros=None,
        )
    )
    session.commit()

    shown = _status(session, revision).json()
    answer = _ask(session, revision)

    assert shown["can_ask"] is False and shown["refused"] == UNPRICED
    assert shown["budget_left_usd"] == "0.00"
    assert answer.status_code == 409 and answer.json()["message"] == UNPRICED
    assert _rows(session) == []


class _Breaks(_Stages):
    def read_depths_again(
        self, session: Session, revision_id: UUID, request: str
    ) -> dict[str, object]:
        self.asked.append((revision_id, request))
        raise RuntimeError("synthetic failure")


def test_a_request_that_fails_unexpectedly_is_parked_and_says_why(
    factory: sessionmaker[Session], session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    revision = _read(session)
    assert _ask(session, revision).status_code == 202
    stages = _Breaks(refuse=None)
    start = _worker(factory, monkeypatch, stages)

    with pytest.raises(OutboxDispatchError):
        dispatch_committed(factory, start)
    assert dispatch_committed(factory, start) == 0, "parked, not retried for ever"

    session.expire_all()
    shown = _status(session, revision).json()
    assert shown["last"]["state"] == "not_read"
    assert (
        shown["last"]["sentence"] == "Depths were not read again: RuntimeError: synthetic failure"
    )
    assert shown["can_ask"] is True and len(stages.asked) == 1


# --- #1317: the budget left shown is the worker's own cap -----------------------------------------

#: A fixed amount for one drawing set above the admin's maximum, and the settings the worker needs
#: to build the Claude readers (with a made-up key: no client built from it is ever called).
OVER_THE_MAXIMUM = {
    **ON,
    "ai_budget_per_set_usd": Decimal(40),
    "ai_budget_maximum_usd": Decimal(25),
    "form_reader_enabled": True,
    "slot_reader_enabled": True,
    "openrouter_api_key": SecretStr("synthetic-not-a-key"),
    "claude_reader_model_rpm": dict.fromkeys(CLAUDE_PAIR, 60),
}


@pytest.fixture
def store(tmp_path: Path) -> LocalStore:
    return LocalStore(root=tmp_path, ticket_secret=b"synthetic-test-only")


def _worker_stages(
    switches: Mapping[str, object], store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> DatabaseStages:
    """The worker's stages from the same settings, as `workflow.sutradhar.build_stages` makes
    them: the Claude readers' ceiling from `configured_slot_reader`, the fixed amount for a set
    from `GV_AI_BUDGET_PER_SET_USD`. Every client fails the test if it is called."""
    settings = _settings(switches)
    monkeypatch.setenv(AI_BUDGET_ENV, str(settings.ai_budget_per_set_usd))
    form = _stages(store, _MustNotBeCalled())._form_reader
    runtime = configured_slot_reader(settings, form, environ=FRACTION_ENV)
    assert runtime is not None and runtime.spend_cap_usd is not None
    return _stages(store, _MustNotBeCalled(), ceiling=runtime.spend_cap_usd)


def _worker_cap(session: Session, stages: DatabaseStages, revision_id: UUID) -> str:
    """The depth reader's spend cap exactly as the worker builds it (`_depth_pass`), in cents."""
    assert stages._slot_reader is not None
    cap = depth_cap_usd(
        session,
        revision_id,
        budget=stages._ai_budget,
        fixed_usd=stages._ai_budget_fixed_usd(),
        claude_ceiling_usd=stages._slot_reader.spend_cap_usd,
    )
    return str(cap.quantize(Decimal("0.01"), rounding=ROUND_DOWN))


def test_the_budget_left_shown_is_the_worker_s_own_cap(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fixed $40 for a set with the admin's maximum at $25: the screen showed $40 left while
    the worker stops at $25. Both now say $25."""
    sheet = _read_set(session, store)
    worker = _worker_stages(OVER_THE_MAXIMUM, store, monkeypatch)

    shown = _status(session, sheet.revision, OVER_THE_MAXIMUM).json()

    assert shown["budget_left_usd"] == _worker_cap(session, worker, sheet.revision.id) == "25.00"
    assert shown["can_ask"] is True

    # More than the ceiling spent already, less than the set's $40: the ceiling is what one
    # reading may spend, so what the set has left is the cap, the same both ways.
    _spent_reading_the_set(session, sheet, 30_000_000)
    shown = _status(session, sheet.revision, OVER_THE_MAXIMUM).json()

    assert shown["budget_left_usd"] == _worker_cap(session, worker, sheet.revision.id) == "10.00"
    assert shown["can_ask"] is True


def test_a_used_up_budget_is_refused_up_front_with_the_worker_s_own_reason(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    sheet = _read_set(session, store)
    # No depth reading yet, so every question would be new; and the set's whole $40 is spent.
    session.delete(sheet.run)
    session.flush()
    _spent_reading_the_set(session, sheet, 40_000_000)
    worker = _worker_stages(OVER_THE_MAXIMUM, store, monkeypatch)

    shown = _status(session, sheet.revision, OVER_THE_MAXIMUM).json()
    answer = _ask(session, sheet.revision, OVER_THE_MAXIMUM)
    with pytest.raises(DepthsAgainRefused) as refused:
        worker.read_depths_again(session, sheet.revision.id, str(uuid4()))

    assert shown["budget_left_usd"] == _worker_cap(session, worker, sheet.revision.id) == "0.00"
    assert shown["can_ask"] is False
    assert answer.status_code == 409
    assert shown["refused"] == answer.json()["message"] == refused.value.plain_reason
    assert refused.value.plain_reason == BUDGET_USED_UP
    assert _rows(session) == []
