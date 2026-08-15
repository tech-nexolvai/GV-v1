"""Database contract for execution records in issue #194."""

from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.base import Base, Immutable
from app.db.session import session_factory, unit_of_work
from app.models import (
    ExtractionRun,
    ModelInvocation,
    ModelInvocationOutcome,
    Package,
    PackageRevision,
    PackageState,
    Project,
    TaskRun,
    WorkflowRun,
)

pytest_plugins = ("tests.app.postgres_fixture",)

RUN_TABLES = ("workflow_runs", "task_runs", "extraction_runs", "model_invocations")


@pytest.mark.parametrize("table", RUN_TABLES)
def test_every_run_table_is_registered(table: str) -> None:
    """Input: imported run models. Outcome: table registered. Why: Alembic must see it."""

    assert table in Base.metadata.tables


def test_model_invocations_are_marked_immutable() -> None:
    """Input: invocation model. Outcome: immutable marker. Why: failed calls remain auditable."""

    assert issubclass(ModelInvocation, Immutable)


def _persist_revision(session: Session) -> PackageRevision:
    project = Project(name="GV Run Test")
    package = Package(project_id=project.id, vendor=None)
    revision = PackageRevision(
        package_id=package.id,
        revision_number=1,
        state=PackageState.CREATED,
    )
    session.add(project)
    session.flush()
    session.add(package)
    session.flush()
    session.add(revision)
    session.flush()
    return revision


def _persist_run_chain(session: Session) -> ExtractionRun:
    revision = _persist_revision(session)
    workflow = WorkflowRun(package_revision_id=revision.id, engine_run_id="hatchet-run-001")
    session.add(workflow)
    session.flush()
    task = TaskRun(
        workflow_run_id=workflow.id,
        idempotency_key="package-revision:extract:v1",
        task_type="extract",
        attempt=1,
        outcome="completed",
    )
    session.add(task)
    session.flush()
    extraction = ExtractionRun(
        task_run_id=task.id,
        extractor="vector-dimensions",
        extractor_version="1.4.2",
        config_hash="config-sha256-value",
    )
    session.add(extraction)
    session.flush()
    return extraction


def _invocation(extraction_run_id: UUID, **changes: object) -> ModelInvocation:
    values: dict[str, object] = {
        "extraction_run_id": extraction_run_id,
        "model_id": "gpt-5-mini-2026-08-01",
        "prompt_id": "dimension-reader-v3",
        "template_id": "crop-dimension-v2",
        "crop_artifact_id": None,
        "input_tokens": 600,
        "output_tokens": 40,
        "cost_micros": 137,
        "latency_ms": 850,
        "outcome": ModelInvocationOutcome.OK,
    }
    values.update(changes)
    return ModelInvocation(**values)


def test_duplicate_task_delivery_is_rejected_by_idempotency_key(postgres_engine: Engine) -> None:
    """Input: two tasks with one key. Outcome: rejection. Why: retries cannot duplicate work."""

    Base.metadata.create_all(postgres_engine)
    factory = session_factory(postgres_engine)
    with pytest.raises(IntegrityError), unit_of_work(factory) as session:
        extraction = _persist_run_chain(session)
        first = session.get(TaskRun, extraction.task_run_id)
        assert first is not None
        session.add(
            TaskRun(
                workflow_run_id=first.workflow_run_id,
                idempotency_key=first.idempotency_key,
                task_type="extract",
                attempt=2,
                outcome="completed",
            )
        )
        session.flush()


def test_rejected_model_invocation_retains_complete_cost_and_identity(
    postgres_engine: Engine,
) -> None:
    """Input: rejected paid call. Outcome: full row. Why: failures still cost and need audit."""

    Base.metadata.create_all(postgres_engine)
    factory = session_factory(postgres_engine)
    invocation_id: UUID
    with unit_of_work(factory) as session:
        extraction = _persist_run_chain(session)
        invocation = _invocation(
            extraction.id,
            outcome=ModelInvocationOutcome.REJECTED,
            input_tokens=321,
            output_tokens=0,
            cost_micros=91,
        )
        invocation_id = invocation.id
        session.add(invocation)
    with unit_of_work(factory) as session:
        restored = session.get(ModelInvocation, invocation_id)
        assert restored is not None
        assert restored.outcome == ModelInvocationOutcome.REJECTED
        assert restored.model_id == "gpt-5-mini-2026-08-01"
        assert restored.prompt_id == "dimension-reader-v3"
        assert restored.template_id == "crop-dimension-v2"
        assert restored.input_tokens == 321
        assert restored.output_tokens == 0
        assert restored.cost_micros == 91


def test_package_cost_uses_exact_integer_aggregation(postgres_engine: Engine) -> None:
    """Input: costs 137 and 204 micros. Outcome: 341. Why: reporting never uses floats."""

    Base.metadata.create_all(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        extraction = _persist_run_chain(session)
        workflow = session.scalar(
            select(WorkflowRun)
            .join(TaskRun, TaskRun.workflow_run_id == WorkflowRun.id)
            .where(TaskRun.id == extraction.task_run_id)
        )
        assert workflow is not None
        revision_id = workflow.package_revision_id
        session.add_all(
            (
                _invocation(extraction.id, cost_micros=137),
                _invocation(
                    extraction.id,
                    cost_micros=204,
                    outcome=ModelInvocationOutcome.TIMEOUT,
                ),
            )
        )
    with unit_of_work(factory) as session:
        total = session.scalar(
            select(func.sum(ModelInvocation.cost_micros))
            .join(ExtractionRun, ModelInvocation.extraction_run_id == ExtractionRun.id)
            .join(TaskRun, ExtractionRun.task_run_id == TaskRun.id)
            .join(WorkflowRun, TaskRun.workflow_run_id == WorkflowRun.id)
            .where(WorkflowRun.package_revision_id == revision_id)
        )
        assert total == 341
        assert isinstance(total, int)


@pytest.mark.parametrize("extractor_version", [None, ""])
def test_extractor_version_is_required(
    postgres_engine: Engine, extractor_version: str | None
) -> None:
    """Input: absent extractor version. Outcome: rejection. Why: regressions need attribution."""

    Base.metadata.create_all(postgres_engine)
    factory = session_factory(postgres_engine)
    with pytest.raises(IntegrityError), unit_of_work(factory) as session:
        extraction = _persist_run_chain(session)
        extraction.extractor_version = extractor_version  # type: ignore[assignment]
        session.flush()


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"input_tokens": -1}, "negative token count"),
        ({"cost_micros": -1}, "negative exact cost"),
        ({"outcome": "silently_skipped"}, "unrecognised outcome"),
    ],
)
def test_invalid_invocation_accounting_is_rejected(
    postgres_engine: Engine,
    changes: dict[str, object],
    reason: str,
) -> None:
    """Input: malformed accounting. Outcome: rejection. Why: reports require truthful rows."""

    del reason
    Base.metadata.create_all(postgres_engine)
    factory = session_factory(postgres_engine)
    with pytest.raises(IntegrityError), unit_of_work(factory) as session:
        extraction = _persist_run_chain(session)
        session.add(_invocation(extraction.id, **changes))
        session.flush()
