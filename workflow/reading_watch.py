"""Watching a reading while it runs: the progress line and the call ledger (#1178).

**The problem.** A stage's work and its record land in one transaction, so that a stage either
completes or is retried from its start. That is right for the readings. It was wrong for two things
that are not readings:

* **where the reading is** — for eight minutes nothing said which part was running or how many pages
  were done, because nothing was committed until the end;
* **the paid model calls** — every call's ledger row (`model_invocations`) was written only after the
  reader returned, inside the stage's transaction. A reader that timed out on its 60th question rolled
  back the record of the 59 answers already paid for: the Usage page under-counted, and the retry
  could not reuse them (#1112), so it paid for all of them again.

**What this module does.** Both are written *beside* the stage, each in its own short transaction on
the same database, and committed at once:

* `ProgressReporter` writes the one progress row per revision (`app.runs.reading_progress`);
  `PageBatchProgress` turns "this batch asks these questions" and "this question was answered" into
  "page k of N";
* `CallLedger` is the attempt recorder the readers are handed. Each paid attempt is written to the
  ledger the moment it returns, under a ledger run of its own (a task run and an extraction run that
  hold nothing but these calls). The stage, when it finishes, still writes its own row for every
  attempt under its own run — but as a twin that names the ledger row (`reused_from`, no tokens, no
  cost), exactly the shape a reused stored answer already has. So the run's evidence is complete, the
  call is counted once (by its ledger row), and a retry finds the ledger row and reuses its answer.

**Nothing here can stop or change a reading.** A side write that fails is logged by type and skipped:
an unwritten progress line only means the screen says less, and an attempt the ledger could not take
is written by the stage the old way, as a paid row of its own, so no call is ever lost or counted
twice. Neither half reads anything a check decides on.

Source: issue #1178 · Verification: `tests/workflow/test_reading_progress.py`
"""

from __future__ import annotations

import logging
import threading
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Final, Protocol
from uuid import UUID, uuid4

from sqlalchemy import ColumnElement, Engine, select
from sqlalchemy.orm import Session

from app.models.runs import ExtractionRun, ModelInvocation, TaskRun, WorkflowRun
from app.runs.invocations import record as record_model_invocation
from app.runs.reading_progress import record_progress, wait_briefly_for_locks
from extraction.form_reader.bedrock import AttemptUsage

if TYPE_CHECKING:
    from extraction.models.invocations import InvocationRecord
    from extraction.slot_reader.bedrock import CropJob

__all__ = [
    "LEDGER_EXTRACTOR",
    "LEDGER_TASK_TYPE",
    "BatchProgress",
    "CallLedger",
    "PageBatchProgress",
    "ProgressReporter",
    "ledger_key_prefix",
]

logger = logging.getLogger("gv.workflow.reading_watch")

#: What a ledger run is called. Never `slot-reader…` or `form-reader…`: nothing that looks for the
#: newest reading run may ever find a ledger run, which holds calls and no readings.
LEDGER_EXTRACTOR: Final = "workflow.call_ledger"
LEDGER_EXTRACTOR_VERSION: Final = "call-ledger-v1"
LEDGER_TASK_TYPE: Final = "reader_call_ledger"


def ledger_key_prefix(stage_key: str) -> str:
    """The prefix every ledger task run of one stage carries: the stage's claim key, then a tag.

    It is what lets a stage run again after a failure count what the failed attempt already spent.
    """
    return f"{stage_key}:reader-calls:"


class ProgressReporter:
    """Says which part of one revision's reading is running. Never raises."""

    def __init__(
        self, engine: Engine | None, package_revision_id: UUID, reading_id: UUID | None = None
    ) -> None:
        self._engine = engine
        self._revision = package_revision_id
        self._reading_id = reading_id

    def stage(
        self, stage: str, *, pages_done: int | None = None, pages_total: int | None = None
    ) -> None:
        record_progress(
            self._engine,
            self._revision,
            stage,
            pages_done=pages_done,
            pages_total=pages_total,
            reading_id=self._reading_id,
        )


class BatchProgress(Protocol):
    """Told each batch of questions before it is asked; returns what to tell as each is answered."""

    def batch(self, name: str, jobs: Sequence[CropJob]) -> Callable[[CropJob], None]: ...


#: The slot reader's batch names, as the reading stages a reviewer is told about.
_BATCH_STAGES: Final[Mapping[str, str]] = {
    "rows": "rows",
    "dimensions": "dimensions",
    "chains": "chains",
    "architect_pairing": "architect_pairing",
}


class PageBatchProgress:
    """'Page k of N' for a batch of reader questions.

    A page is done when every question the batch asks about it has been answered (or abstained);
    N is the number of pages the batch asks about. Written only when the count moves.
    """

    def __init__(self, reporter: ProgressReporter) -> None:
        self._reporter = reporter

    def batch(self, name: str, jobs: Sequence[CropJob]) -> Callable[[CropJob], None]:
        stage = _BATCH_STAGES.get(name, "architect_pairing")
        asked = Counter(job.page_index for job in jobs)
        answered: Counter[int] = Counter()
        done = [0]
        self._reporter.stage(stage, pages_done=0, pages_total=len(asked))

        def one_answered(job: CropJob) -> None:
            answered[job.page_index] += 1
            now = sum(1 for page, count in asked.items() if answered[page] >= count)
            if now != done[0]:
                done[0] = now
                self._reporter.stage(stage, pages_done=now, pages_total=len(asked))

        return one_answered


class CallLedger:
    """The readers' attempt recorder: buffers every attempt and ledgers each paid one at once.

    A drop-in for `ThreadSafeAttemptRecorder` (`record`, `snapshot`), called from the reader threads.
    `ledgered()` says which attempts (by their position in `snapshot()`) already have a ledger row,
    and its id, so the stage writes their twins rather than second paid rows.

    `to_record(run_id, attempt)` builds the ledger row the stage would have written for the attempt,
    priced the same way. The ledger run is opened on the first paid attempt, in its own committed
    transaction, under a task run keyed `ledger_key_prefix(stage_key) + key_tag + <uuid>` on the
    stage's workflow run — and only when that workflow run is already committed, so no side write
    ever waits on the stage's own uncommitted rows. `key_tag` (#1328) names who asked, so the calls
    one "Read depths again" request paid for are found by its id
    (`workflow.depth_again.request_ledger_tag`); the key still starts with the stage's prefix, so the
    stage's spend carry-over (`ledger_spend_filter`) counts them as before.
    """

    def __init__(
        self,
        engine: Engine | None,
        *,
        workflow_run_id: UUID | None,
        stage_key: str | None,
        reader_version: str,
        to_record: Callable[[UUID, AttemptUsage], InvocationRecord],
        key_tag: str = "",
    ) -> None:
        self._engine = engine if workflow_run_id is not None and stage_key is not None else None
        self._key_tag = key_tag
        self._workflow_run_id = workflow_run_id
        self._stage_key = stage_key
        self._reader_version = reader_version
        self._to_record = to_record
        self._attempts: list[AttemptUsage] = []
        self._ledgered: dict[int, UUID] = {}
        self._unconfirmed: dict[int, UUID] = {}
        self._lock = threading.Lock()
        self._run_lock = threading.Lock()
        self._run_id: UUID | None = None
        self._unavailable = False

    def record(self, attempt: AttemptUsage) -> None:
        with self._lock:
            position = len(self._attempts)
            self._attempts.append(attempt)
        if attempt.reused_from is not None or self._engine is None:
            # A reused answer made no call; the stage records it as before.
            return
        row_id: UUID | None = None
        try:
            run_id = self._ledger_run()
            if run_id is None:
                return
            with Session(self._engine) as side, side.begin():
                wait_briefly_for_locks(side)
                row = record_model_invocation(side, self._to_record(run_id, attempt))
                row_id = row.id
        except Exception as error:  # noqa: BLE001 - the stage then writes this call itself
            logger.warning("model call not ledgered at once: %s", type(error).__name__)
            if row_id is not None:
                # The row reached the database and the commit's answer was lost: it may or may not
                # have landed. The stage looks it up by this id before writing anything (`ledgered_in`).
                with self._lock:
                    self._unconfirmed[position] = row_id
            return
        with self._lock:
            self._ledgered[position] = row_id

    def snapshot(self) -> tuple[AttemptUsage, ...]:
        with self._lock:
            return tuple(self._attempts)

    def ledgered(self) -> dict[int, UUID]:
        """The attempts whose ledger row is known to be committed, by position."""
        with self._lock:
            return dict(self._ledgered)

    def ledgered_in(self, session: Session) -> dict[int, UUID]:
        """`ledgered()`, plus every attempt whose commit answer was lost but whose row exists.

        Checked by id, so a call whose ledger commit succeeded while the driver raised gets a twin,
        not a second paid row, and a call whose commit really failed gets its paid row from the stage.
        Exactly one of the two, never both.
        """
        found = self.ledgered()
        with self._lock:
            unsure = dict(self._unconfirmed)
        if unsure:
            present = set(
                session.scalars(
                    select(ModelInvocation.id).where(ModelInvocation.id.in_(unsure.values()))
                )
            )
            found.update({position: row for position, row in unsure.items() if row in present})
        return found

    @property
    def run_id(self) -> UUID | None:
        """The ledger run, once a paid attempt has opened it."""
        return self._run_id

    def _ledger_run(self) -> UUID | None:
        with self._run_lock:
            if self._run_id is not None or self._unavailable:
                return self._run_id
            assert self._engine is not None
            assert self._workflow_run_id is not None and self._stage_key is not None
            with Session(self._engine) as side, side.begin():
                wait_briefly_for_locks(side)
                # A plain read never waits. A workflow run the stage created and has not committed
                # is invisible here, and then there is nowhere separate to write: the stage keeps
                # every call, as it always did.
                if side.get(WorkflowRun, self._workflow_run_id) is None:
                    self._unavailable = True
                    return None
                task = TaskRun(
                    workflow_run_id=self._workflow_run_id,
                    idempotency_key=(
                        f"{ledger_key_prefix(self._stage_key)}{self._key_tag}{uuid4().hex}"
                    ),
                    task_type=LEDGER_TASK_TYPE,
                    attempt=1,
                    outcome="recorded",
                )
                side.add(task)
                side.flush()
                run = ExtractionRun(
                    task_run_id=task.id,
                    extractor=LEDGER_EXTRACTOR,
                    extractor_version=LEDGER_EXTRACTOR_VERSION,
                    config_hash=f"calls-of:{self._reader_version}"[:200],
                )
                side.add(run)
                side.flush()
                self._run_id = run.id
            return self._run_id


def ledger_spend_filter(stage_key: str) -> ColumnElement[bool]:
    """Rows of the ledger task runs of one stage (for the spend meter's carry-over)."""
    return TaskRun.idempotency_key.like(f"{ledger_key_prefix(stage_key)}%")


def ledger_runs_of(session: Session, stage_key: str) -> list[UUID]:
    """The ledger runs every attempt of one stage opened, oldest first."""
    return list(
        session.scalars(
            select(ExtractionRun.id)
            .join(TaskRun, TaskRun.id == ExtractionRun.task_run_id)
            .where(TaskRun.idempotency_key.like(f"{ledger_key_prefix(stage_key)}%"))
            .order_by(ExtractionRun.created_at, ExtractionRun.id)
        )
    )
