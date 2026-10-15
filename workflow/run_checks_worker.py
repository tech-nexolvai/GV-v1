"""Hatchet's `run_checks`: at most one check run per revision at a time (#1359).

**Why it exists.** "Run checks" and the re-check a person's answer queues (#1351) both write an
outbox row named `run_checks`. The local worker (`make worker`, `scripts/drain_outbox.py`) runs those
rows one after another in one process, and holds each row locked until its run has finished, so two
check runs for one revision never overlap there. Under Hatchet the dispatcher marks a row delivered
the moment the engine *accepts* it, so a follow-up queued while a run is in progress would start at
once, beside the run it was meant to follow, and both would write findings for the same revision.

**The fix is the engine's own concurrency key.** The workflow is declared with one concurrency group
per revision (`input.package_revision_id`), a limit of one, and the strategy that queues
(`GROUP_ROUND_ROBIN`) rather than one that cancels. So at most one run per revision is active; every
other run for that revision waits its turn, and none is dropped: a follow-up must still run, because it
is the one that reads the answer the person just gave. Runs for different revisions are not held up.

**The task is thin.** It runs `workflow/sutradhar.py:run_checks`, the same function the local worker
runs, in one transaction, keyed by the outbox row that asked (the dispatcher puts the row id in the
run's metadata, `workflow/entrypoints.py:OUTBOX_ROW_METADATA_KEY`). Nothing here decides anything.

Verification: `tests/workflow/test_run_checks_worker.py`
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, sessionmaker

if TYPE_CHECKING:  # pragma: no cover - annotations only, keeps the gRPC stack out of the import
    from hatchet_sdk import ConcurrencyExpression, Hatchet
    from hatchet_sdk.runnables.workflow import Workflow

    from workflow.sutradhar import StagesFactory

__all__ = [
    "REVISION_KEY",
    "RUN_CHECKS_WORKFLOW",
    "RunChecksInput",
    "one_run_per_revision",
    "register_run_checks",
]

#: The outbox name, the same string `app/api/measurements.py:RUN_CHECKS_WORKFLOW` enqueues; a
#: mismatch would be a request no worker picks up.
RUN_CHECKS_WORKFLOW: Final = "run_checks"

#: The concurrency group: one per revision. A CEL expression over the run's input, which is the
#: outbox payload, so the revision id is there as the API wrote it.
REVISION_KEY: Final = "input.package_revision_id"


class RunChecksInput(BaseModel):
    """What a check run is asked with: `app/api/measurements.py:_run_checks_payload`, unchanged."""

    package_revision_id: UUID
    discriminators: dict[str, str] = Field(default_factory=dict)
    discriminator_sources: dict[str, str] = Field(default_factory=dict)


def one_run_per_revision() -> ConcurrencyExpression:
    """At most one active run per revision; the rest wait in a queue, oldest first.

    `GROUP_ROUND_ROBIN` is the strategy that queues: a run over the limit waits, and the engine takes
    the groups in turn. The cancelling strategies (`CANCEL_IN_PROGRESS`, `CANCEL_NEWEST`) would drop a
    run a person asked for.
    """
    from hatchet_sdk import ConcurrencyExpression, ConcurrencyLimitStrategy

    return ConcurrencyExpression(
        expression=REVISION_KEY,
        max_runs=1,
        limit_strategy=ConcurrencyLimitStrategy.GROUP_ROUND_ROBIN,
    )


def _outbox_row(ctx: Any) -> str:
    """The outbox row that asked for this run: the key `run_checks` records its work under.

    A run started some other way (by hand in the engine's UI) has no row, so it is keyed by its own
    engine run id, which is also a UUID and is unique to that run.
    """
    from workflow.entrypoints import OUTBOX_ROW_METADATA_KEY

    metadata = getattr(ctx, "additional_metadata", None) or {}
    row = metadata.get(OUTBOX_ROW_METADATA_KEY) if isinstance(metadata, Mapping) else None
    return str(row) if row else str(ctx.workflow_run_id)


def register_run_checks(
    hatchet: Hatchet,
    factory: sessionmaker[Session],
    stages_factory: StagesFactory | None = None,
) -> Workflow[RunChecksInput]:
    """Declare `run_checks` on a Hatchet client, one run per revision at a time.

    `stages_factory` builds the stages for each run, with that run's discriminators; left out, it is
    `workflow/sutradhar.py:build_stages`, as for the local worker.
    """
    from app.db.session import unit_of_work
    from workflow.sutradhar import run_checks

    workflow: Workflow[RunChecksInput] = hatchet.workflow(
        name=RUN_CHECKS_WORKFLOW,
        input_validator=RunChecksInput,
        concurrency=one_run_per_revision(),
    )

    @workflow.task(name=RUN_CHECKS_WORKFLOW)
    def run(payload: RunChecksInput, ctx: Any) -> dict[str, object]:
        with unit_of_work(factory) as session:
            return dict(
                run_checks(
                    session,
                    payload.package_revision_id,
                    _outbox_row(ctx),
                    payload.discriminators,
                    payload.discriminator_sources,
                    stages_factory=stages_factory,
                )
            )

    return workflow
