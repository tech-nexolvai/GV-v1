"""Surviving a restart: resume where it stopped, redo nothing that was paid for (#217, C4.5).

`docs/DESIGN_PLATFORM.md` §6.2 states the requirement and the reason together: *"LangGraph interrupts
restart the node, so every side effect before an interrupt must be idempotent — paid model calls are the
expensive case, half-written evidence the dangerous one."*

**Most of that safety is already built, and this module deliberately does not rebuild it.** The claim in
`workflow/idempotency.py` makes a completed stage a no-op on re-delivery, and #247's
`app/runs/agent_checkpoints.py` makes an agent node's spend idempotent by reserving before it spends. What
was missing is smaller and specific:

- **Nothing performed a resume.** `RESUMABLE_STATES` existed and `_resumes_where_it_failed` guarded the
  edge, but no code moved a package out of `FAILED_RETRYABLE`. A package that failed stayed failed.
- **Nothing counted recovery interventions.** `docs/DESIGN_CONTROLS.md` §6 sources that quantity from this
  story and uses it to decide whether Temporal is worth adopting. An unmeasured trigger, in that
  document's words, "makes the deferral permanent by accident".

**What a resume is allowed to assume: nothing.** It reads the package's state and the claims already
recorded, and re-runs whatever has no completed claim. It does not take a caller's word for where the work
got to — `app/lifecycle/states.py` puts it plainly: read "from the event log rather than from the caller's
good intentions".

Source: backend proposal §9.2–§9.4; `AGENTS.md` §6 · Design: `docs/DESIGN_PLATFORM.md` §6.2 ·
Verification: `tests/workflow/test_durability.py`
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.session import unit_of_work
from app.lifecycle.states import PROCESSING_STATES, RESUMABLE_STATES, transition
from app.models import PackageRevision, PackageState, PackageStateEvent, TaskRun
from workflow.idempotency import stage_idempotency_key
from workflow.review import (
    ENGINE_VERSION,
    STAGES,
    NoStages,
    StageOutcome,
    Stages,
    run_stage,
)

__all__ = [
    "AUTOMATIC_RESUME_ACTOR",
    "NotResumable",
    "WorkflowResult",
    "recovery_interventions",
    "resume_point",
    "run_to_completion",
]

#: The one actor an automatic resume may use.
#:
#: **This constant is how a recovery intervention is counted, so it is load-bearing.** Anant's call on #217
#: is that only a *human* resume counts: an automatic retry is not toil, and Temporal's value is removing
#: toil. Rather than guess which actor strings look like people, a resume this module performs stamps
#: itself with this exact string and `recovery_interventions` counts every resume that did not.
#:
#: The error that leaves is deliberate. A resume by an unrecognised actor counts as an intervention, so a
#: new automatic path added without reading this would *inflate* the number rather than hide it — and
#: `docs/DESIGN_CONTROLS.md` §6 asks for exactly that direction: "not measured is displayed as prominently
#: as a breach", because an unmeasured trigger makes the deferral permanent by accident.
AUTOMATIC_RESUME_ACTOR: Final = "the workflow supervisor"


class NotResumable(Exception):
    """The package cannot be resumed, and the message says what state it is in instead.

    Raised rather than returned, and never swallowed: a caller that asked for a package to be finished and
    got a quiet no-op would report success for a package nobody processed.
    """


@dataclass(frozen=True, slots=True)
class WorkflowResult:
    """What a run did, in enough detail to compare two runs of the same package."""

    package_revision_id: UUID
    final_state: PackageState
    outcomes: tuple[StageOutcome, ...]

    @property
    def stages_run(self) -> tuple[str, ...]:
        """The stages this run actually did work for."""
        return tuple(outcome.stage for outcome in self.outcomes if not outcome.already_done)

    @property
    def stages_skipped(self) -> tuple[str, ...]:
        """The stages this run found already done and did not repeat.

        The whole point of the exercise. After a kill, this is where the work that was already paid for
        shows up as not having been paid for twice.
        """
        return tuple(outcome.stage for outcome in self.outcomes if outcome.already_done)


def resume_point(session: Session, package_revision_id: UUID) -> PackageState | None:
    """The state a resume must re-enter, or None if the package is not waiting to be resumed.

    Read from the event log rather than inferred: `_resumes_where_it_failed` will refuse a resume that
    lands anywhere except the state that failed, so the answer has to be the state the package was in
    when it stopped. That is the most recent processing state in its history.
    """
    revision = session.get(PackageRevision, package_revision_id)
    if revision is None:
        raise NotResumable(f"there is no package revision {package_revision_id}")
    if PackageState(revision.state) not in RESUMABLE_STATES:
        return None

    processing = [state.value for state in PROCESSING_STATES]
    last = session.execute(
        select(PackageStateEvent.to_state)
        .where(
            PackageStateEvent.package_revision_id == package_revision_id,
            PackageStateEvent.to_state.in_(processing),
        )
        .order_by(PackageStateEvent.sequence.desc())
        .limit(1)
    ).scalar_one_or_none()
    if last is None:
        raise NotResumable(
            f"package revision {package_revision_id} is in {revision.state} but its history contains no "
            "processing state, so there is nowhere to resume to"
        )
    return PackageState(last)


def run_to_completion(
    factory: sessionmaker[Session],
    *,
    package_revision_id: UUID,
    workflow_run_id: UUID,
    stages: Stages | None = None,
    actor: str = AUTOMATIC_RESUME_ACTOR,
    engine_version: str = ENGINE_VERSION,
) -> WorkflowResult:
    """Take a package from wherever it stopped to the end of the pipeline.

    Safe to call on a package that has already finished, one that never started, and one killed halfway —
    which is the point. Each stage claims its task first, so a stage that completed before the interrupt
    returns `already_done` and its work is not repeated. That is what makes a restart cheap: the expensive
    thing is a second charge for the same page, not a second function call.

    One transaction per stage, as in `run_all`. A stage that fails stops the walk and the exception
    propagates, because a caller that wanted a finished package needs to hear that it did not get one.

    `actor` defaults to the constant that marks this as automatic. A person driving a recovery by hand
    should pass their own name — that is what makes it countable as an intervention.
    """
    resolved = stages if stages is not None else NoStages()

    with unit_of_work(factory) as session:
        target = resume_point(session, package_revision_id)
        revision = session.get(PackageRevision, package_revision_id)
        if revision is None:  # pragma: no cover - resume_point already refused this
            raise NotResumable(f"there is no package revision {package_revision_id}")
        current = PackageState(revision.state)

    # Whether the stage that failed actually completed decides who moves the package, and both cases
    # happen. See `_resume_plan`.
    start, move_first = _resume_plan(factory, package_revision_id, current, target, engine_version)

    if move_first and target is not None:
        with unit_of_work(factory) as session:
            transition(
                session,
                package_revision_id,
                target,
                actor=actor,
                reason="resuming after a failure",
                workflow_run_id=workflow_run_id,
            )

    outcomes: list[StageOutcome] = []
    for index, (stage, state) in enumerate(STAGES):
        if index < start:
            continue
        with unit_of_work(factory) as session:
            outcomes.append(
                run_stage(
                    session,
                    stage=stage,
                    state=state,
                    package_revision_id=package_revision_id,
                    workflow_run_id=workflow_run_id,
                    stages=resolved,
                    # Only the move out of a failure state is a recovery; the ordinary forward steps
                    # that follow belong to their workers. Attributing all of them to the resumer would
                    # count one intervention six times.
                    actor=(
                        actor
                        if (target is not None and not move_first and index == start)
                        else None
                    ),
                )
            )

    with unit_of_work(factory) as session:
        revision = session.get(PackageRevision, package_revision_id)
        assert revision is not None
        final = PackageState(revision.state)

    return WorkflowResult(
        package_revision_id=package_revision_id,
        final_state=final,
        outcomes=tuple(outcomes),
    )


def _resume_plan(
    factory: sessionmaker[Session],
    package_revision_id: UUID,
    current: PackageState,
    resume_target: PackageState | None,
    engine_version: str,
) -> tuple[int, bool]:
    """Where the walk starts, and whether this function must move the package itself.

    **Both cases are real, and the tests found the second one.** A resume may only land in the state the
    package failed in, and what that state is depends on who recorded the failure:

    - **A supervisor recording a killed worker.** The kill rolled back the failing stage entirely, so the
      package's last committed state is the *previous* stage's, and that is what the failure event records
      as its `from_state`. The resume target is therefore a stage that already completed — its claim is
      still there, so `run_stage` would return `already_done` and move nothing, leaving the package stuck
      in the failure state while the next stage's transition is refused. So this function makes the move
      and the walk starts *after* the target.
    - **The first stage recording its own failure.** `_record_failure` re-states the stage's state only
      when the package is in an *assembly* state, which is true for `ingest` and false for every stage
      after it. So the target is the failed stage itself only in that case, and its claim rolled back with
      the work — there `run_stage` makes the move, keeping the resume and the work in one transaction. I
      had this wrong in the first draft and a test corrected it.

    Told apart by asking whether the target stage's claim exists, which is the same question "did it
    finish?" — not by guessing from the shape of the history.
    """
    order = [state for _, state in STAGES]
    if resume_target is None:
        return _start_index(current, None), False

    index = order.index(resume_target)
    stage_name = STAGES[index][0]
    key = stage_idempotency_key(
        package_revision_id=package_revision_id,
        stage=stage_name,
        engine_version=engine_version,
    )
    with unit_of_work(factory) as session:
        claimed = session.execute(
            select(TaskRun.id).where(TaskRun.idempotency_key == key)
        ).scalar_one_or_none()
    if claimed is None:
        # The stage never finished: it moves the package itself, out of the failure state.
        return index, False
    # It did finish, so nothing in the walk will move the package. This function must.
    return index + 1, True


def _start_index(current: PackageState, resume_target: PackageState | None) -> int:
    """Which stage the walk begins at.

    Three cases, and the order matters:

    - **Resuming a failure.** Start at the stage that failed, so `run_stage` moves the package out of the
      failure state and into exactly that stage. Anywhere else is refused by the state machine.
    - **Picking up a package killed mid-flight.** It sits in the last state that actually committed, so the
      next stage is the one after it. The killed stage's own claim rolled back with its work, so it is
      redone.
    - **Anything else** — never started, or already finished — begins at the top and every completed stage
      short-circuits on its claim.

    Positions come from `STAGES` rather than a second list, so a stage added there cannot be skipped here.

    **This is an optimisation, not the guarantee, and it is worth being clear about which.** Deleting the
    skip entirely leaves every test passing: the walk then visits stages that already completed, and each
    one short-circuits on its claim instead. The claim is what protects paid work — that is #354's design
    and `tests/workflow/test_review_workflow.py` pins it directly. What this saves is a pointless claim
    lookup per finished stage, which matters for a package resumed near the end and nowhere else.

    Verified by removing it and watching nothing fail, which is the only way to tell an optimisation from a
    control.
    """
    order = [state for _, state in STAGES]
    if resume_target is not None:
        return order.index(resume_target)
    if current in order:
        return order.index(current) + 1
    return 0


def recovery_interventions(session: Session, window: timedelta) -> int:
    """How many times a person had to rescue a package in the last `window`.

    **This is the measurement behind the Temporal upgrade trigger** (`docs/DESIGN_CONTROLS.md` §6). It
    counts resumes out of a failure state by any actor other than `AUTOMATIC_RESUME_ACTOR` — see that
    constant for why the boundary is drawn there and which way it errs.

    Zero is a real answer, not a missing one: it means nobody had to intervene. F6.1 (#267) is what
    distinguishes "measured, and it is zero" from "not measured", and its rule is that an unavailable
    measurement reports itself rather than defaulting — this function always has an answer, because the
    event log always exists.
    """
    if window <= timedelta(0):
        raise ValueError("a measurement window must be a positive length of time")

    since = datetime.now(UTC) - window
    resumable = [state.value for state in RESUMABLE_STATES]
    processing = [state.value for state in PROCESSING_STATES]
    counted = session.execute(
        select(func.count())
        .select_from(PackageStateEvent)
        .where(
            PackageStateEvent.from_state.in_(resumable),
            PackageStateEvent.to_state.in_(processing),
            PackageStateEvent.actor != AUTOMATIC_RESUME_ACTOR,
            PackageStateEvent.created_at >= since,
        )
    ).scalar_one()
    return int(counted)
