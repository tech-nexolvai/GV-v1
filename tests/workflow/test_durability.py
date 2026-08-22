"""Surviving a restart, asserted by killing a worker rather than by argument (#217, C4.5).

Every test that matters here kills a real subprocess with `SIGKILL` mid-stage and then looks at what
PostgreSQL kept. That is deliberate: a `raise` unwinds cleanly and runs `except` blocks, so a test built on
one proves the tidy case and says nothing about a worker whose box disappeared.

What the harness produced the first time it ran, which is the shape every test below relies on:

    entered   : ('ingest', 'extract_pages', 'match')
    exit code : -9
    db state  : EXTRACTING
    db claims : ['extract_pages', 'ingest']

`match` announced itself, was killed, and left nothing behind — no claim, no state change. The two stages
that finished kept both.

Source: backend proposal §9.2–§9.4 · Design: `docs/DESIGN_PLATFORM.md` §6.2 · Verification: this file
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.db.session import session_factory, unit_of_work
from app.lifecycle.events import history
from app.lifecycle.side_states import FailureClass, enter_failure
from app.lifecycle.states import PROCESSING_STATES, begin, transition
from app.models import (
    Package,
    PackageRevision,
    PackageState,
    Project,
    TaskRun,
    WorkflowRun,
)
from tests.app.postgres_fixture import alembic_config
from tests.workflow.conftest import kill_at
from workflow.durability import (
    AUTOMATIC_RESUME_ACTOR,
    NotResumable,
    recovery_interventions,
    resume_point,
    run_to_completion,
)
from workflow.review import STAGES, run_all, stage_order

pytest_plugins = ("tests.app.postgres_fixture",)


@pytest.fixture
def factory(postgres_engine: Engine) -> sessionmaker[Session]:
    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")
    return session_factory(postgres_engine)


@pytest.fixture
def url(postgres_engine: Engine) -> str:
    """The URL the killed child connects with — it cannot inherit a session."""
    return postgres_engine.url.render_as_string(hide_password=False)


def _uploaded_revision(session: Session) -> tuple[UUID, UUID]:
    """A revision that has finished uploading, which is where processing may begin."""
    project = Project(name=f"durability {uuid4().hex[:8]}")
    session.add(project)
    session.flush()
    package = Package(project_id=project.id, vendor=None)
    session.add(package)
    session.flush()
    revision = PackageRevision(package_id=package.id, revision_number=1, state=PackageState.CREATED)
    session.add(revision)
    session.flush()
    begin(session, revision.id, actor="anant")
    transition(session, revision.id, PackageState.UPLOADING, actor="anant")
    transition(session, revision.id, PackageState.UPLOADED, actor="anant")
    run = WorkflowRun(package_revision_id=revision.id, engine_run_id=f"hatchet-{uuid4().hex}")
    session.add(run)
    session.flush()
    return revision.id, run.id


def _claims(session: Session, workflow_run_id: UUID) -> set[str]:
    return set(
        session.execute(
            select(TaskRun.task_type).where(TaskRun.workflow_run_id == workflow_run_id)
        ).scalars()
    )


# ---------------------------------------------------------------------------
# Kill and resume, at every stage
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("step", list(stage_order()))
def test_a_killed_worker_resumes_and_finishes(
    step: str, factory: sessionmaker[Session], url: str
) -> None:
    """**The acceptance criterion, at every stage.** Kill the worker entering `step`; the resumed run
    finishes the package and does not repeat a stage that had already completed.

    Parametrised over all six because the interesting cases are the ends. Killed in the first stage there
    is nothing to preserve; killed in the last there is nothing left to do; and a resume that only works
    in the middle would pass a single-case test and fail in production on the two that matter.
    """
    with unit_of_work(factory) as session:
        revision_id, run_id = _uploaded_revision(session)

    killed = kill_at(
        step, database_url=url, package_revision_id=revision_id, workflow_run_id=run_id
    )
    assert killed.exit_code is not None and killed.exit_code < 0, "the child must die by signal"

    with unit_of_work(factory) as session:
        survived = _claims(session, run_id)
    assert step not in survived, (
        f"{step} was killed mid-flight, so its claim must have rolled back — otherwise a retry would "
        "short-circuit and the work would never be done"
    )
    assert (
        set(killed.completed_stages) == survived
    ), "the stages the child believed it finished are exactly the ones the database kept"

    result = run_to_completion(factory, package_revision_id=revision_id, workflow_run_id=run_id)

    assert (
        result.final_state == PackageState.GENERATING_OUTPUTS
    ), "the resumed run takes the package to the end of the pipeline"
    assert step in result.stages_run, "the killed stage is redone, because it never completed"

    # **Positive assertions, because the negative ones are vacuous at the ends.** For `ingest` there are no
    # completed stages, so a loop over them checks nothing and `step not in survived` is trivially true —
    # review pointed out that the first stage, one of the two the docstring calls most interesting, was
    # effectively untested. So the expected set is stated outright.
    expected_completed = tuple(stage_order()[: stage_order().index(step)])
    assert (
        killed.completed_stages == expected_completed
    ), f"killed entering {step}, so exactly {expected_completed} should have finished"
    assert survived == set(expected_completed)
    for done in expected_completed:
        assert done not in result.stages_run, f"{done} completed before the kill and was repeated"
    # Not `stages_skipped == expected_completed`: I asserted that first and it failed, which was the useful
    # part. The walk begins after the last stage that committed, so earlier finished stages are never
    # visited and never reported as skipped — they are absent from `outcomes` entirely. `stages_skipped`
    # covers only stages the walk reached and found already claimed.
    assert not set(result.stages_run) & set(expected_completed)


def test_a_resumed_run_repeats_no_completed_stage(factory: sessionmaker[Session], url: str) -> None:
    """The same property stated as a count, because that is what a paid call is.

    Each stage's claim is one row keyed by task identity. If a resume repeated a completed stage there
    would be two attempts recorded for it, and in the real pipeline the second would be a second charge
    for the same page.
    """
    with unit_of_work(factory) as session:
        revision_id, run_id = _uploaded_revision(session)

    kill_at(
        "validate_evidence",
        database_url=url,
        package_revision_id=revision_id,
        workflow_run_id=run_id,
    )
    run_to_completion(factory, package_revision_id=revision_id, workflow_run_id=run_id)

    with unit_of_work(factory) as session:
        rows = list(
            session.execute(
                select(TaskRun.task_type).where(TaskRun.workflow_run_id == run_id)
            ).scalars()
        )
    assert (
        len(rows) == len(set(rows)) == len(STAGES)
    ), f"one attempt per stage and no more; got {sorted(rows)}"


def test_the_killed_stage_left_nothing_half_written(
    factory: sessionmaker[Session], url: str
) -> None:
    """§2.3: partial work from an interrupted stage is absent, not present-and-incomplete.

    The transaction is the whole mechanism. The killed stage had claimed its task and moved the package
    before it died; neither survives, because both were in the transaction that never committed. A design
    that committed the claim early would leave exactly the "present but incomplete" state this forbids.
    """
    with unit_of_work(factory) as session:
        revision_id, run_id = _uploaded_revision(session)

    kill_at("match", database_url=url, package_revision_id=revision_id, workflow_run_id=run_id)

    with unit_of_work(factory) as session:
        revision = session.get(PackageRevision, revision_id)
        assert revision is not None
        reached = [str(event.to_state) for event in history(session, revision_id)]

    assert (
        PackageState(revision.state) == PackageState.EXTRACTING
    ), "the package sits at the last stage that actually finished"
    assert (
        PackageState.MATCHING.value not in reached
    ), "the killed stage moved the package inside its own transaction, so no MATCHING event survives"


# ---------------------------------------------------------------------------
# Resuming a failure, and where a resume is allowed to land
# ---------------------------------------------------------------------------


def test_a_failed_package_resumes_at_the_stage_that_failed(
    factory: sessionmaker[Session], url: str
) -> None:
    """A resume re-enters the state that failed, not a later one.

    `_resumes_where_it_failed` already refuses the alternative, and the reason is worth restating: a
    failure in `EXTRACTING` resuming at `RUNNING_CHECKS` would run checks against evidence nothing
    validated. This asserts the resume asks for the right state in the first place.
    """
    with unit_of_work(factory) as session:
        revision_id, run_id = _uploaded_revision(session)

    kill_at("match", database_url=url, package_revision_id=revision_id, workflow_run_id=run_id)

    # A supervisor notices and records the failure the killed process never got to record.
    with unit_of_work(factory) as session:
        enter_failure(
            session,
            revision_id,
            actor=AUTOMATIC_RESUME_ACTOR,
            failure_class=FailureClass.RETRYABLE,
            reason="the worker was killed",
        )

    with unit_of_work(factory) as session:
        assert resume_point(session, revision_id) == PackageState.EXTRACTING

    result = run_to_completion(factory, package_revision_id=revision_id, workflow_run_id=run_id)
    assert result.final_state == PackageState.GENERATING_OUTPUTS


class Failing:
    """Stages that raise at a chosen one, so `run_all` records the failure itself."""

    def __init__(self, at: str) -> None:
        self.at = at
        self.calls: list[str] = []

    def _run(self, name: str) -> dict[str, object]:
        self.calls.append(name)
        if name == self.at:
            raise TimeoutError("the renderer stopped answering")
        return {"ran": name}

    def ingest(self, session: Session, package_revision_id: UUID) -> dict[str, object]:
        del session, package_revision_id
        return self._run("ingest")

    def extract_pages(self, session: Session, package_revision_id: UUID) -> tuple[()]:
        del session, package_revision_id
        self._run("extract_pages")
        return ()

    def match(self, session: Session, package_revision_id: UUID) -> dict[str, object]:
        del session, package_revision_id
        return self._run("match")

    def validate_evidence(self, session: Session, package_revision_id: UUID) -> dict[str, object]:
        del session, package_revision_id
        return self._run("validate_evidence")

    def run_checks(self, session: Session, package_revision_id: UUID) -> dict[str, object]:
        del session, package_revision_id
        return self._run("run_checks")

    def generate_outputs(self, session: Session, package_revision_id: UUID) -> dict[str, object]:
        del session, package_revision_id
        return self._run("generate_outputs")


def test_a_stage_that_recorded_its_own_failure_resumes_at_itself(
    factory: sessionmaker[Session],
) -> None:
    """The other resume case, which turns out to be the **first stage specifically**.

    I first wrote this test for `match` and asserted the resume target was `MATCHING`. It is not, and the
    test said so. `_record_failure` only re-states the stage's state when the package is in an *assembly*
    state — which is true for `ingest`, where the rollback leaves it in `UPLOADED`, and false for every
    later stage, where the rollback leaves it in the previous stage's state and the failure is recorded
    from there.

    So the target is the failed stage itself only when the first stage failed, and its claim rolled back
    with the work — meaning the stage moves the package out of the failure state on its own, and
    `run_to_completion` must not move it first. Any later stage takes the mirror-image path covered above.
    """
    with unit_of_work(factory) as session:
        revision_id, run_id = _uploaded_revision(session)

    stages = Failing("ingest")
    with pytest.raises(TimeoutError):
        run_all(
            factory,
            package_revision_id=revision_id,
            workflow_run_id=run_id,
            stages=stages,
        )

    with unit_of_work(factory) as session:
        revision = session.get(PackageRevision, revision_id)
        assert revision is not None
        assert PackageState(revision.state) == PackageState.FAILED_RETRYABLE
        # The failure was recorded from the failing stage's own state, not the previous one.
        assert resume_point(session, revision_id) == PackageState.INGESTING
        assert _claims(session, run_id) == set(), "nothing completed, so no claim survived"

    result = run_to_completion(
        factory, package_revision_id=revision_id, workflow_run_id=run_id, stages=Failing("nothing")
    )
    assert result.final_state == PackageState.GENERATING_OUTPUTS
    assert result.stages_run == stage_order(), "everything runs, because nothing had completed"


def test_a_package_that_never_started_is_not_resumable(factory: sessionmaker[Session]) -> None:
    """`resume_point` answers None for a package that is not waiting to be resumed, and
    `run_to_completion` simply runs it from the top."""
    with unit_of_work(factory) as session:
        revision_id, run_id = _uploaded_revision(session)
        assert resume_point(session, revision_id) is None

    result = run_to_completion(factory, package_revision_id=revision_id, workflow_run_id=run_id)
    assert result.stages_run == stage_order(), "nothing was skipped, because nothing had run"


def test_a_finished_package_can_be_resumed_and_nothing_happens(
    factory: sessionmaker[Session],
) -> None:
    """Idempotent at the top level too. Calling it twice is not an error and does no work the second
    time, which is what makes it safe for a supervisor to call on anything it finds."""
    with unit_of_work(factory) as session:
        revision_id, run_id = _uploaded_revision(session)

    first = run_to_completion(factory, package_revision_id=revision_id, workflow_run_id=run_id)
    second = run_to_completion(factory, package_revision_id=revision_id, workflow_run_id=run_id)

    assert first.stages_run == stage_order()
    assert second.stages_run == (), "the second call repeated nothing"
    assert second.final_state == first.final_state


def test_an_unknown_revision_is_refused_rather_than_ignored(
    factory: sessionmaker[Session],
) -> None:
    with unit_of_work(factory) as session, pytest.raises(NotResumable, match="no package revision"):
        resume_point(session, uuid4())


# ---------------------------------------------------------------------------
# The Temporal upgrade trigger
# ---------------------------------------------------------------------------


def test_an_automatic_resume_is_not_an_intervention(factory: sessionmaker[Session]) -> None:
    """Anant's call on #217: only a person's resume counts.

    An automatic retry is not toil, and Temporal's value is removing toil — so counting retries would make
    a flaky afternoon look like a reason to adopt a new workflow engine.
    """
    with unit_of_work(factory) as session:
        revision_id, run_id = _uploaded_revision(session)
        transition(session, revision_id, PackageState.INGESTING, actor="the ingest worker")
        enter_failure(
            session,
            revision_id,
            actor="the ingest worker",
            failure_class=FailureClass.RETRYABLE,
            reason="a dropped connection",
        )

    run_to_completion(factory, package_revision_id=revision_id, workflow_run_id=run_id)

    with unit_of_work(factory) as session:
        assert recovery_interventions(session, timedelta(hours=1)) == 0


def test_a_human_resume_is_counted(factory: sessionmaker[Session]) -> None:
    """And this is the number the trigger reads."""
    with unit_of_work(factory) as session:
        revision_id, run_id = _uploaded_revision(session)
        transition(session, revision_id, PackageState.INGESTING, actor="the ingest worker")
        enter_failure(
            session,
            revision_id,
            actor="the ingest worker",
            failure_class=FailureClass.RETRYABLE,
            reason="a dropped connection",
        )

    run_to_completion(
        factory, package_revision_id=revision_id, workflow_run_id=run_id, actor="anant"
    )

    with unit_of_work(factory) as session:
        assert recovery_interventions(session, timedelta(hours=1)) == 1
        # The counted event, named. Without this the test passes on any event that happens to carry the
        # actor, which is not the same as counting the recovery — review's point about both of these tests.
        processing = {state.value for state in PROCESSING_STATES}
        mine = [
            event
            for event in history(session, revision_id)
            if event.actor == "anant" and str(event.to_state) in processing
        ]
        assert len(mine) == 1
        assert (
            str(mine[0].from_state) == PackageState.FAILED_RETRYABLE.value
        ), "the counted event is the move out of the failure state — the recovery itself"


def test_a_person_rescuing_a_killed_package_counts_too(
    factory: sessionmaker[Session], url: str
) -> None:
    """**The gap review found in the first version of the metric.**

    A worker killed mid-flight leaves its package in a *processing* state, not a failure state. The first
    metric only counted transitions out of `RESUMABLE_STATES`, so a person picking that package up and
    driving it to the end counted as **nothing** — the number would have drifted toward "no intervention
    needed" while somebody did the work by hand.

    Now anyone who is not a stage worker or the supervisor counts, whatever state they found the package
    in.
    """
    with unit_of_work(factory) as session:
        revision_id, run_id = _uploaded_revision(session)

    kill_at("match", database_url=url, package_revision_id=revision_id, workflow_run_id=run_id)

    with unit_of_work(factory) as session:
        assert resume_point(session, revision_id) is None, "it is mid-flight, not failed"

    run_to_completion(
        factory, package_revision_id=revision_id, workflow_run_id=run_id, actor="anant"
    )

    with unit_of_work(factory) as session:
        assert recovery_interventions(session, timedelta(hours=1)) == 1
        # **Pin the event, not just the count.** Review's point: both intervention tests would have passed
        # if the number came from entirely the wrong events, as long as the actor filter happened to match.
        # So this names the event that must be the one counted.
        processing = {state.value for state in PROCESSING_STATES}
        mine = [
            event
            for event in history(session, revision_id)
            if event.actor == "anant" and str(event.to_state) in processing
        ]
        assert len(mine) == 1, "exactly one event carries the person's name"
        # This package was killed mid-flight, so the rescue starts from a *processing* state rather than a
        # failure one — which is the whole reason the first metric missed it. I first asserted
        # FAILED_RETRYABLE here and the test said otherwise, which is the assertion doing its job.
        assert (
            str(mine[0].from_state) == PackageState.EXTRACTING.value
        ), "the rescue moves it on from the last stage that committed"


def test_zero_is_a_real_answer_not_a_missing_one(factory: sessionmaker[Session]) -> None:
    """A quiet week reads as zero interventions, which is a measurement.

    `docs/DESIGN_CONTROLS.md` §6 asks for "not measured" to be as prominent as a breach; distinguishing
    the two is F6.1's job (#267), and this function's part of the contract is that it always has an
    answer, because the event log always exists.
    """
    with unit_of_work(factory) as session:
        assert recovery_interventions(session, timedelta(hours=1)) == 0


def test_a_window_must_be_a_positive_length_of_time(factory: sessionmaker[Session]) -> None:
    """A zero window would silently return zero — a measurement that cannot detect anything, reported as
    if nothing had happened."""
    with unit_of_work(factory) as session:
        for bad in (timedelta(0), timedelta(seconds=-1)):
            with pytest.raises(ValueError, match="positive"):
                recovery_interventions(session, bad)
        # And the smallest positive window is accepted rather than rejected along with them — a check that
        # only ever refuses would pass if the boundary were on the wrong side.
        assert recovery_interventions(session, timedelta(microseconds=1)) == 0


# ---------------------------------------------------------------------------
# Where "no repeated paid call" actually lives
# ---------------------------------------------------------------------------


def test_a_repeated_node_invocation_is_refused_by_the_database(
    factory: sessionmaker[Session],
) -> None:
    """The no-repeated-paid-call guarantee is #247's, enforced by a unique index — and this is the second
    attempt at asserting it, because the first could not fail for the right reason.

    **What was wrong.** The row was built from invented values, so the *first* insert already violated
    something: measured, it raised `NotNullViolation` on `input_tokens` before ever reaching the foreign key
    or the unique index. `pytest.raises(IntegrityError)` caught that, the test passed, and the constraint it
    claimed to cover was never exercised. Asserting the message named `node_invocation_key` did not save it
    either — `str(IntegrityError)` embeds the whole INSERT statement, so the column name matched the SQL
    text rather than any constraint.

    **What it does now.** Build a real workflow run, task run and extraction run, fill every required
    column, and insert twice. The only thing left that can fail is the duplicate key — and the error type is
    checked, not its text, so a schema change that drops the unique index fails this test instead of
    slipping through on a different violation.
    """
    import hashlib

    import psycopg

    from app.models.runs import ExtractionRun, ModelInvocation

    with unit_of_work(factory) as session:
        _, run_id = _uploaded_revision(session)
        task_run = TaskRun(
            workflow_run_id=run_id,
            idempotency_key=f"key:{uuid4().hex}",
            task_type="extract_pages",
            attempt=1,
            outcome="claimed",
        )
        session.add(task_run)
        session.flush()
        extraction_run = ExtractionRun(
            task_run_id=task_run.id,
            extractor="fake",
            extractor_version="1.0.0",
            config_hash="sha256:" + "0" * 64,
        )
        session.add(extraction_run)
        session.flush()
        extraction_run_id = extraction_run.id

    # The key's own check constraint is `^sha256:[0-9a-f]{64}$`, so it is hashed rather than
    # improvised — an invented string fails that check first and the test would be back to passing on the
    # wrong error, which is precisely the bug being fixed here.
    key = "sha256:" + hashlib.sha256(uuid4().bytes).hexdigest()

    def one() -> dict[str, object]:
        return {
            "id": uuid4(),
            "extraction_run_id": extraction_run_id,
            "model_id": "fake",
            "prompt_id": "fake",
            "template_id": "fake",
            "input_tokens": 1,
            "output_tokens": 1,
            "cost_micros": 1,
            "latency_ms": 1,
            "outcome": "ok",
            "node_invocation_key": key,
        }

    # The first insert must succeed, or the test is back to passing on the wrong error.
    with unit_of_work(factory) as session:
        session.execute(ModelInvocation.__table__.insert().values(**one()))

    with pytest.raises(IntegrityError) as caught, unit_of_work(factory) as session:
        session.execute(ModelInvocation.__table__.insert().values(**one()))

    # The type, not the text. A foreign-key or not-null violation is also an IntegrityError.
    assert isinstance(
        caught.value.orig, psycopg.errors.UniqueViolation
    ), f"refused, but not by a unique constraint: {type(caught.value.orig).__name__}"
    assert "node_invocation_key" in str(
        caught.value.orig
    ), "and it must be the node key's index, not some other unique column"
