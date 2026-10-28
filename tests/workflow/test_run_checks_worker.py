"""At most one check run per revision (#1359).

Under Hatchet, a re-check queued while a check run is in progress (#1351), or a second press of "Run
checks", could start beside the run it was meant to follow. `workflow/run_checks_worker.py` declares
`run_checks` with one concurrency group per revision and a strategy that queues. These tests check
that declaration without an engine, that its key is the revision id the run is asked with, that the
Hatchet worker registers it, and that the local worker, which the live app uses, is unchanged: it
still runs one revision's check runs one after the other.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from hatchet_sdk import ConcurrencyExpression, ConcurrencyLimitStrategy
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.db.session import session_factory
from app.models import OutboxEntry
from tests.app.postgres_fixture import alembic_config
from workflow.run_checks_worker import (
    CHECK_RUN_TAKES_UP_TO,
    QUEUED_RUN_WAITS_UP_TO,
    REVISION_KEY,
    RUN_CHECKS_WORKFLOW,
    RunChecksInput,
    one_run_per_revision,
    register_run_checks,
)

pytest_plugins = ("tests.app.postgres_fixture",)


class _Recorder:
    """Enough of a Hatchet client (and of a workflow) to register against; records what it is given."""

    def __init__(self) -> None:
        self.workflows: list[_Recorder] = []
        self.workflow_kwargs: dict[str, object] = {}
        self.tasks: list[dict[str, object]] = []
        self.functions: dict[str, Callable[..., Any]] = {}
        self.worker_call: tuple[tuple[object, ...], dict[str, object]] | None = None

    def workflow(self, **kwargs: object) -> _Recorder:
        declared = _Recorder()
        declared.workflow_kwargs = kwargs
        self.workflows.append(declared)
        return declared

    def task(self, **kwargs: object) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        self.tasks.append(kwargs)

        def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.functions[str(kwargs.get("name"))] = fn
            return fn

        return decorate

    def worker(
        self, *args: object, **kwargs: object
    ) -> tuple[tuple[object, ...], dict[str, object]]:
        self.worker_call = (args, kwargs)
        return self.worker_call


class _Session:
    def __init__(self) -> None:
        self.committed = False

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        """Nothing to undo."""

    def close(self) -> None:
        """Nothing to close."""


def _registered(stages_factory: object = None) -> tuple[_Recorder, list[_Session]]:
    client = _Recorder()
    sessions: list[_Session] = []

    def factory() -> _Session:
        sessions.append(_Session())
        return sessions[-1]

    register_run_checks(client, factory, stages_factory)  # type: ignore[arg-type]
    return client.workflows[0], sessions


def _evaluate(expression: str, run_input: Mapping[str, object]) -> object:
    """The one CEL form the key uses, a field path on `input`, evaluated over the run's input."""
    root, *path = expression.split(".")
    assert root == "input" and path, f"{expression!r} is not a field of the run's input"
    value: object = run_input
    for name in path:
        assert isinstance(value, Mapping) and name in value, f"the input has no {name!r}"
        value = value[name]
    return value


# ---------------------------------------------------------------------------
# The declaration
# ---------------------------------------------------------------------------


def test_run_checks_is_declared_with_one_queued_run_per_revision() -> None:
    """Fails on main: no `run_checks` workflow was declared for Hatchet at all, so nothing kept two
    runs for one revision apart. One group per revision, a limit of one, and the queueing strategy:
    a cancelling one would drop the follow-up that reads the person's answer."""
    workflow, _ = _registered()

    assert workflow.workflow_kwargs["name"] == RUN_CHECKS_WORKFLOW == "run_checks"
    assert workflow.workflow_kwargs["input_validator"] is RunChecksInput
    concurrency = workflow.workflow_kwargs["concurrency"]
    assert isinstance(concurrency, ConcurrencyExpression)
    assert concurrency.expression == REVISION_KEY == "input.package_revision_id"
    assert concurrency.max_runs == 1
    assert concurrency.limit_strategy is ConcurrencyLimitStrategy.GROUP_ROUND_ROBIN
    assert concurrency.limit_strategy not in {
        ConcurrencyLimitStrategy.CANCEL_IN_PROGRESS,
        ConcurrencyLimitStrategy.CANCEL_NEWEST,
    }
    assert concurrency.is_tenant_scoped is False
    assert one_run_per_revision() == concurrency
    assert [task["name"] for task in workflow.tasks] == ["run_checks"]
    # Stated, not the SDK's defaults: 5 minutes to start would cancel a run waiting its turn (the
    # newest, reading the last answer), and 60 seconds to finish leaves no room for a real run.
    (task,) = workflow.tasks
    assert task["schedule_timeout"] == QUEUED_RUN_WAITS_UP_TO == timedelta(hours=12)
    assert task["execution_timeout"] == CHECK_RUN_TAKES_UP_TO == timedelta(minutes=30)


def test_the_key_is_the_revision_id_the_run_is_asked_with() -> None:
    """The engine evaluates the key over the run's input, which is the outbox payload as the API
    writes it (`_run_checks_payload`). Two runs for one revision share a group; another revision's
    run is in a group of its own, so it is never held up."""
    first, second = uuid4(), uuid4()

    def payload(revision: UUID) -> dict[str, object]:
        return {
            "package_revision_id": str(revision),
            "discriminators": {"wall_config": "BOTH_WALLS"},
            "discriminator_sources": {"wall_config": "reviewer"},
        }

    def key(revision: UUID) -> object:
        run_input = RunChecksInput.model_validate(payload(revision)).model_dump(mode="json")
        return _evaluate(REVISION_KEY, run_input)

    assert key(first) == str(first)
    assert key(first) == key(first)
    assert key(first) != key(second)
    # A payload with nothing stated still validates: the rules then abstain, as on the local worker.
    assert RunChecksInput.model_validate({"package_revision_id": str(first)}).discriminators == {}


# ---------------------------------------------------------------------------
# The task runs what the local worker runs
# ---------------------------------------------------------------------------


def test_the_task_runs_the_local_worker_s_run_checks_keyed_by_the_outbox_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from workflow import sutradhar
    from workflow.entrypoints import OUTBOX_ROW_METADATA_KEY

    calls: list[tuple[object, ...]] = []

    def fake_run_checks(*args: object, **kwargs: object) -> dict[str, object]:
        calls.append((*args, kwargs))
        return {"checks": {"ran": True}}

    monkeypatch.setattr(sutradhar, "run_checks", fake_run_checks)
    stages_factory = object()
    workflow, sessions = _registered(stages_factory)
    revision, row = uuid4(), uuid4()
    run_input = RunChecksInput(
        package_revision_id=revision,
        discriminators={"wall_config": "BOTH_WALLS"},
        discriminator_sources={"wall_config": "reviewer"},
    )
    ctx = SimpleNamespace(
        additional_metadata={OUTBOX_ROW_METADATA_KEY: str(row)}, workflow_run_id=str(uuid4())
    )

    result = workflow.functions["run_checks"](run_input, ctx)

    assert result == {"checks": {"ran": True}}
    session, *rest = calls[0]
    assert session is sessions[0] and sessions[0].committed
    assert rest == [
        revision,
        str(row),
        {"wall_config": "BOTH_WALLS"},
        {"wall_config": "reviewer"},
        {"stages_factory": stages_factory},
    ]

    # Started without an outbox row (by hand in the engine): keyed by the engine's own run id.
    engine_run = str(uuid4())
    workflow.functions["run_checks"](
        run_input, SimpleNamespace(additional_metadata={}, workflow_run_id=engine_run)
    )
    assert calls[1][2] == engine_run


def test_the_hatchet_worker_registers_run_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import Settings
    from workflow import hatchet_app

    client = _Recorder()
    monkeypatch.setattr(hatchet_app, "hatchet_client", lambda _settings: client)
    settings = Settings(  # type: ignore[call-arg]
        database_url="postgresql+psycopg://gv:gv@localhost/gv",
        hatchet_token="a-token",
        _env_file=None,
    )

    hatchet_app.build_worker(settings, factory=object())  # type: ignore[arg-type]

    assert client.worker_call is not None
    registered = client.worker_call[1]["workflows"]
    assert isinstance(registered, list)
    names = [workflow.workflow_kwargs["name"] for workflow in registered]
    assert names == ["process_package_revision", "generate_signed_exports", "run_checks"]
    assert registered[2].workflow_kwargs["concurrency"] == one_run_per_revision()


# ---------------------------------------------------------------------------
# The local worker, which the live app uses, is unchanged
# ---------------------------------------------------------------------------


@pytest.fixture
def factory(postgres_engine: Engine) -> Iterator[sessionmaker[Session]]:
    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")
    yield session_factory(postgres_engine)


def test_the_local_worker_runs_one_revision_s_check_runs_one_after_the_other(
    factory: sessionmaker[Session],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Two check runs queued for one revision (a run and the follow-up a person's answer queued) go
    through `make worker` one at a time: the second starts only after the first has finished, and a
    second drainer started while the first runs finds nothing to take, because the running row and
    the waiting one are both held. That is the behaviour Hatchet's key now gives; nothing here
    changed."""
    import scripts.drain_outbox as worker
    from workflow.outbox import dispatch_committed, enqueue

    revision = uuid4()
    payload = {"package_revision_id": str(revision), "discriminators": {}}
    with factory.begin() as session:
        first = enqueue(session, workflow="run_checks", payload=payload)
        session.flush()
        second = enqueue(session, workflow="run_checks", payload=payload)

    events: list[tuple[str, str]] = []
    taken_alongside: list[int] = []

    def must_not_start(**_: object) -> None:
        raise AssertionError("a second drainer started a check run beside the running one")

    def fake_consume(session: Session, **kwargs: object) -> Mapping[str, object]:
        del session
        key = str(kwargs["idempotency_key"])
        assert kwargs["workflow"] == "run_checks"
        events.append(("start", key))
        taken_alongside.append(dispatch_committed(factory, must_not_start))
        events.append(("end", key))
        return {"ran": True}

    monkeypatch.setattr(worker, "_consume", fake_consume)
    monkeypatch.setattr("app.db.session.session_factory", lambda _engine: factory)
    monkeypatch.setenv("GV_DATABASE_URL", postgres_engine.url.render_as_string(hide_password=False))
    # No `.env` here, so `Settings()` reads only what the test states.
    monkeypatch.chdir(tmp_path)

    assert worker.main([]) == 0

    with factory() as session:
        order = {
            row.id: row
            for row in session.scalars(
                select(OutboxEntry).where(OutboxEntry.workflow == "run_checks")
            )
        }
    expected_first, expected_second = sorted(
        (first, second), key=lambda entry: (order[entry].created_at, entry)
    )
    assert events == [
        ("start", str(expected_first)),
        ("end", str(expected_first)),
        ("start", str(expected_second)),
        ("end", str(expected_second)),
    ]
    assert taken_alongside == [0, 0]
    assert all(row.dispatched_at is not None for row in order.values())
