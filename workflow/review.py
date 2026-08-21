"""The package review workflow: six durable stages, and no business truth held here (#215, C4.3).

`docs/DESIGN_PLATFORM.md` §6: *"Hatchet owns execution; PostgreSQL owns business truth."* This module is
the graph that walks a package revision from ingestion to a reviewable result, and the reason it is
written the way it is comes from that one sentence.

**Every business fact goes to PostgreSQL through `app/lifecycle/`.** A stage claims its task in
`task_runs`, does its work, and moves the package with `transition()`. Hatchet holds the *execution* —
which step is running, what to retry — and nothing a reviewer or a dispute would ever ask about. The
engine could be replaced and the record would be intact, which is what backend §2 asks for.

**This is a spine, not the work.** Extraction, matching, evidence validation, checks and output
generation belong to other tracks and are mostly unbuilt. So the stages call into an injected `Stages`
implementation rather than doing the work here, and the default implementation does nothing and *says*
it did nothing. A stage that pretended to extract would be worse than one that reports it has nothing to
run: the first produces a package that looks reviewed.

**The Hatchet decorators are the thin part.** All the logic is in `run_stage`, an ordinary function
taking a session — so the tests drive the whole workflow with no engine at all, which is what the story's
test plan asks for. `register` wraps those functions in tasks. Nothing here builds a Hatchet client;
`Hatchet()` refuses to construct without a token, so a module that made one at import would be a module
nothing could import.

**Resuming is the point of the claims.** `claim()` short-circuits a stage whose task identity is already
recorded, so a worker killed mid-flight and restarted does not redo completed work. That matters for
paid model calls specifically — wasted seconds are cheap, a second charge for the same page is not.

Source: backend proposal §9.1–§9.4 · Design: `docs/DESIGN_PLATFORM.md` §6 ·
Verification: `tests/workflow/test_review_workflow.py`
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Protocol
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy.orm import Session, sessionmaker

from app.db.session import unit_of_work
from app.lifecycle.side_states import FailureClass, classify, enter_failure
from app.lifecycle.states import transition
from app.models import PackageState
from workflow.idempotency import CLAIMED, claim, stage_idempotency_key

if (
    TYPE_CHECKING
):  # pragma: no cover - annotations only; the gRPC stack stays out of the runtime import
    from hatchet_sdk import Hatchet
    from hatchet_sdk.runnables.task import Task
    from hatchet_sdk.runnables.workflow import Workflow

__all__ = [
    "ENGINE_VERSION",
    "STAGES",
    "WORKFLOW_NAME",
    "NoStages",
    "PackageReviewInput",
    "PageResult",
    "StageOutcome",
    "Stages",
    "join_pages",
    "register",
    "run_all",
    "run_stage",
    "stage_order",
]

#: The workflow's name, as the engine and the outbox both know it. `app/lifecycle/supersede.py` enqueues
#: this exact string, so the two must agree — a mismatch is a package nothing ever picks up.
WORKFLOW_NAME: Final = "process_package_revision"

#: The version of *this* code, inside every stage's idempotency key. A changed engine is a different
#: task rather than a cache hit (`AGENTS.md` §2.7), so bumping this reruns stages instead of reusing
#: answers computed by code that no longer exists.
ENGINE_VERSION: Final = "1.0.0"

#: Each stage, and the state a package reaches when it finishes. Data, in one place, so the graph and
#: the state machine cannot disagree — the same reason `app/lifecycle/states.py` holds its table as data.
#:
#: The order here *is* the pipeline, and it mirrors `MAIN_LINE` in the state machine. A stage that
#: transitioned somewhere the table forbids would be refused by `transition()`, which is the backstop
#: rather than the design: the intent is that these two lists describe the same walk.
STAGES: Final[tuple[tuple[str, PackageState], ...]] = (
    ("ingest", PackageState.INGESTING),
    ("extract_pages", PackageState.EXTRACTING),
    ("match", PackageState.MATCHING),
    ("validate_evidence", PackageState.VALIDATING_EVIDENCE),
    ("run_checks", PackageState.RUNNING_CHECKS),
    ("generate_outputs", PackageState.GENERATING_OUTPUTS),
)


def stage_order() -> tuple[str, ...]:
    """The stage names in pipeline order."""
    return tuple(name for name, _ in STAGES)


class PackageReviewInput(BaseModel):
    """What starting this workflow needs to know.

    Deliberately only identifiers. The payload is what `workflow/outbox.py` wrote inside the caller's
    transaction, and anything else put here would be business state living in the engine — which is the
    thing §6 says must not happen.
    """

    package_revision_id: UUID
    workflow_run_id: UUID


@dataclass(frozen=True, slots=True)
class PageResult:
    """One page's result from the extraction fan-out."""

    index: int
    """The page's position in the document, which is what makes the join deterministic."""

    payload: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class StageOutcome:
    """What one stage did, and whether it had to do anything at all."""

    stage: str
    state: PackageState
    idempotency_key: str
    already_done: bool
    """True when the task identity was already claimed, so this run short-circuited.

    The whole reason the claim comes first. A worker killed mid-flight and restarted re-delivers the
    stage; without this it would repeat work that was already paid for.
    """

    payload: Mapping[str, object]


class Stages(Protocol):
    """The actual work, injected.

    A protocol rather than a base class, and injected rather than imported, for the same reason
    `workflow/outbox.py` takes a `WorkflowStarter`: this module should hold no opinion about how a page
    is read or a check is run, and the tests should be able to supply something that neither renders nor
    calls a model.

    Every method returns whatever it wants recorded about the stage. Nothing here interprets it.
    """

    def ingest(self, session: Session, package_revision_id: UUID) -> Mapping[str, object]: ...

    def extract_pages(
        self, session: Session, package_revision_id: UUID
    ) -> Sequence[PageResult]: ...

    def match(self, session: Session, package_revision_id: UUID) -> Mapping[str, object]: ...

    def validate_evidence(
        self, session: Session, package_revision_id: UUID
    ) -> Mapping[str, object]: ...

    def run_checks(self, session: Session, package_revision_id: UUID) -> Mapping[str, object]: ...

    def generate_outputs(
        self, session: Session, package_revision_id: UUID
    ) -> Mapping[str, object]: ...


class NoStages:
    """The default: does nothing, and says so.

    Every stage reports `implemented: False`. That is deliberate and it is not a placeholder to be
    forgotten — the stages belong to tracks that are mostly unbuilt, and a default that quietly returned
    `{}` would let a package walk the whole pipeline and arrive at `AWAITING_REVIEW` looking processed.
    `AGENTS.md` §2.2: silence must never read as completion. A reviewer reading a package run by this
    sees, on every stage, that nothing ran.
    """

    def _nothing(self, stage: str) -> Mapping[str, object]:
        return {"implemented": False, "stage": stage}

    def ingest(self, session: Session, package_revision_id: UUID) -> Mapping[str, object]:
        del session, package_revision_id
        return self._nothing("ingest")

    def extract_pages(self, session: Session, package_revision_id: UUID) -> Sequence[PageResult]:
        del session, package_revision_id
        return ()

    def match(self, session: Session, package_revision_id: UUID) -> Mapping[str, object]:
        del session, package_revision_id
        return self._nothing("match")

    def validate_evidence(
        self, session: Session, package_revision_id: UUID
    ) -> Mapping[str, object]:
        del session, package_revision_id
        return self._nothing("validate_evidence")

    def run_checks(self, session: Session, package_revision_id: UUID) -> Mapping[str, object]:
        del session, package_revision_id
        return self._nothing("run_checks")

    def generate_outputs(self, session: Session, package_revision_id: UUID) -> Mapping[str, object]:
        del session, package_revision_id
        return self._nothing("generate_outputs")


def join_pages(results: Sequence[PageResult]) -> tuple[PageResult, ...]:
    """The fan-out's join: page results in page order, whatever order they finished in.

    **Deterministic by construction rather than by luck.** Tasks complete in whatever order the engine
    and the pages allow, and a join that preserved completion order would make the same package produce
    a different result on a rerun — which would make a finding impossible to reproduce.

    Ordered by page index, then nothing else is needed: two results for one index would be the same page
    read twice, so that is refused rather than silently deduplicated.

    Per-page *failure isolation* — one unreadable page not failing the package — is B6.4 (#163), which
    `requires: 215` and is deferred. This is the join it will fan out into.
    """
    seen: dict[int, PageResult] = {}
    for result in results:
        # **Refuse the wrong shape rather than count it.** Found by a review of this file's own test stub,
        # which returned a `Mapping` here because the generic stub covered every stage. Nothing failed:
        # iterating a mapping yields its keys, `"ran".index` is a bound method, that is hashable, and one
        # key never gets compared — so the stage reported `{"pages": 1}` for zero pages extracted. A second
        # key would have raised `TypeError` from `sorted` instead, in a test about something else entirely.
        #
        # Every real `extract_pages` is still unbuilt, so this is the window where a wrong return type gets
        # written and silently reports phantom pages. AGENTS.md §2.2: silence must never read as completion.
        if not isinstance(result, PageResult):
            raise TypeError(
                "extract_pages must return PageResult objects; got "
                f"{type(result).__name__}. A mapping or a string here does not fail on its own — it "
                "reports pages that were never read."
            )
        if result.index in seen:
            raise ValueError(
                f"page {result.index} was returned twice by the extraction fan-out. Two results for "
                "one page is the same page read twice, and picking one silently would make the "
                "package's result depend on which arrived first."
            )
        seen[result.index] = result
    return tuple(seen[index] for index in sorted(seen))


def run_stage(
    session: Session,
    *,
    stage: str,
    state: PackageState,
    package_revision_id: UUID,
    workflow_run_id: UUID,
    stages: Stages,
    engine_version: str = ENGINE_VERSION,
) -> StageOutcome:
    """Claim the stage, run it if it has not run, and move the package. Commits nothing.

    In that order, and the order is the durability. The claim goes in first so a re-delivered stage
    recognises itself and returns without repeating the work — `already_done` on the outcome. Only then
    does the work run, and only then does the package move, so a package that says `MATCHING` has
    actually matched.

    The caller owns the transaction, as everywhere else in this codebase: the claim, whatever the stage
    wrote, and the state change land together or not at all. A claim that committed on its own would
    block the retry that should redo the work it never finished.

    Raises:
        UnknownRevision / IllegalTransition: from `transition`, unchanged.
    """
    key = stage_idempotency_key(
        package_revision_id=package_revision_id,
        stage=stage,
        engine_version=engine_version,
    )
    taken = claim(
        session,
        key,
        workflow_run_id=workflow_run_id,
        task_type=stage,
        outcome=CLAIMED,
    )
    if not taken.created:
        # Somebody already ran this. Returning the prior identity rather than re-running is the whole
        # point of the key — see `StageOutcome.already_done`.
        return StageOutcome(
            stage=stage,
            state=state,
            idempotency_key=key,
            already_done=True,
            payload={"claimed_by": str(taken.task_run.workflow_run_id)},
        )

    # Dispatched by name rather than by a six-branch conditional, so `STAGES` stays the single list.
    # `getattr` returns `Any`, which is why the two branches below need no casts.
    runner = getattr(stages, stage)
    produced = runner(session, package_revision_id)
    if stage == "extract_pages":
        # The join is what makes the fan-out reproducible; see `join_pages`.
        joined = join_pages(produced)
        payload: Mapping[str, object] = {"pages": len(joined)}
    else:
        payload = produced

    transition(
        session,
        package_revision_id,
        state,
        actor=f"the {stage.replace('_', ' ')} worker",
        reason=None,
        workflow_run_id=workflow_run_id,
    )
    return StageOutcome(
        stage=stage,
        state=state,
        idempotency_key=key,
        already_done=False,
        payload=payload,
    )


def run_all(
    factory: sessionmaker[Session],
    *,
    package_revision_id: UUID,
    workflow_run_id: UUID,
    stages: Stages,
    engine_version: str = ENGINE_VERSION,
) -> tuple[StageOutcome, ...]:
    """Every stage in order, each in its own transaction. Returns what each one did.

    One transaction per stage, not one for the whole walk: a package that got through matching and then
    failed at checks should keep the matching. The state events record how far it got, which is what a
    dispute reads.

    A stage that raises is recorded as a failure through `app/lifecycle/side_states.py`, in a *separate*
    transaction — the one that raised is rolled back, so writing the failure inside it would be
    discarded along with the thing it was reporting. Then the exception is re-raised: the engine decides
    whether to retry, and it cannot decide if the failure was swallowed here.
    """
    outcomes: list[StageOutcome] = []
    for stage, state in STAGES:
        try:
            with unit_of_work(factory) as session:
                outcomes.append(
                    run_stage(
                        session,
                        stage=stage,
                        state=state,
                        package_revision_id=package_revision_id,
                        workflow_run_id=workflow_run_id,
                        stages=stages,
                        engine_version=engine_version,
                    )
                )
        except Exception as error:
            _record_failure(
                factory,
                package_revision_id,
                actor=f"the {stage.replace('_', ' ')} worker",
                error=error,
            )
            raise
    return tuple(outcomes)


def _record_failure(
    factory: sessionmaker[Session],
    package_revision_id: UUID,
    *,
    actor: str,
    error: BaseException,
) -> None:
    """Record a stage failure, and never let the recording hide what failed.

    **Recording can itself be refused, and that used to lose the real error.** A package only reaches a
    failure state from a *processing* state, so a first-stage failure — where the revision is still
    `UPLOADED` — makes `enter_failure` raise `IllegalTransition`, and that exception replaced the one it
    was trying to report. Proved against the database before writing this:

        IllegalTransition: a package revision in UPLOADED may not move to FAILED_PERMANENT.

    The caller saw a state-machine complaint and no sign of the `ValueError` that had actually stopped the
    package. So a recording problem is attached to the original as a note, and the original is what
    propagates: both visible, neither swallowed.

    This does **not** close the underlying gap. A package that fails during its first stage still ends up
    with no failure state, because the table has nowhere for it to go from `UPLOADED`. That is a lifecycle
    design question — whether `run_stage` should move the package into the stage's state before doing the
    work, or whether `UPLOADED` should be allowed to fail — and it is raised rather than decided here.

    A separate transaction from the failed one on purpose: that one has rolled back, so a reason written
    inside it would be discarded along with the work it describes.
    """
    try:
        with unit_of_work(factory) as session:
            enter_failure(session, package_revision_id, actor=actor, error=error)
    except Exception as write_failed:  # noqa: BLE001 - the original error must outlive this one
        error.add_note(
            f"the failure could not be recorded: {type(write_failed).__name__}: {write_failed}"
        )


def register(
    hatchet: Hatchet,
    *,
    factory: sessionmaker[Session],
    stages: Stages | None = None,
    max_concurrent_packages: int,
) -> Workflow[PackageReviewInput]:
    """Build the `process_package_revision` graph on a Hatchet client.

    Takes the client rather than making one, because `Hatchet()` refuses to construct without a token —
    so a module that built one at import could not be imported by a test, or by anything that merely
    wanted to read the stage list.

    The tasks are deliberately thin. Each one calls `run_stage`, which is an ordinary function; the
    tests exercise those directly and never start an engine, which is what the story's test plan asks
    for. What is *not* tested without a live Hatchet is the wiring below — so it is kept small enough to
    read.

    `max_concurrent_packages` is a required keyword rather than a default, because the right number
    depends on the box and the wrong one is an out-of-memory kill on a shared 8 GB VM. See
    `app/config.py` for where the value comes from and why it starts at 1.
    """
    # Imported here, not at module scope: `workflow/retry.py` imports `stage_order` from this module, so a
    # top-level import would close a cycle. `register` runs once when a worker is built, so the cost is
    # nothing and the direction of the dependency stays readable — the graph asks the policy, never the
    # other way round.
    from workflow.retry import engine_retry_settings

    resolved = stages if stages is not None else NoStages()

    workflow: Workflow[PackageReviewInput] = hatchet.workflow(
        name=WORKFLOW_NAME,
        input_validator=PackageReviewInput,
        concurrency=max_concurrent_packages,
    )

    previous: Task[PackageReviewInput, Any] | None = None
    for stage, state in STAGES:

        def step(
            payload: PackageReviewInput,
            ctx: object,
            *,
            _stage: str = stage,
            _state: PackageState = state,
        ) -> dict[str, object]:
            del ctx
            actor = f"the {_stage.replace('_', ' ')} worker"
            try:
                with unit_of_work(factory) as session:
                    outcome = run_stage(
                        session,
                        stage=_stage,
                        state=_state,
                        package_revision_id=payload.package_revision_id,
                        workflow_run_id=payload.workflow_run_id,
                        stages=resolved,
                    )
            except Exception as error:
                # **A permanent failure must not be retried, and the engine cannot know that.** This task
                # is not `run_all` — it is the engine calling `run_stage` directly — so without this the
                # retry budget above would happily re-run a `ValueError` twice. `workflow/retry.py`'s
                # `should_retry` says never to do that, and giving the engine a budget while leaving this
                # out made the behaviour worse than the zero-retry default it replaced.
                #
                # Recorded through `_record_failure`, which is careful not to let a refused transition
                # replace the error it was reporting.
                _record_failure(factory, payload.package_revision_id, actor=actor, error=error)
                if classify(error) is FailureClass.PERMANENT:
                    # Imported here for the same reason the client is: `hatchet_sdk` pulls in gRPC, and
                    # this module is imported by tests that never start an engine.
                    from hatchet_sdk.exceptions import NonRetryableException

                    permanent = NonRetryableException(str(error))
                    # Notes travel with the exception that actually escapes, not the one it replaces.
                    # `_record_failure` attaches "the failure could not be recorded" to the original, and
                    # a caller reading this task sees only what is raised here — my own test caught that,
                    # by asserting on the notes of whatever came out.
                    for note in getattr(error, "__notes__", []):
                        permanent.add_note(note)
                    raise permanent from error
                raise
            return {
                "stage": outcome.stage,
                "state": outcome.state.value,
                "already_done": outcome.already_done,
            }

        step.__name__ = stage
        # **The retry budget goes to the engine, because the engine is what retries.** `run_all` neither
        # sleeps nor re-runs a stage, so before this the policy in `workflow/retry.py` had no caller and
        # described behaviour nothing implemented. See `engine_retry_settings` for the mapping and for the
        # one place it is fragile. The budget only applies to failures `step` lets through as retryable.
        budget = engine_retry_settings(stage)
        decorate = workflow.task(
            name=stage,
            parents=[previous] if previous else None,
            retries=budget.retries,
            backoff_factor=budget.backoff_factor,
            backoff_max_seconds=budget.backoff_max_seconds,
        )
        previous = decorate(step)

    return workflow
