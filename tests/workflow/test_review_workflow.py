"""The package review workflow: durable, resumable, and holding no truth of its own (#215, C4.3).

Every test here runs without a Hatchet engine, which is the story's test plan and also the design: the
stage logic is ordinary functions taking a session, and the decorators are a thin wrapper over them. If
the tests needed a live engine, the logic would be in the wrong place.

The four acceptance criteria, and what proves each:

- **Business state stays in PostgreSQL.** Asserted by reading it back out of `package_state_events` and
  `task_runs` after a run, and by the import guard below: nothing in `workflow/review.py` writes state
  except through `app/lifecycle/`.
- **Each stage records a `task_run` with its idempotency key.** Read back per stage, and the keys are
  asserted distinct — six stages sharing one key would make the second short-circuit the first.
- **A restart resumes without redoing paid work.** The second run of a stage reports `already_done` and
  runs nothing, which is the whole reason the claim comes before the work.
- **Concurrency caps are configurable.** Settings, with the low defaults Anant chose and a stated reason.

Source: backend proposal §9.1–§9.4 · Design: `docs/DESIGN_PLATFORM.md` §6 · Verification: this file
"""

from __future__ import annotations

import ast
from itertools import pairwise
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.config import Settings
from app.db.session import session_factory, unit_of_work
from app.lifecycle.events import history
from app.lifecycle.states import begin
from app.models import Package, PackageRevision, PackageState, Project, TaskRun, WorkflowRun
from workflow.idempotency import stage_idempotency_key
from workflow.review import (
    ENGINE_VERSION,
    STAGES,
    WORKFLOW_NAME,
    NoStages,
    PackageReviewInput,
    PageResult,
    join_pages,
    run_all,
    run_stage,
    stage_order,
)

pytest_plugins = ("tests.app.postgres_fixture",)

REPO_ROOT = Path(__file__).resolve().parents[2]
DATABASE_URL = "postgresql+psycopg://gv:gv@localhost:5433/gv"


@pytest.fixture
def factory(postgres_engine: Engine) -> sessionmaker[Session]:
    config = Config("alembic.ini")
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")
    return session_factory(postgres_engine)


def _revision_and_run(session: Session) -> tuple[UUID, UUID]:
    """A package revision ready to be processed, and a workflow run to attribute the work to."""
    project = Project(name=f"workflow {uuid4().hex[:8]}")
    session.add(project)
    session.flush()
    package = Package(project_id=project.id, vendor=None)
    session.add(package)
    session.flush()
    revision = PackageRevision(package_id=package.id, revision_number=1, state=PackageState.CREATED)
    session.add(revision)
    session.flush()
    begin(session, revision.id, actor="anant")
    # UPLOADING then UPLOADED, so the revision is where `ingest` can legally take it from.
    from app.lifecycle.states import transition

    transition(session, revision.id, PackageState.UPLOADING, actor="anant")
    transition(session, revision.id, PackageState.UPLOADED, actor="anant")
    run = WorkflowRun(package_revision_id=revision.id, engine_run_id=f"hatchet-{uuid4().hex}")
    session.add(run)
    session.flush()
    return revision.id, run.id


class RecordingStages:
    """Stages that record being called and return something identifiable.

    A fake rather than the real work, because the real work belongs to tracks that are mostly unbuilt —
    and because a test that rendered a PDF would be testing the renderer.
    """

    def __init__(self, pages: int = 3) -> None:
        self.calls: list[str] = []
        self._pages = pages

    def _record(self, stage: str) -> dict[str, object]:
        self.calls.append(stage)
        return {"ran": stage}

    def ingest(self, session: Session, package_revision_id: UUID) -> dict[str, object]:
        del session, package_revision_id
        return self._record("ingest")

    def extract_pages(self, session: Session, package_revision_id: UUID) -> tuple[PageResult, ...]:
        del session, package_revision_id
        self.calls.append("extract_pages")
        # Deliberately out of order, so the join has something to sort.
        return tuple(
            PageResult(index=index, payload={"page": index})
            for index in reversed(range(self._pages))
        )

    def match(self, session: Session, package_revision_id: UUID) -> dict[str, object]:
        del session, package_revision_id
        return self._record("match")

    def validate_evidence(self, session: Session, package_revision_id: UUID) -> dict[str, object]:
        del session, package_revision_id
        return self._record("validate_evidence")

    def run_checks(self, session: Session, package_revision_id: UUID) -> dict[str, object]:
        del session, package_revision_id
        return self._record("run_checks")

    def generate_outputs(self, session: Session, package_revision_id: UUID) -> dict[str, object]:
        del session, package_revision_id
        return self._record("generate_outputs")


# ---------------------------------------------------------------------------
# The graph matches the state machine
# ---------------------------------------------------------------------------


def test_the_stages_walk_the_states_the_machine_expects() -> None:
    """The graph and the transition table describe one pipeline, not two.

    If they disagreed, a stage would be refused by `transition()` at runtime — which is the backstop,
    not the design. This is what says they agree.
    """
    from app.lifecycle.states import MAIN_LINE, TRANSITIONS

    walked = [state for _, state in STAGES]
    assert walked == [
        PackageState.INGESTING,
        PackageState.EXTRACTING,
        PackageState.MATCHING,
        PackageState.VALIDATING_EVIDENCE,
        PackageState.RUNNING_CHECKS,
        PackageState.GENERATING_OUTPUTS,
    ]
    # Each stage's state must be reachable from the one before it.
    for earlier, later in pairwise(walked):
        assert (
            later in TRANSITIONS[earlier]
        ), f"{earlier.value} -> {later.value} is not in the table"
    # And the first must be reachable from where an uploaded package sits.
    assert walked[0] in TRANSITIONS[PackageState.UPLOADED]
    assert all(state in MAIN_LINE for state in walked)


def test_the_workflow_name_is_the_one_the_outbox_enqueues() -> None:
    """`app/lifecycle/supersede.py` enqueues this string. A mismatch is a package nothing picks up,
    which looks like a slow queue rather than a bug — so it is asserted rather than assumed."""
    from app.lifecycle.supersede import PACKAGE_WORKFLOW

    assert WORKFLOW_NAME == PACKAGE_WORKFLOW


def test_the_stage_names_are_the_ones_the_issue_names() -> None:
    assert stage_order() == (
        "ingest",
        "extract_pages",
        "match",
        "validate_evidence",
        "run_checks",
        "generate_outputs",
    )


# ---------------------------------------------------------------------------
# Each stage records a task_run with its idempotency key
# ---------------------------------------------------------------------------


def test_each_stage_records_a_task_run_with_its_key(factory: sessionmaker[Session]) -> None:
    """Input: a full run. Outcome: six task_runs, six keys. Why: the acceptance criterion."""
    stages = RecordingStages()
    with unit_of_work(factory) as session:
        revision_id, run_id = _revision_and_run(session)

    outcomes = run_all(
        factory,
        package_revision_id=revision_id,
        workflow_run_id=run_id,
        stages=stages,
    )

    assert [outcome.stage for outcome in outcomes] == list(stage_order())
    with factory() as session:
        recorded = {
            task_type: key
            for task_type, key in session.execute(
                select(TaskRun.task_type, TaskRun.idempotency_key).where(
                    TaskRun.workflow_run_id == run_id
                )
            ).all()
        }
    assert set(recorded) == set(stage_order())
    for outcome in outcomes:
        assert recorded[outcome.stage] == outcome.idempotency_key


def test_the_stages_have_distinct_keys(factory: sessionmaker[Session]) -> None:
    """Six stages sharing one key would make the second short-circuit the first, and the package would
    stop after `ingest` while every stage reported success."""
    revision_id = uuid4()
    keys = {
        stage: stage_idempotency_key(
            package_revision_id=revision_id, stage=stage, engine_version=ENGINE_VERSION
        )
        for stage in stage_order()
    }
    assert len(set(keys.values())) == len(keys)


def test_a_key_is_stable_across_processes() -> None:
    """No clock, no randomness, no dict ordering — §6.2 requires it, because a key that differs between
    processes recognises nothing and every retry redoes the work."""
    revision_id = uuid4()
    first = stage_idempotency_key(
        package_revision_id=revision_id, stage="match", engine_version=ENGINE_VERSION
    )
    second = stage_idempotency_key(
        package_revision_id=revision_id, stage="match", engine_version=ENGINE_VERSION
    )
    assert first == second and first.startswith("sha256:")


def test_a_changed_engine_version_is_a_different_task() -> None:
    """`AGENTS.md` §2.7: a changed engine is a different task, not a cache hit. Otherwise an upgrade
    would reuse answers computed by code that no longer exists."""
    revision_id = uuid4()
    assert stage_idempotency_key(
        package_revision_id=revision_id, stage="match", engine_version="1.0.0"
    ) != stage_idempotency_key(
        package_revision_id=revision_id, stage="match", engine_version="1.0.1"
    )


# ---------------------------------------------------------------------------
# A restart resumes rather than repeats
# ---------------------------------------------------------------------------


def test_a_redelivered_stage_runs_nothing(factory: sessionmaker[Session]) -> None:
    """**The acceptance criterion, and the reason the claim comes before the work.**

    Wasted seconds are cheap; a second charge for the same page is not. The stage that already ran
    reports `already_done` and its runner is never called again.
    """
    stages = RecordingStages()
    with unit_of_work(factory) as session:
        revision_id, run_id = _revision_and_run(session)

    with unit_of_work(factory) as session:
        first = run_stage(
            session,
            stage="ingest",
            state=PackageState.INGESTING,
            package_revision_id=revision_id,
            workflow_run_id=run_id,
            stages=stages,
        )
    assert first.already_done is False
    assert stages.calls == ["ingest"]

    with unit_of_work(factory) as session:
        second = run_stage(
            session,
            stage="ingest",
            state=PackageState.INGESTING,
            package_revision_id=revision_id,
            workflow_run_id=run_id,
            stages=stages,
        )
    assert second.already_done is True
    assert second.idempotency_key == first.idempotency_key
    assert stages.calls == ["ingest"], "the work ran a second time"


def test_a_short_circuited_stage_writes_no_second_state_event(
    factory: sessionmaker[Session],
) -> None:
    """A re-delivery must not append to the package's history either. Two `INGESTING` events would make
    the trail say the package was ingested twice, which is a story about the package that is not true.
    """
    stages = RecordingStages()
    with unit_of_work(factory) as session:
        revision_id, run_id = _revision_and_run(session)

    for _ in range(3):
        with unit_of_work(factory) as session:
            run_stage(
                session,
                stage="ingest",
                state=PackageState.INGESTING,
                package_revision_id=revision_id,
                workflow_run_id=run_id,
                stages=stages,
            )

    with factory() as session:
        ingesting = [
            event
            for event in history(session, revision_id)
            if event.to_state == PackageState.INGESTING.value
        ]
    assert len(ingesting) == 1


def test_a_resumed_run_finishes_the_remaining_stages(factory: sessionmaker[Session]) -> None:
    """The realistic restart: two stages done, the worker dies, a new one picks it up. The finished
    stages short-circuit and the rest run once each."""
    stages = RecordingStages()
    with unit_of_work(factory) as session:
        revision_id, run_id = _revision_and_run(session)

    for stage, state in STAGES[:2]:
        with unit_of_work(factory) as session:
            run_stage(
                session,
                stage=stage,
                state=state,
                package_revision_id=revision_id,
                workflow_run_id=run_id,
                stages=stages,
            )
    assert stages.calls == ["ingest", "extract_pages"]

    outcomes = run_all(
        factory,
        package_revision_id=revision_id,
        workflow_run_id=run_id,
        stages=stages,
    )

    resumed = {outcome.stage: outcome.already_done for outcome in outcomes}
    assert resumed["ingest"] is True and resumed["extract_pages"] is True
    assert resumed["run_checks"] is False
    assert stages.calls.count("ingest") == 1, "a finished stage ran again on resume"
    with factory() as session:
        assert (
            session.execute(
                select(PackageRevision.state).where(PackageRevision.id == revision_id)
            ).scalar_one()
            == PackageState.GENERATING_OUTPUTS.value
        )


# ---------------------------------------------------------------------------
# The fan-out joins deterministically
# ---------------------------------------------------------------------------


def test_the_join_is_page_order_not_completion_order() -> None:
    """**Determinism by construction.** Tasks finish in whatever order the pages allow, and a join that
    preserved that would make the same package produce different results on a rerun — which makes a
    finding impossible to reproduce."""
    scattered = [
        PageResult(index=4, payload={}),
        PageResult(index=0, payload={}),
        PageResult(index=2, payload={}),
    ]
    assert [page.index for page in join_pages(scattered)] == [0, 2, 4]
    assert join_pages(scattered) == join_pages(list(reversed(scattered)))


def test_the_same_page_twice_is_refused() -> None:
    """Two results for one page is the same page read twice. Picking one silently would make the
    package's result depend on which arrived first."""
    with pytest.raises(ValueError, match="page 1 was returned twice"):
        join_pages([PageResult(index=1, payload={"a": 1}), PageResult(index=1, payload={"a": 2})])


def test_an_empty_fan_out_joins_to_nothing() -> None:
    """A document with no pages is a real answer, not an error to invent."""
    assert join_pages([]) == ()


def test_the_extraction_stage_reports_the_page_count(factory: sessionmaker[Session]) -> None:
    stages = RecordingStages(pages=5)
    with unit_of_work(factory) as session:
        revision_id, run_id = _revision_and_run(session)

    with unit_of_work(factory) as session:
        run_stage(
            session,
            stage="ingest",
            state=PackageState.INGESTING,
            package_revision_id=revision_id,
            workflow_run_id=run_id,
            stages=stages,
        )
    with unit_of_work(factory) as session:
        outcome = run_stage(
            session,
            stage="extract_pages",
            state=PackageState.EXTRACTING,
            package_revision_id=revision_id,
            workflow_run_id=run_id,
            stages=stages,
        )
    assert outcome.payload == {"pages": 5}


# ---------------------------------------------------------------------------
# Business truth lives in PostgreSQL
# ---------------------------------------------------------------------------


def test_the_state_events_record_the_run_that_caused_them(
    factory: sessionmaker[Session],
) -> None:
    """#210 added `workflow_run_id` to state events for exactly this: "which run did this?" answerable
    by a join rather than a text search."""
    stages = RecordingStages()
    with unit_of_work(factory) as session:
        revision_id, run_id = _revision_and_run(session)

    run_all(factory, package_revision_id=revision_id, workflow_run_id=run_id, stages=stages)

    with factory() as session:
        caused = [
            event.to_state
            for event in history(session, revision_id)
            if event.workflow_run_id == run_id
        ]
    assert caused == [state.value for _, state in STAGES]


def test_a_failing_stage_records_the_failure_and_re_raises(
    factory: sessionmaker[Session],
) -> None:
    """The failure has to be recorded in its own transaction — the one that raised is rolled back, so a
    write inside it would be discarded along with the thing it was reporting. And the exception must
    reach the engine, which is what decides whether to retry."""

    class FailingStages(RecordingStages):
        def match(self, session: Session, package_revision_id: UUID) -> dict[str, object]:
            del session, package_revision_id
            raise TimeoutError("the matcher took too long")

    stages = FailingStages()
    with unit_of_work(factory) as session:
        revision_id, run_id = _revision_and_run(session)

    with pytest.raises(TimeoutError):
        run_all(factory, package_revision_id=revision_id, workflow_run_id=run_id, stages=stages)

    with factory() as session:
        state = session.execute(
            select(PackageRevision.state).where(PackageRevision.id == revision_id)
        ).scalar_one()
        reasons = [event.reason or "" for event in history(session, revision_id)]

    # A timeout is retryable — #212's table decides that, not this module.
    assert state == PackageState.FAILED_RETRYABLE.value
    assert any("may work on a retry" in reason for reason in reasons)
    assert any("TimeoutError" in reason for reason in reasons)


def test_a_stage_that_fails_leaves_the_earlier_ones_done(
    factory: sessionmaker[Session],
) -> None:
    """One transaction per stage, not one for the walk. A package that matched and then failed at checks
    should keep the matching — the state events are what a dispute reads to see how far it got."""

    class FailsAtChecks(RecordingStages):
        def run_checks(self, session: Session, package_revision_id: UUID) -> dict[str, object]:
            del session, package_revision_id
            raise ValueError("the rulebook is missing")

    with unit_of_work(factory) as session:
        revision_id, run_id = _revision_and_run(session)

    with pytest.raises(ValueError):
        run_all(
            factory,
            package_revision_id=revision_id,
            workflow_run_id=run_id,
            stages=FailsAtChecks(),
        )

    with factory() as session:
        reached = [event.to_state for event in history(session, revision_id)]
        claimed = session.scalar(
            select(func.count()).select_from(TaskRun).where(TaskRun.workflow_run_id == run_id)
        )
    assert PackageState.VALIDATING_EVIDENCE.value in reached, "earlier stages were rolled back"
    # A ValueError is permanent under #212's table.
    assert PackageState.FAILED_PERMANENT.value in reached

    # **Four claims, not five, and that is the correct number.** The failing stage claimed and then
    # raised, so its transaction rolled back and took the claim with it. `workflow/idempotency.py`
    # designed it that way and says why: "a claim for work that was never recorded would block the
    # retry that should redo it." I first asserted five here, reasoning that a surviving claim stops a
    # blind retry repeating the work — which is backwards. Work that never finished *should* be redone;
    # what must not be repeated is work that completed, and those claims are the four that stand.
    assert claimed == 4


def test_nothing_in_the_workflow_writes_state_outside_the_lifecycle() -> None:
    """The guard #209 installed, checked against this module specifically.

    `workflow/` is where a shortcut would be tempting — the worker has a session and a revision id right
    there. It goes through `app/lifecycle/` or the transition table means nothing.
    """
    source = (REPO_ROOT / "workflow" / "review.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            assert name != "PackageStateEvent", "writes a state event directly"
            if name == "values":
                assert not any(
                    keyword.arg == "state" for keyword in node.keywords
                ), "updates the state column directly"
        if isinstance(node, ast.Assign):
            for target in node.targets:
                assert not (
                    isinstance(target, ast.Attribute) and target.attr == "state"
                ), "assigns to .state"


# ---------------------------------------------------------------------------
# Concurrency is configuration, and it starts low
# ---------------------------------------------------------------------------


def test_the_concurrency_defaults_are_low_on_purpose() -> None:
    """One 8 GB VM shares memory between rendering, OCR and PostgreSQL, so the default is one package at
    a time — Anant's call. Asserted because a default quietly raised later is a change nobody reviews.
    """
    settings = Settings(database_url=DATABASE_URL)  # type: ignore[call-arg]
    assert settings.max_concurrent_packages == 1
    assert settings.max_concurrent_page_tasks == 2


def test_the_caps_are_configurable() -> None:
    """The acceptance criterion. They are settings so they can be raised after a measurement."""
    settings = Settings(  # type: ignore[call-arg]
        database_url=DATABASE_URL,
        max_concurrent_packages=3,
        max_concurrent_page_tasks=8,
    )
    assert (settings.max_concurrent_packages, settings.max_concurrent_page_tasks) == (3, 8)


@pytest.mark.parametrize("value", [0, -1])
def test_a_cap_below_one_is_refused(value: int) -> None:
    """A cap of zero is a worker that accepts nothing, which looks exactly like a queue that is stuck."""
    with pytest.raises(Exception, match="greater than or equal to 1"):
        Settings(database_url=DATABASE_URL, max_concurrent_packages=value)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# The engine is not needed to read any of this
# ---------------------------------------------------------------------------


def test_importing_the_worker_module_needs_no_token() -> None:
    """**Why the factories exist.** `Hatchet()` refuses to construct without a token, so a client built
    at import would make this module unimportable wherever the token is absent — every test, and any
    tool that merely wants the stage list."""
    import workflow.hatchet_app as worker

    assert worker.WORKER_NAME == "gv-package-review"
    assert worker.workflow_name() == WORKFLOW_NAME


def test_building_a_client_without_a_token_fails_loudly() -> None:
    """And it should. A worker that cannot reach the engine must fail on the way up, not on the first
    package."""
    from workflow.hatchet_app import hatchet_client

    with pytest.raises(Exception, match="[Tt]oken"):
        hatchet_client(Settings(database_url=DATABASE_URL))  # type: ignore[call-arg]


def test_the_default_stages_say_they_did_nothing(factory: sessionmaker[Session]) -> None:
    """`NoStages` is the default, and it reports `implemented: False` on every stage.

    Deliberate, not a placeholder to forget: a default that returned `{}` would let a package walk the
    whole pipeline and arrive at `AWAITING_REVIEW` looking processed. Silence must never read as
    completion.
    """
    with unit_of_work(factory) as session:
        revision_id, run_id = _revision_and_run(session)

    with unit_of_work(factory) as session:
        outcome = run_stage(
            session,
            stage="ingest",
            state=PackageState.INGESTING,
            package_revision_id=revision_id,
            workflow_run_id=run_id,
            stages=NoStages(),
        )
    assert outcome.payload == {"implemented": False, "stage": "ingest"}


def test_the_workflow_input_carries_only_identifiers() -> None:
    """Anything else here would be business state living in the engine, which is the thing §6 forbids."""
    assert set(PackageReviewInput.model_fields) == {"package_revision_id", "workflow_run_id"}
