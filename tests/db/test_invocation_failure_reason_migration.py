"""A failed or unsent model call may keep why it failed (#1215).

Verification for: `alembic/versions/0094_invocation_failure_reason.py`, the
`model_invocation_rejection_reason` check in `app/models/runs.py` and the same rule in
`extraction.models.invocations.InvocationRecord`.

Before #1215 the reason column was allowed only on a `rejected` row, so a reader question refused
before it was sent (a picture over the readers' size limit) or a wording call that failed outright
kept no cause anywhere. After it, any outcome except `ok` may name its cause; an `ok` row may not,
and an empty reason is never stored.

Everything here is checked against what the *migrations* install, not what `create_all` builds from
the models (which agrees with itself by construction).
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alembic import command
from app.db.session import session_factory, unit_of_work
from app.models import (
    ExtractionRun,
    ModelInvocation,
    ModelInvocationOutcome,
    TaskRun,
    WorkflowRun,
)
from tests.app.postgres_fixture import alembic_config
from tests.db.test_run_models import _invocation, _persist_revision

pytest_plugins = ("tests.app.postgres_fixture",)

REVISION = "0094_invocation_failure_reason"
BEFORE = "0093_model_invocation_refusals"


def _config(engine: Engine):  # type: ignore[no-untyped-def]
    config = alembic_config()
    config.attributes["database_url"] = engine.url.render_as_string(hide_password=False)
    return config


def _run(session: Session) -> ExtractionRun:
    """A run to hang a call off, with keys of its own so a test can store several."""
    revision = _persist_revision(session)
    workflow = WorkflowRun(package_revision_id=revision.id, engine_run_id=f"run-{uuid4()}")
    session.add(workflow)
    session.flush()
    task = TaskRun(
        workflow_run_id=workflow.id,
        idempotency_key=f"failure-reason:{uuid4()}",
        task_type="extract",
        attempt=1,
        outcome="completed",
    )
    session.add(task)
    session.flush()
    extraction = ExtractionRun(
        task_run_id=task.id, extractor="test", extractor_version="1", config_hash="config"
    )
    session.add(extraction)
    session.flush()
    return extraction


def _store(engine: Engine, **changes: object) -> UUID:
    factory = session_factory(engine)
    with unit_of_work(factory) as session:
        extraction = _run(session)
        row = _invocation(extraction.id, **changes)
        session.add(row)
        session.flush()
        return row.id


def _reason(engine: Engine, row_id: UUID) -> tuple[str, str | None]:
    with Session(engine) as session:
        row = session.get(ModelInvocation, row_id)
        assert row is not None
        return row.outcome, row.rejection_reason


def test_the_revision_follows_the_refusals_and_fits_the_version_column() -> None:
    assert len(REVISION) <= 32
    script = ScriptDirectory.from_config(alembic_config())
    found = script.get_revision(REVISION)
    assert found is not None and found.down_revision == BEFORE
    assert script.get_heads() == [REVISION]


@pytest.mark.parametrize(
    "outcome",
    [
        ModelInvocationOutcome.FAILED,
        ModelInvocationOutcome.TIMEOUT,
        ModelInvocationOutcome.REFUSED,
        ModelInvocationOutcome.BUDGET_REFUSED,
        ModelInvocationOutcome.ACCOUNT_REFUSED,
    ],
)
def test_a_migrated_database_keeps_the_cause_of_a_call_that_did_not_answer(
    postgres_engine: Engine, outcome: ModelInvocationOutcome
) -> None:
    command.upgrade(_config(postgres_engine), "head")

    row_id = _store(
        postgres_engine,
        outcome=outcome,
        rejection_reason="PictureWouldBeResized",
        input_tokens=0,
        output_tokens=0,
        cost_micros=0,
    )

    assert _reason(postgres_engine, row_id) == (outcome.value, "PictureWouldBeResized")


def test_a_rejected_row_still_keeps_its_reason(postgres_engine: Engine) -> None:
    command.upgrade(_config(postgres_engine), "head")

    row_id = _store(
        postgres_engine,
        outcome=ModelInvocationOutcome.REJECTED,
        rejection_reason="malformed_form_answer",
    )

    assert _reason(postgres_engine, row_id) == ("rejected", "malformed_form_answer")


def test_a_successful_row_may_not_carry_a_reason(postgres_engine: Engine) -> None:
    command.upgrade(_config(postgres_engine), "head")

    with pytest.raises(IntegrityError):
        _store(postgres_engine, outcome=ModelInvocationOutcome.OK, rejection_reason="anything")


def test_an_empty_reason_is_never_stored(postgres_engine: Engine) -> None:
    command.upgrade(_config(postgres_engine), "head")

    with pytest.raises(IntegrityError):
        _store(postgres_engine, outcome=ModelInvocationOutcome.FAILED, rejection_reason="")


def test_the_downgrade_refuses_while_a_failed_call_keeps_its_cause(postgres_engine: Engine) -> None:
    """Refuses rather than rewrites (as `0049` and `0093` do): the table is append-only, so a reason
    the old check forbids is neither cleared nor deleted. Nothing changes; the revision stays."""
    config = _config(postgres_engine)
    command.upgrade(config, "head")
    failed = _store(
        postgres_engine,
        outcome=ModelInvocationOutcome.FAILED,
        rejection_reason="PictureWouldBeResized",
        input_tokens=0,
        output_tokens=0,
        cost_micros=0,
    )

    with pytest.raises(RuntimeError, match=r"1 model_invocations row\(s\) record why"):
        command.downgrade(config, BEFORE)

    assert _reason(postgres_engine, failed) == ("failed", "PictureWouldBeResized")
    with postgres_engine.connect() as connection:
        version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert version == REVISION


def test_the_migration_goes_down_and_up_when_no_row_needs_it(postgres_engine: Engine) -> None:
    """With only rejected reasons stored, down restores the old check and up the new one."""
    config = _config(postgres_engine)
    command.upgrade(config, "head")
    rejected = _store(
        postgres_engine,
        outcome=ModelInvocationOutcome.REJECTED,
        rejection_reason="malformed_form_answer",
    )
    plain_failure = _store(postgres_engine, outcome=ModelInvocationOutcome.FAILED)

    command.downgrade(config, BEFORE)

    assert _reason(postgres_engine, rejected) == ("rejected", "malformed_form_answer")
    assert _reason(postgres_engine, plain_failure) == ("failed", None)
    with pytest.raises(IntegrityError):
        _store(postgres_engine, outcome=ModelInvocationOutcome.FAILED, rejection_reason="Anything")

    command.upgrade(config, "head")

    again = _store(
        postgres_engine, outcome=ModelInvocationOutcome.FAILED, rejection_reason="TimeoutError"
    )
    assert _reason(postgres_engine, again) == ("failed", "TimeoutError")
