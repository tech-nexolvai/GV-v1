"""Database contract for execution records in issue #194, and the outcome set they may record (#313)."""

from __future__ import annotations

import re
from uuid import UUID

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint, Engine, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alembic import command
from app.db.base import Base, Immutable
from app.db.session import session_factory, unit_of_work
from app.models import (
    Document,
    DocumentVersion,
    ExtractionFailure,
    ExtractionRun,
    ModelInvocation,
    ModelInvocationOutcome,
    Package,
    PackageRevision,
    PackageState,
    Project,
    SourceArtifact,
    TaskRun,
    WorkflowRun,
)
from tests.app.postgres_fixture import alembic_config

pytest_plugins = ("tests.app.postgres_fixture",)

RUN_TABLES = (
    "workflow_runs",
    "task_runs",
    "extraction_runs",
    "model_invocations",
    "extraction_failures",
)


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


def test_a_failed_model_call_retains_complete_cost_and_identity(
    postgres_engine: Engine,
) -> None:
    """Input: failed paid call. Outcome: full row. Why: a failure still cost money (#313).

    The gap this issue closed. `failed` was not in the enum or the constraint, so a call that came
    back with no answer and was neither a timeout nor a refusal could not be stored at all — the
    insert was rejected and the record of a paid attempt was lost. `E2.3` (#251) says every call is
    recorded; `F5.3` (#266) bills from these rows, and the input tokens were spent before it failed.
    """

    Base.metadata.create_all(postgres_engine)
    factory = session_factory(postgres_engine)
    invocation_id: UUID
    with unit_of_work(factory) as session:
        extraction = _persist_run_chain(session)
        invocation = _invocation(
            extraction.id,
            outcome=ModelInvocationOutcome.FAILED,
            input_tokens=512,
            output_tokens=0,
            cost_micros=118,
            latency_ms=2400,
        )
        invocation_id = invocation.id
        session.add(invocation)
    with unit_of_work(factory) as session:
        restored = session.get(ModelInvocation, invocation_id)
        assert restored is not None
        assert restored.outcome == ModelInvocationOutcome.FAILED
        # Provenance, in full. A failed call is only worth keeping if it can be attributed.
        assert restored.model_id == "gpt-5-mini-2026-08-01"
        assert restored.prompt_id == "dimension-reader-v3"
        assert restored.template_id == "crop-dimension-v2"
        # Accounting, in full. Output tokens are legitimately zero; the input tokens are not.
        assert restored.input_tokens == 512
        assert restored.output_tokens == 0
        assert restored.cost_micros == 118
        assert restored.latency_ms == 2400


# ---------------------------------------------------------------------------
# The enum and the constraint cannot drift again (#313)
# ---------------------------------------------------------------------------
#
# Why neither existing guard caught `failed` being missing, which is what these two are shaped around:
#
#   * Every test above calls `Base.metadata.create_all`, building the tables from the **ORM metadata**.
#     The ORM's check constraint is generated from `ModelInvocationOutcome` itself, so the enum and the
#     thing under test are one source. Add a member and they all still pass, migration or no migration.
#   * `tests/app/test_migrations_roundtrip.py` uses Alembic's `compare_metadata`, which does not compare
#     check constraints at all. A constraint disagreeing with the models yields an empty diff.
#
# So a drift test must read what the *migrations* install. One below reads the migration chain and needs
# no database; the other reads a migrated database and is authoritative.


def _outcomes_from_the_migrations() -> tuple[str, frozenset[str]]:
    """The outcome values the newest migration to define them installs, and which revision that is.

    Walks the revision chain newest-first, so it keeps working when a later migration widens the set
    again — it finds that one rather than this story's. The convention it relies on is that a migration
    touching this constraint names its values in a module-level `MODEL_OUTCOMES`, which `0005` and
    `0015` both do; the failure message says so, because a migration that inlined the string instead
    would make this test fail rather than quietly pass.
    """
    script = ScriptDirectory.from_config(alembic_config())
    for revision in script.walk_revisions():
        values = getattr(revision.module, "MODEL_OUTCOMES", None)
        if values is not None:
            return revision.revision, frozenset(
                value.strip().strip("'") for value in values.split(",")
            )
    raise AssertionError(
        "no migration defines MODEL_OUTCOMES. A migration that changes the "
        "model_invocation_outcome constraint must name its values in a module-level MODEL_OUTCOMES "
        "constant, so this test can compare them against the enum."
    )


def test_the_enum_and_the_migrated_constraint_list_the_same_outcomes() -> None:
    """Input: enum and newest migration. Outcome: identical sets. Why: they drifted, silently.

    **The scope item, and the one that runs without a database.** `failed` was in neither for as long
    as it was missing, and nothing failed — the enum agreed with the ORM, the ORM agreed with itself,
    and the migration was never consulted. Adding a member without a migration now fails here.
    """
    revision, migrated = _outcomes_from_the_migrations()
    declared = frozenset(outcome.value for outcome in ModelInvocationOutcome)

    assert declared == migrated, (
        f"ModelInvocationOutcome and migration {revision} disagree.\n"
        f"  only in the enum:      {sorted(declared - migrated)}\n"
        f"  only in the migration: {sorted(migrated - declared)}\n\n"
        "A member added to the enum needs a new migration widening the CHECK constraint, or the "
        "database will reject every row using it. Never edit a shipped migration — add one."
    )


def test_failed_is_among_them() -> None:
    """Input: the agreed set. Outcome: contains `failed`. Why: the two agreeing on four is not the fix.

    Without this, deleting `FAILED` from the enum *and* from the migration would leave the test above
    perfectly green while restoring the exact bug (#313) — two things agreeing is not the same as two
    things being right.
    """
    _, migrated = _outcomes_from_the_migrations()

    assert ModelInvocationOutcome.FAILED.value == "failed"
    assert "failed" in migrated


def test_the_database_really_enforces_the_declared_outcomes(postgres_engine: Engine) -> None:
    """Input: a migrated database. Outcome: constraint matches the enum. Why: this is the real check.

    The authoritative version, and the only one that inspects what a deployed database actually
    enforces. Built with `alembic upgrade head` rather than `create_all` — deliberately, because
    `create_all` builds from the ORM metadata and would compare the enum with itself, which is how
    this went unnoticed. The constraint is read back with `pg_get_constraintdef`, so what is asserted
    is what PostgreSQL will apply to an insert.

    **The name comes from the ORM, the values come from the database.** Two things I got wrong first
    time, both only visible against a real PostgreSQL:

    `app/db/base.py` sets a naming convention (`ck_%(table_name)s_%(constraint_name)s`), so the stored
    name is `ck_model_invocations_model_invocation_outcome`, not the `model_invocation_outcome` written
    in the model and the migration. Asking the ORM for the name rather than hardcoding either spelling
    keeps this working if the convention changes — and the name is not what is under test anyway.

    And PostgreSQL does not store `outcome IN (...)` as written: it rewrites it to
    `outcome = ANY (ARRAY[...])`. Matching on `" IN "` therefore found nothing at all, which is worth
    knowing before writing any test that reads a check constraint back.
    """
    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")

    constraint = next(
        candidate
        for candidate in ModelInvocation.__table__.constraints
        if isinstance(candidate, CheckConstraint) and "outcome IN" in str(candidate.sqltext)
    )

    with postgres_engine.connect() as connection:
        definition = connection.execute(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = :name AND conrelid = 'model_invocations'::regclass"
            ),
            {"name": constraint.name},
        ).scalar_one()

    # The values survive the rewrite as quoted literals — `'ok'::character varying` and so on — so the
    # quoted lowercase tokens are exactly the permitted set, and nothing else in this definition is
    # quoted.
    enforced = frozenset(re.findall(r"'([a-z_]+)'", definition))
    declared = frozenset(outcome.value for outcome in ModelInvocationOutcome)

    assert enforced == declared, (
        f"the database enforces {sorted(enforced)} but the enum declares {sorted(declared)}.\n"
        f"constraint: {definition}\n\n"
        "A value the enum offers and the database refuses is an insert that fails in production and "
        "nowhere else."
    )


def test_a_failed_row_survives_the_migration_that_permits_it(postgres_engine: Engine) -> None:
    """Input: migrated schema, failed row. Outcome: accepted. Why: prove the migration, not the ORM.

    Every other persistence test here builds its tables with `create_all`, so all of them would pass
    with migration `0015` deleted. This one inserts through the migrated schema, which is the schema a
    deployment actually has.
    """
    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")

    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        extraction = _persist_run_chain(session)
        session.add(_invocation(extraction.id, outcome=ModelInvocationOutcome.FAILED))


def _persist_document_version(session: Session, package_id: UUID) -> UUID:
    """A document version to hang an extraction failure off, built the long way the schema requires."""
    digest = "b" * 64
    document = Document(package_id=package_id, kind="shop")
    session.add(document)
    session.flush()
    artifact = SourceArtifact(storage_key=f"documents/{document.id}", sha256=digest, size=1024)
    session.add(artifact)
    session.flush()
    version = DocumentVersion(
        document_id=document.id, source_artifact_id=artifact.id, sha256=digest, page_count=1
    )
    session.add(version)
    session.flush()
    return version.id


def _failure(extraction: ExtractionRun, version_id: UUID, **changes: object) -> ExtractionFailure:
    values: dict[str, object] = {
        "extraction_run_id": extraction.id,
        "document_version_id": version_id,
        "page_index": None,
        "reason": "document_unreadable",
        "error_type": "UnreadablePdf",
    }
    values.update(changes)
    return ExtractionFailure(**values)


def test_extraction_failures_are_marked_immutable() -> None:
    """A drawing that could not be read is a fact about the package, not a note to be tidied later.

    A later successful re-read adds rows elsewhere; it does not get to erase the attempt that failed.
    """
    assert issubclass(ExtractionFailure, Immutable)


def test_a_document_level_failure_may_not_claim_a_page(postgres_engine: Engine) -> None:
    """`page_index IS NULL` is what *means* "the whole document", so a value there contradicts the reason.

    Enforced in the database rather than in the recorder, because the two states are one nullable
    column apart: without this, a row saying `document_unreadable` on page 3 stores cleanly and a
    reader has to guess which half was meant.
    """
    Base.metadata.create_all(postgres_engine)
    factory = session_factory(postgres_engine)
    with pytest.raises(IntegrityError), unit_of_work(factory) as session:
        extraction = _persist_run_chain(session)
        task = session.get(TaskRun, extraction.task_run_id)
        assert task is not None
        workflow = session.get(WorkflowRun, task.workflow_run_id)
        assert workflow is not None
        revision = session.get(PackageRevision, workflow.package_revision_id)
        assert revision is not None
        version_id = _persist_document_version(session, revision.package_id)
        session.add(_failure(extraction, version_id, reason="document_unreadable", page_index=3))
        session.flush()


def test_a_page_level_failure_must_name_its_page(postgres_engine: Engine) -> None:
    """The other half of the same constraint, asserted separately because one check can pass while the
    other does not — and a `page_unreadable` row with no page names a gap nobody can go and look at.
    """
    Base.metadata.create_all(postgres_engine)
    factory = session_factory(postgres_engine)
    with pytest.raises(IntegrityError), unit_of_work(factory) as session:
        extraction = _persist_run_chain(session)
        task = session.get(TaskRun, extraction.task_run_id)
        assert task is not None
        workflow = session.get(WorkflowRun, task.workflow_run_id)
        assert workflow is not None
        revision = session.get(PackageRevision, workflow.package_revision_id)
        assert revision is not None
        version_id = _persist_document_version(session, revision.package_id)
        session.add(_failure(extraction, version_id, reason="page_unreadable", page_index=None))
        session.flush()


def test_an_unknown_reason_is_refused(postgres_engine: Engine) -> None:
    """The vocabulary is closed. A reason nothing recognises is a row no reviewer can act on, and it
    would be discovered by whoever later filtered on the two values that were meant to be exhaustive.
    """
    Base.metadata.create_all(postgres_engine)
    factory = session_factory(postgres_engine)
    with pytest.raises(IntegrityError), unit_of_work(factory) as session:
        extraction = _persist_run_chain(session)
        task = session.get(TaskRun, extraction.task_run_id)
        assert task is not None
        workflow = session.get(WorkflowRun, task.workflow_run_id)
        assert workflow is not None
        revision = session.get(PackageRevision, workflow.package_revision_id)
        assert revision is not None
        version_id = _persist_document_version(session, revision.package_id)
        session.add(_failure(extraction, version_id, reason="looked_wrong", page_index=None))
        session.flush()


def test_both_valid_shapes_are_accepted(postgres_engine: Engine) -> None:
    """The positive control. Three constraints that reject everything would pass every test above."""
    Base.metadata.create_all(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        extraction = _persist_run_chain(session)
        task = session.get(TaskRun, extraction.task_run_id)
        assert task is not None
        workflow = session.get(WorkflowRun, task.workflow_run_id)
        assert workflow is not None
        revision = session.get(PackageRevision, workflow.package_revision_id)
        assert revision is not None
        version_id = _persist_document_version(session, revision.package_id)
        session.add(_failure(extraction, version_id, reason="document_unreadable", page_index=None))
        session.add(_failure(extraction, version_id, reason="page_unreadable", page_index=0))
        session.flush()

        stored = session.execute(select(ExtractionFailure)).scalars().all()
        assert sorted(row.reason for row in stored) == ["document_unreadable", "page_unreadable"]
