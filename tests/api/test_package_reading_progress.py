"""`GET .../packages/{id}` says where the reading is while it runs (#1178).

Synthetic rows only: a revision walked into `EXTRACTING`, one progress line, one paid call and one
reused answer in the ledger.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.lifecycle.side_states import enter_failure
from app.lifecycle.states import begin, transition
from app.models import PackageRevision, PackageState
from app.models.runs import REUSED_FROM_KEY, ExtractionRun, ModelInvocation, TaskRun, WorkflowRun
from app.runs.reading_progress import record_progress
from tests.api.test_packages import PROJECT, _client, _new_package, session, store

pytest_plugins = ("tests.app.postgres_fixture",)

__all__ = ["session", "store"]  # fixtures, imported for this module's tests


def _revision(session: Session, package_id: UUID) -> PackageRevision:
    revision = session.query(PackageRevision).filter_by(package_id=package_id).one()
    return revision


def _walk(session: Session, revision_id: UUID, *states: PackageState) -> None:
    begin(session, revision_id, actor="reviewer")
    for state in states:
        transition(session, revision_id, state, actor="the worker")
    session.commit()


def _ledger_call(session: Session, revision_id: UUID, *, cost: int, reused: bool = False) -> None:
    workflow = WorkflowRun(package_revision_id=revision_id, engine_run_id=str(uuid4()))
    session.add(workflow)
    session.flush()
    task = TaskRun(
        workflow_run_id=workflow.id,
        idempotency_key=str(uuid4()),
        task_type="reader_call_ledger",
        attempt=1,
        outcome="recorded",
    )
    session.add(task)
    session.flush()
    run = ExtractionRun(
        task_run_id=task.id,
        extractor="workflow.call_ledger",
        extractor_version="call-ledger-v1",
        config_hash="test",
    )
    session.add(run)
    session.flush()
    session.add(
        ModelInvocation(
            extraction_run_id=run.id,
            model_id="anthropic.claude-opus-5-5",
            prompt_id="slot-test",
            template_id="slot-test",
            input_tokens=0 if reused else 1000,
            output_tokens=0 if reused else 100,
            cost_micros=cost,
            latency_ms=5,
            outcome="ok",
            reader_page_index=0,
            reader_attempt_number=1,
            reader_question_packet={REUSED_FROM_KEY: str(uuid4())} if reused else {"q": 1},
        )
    )
    session.commit()


def _own_revision_call(session: Session, revision_id: UUID, *, cost: int) -> None:
    """A call that carries its own revision (as the review assistant's do)."""
    session.add(
        ModelInvocation(
            package_revision_id=revision_id,
            model_id="anthropic.claude-sonnet-5-5",
            prompt_id="assistant-test",
            template_id="assistant-test",
            input_tokens=100,
            output_tokens=10,
            cost_micros=cost,
            latency_ms=5,
            outcome="ok",
        )
    )
    session.commit()


def test_while_reading_the_package_says_the_stage_the_page_and_the_calls_so_far(
    session: Session, store: Any
) -> None:
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
    record_progress(
        session.get_bind(),  # type: ignore[arg-type]
        revision.id,
        "dimensions",
        pages_done=4,
        pages_total=17,
    )
    _ledger_call(session, revision.id, cost=250_000)
    _ledger_call(session, revision.id, cost=0, reused=True)
    _own_revision_call(session, revision.id, cost=10_000)

    answer = _client(session, store).get(f"/api/v1/projects/{PROJECT}/packages/{package_id}")

    assert answer.status_code == 200, answer.text
    assert answer.json()["state"] == "EXTRACTING"
    reading = answer.json()["reading"]
    assert reading.pop("updated_at") is not None
    # Counted as the Usage page counts the revision: the reading call and the review-time call.
    assert reading == {
        "stage": "dimensions",
        "label": "AI readers reading the dimensions",
        "pages_done": 4,
        "pages_total": 17,
        "calls": 2,
        "cost_usd": "0.260000",
        "unpriced_calls": 0,
        "retrying": False,
    }


def test_before_the_worker_writes_a_line_the_state_names_the_stage(
    session: Session, store: Any
) -> None:
    package_id = _new_package(session)
    revision = _revision(session, package_id)
    _walk(session, revision.id, PackageState.UPLOADING, PackageState.UPLOADED)

    answer = _client(session, store).get(f"/api/v1/projects/{PROJECT}/packages/{package_id}")

    assert answer.json()["reading"] == {
        "stage": "waiting",
        "label": "waiting for the reader to start",
        "pages_done": None,
        "pages_total": None,
        "calls": 0,
        "cost_usd": "0.000000",
        "unpriced_calls": 0,
        "updated_at": None,
        "retrying": False,
    }


def test_once_handed_to_the_reviewer_there_is_no_reading_and_the_list_never_carries_one(
    session: Session, store: Any
) -> None:
    package_id = _new_package(session)
    revision = _revision(session, package_id)
    _walk(
        session,
        revision.id,
        PackageState.UPLOADING,
        PackageState.UPLOADED,
        PackageState.INGESTING,
        PackageState.EXTRACTING,
        PackageState.MATCHING,
        PackageState.VALIDATING_EVIDENCE,
        PackageState.NEEDS_INPUT,
    )
    record_progress(session.get_bind(), revision.id, "form")  # type: ignore[arg-type]
    client = _client(session, store)

    one = client.get(f"/api/v1/projects/{PROJECT}/packages/{package_id}")
    listed = client.get(f"/api/v1/projects/{PROJECT}/packages")

    assert one.json()["state"] == "NEEDS_INPUT"
    assert one.json()["reading"] is None
    assert all(item["reading"] is None for item in listed.json()["items"])


def test_a_retry_after_a_failure_shows_its_own_progress_and_an_old_line_is_not_shown(
    session: Session, store: Any
) -> None:
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
    bind = session.get_bind()
    record_progress(bind, revision.id, "pages", pages_done=3, pages_total=9)  # type: ignore[arg-type]
    enter_failure(session, revision.id, actor="the worker", error=TimeoutError("synthetic"))
    session.commit()
    client = _client(session, store)
    url = f"/api/v1/projects/{PROJECT}/packages/{package_id}"

    # The failed attempt's line is older than the failure: nothing is running, nothing is shown.
    assert client.get(url).json()["reading"] is None

    # The retry writes a line: shown, marked as a retry, while the package still says failed.
    record_progress(bind, revision.id, "rows", pages_done=1, pages_total=9)  # type: ignore[arg-type]
    answer = client.get(url).json()
    assert answer["state"] == "FAILED_RETRYABLE"
    assert answer["reading"]["retrying"] is True
    assert (answer["reading"]["stage"], answer["reading"]["pages_done"]) == ("rows", 1)


# --- #1206: why the reading stopped, in plain words ---------------------------------------------


def test_out_of_credit_is_said_plainly_on_the_package(session: Session, store: Any) -> None:
    """The provider refused the account (HTTP 402): the package waits, retryable, and says so in
    words a person can act on, instead of "Processing failed"."""
    from app.lifecycle.side_states import OUT_OF_CREDIT, FailureClass

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
    enter_failure(
        session,
        revision.id,
        actor="the extract pages worker",
        failure_class=FailureClass.RETRYABLE,
        reason=OUT_OF_CREDIT,
    )
    session.commit()

    answer = _client(session, store).get(f"/api/v1/projects/{PROJECT}/packages/{package_id}")

    assert answer.status_code == 200, answer.text
    assert answer.json()["state"] == "FAILED_RETRYABLE"
    assert answer.json()["stop_reason"] == (
        "The AI provider account is out of credit. Top it up, then run the reading again."
    )


def test_any_other_failure_keeps_its_words_off_the_package(session: Session, store: Any) -> None:
    """A failure's reason may be an exception's own words: only a reason written for a person is
    shown, so the screen keeps its general message for the rest."""
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
    enter_failure(session, revision.id, actor="the worker", error=TimeoutError("socket 10.0.0.4"))
    session.commit()

    answer = _client(session, store).get(f"/api/v1/projects/{PROJECT}/packages/{package_id}")

    assert answer.json()["state"] == "FAILED_RETRYABLE"
    assert answer.json()["stop_reason"] is None
