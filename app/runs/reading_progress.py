"""Where a package's reading is, while it runs (#1178).

**Why.** Reading a drawing set takes minutes and makes dozens of paid model calls. The whole reading
used to run in one database transaction, so for eight minutes the package said `UPLOADED`, the screen
said "Reading…" with nothing else, and the calls already paid for were invisible (and lost, if the
reading then failed). The pipeline now commits at each stage boundary, writes every model call to the
ledger the moment it returns, and keeps one progress row per revision saying which part of the reading
is running and, where that part walks pages, how many are done.

**Two halves.** `record_progress` is the writer the worker calls; it uses its own short transaction
and never raises, because a progress line that could not be written must never stop a reading.
`reading_progress_for` is the reader the API calls; it adds how many model calls the reading has made
so far and what they cost, counted from the committed ledger exactly as the Usage page counts them.

Nothing here decides anything about a drawing. It only reports where the work is.

Source: issue #1178 · Verification: `tests/workflow/test_reading_progress.py`,
`tests/api/test_package_reading_progress.py`
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Final
from uuid import UUID, uuid4

from sqlalchemy import Engine, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.base import utc_now
from app.models import PackageState, PackageStateEvent
from app.models.runs import (
    READING_STAGES,
    ExtractionRun,
    ModelInvocation,
    ReadingProgress,
    TaskRun,
    WorkflowRun,
    made_a_call,
)
from app.usage_history import charged_cost

__all__ = [
    "READING_STAGE_LABELS",
    "STATE_STAGES",
    "ReadingSnapshot",
    "count_reading_calls",
    "reading_progress_for",
    "record_progress",
    "side_engine",
]

logger = logging.getLogger("gv.runs.reading_progress")

#: What a reviewer is told for each part of the reading. Plain words, no internal names.
READING_STAGE_LABELS: Final[dict[str, str]] = {
    "waiting": "waiting for the reader to start",
    "ingest": "checking the uploaded files",
    "pages": "reading each page's text and lines",
    "architect": "reading the architect's drawings",
    "preparing": "preparing pages for the AI readers",
    "rows": "AI readers finding each page's countertop line",
    "dimensions": "AI readers reading the dimensions",
    "chains": "AI readers reading equal-share dimension chains",
    "architect_pairing": "AI readers pairing rows with the architect's views",
    "whole_pages": "AI readers reading whole pages",
    "saving": "saving what the readers found",
    "match": "matching the drawings",
    "validate": "checking the evidence",
    "form": "filling in the measurements form",
}

if set(READING_STAGE_LABELS) != set(READING_STAGES):  # pragma: no cover - import-time guard
    raise RuntimeError("every reading stage needs exactly one label")

#: The stage a package state stands for when no progress row says more (an older package, or a
#: state written before the worker's first progress line).
STATE_STAGES: Final[dict[PackageState, str]] = {
    PackageState.UPLOADED: "waiting",
    PackageState.INGESTING: "ingest",
    PackageState.EXTRACTING: "pages",
    PackageState.MATCHING: "match",
    PackageState.VALIDATING_EVIDENCE: "validate",
}

#: States in which a revision is being read (the same set `required-inputs.still_reading` uses).
READING_STATES: Final[frozenset[PackageState]] = frozenset(STATE_STAGES)


#: How long a side write (a progress line, a ledger row) may wait for a lock before giving up. A side
#: write never needs a row a stage holds, so any wait is a surprise; a few seconds lets an ordinary
#: brief lock pass and stops a real one from stalling the reader. Then the write is skipped (progress)
#: or left to the stage (a ledger row), which is the safe direction for both.
SIDE_LOCK_WAIT: Final = "5s"


def wait_briefly_for_locks(session: Session) -> None:
    """Bound this side transaction's lock waits (`SET LOCAL`, so it ends with the transaction)."""
    session.execute(text(f"SET LOCAL lock_timeout = '{SIDE_LOCK_WAIT}'"))


def side_engine(session: Session) -> Engine | None:
    """The engine a short side transaction can use, or `None` where there is none.

    Only a session bound to an engine can open a second, independent transaction. A session bound
    to one connection (a test's outer transaction, a caller's own connection) has nowhere separate
    to write, so the side writes are skipped there and everything lands with the stage, as before.
    """
    bind = session.get_bind()
    return bind if isinstance(bind, Engine) else None


def record_progress(
    engine: Engine | None,
    package_revision_id: UUID,
    stage: str,
    *,
    pages_done: int | None = None,
    pages_total: int | None = None,
) -> bool:
    """Say, in its own committed transaction, which part of the reading is running. Never raises.

    Returns whether the line was written. A failure is logged by type only (a database error can
    quote the row it refused) and swallowed: the reading must go on whether or not it can be watched.
    """
    if engine is None:
        return False
    if stage not in READING_STAGE_LABELS:
        raise ValueError(f"unknown reading stage {stage!r}")
    if pages_done is not None and (pages_total is None or not 0 <= pages_done <= pages_total):
        raise ValueError("pages_done needs pages_total and must lie between 0 and it")
    now = utc_now()
    values = {
        "stage": stage,
        "pages_done": pages_done,
        "pages_total": pages_total,
        "updated_at": now,
    }
    try:
        with Session(engine) as side, side.begin():
            wait_briefly_for_locks(side)
            side.execute(
                insert(ReadingProgress)
                .values(
                    id=uuid4(),
                    created_at=now,
                    package_revision_id=package_revision_id,
                    **values,
                )
                .on_conflict_do_update(index_elements=["package_revision_id"], set_=values)
            )
    except Exception as error:  # noqa: BLE001 - progress is never allowed to stop a reading
        logger.warning("reading progress not recorded: %s", type(error).__name__)
        return False
    return True


@dataclass(frozen=True, slots=True)
class ReadingSnapshot:
    """What the API says about a reading in progress."""

    stage: str
    label: str
    pages_done: int | None
    pages_total: int | None
    calls: int
    cost_usd: str
    unpriced_calls: int
    updated_at: datetime | None
    retrying: bool


def _usd(micros: int) -> str:
    return format((Decimal(micros) / Decimal(1_000_000)).quantize(Decimal("0.000001")), "f")


def _last_failure_at(session: Session, package_revision_id: UUID) -> datetime | None:
    return session.scalar(
        select(func.max(PackageStateEvent.created_at)).where(
            PackageStateEvent.package_revision_id == package_revision_id,
            PackageStateEvent.to_state == PackageState.FAILED_RETRYABLE.value,
        )
    )


def reading_progress_for(
    session: Session, package_revision_id: UUID, state: PackageState
) -> ReadingSnapshot | None:
    """The reading's progress, or `None` when the revision is not being read.

    The stage comes from the progress row when there is one, else from the package state.

    **A retry is a reading too (#1178 review).** After a failure the package says
    `FAILED_RETRYABLE` until the retried stage commits — minutes, for the reading stage — because
    the retry's move back into the stage is inside the stage's own transaction. A progress line
    written *after* the failure is the retry's, so it is shown, marked `retrying`. A progress line
    older than the failure is the failed attempt's and is not shown as if anything were running.

    Calls and cost count every model call recorded for the revision, the way the Usage page counts
    them (a reading call through its run, a review-time call by its own revision); reused stored
    answers and ledger twins are not calls.
    """
    retrying = False
    if state not in READING_STATES and state is not PackageState.FAILED_RETRYABLE:
        return None
    row = session.execute(
        select(ReadingProgress).where(ReadingProgress.package_revision_id == package_revision_id)
    ).scalar_one_or_none()
    if state is PackageState.FAILED_RETRYABLE:
        failed_at = _last_failure_at(session, package_revision_id)
        if row is None or failed_at is None or row.updated_at <= failed_at:
            return None
        retrying = True
    if row is not None:
        stage, done, total, updated = row.stage, row.pages_done, row.pages_total, row.updated_at
    else:
        stage, done, total, updated = STATE_STAGES[state], None, None, None
    calls = session.execute(
        select(
            ModelInvocation.model_id,
            ModelInvocation.cost_micros,
            ModelInvocation.outcome,
            ModelInvocation.input_tokens,
            ModelInvocation.output_tokens,
        )
        .outerjoin(ExtractionRun, ExtractionRun.id == ModelInvocation.extraction_run_id)
        .outerjoin(TaskRun, TaskRun.id == ExtractionRun.task_run_id)
        .outerjoin(WorkflowRun, WorkflowRun.id == TaskRun.workflow_run_id)
        .where(
            or_(
                ModelInvocation.package_revision_id == package_revision_id,
                WorkflowRun.package_revision_id == package_revision_id,
            )
        )
        .where(made_a_call())
    ).all()
    micros = 0
    unpriced = 0
    for call in calls:
        charged = charged_cost(
            call.model_id, call.cost_micros, call.outcome, call.input_tokens, call.output_tokens
        )
        if charged.micros is None:
            unpriced += 1
        else:
            micros += charged.micros
    return ReadingSnapshot(
        stage=stage,
        label=READING_STAGE_LABELS[stage],
        pages_done=done,
        pages_total=total,
        calls=len(calls),
        cost_usd=_usd(micros),
        unpriced_calls=unpriced,
        updated_at=updated,
        retrying=retrying,
    )


def count_reading_calls(session: Session, package_revision_id: UUID) -> int:
    """How many model calls the revision's reading has recorded (for tests and logs)."""
    return int(
        session.execute(
            select(func.count(ModelInvocation.id))
            .join(ExtractionRun, ExtractionRun.id == ModelInvocation.extraction_run_id)
            .join(TaskRun, TaskRun.id == ExtractionRun.task_run_id)
            .join(WorkflowRun, WorkflowRun.id == TaskRun.workflow_run_id)
            .where(WorkflowRun.package_revision_id == package_revision_id)
            .where(made_a_call())
        ).scalar_one()
    )
