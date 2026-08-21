"""The retry and failure policy (#216, C4.4).

Two of these tests are guards rather than behaviour checks, and they are the important ones. §6.3 says OCR
disagreement is *never* auto-resolved and an unknown unit is *never* assumed — properties that hold only
while no code exists to break them, so they are asserted against the source itself. A behaviour test can
only show that today's code returns the right answer; the AST test shows there is no branch that could
return a different one.

Source: backend proposal §9.2–§9.4 · Design: `docs/DESIGN_PLATFORM.md` §6.3 · Verification: this file
"""

from __future__ import annotations

import ast
import random
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.db.session import session_factory, unit_of_work
from app.lifecycle.events import history
from app.lifecycle.side_states import FailureClass
from app.lifecycle.states import MAIN_LINE, begin, transition
from app.models import Package, PackageRevision, PackageState, Project, TaskRun, WorkflowRun
from tests.app.postgres_fixture import alembic_config
from verdict.operands import EvidenceStatus
from workflow.retry import (
    FAILURE_POLICY,
    OCR_RULE,
    PDF_REPAIR_RULE,
    RETRY_POLICY,
    STAGE_RULE,
    UNKNOWN_TASK_RULE,
    Situation,
    claim_pdf_repair,
    delay_for,
    engine_delays,
    engine_retry_settings,
    give_up,
    on_ocr_disagreement,
    policy_delays,
    rule_for,
    should_retry,
    total_delay_bound,
)
from workflow.review import stage_order

pytest_plugins = ("tests.app.postgres_fixture",)

REPO_ROOT = Path(__file__).resolve().parents[2]
RETRY_SOURCE = REPO_ROOT / "workflow" / "retry.py"


@pytest.fixture
def factory(postgres_engine: Engine) -> sessionmaker[Session]:
    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")
    return session_factory(postgres_engine)


def _revision_and_run(
    session: Session, *, processing: PackageState = PackageState.INGESTING
) -> tuple[UUID, UUID]:
    """A package revision part-way through processing, and a run to attribute the work to.

    Walked down the main line rather than dropped straight into a processing state, because #209's table
    will not allow the shortcut — and it is right not to: only a package that is *being* processed can
    fail, and a test that started one in `RUNNING_CHECKS` would be describing a package that never ran.
    """
    project = Project(name=f"retry {uuid4().hex[:8]}")
    session.add(project)
    session.flush()
    package = Package(project_id=project.id, vendor=None)
    session.add(package)
    session.flush()
    revision = PackageRevision(package_id=package.id, revision_number=1, state=PackageState.CREATED)
    session.add(revision)
    session.flush()
    begin(session, revision.id, actor="anant")
    run = WorkflowRun(package_revision_id=revision.id, engine_run_id=f"hatchet-{uuid4().hex}")
    session.add(run)
    session.flush()

    for state in MAIN_LINE[1 : MAIN_LINE.index(processing) + 1]:
        transition(session, revision.id, state, actor="anant", workflow_run_id=run.id)
    return revision.id, run.id


def _function_ast(name: str) -> ast.FunctionDef:
    module = ast.parse(RETRY_SOURCE.read_text())
    for node in ast.walk(module):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} is not in workflow/retry.py")


# ---------------------------------------------------------------------------
# The §6.3 table
# ---------------------------------------------------------------------------


def test_every_situation_the_design_names_has_a_policy() -> None:
    """Four rows in §6.3, four rows here. A missing row is a situation nobody decided."""
    assert set(FAILURE_POLICY) == {
        Situation.MALFORMED_PDF,
        Situation.OCR_DISAGREEMENT,
        Situation.UNKNOWN_UNIT,
        Situation.TRANSIENT_FAILURE,
    }


def test_only_a_transient_failure_is_ever_retried() -> None:
    """The other three are not slow paths, they are decisions. Retrying a malformed PDF, an OCR
    disagreement or an unknown unit would produce the same answer, or worse, a different one."""
    retried = {situation for situation, policy in FAILURE_POLICY.items() if policy.retried}
    assert retried == {Situation.TRANSIENT_FAILURE}


def test_every_policy_records_what_was_rejected_and_who_decided() -> None:
    """The reason has to travel with the rule. Each of these rows contradicts an ordinary engineering
    instinct — retry it, prefer the better reading, assume millimetres — and a rule without its reason
    gets 'simplified' back to the instinct."""
    for situation, policy in FAILURE_POLICY.items():
        assert policy.what_happens.strip(), situation
        assert policy.rejected_alternative.strip(), situation
        assert policy.decided_by.strip(), situation


# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------


def test_every_pipeline_stage_has_a_budget() -> None:
    """No stage falls through to the unknown-task rule by accident."""
    for stage in stage_order():
        assert stage in RETRY_POLICY, f"{stage} has no retry budget"
        assert RETRY_POLICY[stage] == STAGE_RULE


def test_the_chosen_numbers_are_the_ones_that_were_chosen() -> None:
    """Pinned deliberately. These are decisions, not defaults, so a change should have to edit a test
    that says whose decision it was.

    Stages: 3 attempts, 2s base, 120s cap — Anant on #216.
    OCR: 2 attempts — already decided in `docs/DESIGN_AI.md` line 50, not re-decided by this story.
    PDF repair: exactly 1 — this issue's Scope.
    """
    assert STAGE_RULE.max_attempts == 3
    assert STAGE_RULE.base_delay_s == 2.0
    assert STAGE_RULE.max_delay_s == 120.0
    assert OCR_RULE.max_attempts == 2
    assert PDF_REPAIR_RULE.max_attempts == 1


def test_an_unrecognised_task_type_gets_one_attempt() -> None:
    """The safe direction, and the same one `side_states.classify` takes for an exception it has no entry
    for. A task nobody wrote a budget for should fail visibly rather than quietly inherit retries.
    """
    assert rule_for("a_task_nobody_wrote_a_policy_for") == UNKNOWN_TASK_RULE
    assert UNKNOWN_TASK_RULE.max_attempts == 1


# ---------------------------------------------------------------------------
# should_retry
# ---------------------------------------------------------------------------


def test_a_permanent_failure_is_never_retried_whatever_the_budget() -> None:
    """Every task type, every attempt inside the budget. Repeating a `ValueError` produces the same
    `ValueError` more slowly, and #212 deliberately classes an *unrecognised* exception as permanent —
    so a failure this system does not understand is not retried either."""
    for task_type in (*RETRY_POLICY, "something_unrecognised"):
        for attempt in (1, 2, 3):
            assert not should_retry(
                task_type, attempt=attempt, failure_class=FailureClass.PERMANENT
            ), task_type


def test_a_transient_failure_is_retried_until_the_budget_is_spent() -> None:
    assert should_retry("ingest", attempt=1, failure_class=FailureClass.RETRYABLE)
    assert should_retry("ingest", attempt=2, failure_class=FailureClass.RETRYABLE)
    assert not should_retry("ingest", attempt=3, failure_class=FailureClass.RETRYABLE)
    # OCR's smaller budget is spent a step earlier.
    assert should_retry("ocr", attempt=1, failure_class=FailureClass.RETRYABLE)
    assert not should_retry("ocr", attempt=2, failure_class=FailureClass.RETRYABLE)
    # One attempt means never again.
    assert not should_retry("pdf_repair", attempt=1, failure_class=FailureClass.RETRYABLE)


def test_attempt_numbering_is_one_based_and_says_so() -> None:
    """An off-by-one here silently doubles or halves every budget, so a zero is refused rather than
    interpreted."""
    with pytest.raises(ValueError, match="1-based"):
        should_retry("ingest", attempt=0, failure_class=FailureClass.RETRYABLE)
    with pytest.raises(ValueError, match="1-based"):
        delay_for("ingest", attempt=0)


# ---------------------------------------------------------------------------
# Backoff: bounded, and jittered
# ---------------------------------------------------------------------------


def test_the_backoff_grows_then_stops_at_the_cap() -> None:
    """2, 4, 8, 16 … and then flat. Without the cap, attempt 20 would sleep for a fortnight."""
    at_full_jitter = [delay_for("ingest", attempt=n, jitter_fraction=1.0) for n in range(1, 9)]
    assert at_full_jitter[:6] == [2.0, 4.0, 8.0, 16.0, 32.0, 64.0]
    assert at_full_jitter[6] == 120.0, "the cap"
    assert at_full_jitter[7] == 120.0, "and it stays there"


def test_the_delay_is_jittered_but_never_collapses_to_nothing() -> None:
    """**Equal jitter, and this is the test that says why.** The delay stays within `[cap/2, cap]`.

    Full jitter — a uniform draw from `[0, cap]` — can return almost zero, which throws the backoff away
    exactly when the struggling dependency most needs the gap. Asserting the lower bound is what stops
    somebody 'simplifying' it to `random.uniform(0, capped)`.
    """
    rng = random.Random(20260821)
    seen: set[float] = set()
    for _ in range(400):
        delay = delay_for("ingest", attempt=3, jitter_fraction=rng.random())
        assert 4.0 <= delay <= 8.0, "equal jitter keeps the delay in the upper half of the window"
        seen.add(delay)
    assert len(seen) > 100, "a jitter that returns the same number is not jitter"


def test_a_jitter_fraction_outside_zero_to_one_is_refused() -> None:
    for bad in (-0.1, 1.1):
        with pytest.raises(ValueError, match="proportion"):
            delay_for("ingest", attempt=1, jitter_fraction=bad)


def test_the_total_wait_is_bounded_for_every_budget() -> None:
    """ "Bounded" as a number somebody can check, not an adjective.

    Three attempts at a 2s base waits at most 2 + 4 = 6 seconds, because the final attempt is not
    followed by a wait.
    """
    assert total_delay_bound("ingest") == 6.0
    assert total_delay_bound("ocr") == 2.0
    assert total_delay_bound("pdf_repair") == 0.0
    for task_type in RETRY_POLICY:
        bound = total_delay_bound(task_type)
        assert 0.0 <= bound < 600.0, f"{task_type} could wait {bound}s in total"


def test_a_rule_with_no_delay_waits_no_time() -> None:
    """PDF repair never waits, because there is never a second attempt to wait for."""
    assert delay_for("pdf_repair", attempt=1) == 0.0


def test_the_engine_gets_the_budget_the_policy_describes() -> None:
    """The policy reaches Hatchet, because Hatchet is what retries.

    Before this mapping existed, `should_retry` and `delay_for` had no caller at all: `run_all` neither
    sleeps nor re-runs a stage, so retrying was already the engine's job and the engine had never been
    told these numbers. The table read like a control while controlling nothing.
    """
    for stage in stage_order():
        settings = engine_retry_settings(stage)
        # Hatchet counts retries, we count attempts.
        assert settings.retries == STAGE_RULE.max_attempts - 1 == 2
        assert settings.backoff_max_seconds == int(STAGE_RULE.max_delay_s) == 120
        # Asserted independently of `engine_delays`, which reads `RetryRule` directly — otherwise a
        # wrongly mapped factor would agree with itself and fail nothing.
        assert settings.backoff_factor == STAGE_RULE.base_delay_s == 2.0

    assert engine_retry_settings("ocr").retries == 1, "OCR's smaller budget survives the mapping"
    assert engine_retry_settings("ocr").backoff_factor == OCR_RULE.base_delay_s == 2.0
    assert engine_retry_settings("pdf_repair").retries == 0, "one attempt means no retries"
    assert engine_retry_settings("pdf_repair").backoff_factor == 0.0


def test_the_engine_and_the_policy_compute_the_same_waits() -> None:
    """**The trap this test exists for.**

    `base_delay_s` is mapped onto Hatchet's `backoff_factor`, and those are not the same kind of number.
    Hatchet's documented sequence for `backoffFactor: 2` is 2s, 4s, 8s — the first wait *is* the factor,
    then it multiplies. `delay_for` computes `base * 2 ** (attempt - 1)`. At a base of 2 both give 2, 4, 8
    and the mapping looks obviously correct. At a base of 3 the engine waits 3, 9, 27 while this module
    documents 3, 6, 12.

    So the agreement is an arithmetic accident of the chosen value, not a property of the mapping. If
    somebody changes `base_delay_s`, this fails — which is the whole point, because the alternative is an
    engine quietly waiting three times longer than the policy says it does.
    """
    for task_type in RETRY_POLICY:
        assert engine_delays(task_type) == policy_delays(task_type), (
            f"{task_type}: the engine would wait {engine_delays(task_type)} while this module's policy "
            f"says {policy_delays(task_type)}. base_delay_s is a factor to Hatchet and a base here; they "
            "only agree at 2."
        )


# ---------------------------------------------------------------------------
# OCR disagreement: the guard that matters
# ---------------------------------------------------------------------------


def test_ocr_disagreement_is_conflicting_whatever_it_is_given() -> None:
    """Nothing about the readings changes the answer, because nothing about them is read."""
    assert on_ocr_disagreement([]) is EvidenceStatus.CONFLICTING
    assert on_ocr_disagreement([object()]) is EvidenceStatus.CONFLICTING  # type: ignore[list-item]
    assert on_ocr_disagreement([object()] * 5) is EvidenceStatus.CONFLICTING  # type: ignore[list-item]


def test_there_is_no_code_that_could_pick_a_winning_reading() -> None:
    """**The acceptance criterion, proved against the source rather than the behaviour.**

    `AGENTS.md` §2.3 and backend §6.2: a disagreement is never resolved by preference. A behaviour test
    shows today's answer is right. This shows there is no branch that could give a different one — the
    body is a single return of one constant, and the readings parameter never appears in it.

    If someone later adds "prefer the higher-confidence reading", every assertion here fails.
    """
    function = _function_ast("on_ocr_disagreement")

    body = [node for node in function.body if not isinstance(node, ast.Expr)]  # drop the docstring
    assert len(body) == 1, "more than one statement means there is something to branch on"
    returned = body[0]
    assert isinstance(returned, ast.Return)
    assert isinstance(returned.value, ast.Attribute)
    assert returned.value.attr == "CONFLICTING"

    # No decision-making syntax anywhere in the function.
    for node in ast.walk(function):
        assert not isinstance(
            node, (ast.If, ast.IfExp, ast.Compare, ast.For, ast.While, ast.BoolOp, ast.Match)
        ), f"{type(node).__name__} in on_ocr_disagreement is a way to pick a winner"

    # And the readings are not read: the parameter name appears nowhere in the body.
    parameter = function.args.args[0].arg
    used = {node.id for node in ast.walk(function) if isinstance(node, ast.Name)}
    assert parameter not in used, f"{parameter} is inspected, so a winner could be chosen from it"


def test_this_module_never_looks_at_confidence() -> None:
    """§2.3, at module scope. Confidence is the tempting way to resolve a disagreement, so its absence is
    asserted rather than assumed — including in the retry paths, where "retry the more confident reader"
    would be just as wrong."""
    identifiers = {
        node.attr if isinstance(node, ast.Attribute) else node.id
        for node in ast.walk(ast.parse(RETRY_SOURCE.read_text()))
        if isinstance(node, (ast.Name, ast.Attribute))
    }
    assert not {name for name in identifiers if "confidence" in name.lower()}


# ---------------------------------------------------------------------------
# Unknown unit: routed, not re-implemented
# ---------------------------------------------------------------------------


def test_the_unknown_unit_policy_points_at_the_module_that_owns_it() -> None:
    """ADR-0001's refusal lives in `evidence/gate.py`, and this story's plan says not to re-implement it.
    So the table names the owner, and the owner really does refuse — `RefusalReason.UNKNOWN_UNIT` exists
    and `tests/evidence/test_gate.py` is where its behaviour is proved."""
    from evidence.gate import RefusalReason

    policy = FAILURE_POLICY[Situation.UNKNOWN_UNIT]
    assert "evidence/gate.py" in policy.decided_by
    assert "ADR-0001" in policy.decided_by
    assert RefusalReason.UNKNOWN_UNIT is not None
    assert not policy.retried, "an unknown unit is a decision, not a transient failure"


def test_this_module_adds_no_unit_handling_of_its_own() -> None:
    """The way this criterion actually gets broken is not a wrong answer, it is a well-meaning shortcut:
    a `units` import and a "default to mm" that never reaches review. There is no such import."""
    imported: set[str] = set()
    for node in ast.walk(ast.parse(RETRY_SOURCE.read_text())):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not {
        name for name in imported if name.split(".")[0] == "units"
    }, "workflow/retry.py must route unknown units to evidence/gate.py, not handle them"


# ---------------------------------------------------------------------------
# PDF repair: once, and recorded
# ---------------------------------------------------------------------------


def test_a_pdf_is_repaired_at_most_once_ever(factory: sessionmaker[Session]) -> None:
    """The hard cap, and it is the database that enforces it.

    A counter in Python would be correct until two workers ran the repair at the same moment. The claim
    takes a unique idempotency key, so the second attempt loses whether it is a retry, a second worker or
    a restart.
    """
    with unit_of_work(factory) as session:
        revision_id, run_id = _revision_and_run(session)
        document_id = uuid4()

        first = claim_pdf_repair(
            session,
            package_revision_id=revision_id,
            document_id=document_id,
            workflow_run_id=run_id,
        )
        second = claim_pdf_repair(
            session,
            package_revision_id=revision_id,
            document_id=document_id,
            workflow_run_id=run_id,
        )

    assert first is True, "the first attempt is allowed"
    assert second is False, "and there is never a second"


def test_the_repair_attempt_is_recorded(factory: sessionmaker[Session]) -> None:
    """ "Recorded" is the half of the requirement that a reviewer depends on: it answers "was this file
    modified before anything was read from it?"."""
    with unit_of_work(factory) as session:
        revision_id, run_id = _revision_and_run(session)
        claim_pdf_repair(
            session,
            package_revision_id=revision_id,
            document_id=uuid4(),
            workflow_run_id=run_id,
        )

    with unit_of_work(factory) as session:
        rows = list(
            session.execute(
                select(TaskRun).where(
                    TaskRun.workflow_run_id == run_id, TaskRun.task_type == "pdf_repair"
                )
            ).scalars()
        )
    assert len(rows) == 1
    assert rows[0].attempt >= 1


def test_an_engine_upgrade_does_not_re_open_the_repair(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """**A regression test for a real defect, found in review of #216.**

    The repair key first used `ENGINE_VERSION`. That is right for a stage — new code is a different task,
    not a cache hit — and exactly wrong here: bumping the engine version produced a different key, the
    claim succeeded, and the same file was repaired again. Confirmed against the database before fixing:

        repair #1 at engine 1.0.0 : True
        repair #2 at engine 1.0.0 : False
        repair #3 at engine 1.1.0 : True   <- the leak

    "At most once per engine version" is not a cap on modifying a source document; it is a cap that resets
    whenever this code changes, which is precisely when nobody is thinking about it.
    """
    with unit_of_work(factory) as session:
        revision_id, run_id = _revision_and_run(session)
        document_id = uuid4()
        assert claim_pdf_repair(
            session,
            package_revision_id=revision_id,
            document_id=document_id,
            workflow_run_id=run_id,
        )

    # Somebody ships new engine code. The repair must still be spent.
    monkeypatch.setattr("workflow.review.ENGINE_VERSION", "9.9.9", raising=False)
    monkeypatch.setattr("workflow.retry.ENGINE_VERSION", "9.9.9", raising=False)

    with unit_of_work(factory) as session:
        again = claim_pdf_repair(
            session,
            package_revision_id=revision_id,
            document_id=document_id,
            workflow_run_id=run_id,
        )
    assert again is False, "an engine upgrade must not hand out a second repair of the same file"


def test_the_repair_cap_does_not_depend_on_the_engine_version() -> None:
    """The same property at the source, because the behaviour test above can only cover the versions it
    thinks to try. `claim_pdf_repair` must not mention `ENGINE_VERSION` at all."""
    function = _function_ast("claim_pdf_repair")
    names = {
        node.id if isinstance(node, ast.Name) else node.attr
        for node in ast.walk(function)
        if isinstance(node, (ast.Name, ast.Attribute))
    }
    assert (
        "ENGINE_VERSION" not in names
    ), "the repair cap must be absolute, so its key cannot be versioned by the engine"
    assert "REPAIR_CAP_VERSION" in names


def test_two_documents_each_get_their_own_single_repair(factory: sessionmaker[Session]) -> None:
    """The cap is per document, not per package. One broken file in a package of twenty must not spend
    the other nineteen files' only attempt."""
    with unit_of_work(factory) as session:
        revision_id, run_id = _revision_and_run(session)
        one, two = uuid4(), uuid4()
        assert claim_pdf_repair(
            session, package_revision_id=revision_id, document_id=one, workflow_run_id=run_id
        )
        assert claim_pdf_repair(
            session, package_revision_id=revision_id, document_id=two, workflow_run_id=run_id
        )
        assert not claim_pdf_repair(
            session, package_revision_id=revision_id, document_id=one, workflow_run_id=run_id
        )


# ---------------------------------------------------------------------------
# Exhaustion is an outcome, not a silence
# ---------------------------------------------------------------------------


def test_running_out_of_retries_is_recorded_as_a_state_change(
    factory: sessionmaker[Session],
) -> None:
    """§2.2: silence must never read as completion. Exhaustion transitions the package and says how many
    attempts it took, so nobody has to read the logs to find out."""
    with unit_of_work(factory) as session:
        revision_id, _ = _revision_and_run(session, processing=PackageState.RUNNING_CHECKS)
        event = give_up(
            session,
            revision_id,
            task_type="run_checks",
            attempts=3,
            actor="the run checks worker",
        )
        assert event.to_state == PackageState.FAILED_RETRYABLE
        assert event.reason is not None
        assert "3 attempts" in event.reason
        assert "run_checks" in event.reason

    with unit_of_work(factory) as session:
        recorded = history(session, revision_id)
    assert recorded[-1].to_state == PackageState.FAILED_RETRYABLE


def test_exhaustion_stays_labelled_as_something_a_retry_might_fix(
    factory: sessionmaker[Session],
) -> None:
    """Anant's call on #216. The cause was transient, so calling it permanent would tell a reviewer that a
    database blip cannot be recovered from — which is false, and would stop somebody retrying a package
    that would then succeed."""
    with unit_of_work(factory) as session:
        revision_id, _ = _revision_and_run(session)
        event = give_up(
            session, revision_id, task_type="ingest", attempts=3, actor="the ingest worker"
        )
        # `==`, not `is`: the ORM hands the column back as a plain string, and `PackageState` is a
        # StrEnum, so identity fails where equality is correct.
        assert event.to_state == PackageState.FAILED_RETRYABLE
        assert event.to_state != PackageState.FAILED_PERMANENT


def test_one_attempt_reads_as_one_attempt(factory: sessionmaker[Session]) -> None:
    """Reviewer-facing text, so it should not say "1 attempts"."""
    with unit_of_work(factory) as session:
        revision_id, _ = _revision_and_run(session)
        event = give_up(
            session, revision_id, task_type="pdf_repair", attempts=1, actor="the repair worker"
        )
        assert event.reason is not None
        assert "1 attempt at" in event.reason


def test_a_task_that_never_ran_cannot_have_exhausted_anything(
    factory: sessionmaker[Session],
) -> None:
    with unit_of_work(factory) as session:
        revision_id, _ = _revision_and_run(session)
        with pytest.raises(ValueError, match="never ran"):
            give_up(session, revision_id, task_type="ingest", attempts=0, actor="anant")
