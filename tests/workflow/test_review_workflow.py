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
from collections.abc import Callable, Mapping, Sequence
from itertools import pairwise
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.config import Settings
from app.db.session import session_factory, unit_of_work
from app.lifecycle.events import history
from app.lifecycle.states import begin
from app.models import Package, PackageRevision, PackageState, Project, TaskRun, WorkflowRun
from tests.app.postgres_fixture import alembic_config
from workflow.idempotency import stage_idempotency_key
from workflow.review import (
    ENGINE_VERSION,
    STAGES,
    WORKFLOW_NAME,
    NoStages,
    PackageReviewInput,
    PageResult,
    _record_failure,
    join_pages,
    register,
    run_all,
    run_stage,
    stage_order,
)

pytest_plugins = ("tests.app.postgres_fixture",)

REPO_ROOT = Path(__file__).resolve().parents[2]
DATABASE_URL = "postgresql+psycopg://gv:gv@localhost:5433/gv"


@pytest.fixture
def factory(postgres_engine: Engine) -> sessionmaker[Session]:
    # Resolved from REPO_ROOT, not the working directory: `Config("alembic.ini")` only finds the file
    # when pytest happens to be run from the repository root. Several older test modules still do that.
    config = alembic_config()
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


class RecordingClient:
    """Enough of a Hatchet client for `register` to build its graph against, and nothing more.

    The point is the boundary, not the engine. `register`'s body is the one part of this story no other
    test reaches — it is the wiring between our stage table and the SDK — so without something here, a
    wrong keyword or a mis-shaped argument would first show up at worker startup on a real box. A stub
    that records what it was handed turns that into a test failure.
    """

    def __init__(self) -> None:
        self.workflow_kwargs: dict[str, object] = {}
        self.tasks: list[dict[str, object]] = []
        self.functions: dict[str, object] = {}

    def workflow(self, **kwargs: object) -> RecordingClient:
        self.workflow_kwargs = kwargs
        return self

    def task(self, **kwargs: object) -> Callable[[object], object]:
        self.tasks.append(kwargs)

        def decorate(fn: object) -> object:
            # Kept, so a test can call the registered function and see what the engine would see. The
            # bodies of these tasks are the one part of the story no other test reaches.
            self.functions[str(kwargs.get("name"))] = fn
            return fn

        return decorate


def test_registering_the_graph_passes_the_arguments_the_sdk_expects(
    factory: sessionmaker[Session],
) -> None:
    """The wiring, checked without an engine.

    `concurrency` is deliberately a plain `int`. The SDK's own signature is
    `int | ConcurrencyExpression | list[ConcurrencyExpression] | None`, and it converts an int itself —
    `ConcurrencyExpression.from_int(...)` in `hatchet_sdk/runnables/workflow.py` — so building the
    expression by hand here would only duplicate what the SDK documents it does with the integer.
    """
    client = RecordingClient()
    register(
        client,  # type: ignore[arg-type]
        factory=factory,
        max_concurrent_packages=1,
    )

    assert client.workflow_kwargs["name"] == WORKFLOW_NAME
    assert client.workflow_kwargs["input_validator"] is PackageReviewInput
    assert client.workflow_kwargs["concurrency"] == 1

    # Six stages, in pipeline order, each depending on the one before it and the first on nothing.
    assert [task["name"] for task in client.tasks] == list(stage_order())

    # **Every stage carries its retry budget to the engine.** Hatchet is what retries — `run_all` neither
    # sleeps nor re-runs a stage — so a stage registered without these settings gets the SDK's defaults,
    # which are zero retries. That is a policy stated in `workflow/retry.py` and not applied anywhere,
    # which is the shape of failure this repository keeps finding.
    from workflow.retry import engine_retry_settings

    for task in client.tasks:
        expected = engine_retry_settings(str(task["name"]))
        for setting, value in expected._asdict().items():
            # Presence asserted before equality, so a stage registered with no budget at all reports that
            # rather than dying on a KeyError that names nothing.
            assert setting in task, (
                f"{task['name']} was registered without {setting}, so it falls back to the SDK default "
                "and the policy in workflow/retry.py applies to nothing"
            )
            assert task[setting] == value, f"{task['name']} lost {setting}"
        assert task["retries"] == 2, "three attempts is two retries"
        assert task["backoff_factor"] == 2.0, "the base delay reaches the engine as its factor"
    assert client.tasks[0]["parents"] is None, "the first stage waits for nothing"
    for task in client.tasks[1:]:
        parents = task["parents"]
        assert (
            isinstance(parents, list) and len(parents) == 1
        ), "each later stage depends on exactly its predecessor, which is what makes the walk a line"


class Boom:
    """Stages that fail on demand. Every method spelled out, and that is the point.

    The first version used `__getattr__` to answer for all six, which meant mypy could not check any of
    them — and that is exactly how a `dict` came to be returned where `extract_pages` must return a
    sequence of `PageResult`, reporting a page that was never read. Written out, the protocol is checked
    and the instance needs no `type: ignore` to be accepted.
    """

    def __init__(self, error: Exception, *, at: str = "ingest") -> None:
        self.error = error
        self.at = at
        self.calls: list[str] = []

    def _run(self, name: str) -> dict[str, object]:
        self.calls.append(name)
        if name == self.at:
            raise self.error
        return {"ran": name}

    def ingest(self, session: Session, package_revision_id: UUID) -> Mapping[str, object]:
        del session, package_revision_id
        return self._run("ingest")

    def extract_pages(self, session: Session, package_revision_id: UUID) -> Sequence[PageResult]:
        del session, package_revision_id
        self._run("extract_pages")
        return ()

    def match(self, session: Session, package_revision_id: UUID) -> Mapping[str, object]:
        del session, package_revision_id
        return self._run("match")

    def validate_evidence(
        self, session: Session, package_revision_id: UUID
    ) -> Mapping[str, object]:
        del session, package_revision_id
        return self._run("validate_evidence")

    def run_checks(self, session: Session, package_revision_id: UUID) -> Mapping[str, object]:
        del session, package_revision_id
        return self._run("run_checks")

    def generate_outputs(self, session: Session, package_revision_id: UUID) -> Mapping[str, object]:
        del session, package_revision_id
        return self._run("generate_outputs")


def test_a_stage_runs_inside_its_own_state(factory: sessionmaker[Session]) -> None:
    """**The frozen-set boundary, which is why the transition moved to the front of `run_stage`.**

    `app/lifecycle/states.py` puts `UPLOADED` in `ASSEMBLY_STATES`, where a revision's document set may
    still change, and names the boundary: "`INGESTING` is the first state in which something has read the
    set, and a set that can change after it has been read is a set nobody can be held to" (ADR-0018).

    Under the original work-then-move order, `ingest` read the set while the package still said `UPLOADED`
    — before the set was frozen. This asserts the fix from inside the stage itself: when the work runs, the
    package is already in the stage's state.
    """
    seen: dict[str, str] = {}

    class Watching:
        """Records the state each stage sees. Spelled out so the protocol is actually checked."""

        def _see(self, session: Session, package_revision_id: UUID, name: str) -> None:
            revision = session.get(PackageRevision, package_revision_id)
            assert revision is not None
            seen[name] = str(revision.state)

        def ingest(self, session: Session, package_revision_id: UUID) -> Mapping[str, object]:
            self._see(session, package_revision_id, "ingest")
            return {}

        def extract_pages(
            self, session: Session, package_revision_id: UUID
        ) -> Sequence[PageResult]:
            self._see(session, package_revision_id, "extract_pages")
            return ()

        def match(self, session: Session, package_revision_id: UUID) -> Mapping[str, object]:
            self._see(session, package_revision_id, "match")
            return {}

        def validate_evidence(
            self, session: Session, package_revision_id: UUID
        ) -> Mapping[str, object]:
            self._see(session, package_revision_id, "validate_evidence")
            return {}

        def run_checks(self, session: Session, package_revision_id: UUID) -> Mapping[str, object]:
            self._see(session, package_revision_id, "run_checks")
            return {}

        def generate_outputs(
            self, session: Session, package_revision_id: UUID
        ) -> Mapping[str, object]:
            self._see(session, package_revision_id, "generate_outputs")
            return {}

    with unit_of_work(factory) as session:
        revision_id, run_id = _revision_and_run(session)

    run_all(
        factory,
        package_revision_id=revision_id,
        workflow_run_id=run_id,
        stages=Watching(),
    )

    for stage, state in STAGES:
        assert seen[stage] == state.value, (
            f"{stage} ran while the package said {seen[stage]}, not {state.value}. Work must happen "
            "inside its own state, or INGESTING does not mean the document set has been read."
        )
    assert seen["ingest"] != PackageState.UPLOADED.value, "ingest must not read an unfrozen set"


def test_a_permanent_failure_is_not_handed_back_for_retry(
    factory: sessionmaker[Session],
) -> None:
    """**A bug this PR introduced, then fixed.**

    Giving the engine a retry budget without this check made things worse than the zero-retry default it
    replaced: Hatchet retries any exception, so a `ValueError` — which #212 classifies as PERMANENT — would
    have been re-run twice with backoff. `workflow/retry.py`'s `should_retry` says never to retry a
    permanent failure, and the task body is not `run_all`, so nothing was consulting it.

    The task now raises `NonRetryableException`, which is how the engine is told not to try again.
    """
    from hatchet_sdk.exceptions import NonRetryableException

    with unit_of_work(factory) as session:
        revision_id, run_id = _revision_and_run(session)

    stages = Boom(ValueError("the rulebook is missing"), at="run_checks")
    client = RecordingClient()
    register(client, factory=factory, stages=stages, max_concurrent_packages=1)  # type: ignore[arg-type]  # the client is a stub, not the stages

    payload = PackageReviewInput(package_revision_id=revision_id, workflow_run_id=run_id)
    # Walk the earlier stages so the package is processing when the failing one runs.
    for stage in stage_order():
        function = client.functions[stage]
        assert callable(function)
        if stage != "run_checks":
            function(payload, None)
            continue
        with pytest.raises(NonRetryableException):
            function(payload, None)
        break

    assert stages.calls.count("run_checks") == 1, "it must not be retried in-process either"

    with unit_of_work(factory) as session:
        reached = [event.to_state for event in history(session, revision_id)]
    assert PackageState.FAILED_PERMANENT.value in [str(state) for state in reached]


def test_a_retryable_failure_is_handed_back_for_retry(factory: sessionmaker[Session]) -> None:
    """The other branch, which had no test at all.

    Both failure tests drove a `ValueError`, and #212 classes that PERMANENT — so the bare `raise` that
    lets a *retryable* failure reach the engine was never executed. That branch is the whole point of
    giving Hatchet a retry budget: without it the budget applies to nothing reachable.

    `TimeoutError` is RETRYABLE in `app/lifecycle/side_states.py`. What must happen is the opposite of the
    permanent case — the original exception escapes so the engine sees a normal failure and retries it, and
    `NonRetryableException` is not raised.
    """
    from hatchet_sdk.exceptions import NonRetryableException

    with unit_of_work(factory) as session:
        revision_id, run_id = _revision_and_run(session)

    stages = Boom(TimeoutError("the renderer stopped answering"), at="run_checks")
    client = RecordingClient()
    register(client, factory=factory, stages=stages, max_concurrent_packages=1)  # type: ignore[arg-type]  # the client is a stub, not the stages

    payload = PackageReviewInput(package_revision_id=revision_id, workflow_run_id=run_id)
    for stage in stage_order():
        function = client.functions[stage]
        assert callable(function)
        if stage != "run_checks":
            function(payload, None)
            continue
        with pytest.raises(TimeoutError) as caught:
            function(payload, None)
        break

    assert not isinstance(
        caught.value, NonRetryableException
    ), "a transient failure must reach the engine as an ordinary exception, or the retry budget is unused"
    with unit_of_work(factory) as session:
        reached = [str(event.to_state) for event in history(session, revision_id)]
    assert PackageState.FAILED_RETRYABLE.value in reached
    assert PackageState.FAILED_PERMANENT.value not in reached


def test_the_page_join_refuses_anything_that_is_not_a_page(factory: sessionmaker[Session]) -> None:
    """A wrong return type from `extract_pages` must fail, not become a page count.

    This is the bug a review found in this file's own stub, and the reason it is worth fixing in
    `join_pages` rather than only in the stub: iterating a mapping yields keys, a string has an `.index`
    attribute that is hashable, and a single key is never compared — so `{"ran": "x"}` produced
    `{"pages": 1}` for zero pages read. Every real `extract_pages` is still unbuilt, so this is exactly
    when a wrong shape gets written.
    """
    del factory
    with pytest.raises(TypeError, match="never read"):
        join_pages({"ran": "extract_pages"})  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="PageResult"):
        join_pages([{"index": 0}])  # type: ignore[list-item]
    # The right shape still works.
    assert join_pages((PageResult(index=1, payload={}), PageResult(index=0, payload={}))) == (
        PageResult(index=0, payload={}),
        PageResult(index=1, payload={}),
    )


def test_a_first_stage_failure_is_recorded_even_though_the_move_rolled_back(
    factory: sessionmaker[Session],
) -> None:
    """**The gap Anant's ruling was meant to close, closed.**

    `run_stage` now moves the package into the stage's state before doing the work — but that move shares
    the transaction with the work, so a failure rolls it back. Measured: after a failing `ingest` the
    revision reads `UPLOADED` again, and `UPLOADED` has no edge to a failure state. The reorder alone left
    a first-stage failure with nowhere to go.

    `_record_failure` now re-states the stage's state inside the recording transaction and fails from
    there. The attempt really did happen in `INGESTING`; the rollback erased the record of it, not the
    fact. So a package that dies in ingestion says so, instead of sitting in an assembly state with no
    explanation.
    """
    from hatchet_sdk.exceptions import NonRetryableException

    with unit_of_work(factory) as session:
        revision_id, run_id = _revision_and_run(session)

    stages = Boom(ValueError("the rulebook is missing"), at="ingest")
    client = RecordingClient()
    register(client, factory=factory, stages=stages, max_concurrent_packages=1)  # type: ignore[arg-type]  # the client is a stub, not the stages

    function = client.functions["ingest"]
    assert callable(function)
    payload = PackageReviewInput(package_revision_id=revision_id, workflow_run_id=run_id)

    with pytest.raises(NonRetryableException) as caught:
        function(payload, None)

    assert "the rulebook is missing" in str(caught.value), "the real error still reaches the caller"
    assert not getattr(
        caught.value, "__notes__", []
    ), "nothing to note — the failure was recordable"

    with unit_of_work(factory) as session:
        reached = [str(event.to_state) for event in history(session, revision_id)]
    assert PackageState.INGESTING.value in reached, "the attempt is recorded as having happened"
    assert reached[-1] == PackageState.FAILED_PERMANENT.value


def test_a_failure_that_truly_cannot_be_recorded_still_reports_itself(
    factory: sessionmaker[Session],
) -> None:
    """The defence that stays, for what the walk above cannot rescue.

    Recording can still fail — an unknown revision, a database problem — and when it does, the exception it
    raises must not replace the one it was reporting. That is what used to happen: the caller saw a
    state-machine complaint and no sign of the error that actually stopped the package.
    """
    error = ValueError("the rulebook is missing")
    _record_failure(
        factory,
        uuid4(),  # no such revision, so recording cannot succeed
        actor="the ingest worker",
        error=error,
        stage_state=PackageState.INGESTING,
    )

    notes = getattr(error, "__notes__", [])
    assert any(
        "could not be recorded" in note for note in notes
    ), "the caller should be told the failure state was not written, not left to guess"
    assert "the rulebook is missing" in str(error), "and the original error is untouched"


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
