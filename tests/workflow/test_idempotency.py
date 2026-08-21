"""Idempotency keys and the constraint that enforces them (#214).

Four claims are made by `workflow/idempotency.py`, and each one has a section below.

* **The key is stable across processes and restarts.** Tested the only way that means anything:
  by computing it in fresh interpreters under different `PYTHONHASHSEED` values and comparing
  against a digest written into this file. An in-process comparison would pass even if the key were
  built from the salted built-in `hash()`, which is exactly the defect that would make every stored
  key stop matching after a restart — and it would show up as work being silently repeated, not as
  an error.
* **The key is the identity of the task**, so the extractor version and the config are in it, dict
  ordering is not, and no component can impersonate another.
* **Uniqueness is the database's, not this module's.** The negative tests read the constraint name
  PostgreSQL reported, because `pytest.raises(IntegrityError)` accepts whichever constraint fired
  first and a row usually violates more than one thing. A test that passes for the wrong reason is
  worse than no test: it also stops anybody looking again.
* **A retry is a no-op that returns the prior result**, including when the retry is a second process
  racing the first. The race is run for real with two connections, not simulated.

The database tests run against the migrated schema — `alembic upgrade head` — rather than
`Base.metadata.create_all`. The constraint that matters is the one deployed by
`alembic/versions/0005_run_records.py`, and only the migrated schema shows what production actually
installed, including the constraint's real name.

Nothing here builds a `CanonicalObservation`. `check_canonical_observation_provenance()` in
`0006_evidence_plane.py` is `DEFERRABLE INITIALLY DEFERRED` and fires at `COMMIT`, so a fixture that
touches one can pass a flush and fail later; a task run needs none, and this stays out of it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.db.session import session_factory, unit_of_work
from app.models import (
    Package,
    PackageRevision,
    PackageState,
    Project,
    TaskRun,
    WorkflowRun,
)
from tests.app.postgres_fixture import alembic_config
from workflow.idempotency import (
    CLAIMED,
    Claim,
    _is_duplicate_idempotency_key,
    claim,
    idempotency_key,
)

pytest_plugins = ("tests.app.postgres_fixture",)

REPO_ROOT = Path(__file__).resolve().parents[2]

DOCUMENT_VERSION_ID = UUID("11111111-2222-3333-4444-555555555555")

#: The exact key for the inputs in `_reference_key()`. Written down rather than recomputed, because
#: a test that compares the function to itself cannot notice the serialisation changing. If a change
#: to `workflow/idempotency.py` moves this digest, every key already stored in `task_runs` has
#: stopped matching and every claimed task would be claimed again — so this constant is a decision
#: to be made deliberately, not a number to update until the test goes green.
REFERENCE_KEY = "sha256:d94518773bf82174f3644ec846ddbb1b89c5b87ceae2061ca93035a550e83886"

#: Runs `_reference_key()`'s inputs in a fresh interpreter and prints the result.
REFERENCE_PROGRAM = """
from decimal import Decimal
from uuid import UUID

from workflow.idempotency import idempotency_key

print(
    idempotency_key(
        document_version_id=UUID("11111111-2222-3333-4444-555555555555"),
        region="page:7",
        task_type="extract_page",
        extractor_version="pdfplumber-1.4.2",
        config={
            "dpi": 300,
            "lanes": ["vector", "ocr"],
            "threshold": Decimal("0.5"),
            "strict": True,
        },
    )
)
"""


def _reference_key(**changes: Any) -> str:
    """One realistic set of inputs, with named parts swapped out by the tests that vary them."""

    arguments: dict[str, Any] = {
        "document_version_id": DOCUMENT_VERSION_ID,
        "region": "page:7",
        "task_type": "extract_page",
        "extractor_version": "pdfplumber-1.4.2",
        "config": {
            "dpi": 300,
            "lanes": ["vector", "ocr"],
            "threshold": Decimal("0.5"),
            "strict": True,
        },
    }
    arguments.update(changes)
    return idempotency_key(**arguments)


# ---------------------------------------------------------------------------
# Stable across processes and restarts
# ---------------------------------------------------------------------------


def _key_in_a_fresh_interpreter(hash_seed: str) -> str:
    """Compute the reference key in a new process with a chosen string-hash salt."""

    environment = dict(os.environ)
    environment["PYTHONHASHSEED"] = hash_seed
    environment["PYTHONPATH"] = str(REPO_ROOT)
    result = subprocess.run(
        [sys.executable, "-c", REFERENCE_PROGRAM],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
        env=environment,
    )
    return result.stdout.strip()


@pytest.mark.parametrize("hash_seed", ["0", "1", "random"])
def test_the_key_survives_a_restart_and_a_different_hash_seed(hash_seed: str) -> None:
    """Input: a fresh interpreter, salted differently. Outcome: the same key. Why: retries match."""

    assert _key_in_a_fresh_interpreter(hash_seed) == REFERENCE_KEY


def test_the_key_is_the_digest_written_down_in_this_file() -> None:
    """Input: the reference task. Outcome: the recorded digest. Why: stored keys must keep matching."""

    assert _reference_key() == REFERENCE_KEY


def test_the_key_fits_the_column_it_is_stored_in() -> None:
    """Input: a key. Outcome: well under 500 characters. Why: the column must never truncate."""

    key = _reference_key()
    assert key.startswith("sha256:")
    assert len(key) == 71


# ---------------------------------------------------------------------------
# The key is the identity of the task
# ---------------------------------------------------------------------------


def test_a_changed_extractor_is_a_different_task_not_a_cache_hit() -> None:
    """Input: a new reader version. Outcome: a new key. Why: an old answer is not this reader's."""

    assert _reference_key(extractor_version="pdfplumber-1.4.3") != REFERENCE_KEY


def test_a_changed_config_value_is_a_different_task() -> None:
    """Input: a different dpi. Outcome: a new key. Why: a retuned reader must read again."""

    assert _reference_key(config={"dpi": 400}) != _reference_key(config={"dpi": 300})


@pytest.mark.parametrize(
    ("changes", "why"),
    [
        ({"document_version_id": uuid4()}, "a different document version"),
        ({"region": "page:8"}, "a different region"),
        ({"region": None}, "the whole document rather than a region"),
        ({"task_type": "classify_page"}, "a different kind of work"),
        ({"extractor_version": "pdfplumber-2.0.0"}, "a different reader version"),
        ({"config": {"dpi": 300}}, "a different configuration"),
    ],
)
def test_every_component_is_part_of_the_identity(changes: dict[str, Any], why: str) -> None:
    """Input: one component changed. Outcome: a new key. Why: all five name the task."""

    assert _reference_key(**changes) != REFERENCE_KEY, why


def test_config_key_order_does_not_change_the_key() -> None:
    """Input: the same config written backwards. Outcome: one key. Why: dict order is not identity."""

    forwards = {"alpha": 1, "beta": 2, "gamma": {"inner_a": "x", "inner_b": "y"}}
    backwards = {"gamma": {"inner_b": "y", "inner_a": "x"}, "beta": 2, "alpha": 1}
    assert list(forwards) != list(backwards)
    assert _reference_key(config=forwards) == _reference_key(config=backwards)


def test_list_order_does_change_the_key() -> None:
    """Input: reordered lanes. Outcome: a new key. Why: a list's order is part of what was asked."""

    assert _reference_key(config={"lanes": ["ocr", "vector"]}) != _reference_key(
        config={"lanes": ["vector", "ocr"]}
    )


def test_the_absent_region_and_the_word_none_are_different_tasks() -> None:
    """Input: None against "None". Outcome: two keys. Why: a value must not read as its own name."""

    assert _reference_key(region=None) != _reference_key(region="None")


@pytest.mark.parametrize(
    ("first", "second", "why"),
    [
        ({"pages": 1}, {"pages": "1"}, "a number and its text spelling"),
        ({"strict": True}, {"strict": 1}, "a flag and the integer one"),
        ({"strict": True}, {"strict": "true"}, "a flag and its text spelling"),
        ({"limit": Decimal("1.5")}, {"limit": "1.5"}, "an exact number and its text spelling"),
        ({"limit": Decimal("1.5")}, {"limit": Fraction(3, 2)}, "two exact spellings of one value"),
        ({"limit": Decimal("1.0")}, {"limit": Decimal("1.00")}, "two precisions of one value"),
    ],
)
def test_types_are_never_flattened_into_text(
    first: Mapping[str, object], second: Mapping[str, object], why: str
) -> None:
    """Input: two configs that differ only in type. Outcome: two keys. Why: no coerced collision."""

    assert _reference_key(config=first) != _reference_key(config=second), why


def test_no_component_can_impersonate_another() -> None:
    """Input: a region carrying a separator. Outcome: no collision. Why: joined text can be forged."""

    # Joined with any separator, these two tasks are the same string: "page:7|extract_page|read".
    # They are different work — one reads region "page:7|extract_page", the other reads page 7 —
    # and a key that could not tell them apart would serve one task's result for the other's.
    smuggled = _reference_key(region="page:7|extract_page", task_type="read")
    honest = _reference_key(region="page:7", task_type="extract_page|read")
    assert smuggled != honest


# ---------------------------------------------------------------------------
# What the key refuses, and why
# ---------------------------------------------------------------------------


def test_floats_are_refused_in_the_config() -> None:
    """Input: a float threshold. Outcome: TypeError. Why: AGENTS.md section 6 forbids inexact."""

    with pytest.raises(TypeError, match="float"):
        _reference_key(config={"threshold": 0.1})


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"lanes": {"vector", "ocr"}}, TypeError),
        ({"blob": b"bytes"}, TypeError),
        ({"when": object()}, TypeError),
        ({"threshold": Decimal("NaN")}, ValueError),
        ({"": 1}, ValueError),
        ({"$decimal": "1.5"}, ValueError),
    ],
)
def test_a_config_value_that_cannot_be_serialised_unambiguously_is_refused(
    value: Mapping[str, object], expected: type[Exception]
) -> None:
    """Input: an unserialisable config. Outcome: refusal. Why: a guessed encoding can collide."""

    with pytest.raises(expected):
        _reference_key(config=value)


def test_a_non_string_config_key_is_refused() -> None:
    """Input: an integer key. Outcome: TypeError. Why: JSON would turn 1 and "1" into one key."""

    with pytest.raises(TypeError, match="config keys"):
        _reference_key(config={1: "one"})


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"document_version_id": "11111111-2222-3333-4444-555555555555"}, TypeError),
        ({"region": ""}, ValueError),
        ({"region": "   "}, ValueError),
        ({"region": 7}, TypeError),
        ({"task_type": ""}, ValueError),
        ({"extractor_version": ""}, ValueError),
        ({"extractor_version": None}, TypeError),
        ({"config": [("dpi", 300)]}, TypeError),
    ],
)
def test_an_incomplete_task_identity_is_refused(
    changes: dict[str, Any], expected: type[Exception]
) -> None:
    """Input: a missing or mistyped component. Outcome: refusal. Why: a blank is not an identity."""

    with pytest.raises(expected):
        _reference_key(**changes)


def test_exact_numbers_are_accepted() -> None:
    """Input: Decimal and Fraction. Outcome: a key. Why: they are how a config states a number."""

    assert _reference_key(config={"limit": Decimal("0.125"), "ratio": Fraction(1, 8)}).startswith(
        "sha256:"
    )


# ---------------------------------------------------------------------------
# Claiming: the database decides, this module reports
# ---------------------------------------------------------------------------


def _upgrade(engine: Engine) -> None:
    config = alembic_config()
    config.attributes["database_url"] = engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")


@dataclass
class Fixture:
    """A migrated database with one workflow run to hang task runs from."""

    engine: Engine
    factory: sessionmaker[Session]
    workflow_run_id: UUID
    package_revision_id: UUID


@pytest.fixture
def workflow_fixture(postgres_engine: Engine) -> Iterator[Fixture]:
    """Migrate the schema and commit one workflow run, the way a dispatched workflow would."""

    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    # Committed before the test starts, not held open around it. A fixture that yields inside its
    # own transaction leaves rows invisible to every other connection, which would make the
    # concurrency test below prove nothing.
    with unit_of_work(factory) as session:
        project = Project(name="GV Idempotency Test")
        session.add(project)
        session.flush()
        package = Package(project_id=project.id, vendor=None)
        session.add(package)
        session.flush()
        revision = PackageRevision(
            package_id=package.id, revision_number=1, state=PackageState.CREATED
        )
        session.add(revision)
        session.flush()
        workflow_run = WorkflowRun(package_revision_id=revision.id, engine_run_id="hatchet-214")
        session.add(workflow_run)
        session.flush()
        workflow_run_id = workflow_run.id
        revision_id = revision.id
    yield Fixture(
        engine=postgres_engine,
        factory=factory,
        workflow_run_id=workflow_run_id,
        package_revision_id=revision_id,
    )


def _claim(fixture: Fixture, key: str, **changes: Any) -> Claim:
    """Claim a key in its own committed transaction, as a separate delivery would."""

    with unit_of_work(fixture.factory) as session:
        return claim(
            session,
            key,
            workflow_run_id=fixture.workflow_run_id,
            task_type=changes.pop("task_type", "extract_page"),
            **changes,
        )


def _task_run_count(fixture: Fixture) -> int:
    with unit_of_work(fixture.factory) as session:
        count = session.scalar(select(func.count()).select_from(TaskRun))
    assert count is not None
    return count


def test_the_first_delivery_takes_the_claim(workflow_fixture: Fixture) -> None:
    """Input: an unclaimed key. Outcome: a new row. Why: somebody has to be told to do the work."""

    result = _claim(workflow_fixture, REFERENCE_KEY)

    assert result.created is True
    assert result.task_run.idempotency_key == REFERENCE_KEY
    assert result.task_run.outcome == CLAIMED
    assert result.task_run.attempt == 1
    assert result.task_run.created_at.tzinfo is not None
    assert _task_run_count(workflow_fixture) == 1


def test_a_retry_returns_the_prior_run_and_writes_no_second_row(
    workflow_fixture: Fixture,
) -> None:
    """Input: the same key twice. Outcome: one row, returned twice. Why: a retry is not new work."""

    first = _claim(workflow_fixture, REFERENCE_KEY)
    with unit_of_work(workflow_fixture.factory) as session:
        stored = session.get(TaskRun, first.task_run.id)
        assert stored is not None
        stored.outcome = "ok"

    second = _claim(workflow_fixture, REFERENCE_KEY, attempt=2)

    assert second.created is False
    assert second.task_run.id == first.task_run.id
    # The prior *result*, not merely the prior row: the caller retrying learns the work finished.
    assert second.task_run.outcome == "ok"
    assert second.task_run.attempt == 1
    assert _task_run_count(workflow_fixture) == 1


def test_a_retry_does_not_repeat_a_paid_model_call(workflow_fixture: Fixture) -> None:
    """Input: three deliveries. Outcome: one call. Why: a retry must not be charged twice."""

    calls: list[str] = []

    def deliver() -> None:
        with unit_of_work(workflow_fixture.factory) as session:
            taken = claim(
                session,
                REFERENCE_KEY,
                workflow_run_id=workflow_fixture.workflow_run_id,
                task_type="extract_page",
            )
            if taken.created:
                calls.append("model call")
                taken.task_run.outcome = "ok"

    deliver()
    deliver()
    deliver()

    assert calls == ["model call"]
    assert _task_run_count(workflow_fixture) == 1


def test_a_different_key_is_different_work(workflow_fixture: Fixture) -> None:
    """Input: two keys. Outcome: two rows. Why: the constraint must not block unrelated tasks."""

    _claim(workflow_fixture, REFERENCE_KEY)
    other = _claim(workflow_fixture, _reference_key(region="page:8"))

    assert other.created is True
    assert _task_run_count(workflow_fixture) == 2


def test_the_claim_is_only_real_once_the_caller_commits(workflow_fixture: Fixture) -> None:
    """Input: a rolled-back claim. Outcome: the key is free. Why: unrecorded work must be redone."""

    factory = workflow_fixture.factory
    session = factory()
    try:
        taken = claim(
            session,
            REFERENCE_KEY,
            workflow_run_id=workflow_fixture.workflow_run_id,
            task_type="extract_page",
        )
        assert taken.created is True
        session.rollback()
    finally:
        session.close()

    assert _task_run_count(workflow_fixture) == 0
    assert _claim(workflow_fixture, REFERENCE_KEY).created is True


def test_a_losing_claim_does_not_discard_the_callers_other_pending_work(
    workflow_fixture: Fixture,
) -> None:
    """Input: unflushed work plus a lost claim. Outcome: the work survives. Why: silent loss."""

    # `claim` inserts inside a savepoint and rolls it back when it loses. That rollback must not
    # reach the caller's own rows. It does not, because opening a nested transaction flushes the
    # session *before* emitting the SAVEPOINT — SQLAlchemy's ordering, not this project's, which is
    # exactly why it is asserted here. If it ever flushed after the savepoint instead, the row added
    # below would disappear at commit with no error raised anywhere.
    _claim(workflow_fixture, REFERENCE_KEY)

    with unit_of_work(workflow_fixture.factory) as session:
        session.add(
            WorkflowRun(
                package_revision_id=workflow_fixture.package_revision_id,
                engine_run_id="hatchet-214-second",
            )
        )
        result = claim(
            session,
            REFERENCE_KEY,
            workflow_run_id=workflow_fixture.workflow_run_id,
            task_type="extract_page",
        )
        assert result.created is False

    with unit_of_work(workflow_fixture.factory) as session:
        survived = session.scalar(
            select(WorkflowRun).where(WorkflowRun.engine_run_id == "hatchet-214-second")
        )
    assert survived is not None


# ---------------------------------------------------------------------------
# The constraint is the enforcement — proved by name, and against a real race
# ---------------------------------------------------------------------------


def _violated_constraint(error: IntegrityError) -> str | None:
    """The constraint PostgreSQL actually rejected on, from the driver's own diagnostics."""

    diagnostic = getattr(getattr(error, "orig", None), "diag", None)
    name: str | None = getattr(diagnostic, "constraint_name", None)
    return name


def test_the_database_is_what_rejects_a_duplicate_key(workflow_fixture: Fixture) -> None:
    """Input: a duplicate insert. Outcome: the key's unique constraint fires. Why: right reason."""

    _claim(workflow_fixture, REFERENCE_KEY)

    with pytest.raises(IntegrityError) as raised, unit_of_work(workflow_fixture.factory) as session:
        session.add(
            TaskRun(
                workflow_run_id=workflow_fixture.workflow_run_id,
                idempotency_key=REFERENCE_KEY,
                task_type="extract_page",
                attempt=2,
                outcome=CLAIMED,
            )
        )
        session.flush()

    # The migration writes an unnamed `sa.UniqueConstraint`, which would get PostgreSQL's default
    # name — except that `alembic/env.py` passes `Base.metadata` as `target_metadata`, so the
    # project's naming convention is applied on the migration path too. Both build routes therefore
    # install one spelling, and this asserts it rather than accepting a set of possibilities.
    assert _violated_constraint(raised.value) == "uq_task_runs_idempotency_key"


def test_an_integrity_error_that_is_not_a_duplicate_key_still_raises(
    workflow_fixture: Fixture,
) -> None:
    """Input: an unknown workflow run. Outcome: the foreign key fires. Why: no silent no-op."""

    with pytest.raises(IntegrityError) as raised, unit_of_work(workflow_fixture.factory) as session:
        claim(session, REFERENCE_KEY, workflow_run_id=uuid4(), task_type="extract_page")

    constraint = _violated_constraint(raised.value)
    assert constraint is not None
    assert "workflow_run_id" in constraint
    assert _task_run_count(workflow_fixture) == 0


def test_a_foreign_key_failure_is_not_classified_as_a_duplicate_delivery(
    workflow_fixture: Fixture,
) -> None:
    """Input: a real foreign-key error. Outcome: not a duplicate. Why: it must not be absorbed."""

    # The test above passes even if the classifier says "duplicate", because the follow-up SELECT
    # finds no prior row and the error is re-raised regardless. That makes it a good end-to-end
    # assertion and a poor test of the classifier, so the classifier is asked directly, with an
    # error object a real PostgreSQL produced rather than one built by hand.
    with pytest.raises(IntegrityError) as raised, unit_of_work(workflow_fixture.factory) as session:
        session.add(
            TaskRun(
                workflow_run_id=uuid4(),
                idempotency_key=REFERENCE_KEY,
                task_type="extract_page",
                attempt=1,
                outcome=CLAIMED,
            )
        )
        session.flush()

    assert _violated_constraint(raised.value) == "fk_task_runs_workflow_run_id_workflow_runs"
    assert _is_duplicate_idempotency_key(raised.value) is False


def test_a_real_duplicate_key_failure_is_classified_as_a_duplicate_delivery(
    workflow_fixture: Fixture,
) -> None:
    """Input: a real unique violation. Outcome: a duplicate. Why: only this one may be absorbed."""

    _claim(workflow_fixture, REFERENCE_KEY)

    with pytest.raises(IntegrityError) as raised, unit_of_work(workflow_fixture.factory) as session:
        session.add(
            TaskRun(
                workflow_run_id=workflow_fixture.workflow_run_id,
                idempotency_key=REFERENCE_KEY,
                task_type="extract_page",
                attempt=2,
                outcome=CLAIMED,
            )
        )
        session.flush()

    assert _is_duplicate_idempotency_key(raised.value) is True


def _wait_for_an_insert_blocked_on_another_transaction(engine: Engine, seconds: float) -> bool:
    """Poll until a backend is waiting on another transaction, which is the conflict happening.

    PostgreSQL detects a duplicate while updating the unique index, and if the conflicting row was
    written by a transaction that has not ended, the second inserter waits on that transaction id
    rather than failing immediately. Seeing `wait_event = 'transactionid'` is therefore direct
    evidence that the index — not any application check — is what stopped the second writer.
    """

    deadline = time.monotonic() + seconds
    query = text(
        "SELECT count(*) FROM pg_stat_activity "
        "WHERE pid <> pg_backend_pid() AND datname = current_database() "
        "AND wait_event_type = 'Lock' AND wait_event = 'transactionid'"
    )
    while time.monotonic() < deadline:
        with engine.connect() as connection:
            if (connection.scalar(query) or 0) > 0:
                return True
        time.sleep(0.02)
    return False


def test_the_second_writer_is_stopped_by_the_index_and_gets_the_winners_row(
    workflow_fixture: Fixture,
) -> None:
    """Input: an overlapping claim. Outcome: it waits, loses, returns the prior row. Why: races."""

    # The interleaving is forced rather than hoped for. An earlier version of this test released two
    # threads from a barrier and asserted the outcome; it passed against a deliberately broken
    # implementation that only did a `SELECT` before inserting, because the threads happened to run
    # one after the other and the second one's pre-check saw a committed row. Nothing was concurrent
    # about it.
    #
    # So the winner now holds its transaction open until the test can see the second insert waiting
    # on it, which is the state no pre-check can survive: at that moment the loser has already
    # looked, seen nothing, and issued its INSERT.
    inserted = threading.Event()
    release = threading.Event()
    taken: dict[str, Claim] = {}
    failures: list[BaseException] = []
    lock = threading.Lock()

    def winner() -> None:
        try:
            with unit_of_work(workflow_fixture.factory) as session:
                result = claim(
                    session,
                    REFERENCE_KEY,
                    workflow_run_id=workflow_fixture.workflow_run_id,
                    task_type="extract_page",
                )
                with lock:
                    taken["winner"] = result
                inserted.set()
                release.wait(timeout=30)
        except (AssertionError, SQLAlchemyError) as error:  # reported to the main thread
            with lock:
                failures.append(error)
            inserted.set()

    def loser() -> None:
        try:
            assert inserted.wait(timeout=30)
            with unit_of_work(workflow_fixture.factory) as session:
                result = claim(
                    session,
                    REFERENCE_KEY,
                    workflow_run_id=workflow_fixture.workflow_run_id,
                    task_type="extract_page",
                )
                with lock:
                    taken["loser"] = result
        except (AssertionError, SQLAlchemyError) as error:  # reported to the main thread
            with lock:
                failures.append(error)

    threads = [
        threading.Thread(target=winner, name="winner"),
        threading.Thread(target=loser, name="loser"),
    ]
    blocked = False
    try:
        for thread in threads:
            thread.start()
        assert inserted.wait(timeout=30), "the first claim never inserted"
        blocked = _wait_for_an_insert_blocked_on_another_transaction(workflow_fixture.engine, 15.0)
    finally:
        release.set()
        for thread in threads:
            thread.join(timeout=30)
            assert not thread.is_alive(), f"{thread.name} never finished"

    assert failures == []
    assert blocked, "the second insert never waited on the first transaction"
    assert taken["winner"].created is True
    assert taken["loser"].created is False
    assert taken["loser"].task_run.id == taken["winner"].task_run.id
    assert _task_run_count(workflow_fixture) == 1


# ---------------------------------------------------------------------------
# Arguments the claim refuses outright
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"key": ""}, ValueError),
        ({"key": "   "}, ValueError),
        ({"key": None}, TypeError),
        ({"task_type": ""}, ValueError),
        ({"outcome": ""}, ValueError),
        ({"attempt": 0}, ValueError),
        ({"attempt": -1}, ValueError),
        ({"attempt": "2"}, TypeError),
        ({"attempt": True}, TypeError),
        ({"workflow_run_id": "not-a-uuid"}, TypeError),
    ],
)
def test_an_unusable_claim_is_refused_before_it_reaches_the_database(
    changes: dict[str, Any], expected: type[Exception]
) -> None:
    """Input: a malformed claim. Outcome: refusal. Why: a blank identity claims every task at once."""

    arguments: dict[str, Any] = {
        "key": REFERENCE_KEY,
        "workflow_run_id": uuid4(),
        "task_type": "extract_page",
    }
    arguments.update(changes)
    key = arguments.pop("key")

    # No session is touched: every one of these is refused before any statement is prepared, so a
    # `None` session is enough and its absence proves the refusal came first.
    with pytest.raises(expected):
        claim(None, key, **arguments)  # type: ignore[arg-type]
