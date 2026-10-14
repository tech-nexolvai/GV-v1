"""The reading commits per stage, ledgers every paid call at once, and says where it is (#1178).

Verification for `scripts/drain_outbox.py:_extract_package`, `workflow/reading_watch.py`,
`app/runs/reading_progress.py` and their wiring in `workflow/stages.py`. Every test watches from a
second database session while the first is still inside a stage, which is what a reviewer's browser
does. Fake readers and synthetic sheets only; no network, nothing paid.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.lifecycle.states import begin, transition
from app.models import (
    Package,
    PackageRevision,
    PackageState,
    PackageStateEvent,
    Project,
    ReadingProgress,
)
from app.models.runs import REUSED_FROM_KEY, ExtractionRun, ModelInvocation, TaskRun
from app.runs.reading_progress import count_reading_calls, reading_progress_for, record_progress
from extraction.form_reader.bedrock import AttemptUsage
from tests.workflow.test_reader_reuse import MemoryStore, _page, _readers, _runtime
from tests.workflow.test_slot_reader import _scaffold
from workflow.idempotency import stage_idempotency_key
from workflow.reader_reuse import StoredReaderAnswers
from workflow.reading_watch import LEDGER_EXTRACTOR, CallLedger, ledger_runs_of
from workflow.review import ENGINE_VERSION
from workflow.slot_reader import read_slot_pages

pytest_plugins = ("tests.app.postgres_fixture",)


@pytest.fixture
def engine(postgres_engine: Engine) -> Engine:
    from alembic import command
    from tests.app.postgres_fixture import alembic_config

    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")
    return postgres_engine


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    from app.db.session import session_factory

    opened = session_factory(engine)()
    try:
        yield opened
    finally:
        opened.close()


def _uploaded_revision(session: Session) -> UUID:
    project = Project(name="reading progress")
    session.add(project)
    session.flush()
    package = Package(project_id=project.id)
    session.add(package)
    session.flush()
    revision = PackageRevision(package_id=package.id, revision_number=1, state=PackageState.CREATED)
    session.add(revision)
    session.flush()
    begin(session, revision.id, actor="reviewer")
    transition(session, revision.id, PackageState.UPLOADING, actor="reviewer")
    transition(session, revision.id, PackageState.UPLOADED, actor="reviewer")
    session.commit()
    return revision.id


def _state(engine: Engine, revision_id: UUID) -> str:
    with Session(engine) as watcher:
        return str(
            watcher.scalar(select(PackageRevision.state).where(PackageRevision.id == revision_id))
        )


def _claimed(engine: Engine, revision_id: UUID, stage: str) -> bool:
    key = stage_idempotency_key(
        package_revision_id=revision_id, stage=stage, engine_version=ENGINE_VERSION
    )
    with Session(engine) as watcher:
        return watcher.scalar(select(TaskRun.id).where(TaskRun.idempotency_key == key)) is not None


class _WatchedStages:
    """Fake stages that look from a second session at what the earlier stages left committed."""

    def __init__(
        self,
        engine: Engine,
        *,
        fail_extract: list[BaseException] | None = None,
        fail: Mapping[str, list[BaseException]] | None = None,
    ) -> None:
        self.engine = engine
        self.calls: dict[str, int] = {}
        self.seen: dict[str, object] = {}
        self.fail_extract = list(fail_extract or [])
        self.fail = {stage: list(errors) for stage, errors in (fail or {}).items()}

    def _count(self, stage: str) -> None:
        self.calls[stage] = self.calls.get(stage, 0) + 1
        if self.fail.get(stage):
            raise self.fail[stage].pop(0)

    def ingest(self, session: Session, package_revision_id: UUID) -> dict[str, object]:
        self._count("ingest")
        return {"implemented": True}

    def extract_pages(self, session: Session, package_revision_id: UUID) -> tuple[object, ...]:
        self._count("extract_pages")
        # Inside the extraction stage: ingest's work is already visible to anyone else.
        self.seen["state during extract"] = _state(self.engine, package_revision_id)
        self.seen["ingest claimed during extract"] = _claimed(
            self.engine, package_revision_id, "ingest"
        )
        self.seen["extract claimed during extract"] = _claimed(
            self.engine, package_revision_id, "extract_pages"
        )
        # A progress line written now is visible now, while this stage's transaction is open and
        # holds its lock on the revision.
        record_progress(self.engine, package_revision_id, "pages", pages_done=2, pages_total=5)
        with Session(self.engine) as watcher:
            self.seen["reading during extract"] = reading_progress_for(
                watcher, package_revision_id, PackageState.INGESTING
            )
        if self.fail_extract:
            raise self.fail_extract.pop(0)
        return ()

    def match(self, session: Session, package_revision_id: UUID) -> dict[str, object]:
        self._count("match")
        self.seen["state during match"] = _state(self.engine, package_revision_id)
        self.seen["extract claimed during match"] = _claimed(
            self.engine, package_revision_id, "extract_pages"
        )
        return {"proposed": 0}

    def validate_evidence(self, session: Session, package_revision_id: UUID) -> dict[str, object]:
        self._count("validate_evidence")
        self.seen["state during validate"] = _state(self.engine, package_revision_id)
        return {"crops": 0}


def test_each_stage_is_visible_to_a_second_session_before_the_next_one_ends(
    engine: Engine, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scripts.drain_outbox as worker

    revision_id = _uploaded_revision(session)
    stages = _WatchedStages(engine)
    monkeypatch.setattr(worker, "_stages", lambda **_: stages)

    worker._extract_package(session, revision_id, str(uuid4()))
    # Not committed yet: the last stage and the hand-over land with the caller's commit.
    assert _state(engine, revision_id) == PackageState.MATCHING.value
    session.commit()

    assert stages.seen["state during extract"] == PackageState.INGESTING.value
    assert stages.seen["ingest claimed during extract"] is True
    assert stages.seen["extract claimed during extract"] is False
    assert stages.seen["state during match"] == PackageState.EXTRACTING.value
    assert stages.seen["extract claimed during match"] is True
    assert stages.seen["state during validate"] == PackageState.MATCHING.value
    reading = stages.seen["reading during extract"]
    assert reading is not None
    assert (reading.stage, reading.pages_done, reading.pages_total) == ("pages", 2, 5)  # type: ignore[attr-defined]
    assert reading.label == "reading each page's text and lines"  # type: ignore[attr-defined]
    assert _state(engine, revision_id) == PackageState.NEEDS_INPUT.value
    with Session(engine) as watcher:
        # The tail ("filling in the measurements form") is the last line the worker wrote.
        assert watcher.scalar(
            select(ReadingProgress.stage).where(ReadingProgress.package_revision_id == revision_id)
        ) == ("form")
        # Once handed to the reviewer, the package no longer reports a reading.
        assert reading_progress_for(watcher, revision_id, PackageState.NEEDS_INPUT) is None


def test_a_failure_after_stage_one_is_recorded_and_the_retry_resumes_without_redoing_it(
    engine: Engine, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scripts.drain_outbox as worker

    revision_id = _uploaded_revision(session)
    stages = _WatchedStages(engine, fail_extract=[TimeoutError("a reader timed out")])
    monkeypatch.setattr(worker, "_stages", lambda **_: stages)
    outbox_row = str(uuid4())

    with pytest.raises(TimeoutError):
        worker._extract_package(session, revision_id, outbox_row)
    session.close()

    # The failure is recorded, and it names the stage that failed — not the one before it.
    assert _state(engine, revision_id) == PackageState.FAILED_RETRYABLE.value
    assert _claimed(engine, revision_id, "ingest") is True
    assert _claimed(engine, revision_id, "extract_pages") is False
    with Session(engine) as watcher:
        entered = watcher.scalar(
            select(PackageStateEvent.from_state)
            .where(
                PackageStateEvent.package_revision_id == revision_id,
                PackageStateEvent.to_state == PackageState.FAILED_RETRYABLE.value,
            )
            .order_by(PackageStateEvent.sequence.desc())
            .limit(1)
        )
    assert entered == PackageState.EXTRACTING.value

    # The outbox redelivers the same row: ingest is not run again, extraction resumes and ends.
    from app.db.session import session_factory

    with session_factory(engine)() as retry:
        worker._extract_package(retry, revision_id, outbox_row)
        retry.commit()
    assert stages.calls == {"ingest": 1, "extract_pages": 2, "match": 1, "validate_evidence": 1}
    assert _state(engine, revision_id) == PackageState.NEEDS_INPUT.value


class _Killed(BaseException):
    """What a killed worker looks like to this process: nothing below `Exception` runs."""


def test_a_killed_worker_leaves_the_last_finished_stage_and_the_redelivery_resumes_there(
    engine: Engine, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scripts.drain_outbox as worker

    revision_id = _uploaded_revision(session)
    stages = _WatchedStages(engine, fail_extract=[_Killed()])
    monkeypatch.setattr(worker, "_stages", lambda **_: stages)
    outbox_row = str(uuid4())

    with pytest.raises(_Killed):
        worker._extract_package(session, revision_id, outbox_row)
    session.rollback()
    session.close()
    assert _state(engine, revision_id) == PackageState.INGESTING.value

    from app.db.session import session_factory

    with session_factory(engine)() as retry:
        worker._extract_package(retry, revision_id, outbox_row)
        retry.commit()
    assert stages.calls["ingest"] == 1 and stages.calls["extract_pages"] == 2
    assert _state(engine, revision_id) == PackageState.NEEDS_INPUT.value


# ---------------------------------------------------------------------------------------------
# The call ledger: a paid call is recorded the moment it returns
# ---------------------------------------------------------------------------------------------


class _TimesOutAfter:
    """Fake readers that answer the first `answered` calls and time out on every later one."""

    def __init__(self, readers: Any, answered: int) -> None:
        self.readers = readers
        self.answered = answered
        self.made = 0
        self.lock = threading.Lock()

    def for_current_thread(self) -> _TimesOutAfter:
        return self

    def converse(self, **kwargs: Any) -> Mapping[str, Any]:
        with self.lock:
            self.made += 1
            if self.made > self.answered:
                raise TimeoutError("the reader did not answer in time")
        answer: Mapping[str, Any] = self.readers.converse(**kwargs)
        return answer


def _stages_for(configured: Any) -> Any:
    from workflow.stages import DatabaseStages, _SpendMeter

    stages = object.__new__(DatabaseStages)
    stages._form_reader = configured.form
    stages._meter = _SpendMeter(cap_micros=10_000_000)
    return stages


def _ledger(engine: Engine, run: Any, session: Session, stages: Any, stage_key: str) -> CallLedger:
    task = session.get(TaskRun, run.task_run_id)
    assert task is not None
    return CallLedger(
        engine,
        workflow_run_id=task.workflow_run_id,
        stage_key=stage_key,
        reader_version="slot-reader-test",
        to_record=lambda run_id, attempt: stages._reader_invocation(run_id, attempt),
    )


def _ledger_rows(engine: Engine, stage_key: str) -> list[ModelInvocation]:
    with Session(engine) as watcher:
        runs = ledger_runs_of(watcher, stage_key)
        return list(
            watcher.scalars(
                select(ModelInvocation).where(ModelInvocation.extraction_run_id.in_(runs))
            )
        )


def test_a_reader_timeout_keeps_the_ledger_rows_and_the_retry_pays_for_no_answer_twice(
    engine: Engine, session: Session
) -> None:
    revision, version, page_row, run = _scaffold(session)
    session.commit()
    stage_key = f"test-stage:{uuid4().hex}"
    page = _page(version.id, page_row.id)

    # First attempt: four answers come back, then a reader times out and the stage fails.
    readers = _readers(page)
    timing_out = _TimesOutAfter(readers, answered=4)
    configured = replace(
        _runtime(readers), form=replace(_runtime(readers).form, clients=timing_out)
    )
    stages = _stages_for(configured)
    ledger = _ledger(engine, run, session, stages, stage_key)
    with pytest.raises(TimeoutError):
        read_slot_pages(
            [page],
            runtime=configured,
            record_attempt=ledger.record,
            store=MemoryStore(),  # type: ignore[arg-type]
        )
    session.rollback()  # the stage's own transaction is gone; the ledger's rows are not

    kept = _ledger_rows(engine, stage_key)
    paid = [row for row in kept if row.outcome == "ok"]
    assert len(paid) == 4
    assert all((row.cost_micros or 0) > 0 and row.private_raw_response for row in paid)
    with Session(engine) as watcher:
        # The Usage page and the reading's own count see them: committed, attributed, counted once.
        assert count_reading_calls(watcher, revision.id) >= 4
        ledger_run = watcher.get(ExtractionRun, ledger.run_id)
        assert ledger_run is not None and ledger_run.extractor == LEDGER_EXTRACTOR

    # The retry: the same questions, readers that would answer anew. The four answers already paid
    # for are reused from the ledger; only the rest are asked.
    fresh_readers = _readers(page)
    retry_runtime = _runtime(fresh_readers)
    retry_stages = _stages_for(retry_runtime)
    retry_ledger = _ledger(engine, run, session, retry_stages, stage_key)
    read_slot_pages(
        [page],
        runtime=retry_runtime,
        record_attempt=retry_ledger.record,
        store=MemoryStore(),  # type: ignore[arg-type]
        stored_answers=StoredReaderAnswers(session),
    )
    attempts = retry_ledger.snapshot()
    reused = [attempt for attempt in attempts if attempt.reused_from is not None]
    asked = [attempt for attempt in attempts if attempt.reused_from is None]
    assert len(reused) == 4
    assert {UUID(attempt.reused_from or "") for attempt in reused} == {row.id for row in paid}
    assert fresh_readers.requests or fresh_readers.row_requests or fresh_readers.wall_requests
    # Every question was answered exactly once by a paid call across the two attempts.
    assert len(asked) + len(paid) == len(attempts)
    assert len(retry_ledger.ledgered()) == len(asked)

    # The stage's own run then gets twins, not second paid rows: the calls are counted once.
    retry_stages._record_reader_attempts(
        session, run.id, attempts, ledgered=retry_ledger.ledgered()
    )
    session.commit()
    twins = list(
        session.scalars(select(ModelInvocation).where(ModelInvocation.extraction_run_id == run.id))
    )
    assert len(twins) == len(attempts)
    assert all(row.reader_question_packet[REUSED_FROM_KEY] for row in twins)  # type: ignore[index]
    assert all((row.input_tokens, row.output_tokens, row.cost_micros) == (0, 0, 0) for row in twins)
    paid_total = len(_ledger_rows(engine, stage_key))
    assert count_reading_calls(session, revision.id) == paid_total
    # And the drawing-set spend meter was charged for this attempt's real calls.
    assert retry_stages._meter.spent_micros > 0


@dataclass(frozen=True)
class _SlotRuntime:
    """Just enough of a slot-reader runtime for `_read_slots` to reach the reader."""

    product: object = None
    spend_cap_usd: object = None
    reuse_answers: bool = False


def test_the_stage_ledgers_calls_made_before_a_reader_timeout(
    engine: Engine, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through the stage's own `_read_slots`: its recorder is the ledger, not a buffer."""
    import workflow.stages as stage_module
    from workflow.stages import _SpendMeter

    revision, _version, _page_row, run = _scaffold(session)
    session.commit()
    task = session.get(TaskRun, run.task_run_id)
    assert task is not None
    configured = _runtime(_readers(_page(uuid4(), uuid4())))
    stages = _stages_for(configured)
    stages._slot_reader = _SlotRuntime()
    stages._architect_reader = None
    stages._architect_pages = {}
    stages._meter = _SpendMeter(cap_micros=10_000_000)
    stages._store = MemoryStore()
    stages._dpi = 150

    def times_out(pages: object, *, record_attempt: Any, **_: object) -> None:
        for number in (1, 2):
            record_attempt(
                AttemptUsage(
                    model_id=configured.form.reader_ids[0],
                    prompt_id="slot-test",
                    template_id="slot-test",
                    input_tokens=1000,
                    output_tokens=100,
                    latency_ms=5,
                    malformed=False,
                    raw_response_text='{"text": "2\\""}',
                    attempt_number=number,
                )
            )
        raise TimeoutError("the third question timed out")

    monkeypatch.setattr(stage_module, "read_slot_pages", times_out)
    with pytest.raises(TimeoutError):
        stages._read_slots(session, package_revision_id=revision.id, pages=[], task_run_id=task.id)
    session.rollback()

    rows = _ledger_rows(engine, task.idempotency_key)
    assert len(rows) == 2 and all(row.outcome == "ok" and row.cost_micros for row in rows)
    with Session(engine) as watcher:
        assert count_reading_calls(watcher, revision.id) == 2


def test_progress_is_never_allowed_to_stop_a_reading(engine: Engine) -> None:
    """A progress line that cannot be written is skipped, not raised."""
    assert record_progress(None, uuid4(), "pages") is False
    with pytest.raises(ValueError):
        record_progress(engine, uuid4(), "pages", pages_done=3, pages_total=2)
    with Session(engine) as watcher:
        assert watcher.scalar(select(func.count(ReadingProgress.id))) == 0


# ---------------------------------------------------------------------------------------------
# Failures in every stage, a failure on the retry, and the permanent stop
# ---------------------------------------------------------------------------------------------


def _failed_from(engine: Engine, revision_id: UUID, to_state: PackageState) -> str | None:
    with Session(engine) as watcher:
        return watcher.scalar(
            select(PackageStateEvent.from_state)
            .where(
                PackageStateEvent.package_revision_id == revision_id,
                PackageStateEvent.to_state == to_state.value,
            )
            .order_by(PackageStateEvent.sequence.desc())
            .limit(1)
        )


def _deliver(engine: Engine, worker: Any, revision_id: UUID, outbox_row: str) -> None:
    from app.db.session import session_factory

    with session_factory(engine)() as delivery:
        worker._extract_package(delivery, revision_id, outbox_row)
        delivery.commit()


def test_fail_retry_fail_permanently_stops_and_a_later_delivery_does_no_work(
    engine: Engine, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second failure on the retry is recorded; a permanent one ends the retries."""
    import scripts.drain_outbox as worker
    from app.lifecycle.states import IllegalTransition

    revision_id = _uploaded_revision(session)
    stages = _WatchedStages(
        engine, fail_extract=[TimeoutError("first"), ValueError("a broken drawing")]
    )
    monkeypatch.setattr(worker, "_stages", lambda **_: stages)
    outbox_row = str(uuid4())

    with pytest.raises(TimeoutError):
        _deliver(engine, worker, revision_id, outbox_row)
    assert _state(engine, revision_id) == PackageState.FAILED_RETRYABLE.value

    with pytest.raises(ValueError):
        _deliver(engine, worker, revision_id, outbox_row)
    # Recorded, from the stage that failed — not left as a note on FAILED_RETRYABLE.
    assert _state(engine, revision_id) == PackageState.FAILED_PERMANENT.value
    assert _failed_from(engine, revision_id, PackageState.FAILED_PERMANENT) == "EXTRACTING"

    # A later delivery is refused at its first move, before the stage does anything (or pays).
    with pytest.raises(IllegalTransition):
        _deliver(engine, worker, revision_id, outbox_row)
    assert stages.calls["extract_pages"] == 2
    assert _state(engine, revision_id) == PackageState.FAILED_PERMANENT.value


def test_fail_retry_fail_again_retryably_is_recorded_and_the_third_delivery_finishes(
    engine: Engine, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scripts.drain_outbox as worker

    revision_id = _uploaded_revision(session)
    stages = _WatchedStages(engine, fail_extract=[TimeoutError("one"), TimeoutError("two")])
    monkeypatch.setattr(worker, "_stages", lambda **_: stages)
    outbox_row = str(uuid4())
    for _ in range(2):
        with pytest.raises(TimeoutError):
            _deliver(engine, worker, revision_id, outbox_row)
    with Session(engine) as watcher:
        failures = watcher.scalar(
            select(func.count(PackageStateEvent.id)).where(
                PackageStateEvent.package_revision_id == revision_id,
                PackageStateEvent.to_state == PackageState.FAILED_RETRYABLE.value,
            )
        )
    assert failures == 2
    _deliver(engine, worker, revision_id, outbox_row)
    assert _state(engine, revision_id) == PackageState.NEEDS_INPUT.value
    assert stages.calls["ingest"] == 1


@pytest.mark.parametrize(
    ("stage", "state", "before"),
    [
        ("match", PackageState.MATCHING, ("ingest", "extract_pages")),
        (
            "validate_evidence",
            PackageState.VALIDATING_EVIDENCE,
            ("ingest", "extract_pages", "match"),
        ),
    ],
)
def test_a_failure_in_a_later_stage_is_recorded_there_and_the_retry_starts_there(
    engine: Engine,
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    state: PackageState,
    before: tuple[str, ...],
) -> None:
    import scripts.drain_outbox as worker

    revision_id = _uploaded_revision(session)
    stages = _WatchedStages(engine, fail={stage: [TimeoutError("synthetic")]})
    monkeypatch.setattr(worker, "_stages", lambda **_: stages)
    outbox_row = str(uuid4())
    with pytest.raises(TimeoutError):
        _deliver(engine, worker, revision_id, outbox_row)
    assert _state(engine, revision_id) == PackageState.FAILED_RETRYABLE.value
    assert _failed_from(engine, revision_id, PackageState.FAILED_RETRYABLE) == state.value

    _deliver(engine, worker, revision_id, outbox_row)
    assert _state(engine, revision_id) == PackageState.NEEDS_INPUT.value
    assert all(stages.calls[name] == 1 for name in before)
    assert stages.calls[stage] == 2


def test_a_failure_in_the_hand_over_is_recorded_against_the_last_stage(
    engine: Engine, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The phrase index, value hunter, form proposal and NEEDS_INPUT share validate's transaction."""
    import scripts.drain_outbox as worker
    from workflow import sutradhar

    revision_id = _uploaded_revision(session)
    stages = _WatchedStages(engine)
    monkeypatch.setattr(worker, "_stages", lambda **_: stages)
    failing = [TimeoutError("the proposal step's database went away")]

    def proposal(session: object, package_revision_id: UUID) -> Mapping[str, object]:
        if failing:
            raise failing.pop(0)
        return {"ran": False, "reason": "synthetic"}

    # The flow's steps live in `workflow/sutradhar.py` since #1227, so the step is replaced there.
    monkeypatch.setattr(sutradhar, "propose_measurements", proposal)
    outbox_row = str(uuid4())
    with pytest.raises(TimeoutError):
        _deliver(engine, worker, revision_id, outbox_row)
    assert _state(engine, revision_id) == PackageState.FAILED_RETRYABLE.value
    assert _failed_from(engine, revision_id, PackageState.FAILED_RETRYABLE) == "VALIDATING_EVIDENCE"
    assert _claimed(engine, revision_id, "validate_evidence") is False

    _deliver(engine, worker, revision_id, outbox_row)
    assert _state(engine, revision_id) == PackageState.NEEDS_INPUT.value
    assert stages.calls["validate_evidence"] == 2 and stages.calls["match"] == 1


# ---------------------------------------------------------------------------------------------
# The spend carry-over, the ledger's fallback, page counts, locks and hooks
# ---------------------------------------------------------------------------------------------


def _committed_ledger_call(
    engine: Engine, workflow_run_id: UUID, stage_key: str, cost: int
) -> UUID:
    from extraction.models.invocations import InvocationRecord

    ledger = CallLedger(
        engine,
        workflow_run_id=workflow_run_id,
        stage_key=stage_key,
        reader_version="slot-reader-test",
        to_record=lambda run_id, attempt: InvocationRecord(
            extraction_run_id=run_id,
            model_id=attempt.model_id,
            prompt_id=attempt.prompt_id,
            template_id=attempt.template_id,
            crop_artifact_id=None,
            input_tokens=attempt.input_tokens or 0,
            output_tokens=attempt.output_tokens or 0,
            cost_micros=cost,
            latency_ms=1,
            outcome="ok",
        ),
    )
    ledger.record(_attempt())
    (row_id,) = ledger.ledgered().values()
    return row_id


def _attempt(number: int = 1) -> AttemptUsage:
    return AttemptUsage(
        model_id="anthropic.claude-opus-5-5",
        prompt_id="slot-test",
        template_id="slot-test",
        input_tokens=1000,
        output_tokens=100,
        latency_ms=5,
        malformed=False,
        raw_response_text="synthetic",
        attempt_number=number,
    )


def test_a_retried_stage_starts_its_spend_meter_from_what_failed_attempts_paid(
    engine: Engine, session: Session
) -> None:
    from workflow.stages import stage_spend_so_far

    _revision, _version, _page_row, run = _scaffold(session)
    session.commit()
    claim = session.get(TaskRun, run.task_run_id)
    assert claim is not None
    # An earlier attempt of this stage paid 300 000 and rolled back; another stage paid 7.
    _committed_ledger_call(engine, claim.workflow_run_id, claim.idempotency_key, 300_000)
    _committed_ledger_call(engine, claim.workflow_run_id, f"other-stage-{uuid4().hex}", 7)
    # This attempt's own run has one call of 5 000 so far.
    session.add(
        ModelInvocation(
            extraction_run_id=run.id,
            model_id="anthropic.claude-opus-5-5",
            prompt_id="slot-test",
            template_id="slot-test",
            input_tokens=10,
            output_tokens=1,
            cost_micros=5_000,
            latency_ms=1,
            outcome="ok",
        )
    )
    session.flush()
    assert stage_spend_so_far(session, claim) == (305_000, 0)


def test_a_call_the_ledger_could_not_take_is_written_by_the_stage_as_a_paid_row(
    engine: Engine, session: Session
) -> None:
    revision, version, page_row, run = _scaffold(session)
    session.commit()
    configured = _runtime(_readers(_page(version.id, page_row.id)))
    stages = _stages_for(configured)
    refused = [True]

    def to_record(run_id: UUID, attempt: AttemptUsage) -> Any:
        if refused:
            refused.pop()
            raise RuntimeError("the ledger write failed")
        return stages._reader_invocation(run_id, attempt)

    task = session.get(TaskRun, run.task_run_id)
    assert task is not None
    stage_key = f"test-stage:{uuid4().hex}"
    ledger = CallLedger(
        engine,
        workflow_run_id=task.workflow_run_id,
        stage_key=stage_key,
        reader_version="slot-reader-test",
        to_record=to_record,
    )
    ledger.record(_attempt(1))
    ledger.record(_attempt(2))
    assert set(ledger.ledgered()) == {1}

    stages._record_reader_attempts(
        session, run.id, ledger.snapshot(), ledgered=ledger.ledgered_in(session)
    )
    session.commit()
    own = list(
        session.scalars(select(ModelInvocation).where(ModelInvocation.extraction_run_id == run.id))
    )
    paid = [row for row in own if REUSED_FROM_KEY not in (row.reader_question_packet or {})]
    assert len(own) == 2 and len(paid) == 1 and (paid[0].cost_micros or 0) > 0
    # Two calls, each counted once: one by its ledger row, one by the stage's paid row.
    assert count_reading_calls(session, revision.id) == 2


def test_a_ledger_row_whose_commit_answer_was_lost_is_looked_up_by_id_before_any_fallback(
    engine: Engine, session: Session
) -> None:
    _revision, _version, _page_row, run = _scaffold(session)
    session.commit()
    task = session.get(TaskRun, run.task_run_id)
    assert task is not None
    landed = _committed_ledger_call(engine, task.workflow_run_id, f"k:{uuid4().hex}", 1_000)
    ledger = CallLedger(
        engine,
        workflow_run_id=task.workflow_run_id,
        stage_key=f"k:{uuid4().hex}",
        reader_version="slot-reader-test",
        to_record=lambda run_id, attempt: None,  # type: ignore[arg-type,return-value]
    )
    ledger._engine = None  # buffer only; the two outcomes are set by hand below
    ledger.record(_attempt(1))
    ledger.record(_attempt(2))
    never_landed = uuid4()
    ledger._unconfirmed = {0: landed, 1: never_landed}
    assert ledger.ledgered_in(session) == {0: landed}


def test_page_batch_progress_counts_a_page_done_when_all_its_questions_are_answered() -> None:
    from types import SimpleNamespace

    from workflow.reading_watch import PageBatchProgress

    lines: list[tuple[str, int | None, int | None]] = []

    class Reporter:
        def stage(
            self, stage: str, *, pages_done: int | None = None, pages_total: int | None = None
        ) -> None:
            lines.append((stage, pages_done, pages_total))

    jobs = [SimpleNamespace(page_index=page) for page in (0, 0, 1, 1, 2)]
    answered = PageBatchProgress(Reporter()).batch("dimensions", jobs)  # type: ignore[arg-type]
    for job in (jobs[0], jobs[2], jobs[4], jobs[1], jobs[3]):
        answered(job)  # type: ignore[arg-type]
    assert lines == [
        ("dimensions", 0, 3),
        ("dimensions", 1, 3),  # page 2 (one question) done
        ("dimensions", 2, 3),  # page 0 done
        ("dimensions", 3, 3),  # page 1 done
    ]


def test_side_writes_do_not_wait_on_the_lock_a_running_stage_holds(
    engine: Engine, session: Session
) -> None:
    """Under a real stage's lock (its state move holds FOR UPDATE on the revision)."""
    import time

    from app.models import WorkflowRun

    revision_id = _uploaded_revision(session)
    workflow = WorkflowRun(package_revision_id=revision_id, engine_run_id=str(uuid4()))
    session.add(workflow)
    session.commit()
    transition(session, revision_id, PackageState.INGESTING, actor="the ingest worker")  # holds it
    started = time.monotonic()
    assert record_progress(engine, revision_id, "ingest") is True
    assert _committed_ledger_call(engine, workflow.id, f"k:{uuid4().hex}", 1_000)
    assert time.monotonic() - started < 4  # well inside the side writes' lock wait
    with Session(engine) as watcher:
        assert (
            watcher.scalar(
                select(ReadingProgress.stage).where(
                    ReadingProgress.package_revision_id == revision_id
                )
            )
            == "ingest"
        )
    session.rollback()


class _BrokenProgress:
    def __init__(self, *, in_batch: bool) -> None:
        self.in_batch = in_batch

    def batch(self, name: str, jobs: object) -> Any:
        if self.in_batch:
            raise RuntimeError("the hook broke")

        def answered(job: object) -> None:
            raise RuntimeError("the hook broke")

        return answered


@pytest.mark.parametrize("in_batch", [True, False])
def test_a_progress_hook_that_raises_changes_no_reading(in_batch: bool) -> None:
    from tests.workflow.test_reader_reuse import _readings

    page = _page(uuid4(), uuid4())
    (plain,) = read_slot_pages(
        [page],
        runtime=_runtime(_readers(page)),
        record_attempt=lambda _a: None,
        store=MemoryStore(),  # type: ignore[arg-type]
    )
    (watched,) = read_slot_pages(
        [page],
        runtime=_runtime(_readers(page)),
        record_attempt=lambda _a: None,
        store=MemoryStore(),  # type: ignore[arg-type]
        progress=_BrokenProgress(in_batch=in_batch),
    )
    assert _readings(watched) == _readings(plain)
