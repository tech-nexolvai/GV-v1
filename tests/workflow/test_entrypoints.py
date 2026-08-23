"""The two long-running processes (#415, F3.1).

The happy paths here are nearly trivial — read settings, call a thing, log a count. The tests that earn
their place are the refusals and the stop: a dispatcher that starts without a token and reports "0 rows"
on a full queue is indistinguishable from the silently-stuck outbox this story exists to end, and a loop
that ignores `SIGTERM` gets killed mid-transaction on every deploy.

No engine and no database: `dispatch_committed` is injected, so these run anywhere.

Source: backend proposal §9.1–§9.4 · Design: `docs/DESIGN_PLATFORM.md` §6.1, §6.2 ·
Verification: this file
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

import pytest

from app.config import Settings
from workflow import entrypoints
from workflow.entrypoints import (
    EXIT_MISCONFIGURED,
    EXIT_OK,
    OUTBOX_ROW_METADATA_KEY,
    Shutdown,
    run_dispatcher,
    run_worker,
    settings_or_none,
)
from workflow.outbox import OutboxDispatchError

DATABASE_URL = "postgresql+psycopg://gv:gv@localhost:5433/gv"


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": DATABASE_URL,
        "hatchet_token": "a-token",
        "outbox_poll_seconds": 0.01,
        "outbox_batch_limit": 7,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The dispatcher loop
# ---------------------------------------------------------------------------


def test_an_empty_outbox_dispatches_nothing_and_raises_nothing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The acceptance criterion, and the state that used to be ambiguous.

    An idle queue and a dead dispatcher looked identical: nothing happened in either case. So the empty
    pass must complete quietly *and* say so — "0 rows dispatched" is the evidence the process is alive.
    """
    calls: list[int] = []

    def empty(factory: object, start: object, *, limit: int) -> int:
        calls.append(limit)
        return 0

    monkeypatch.setattr(entrypoints, "dispatch_committed", empty)

    with caplog.at_level(logging.INFO, logger="gv.workflow.entrypoints"):
        code = run_dispatcher(
            _settings(), factory=object(), start=_never_started, max_passes=3  # type: ignore[arg-type]
        )

    assert code == EXIT_OK
    assert calls == [7, 7, 7], "three passes, each using the configured batch limit"
    assert any(
        "0 row(s) dispatched" in r.getMessage() for r in caplog.records
    ), "an empty pass must still report, or an idle queue cannot be told from a stopped dispatcher"


def test_the_batch_limit_and_interval_come_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both are configuration, not literals — the criterion says so, and this is what checks it."""
    seen: list[int] = []
    waits: list[float] = []

    monkeypatch.setattr(
        entrypoints,
        "dispatch_committed",
        lambda factory, start, *, limit: (seen.append(limit), 0)[1],
    )

    class RecordingShutdown(Shutdown):
        def wait(self, seconds: float) -> None:
            waits.append(seconds)

    run_dispatcher(
        _settings(outbox_batch_limit=3, outbox_poll_seconds=0.25),
        factory=object(),  # type: ignore[arg-type]
        start=_never_started,
        shutdown=RecordingShutdown(),
        max_passes=2,
    )

    assert seen == [3, 3]
    assert waits == [0.25], "one wait between two passes, of the configured length"


def test_a_failing_row_does_not_stop_the_loop(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """One unstartable payload must not block delivery for every other package.

    `dispatch_committed` raises `OutboxDispatchError` *after* committing the rows that did go out, so the
    honest response is to log both numbers and keep polling. Exiting here would turn a single bad row
    into a total outage — the exact failure the outbox pattern is meant to prevent.
    """
    row = uuid4()
    passes = {"n": 0}

    def sometimes_fails(factory: object, start: object, *, limit: int) -> int:
        passes["n"] += 1
        if passes["n"] == 1:
            raise OutboxDispatchError(2, ((row, RuntimeError("engine said no")),))
        return 1

    monkeypatch.setattr(entrypoints, "dispatch_committed", sometimes_fails)

    with caplog.at_level(logging.INFO, logger="gv.workflow.entrypoints"):
        code = run_dispatcher(
            _settings(), factory=object(), start=_never_started, max_passes=2  # type: ignore[arg-type]
        )

    assert code == EXIT_OK, "a failed row is not a reason to exit"
    assert passes["n"] == 2, "the loop continued after the failure"
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert (
        "2 row(s) dispatched" in logged and "1 failed" in logged
    ), "both numbers matter: how many went out, and how many did not"
    assert str(row) in logged, "the failing row is named, or nobody can go and look at it"


def test_a_stop_request_ends_the_loop_between_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """`SIGTERM` finishes the pass in flight and then stops.

    Checked by stopping *during* a pass and asserting that pass completed and no further one began. A
    pass is one transaction, so interrupting it inside would roll back and re-dispatch the same rows —
    correct, but pointless work on every deploy.
    """
    stopper = Shutdown()
    completed = {"n": 0}

    def stop_midway(factory: object, start: object, *, limit: int) -> int:
        stopper.request_stop()  # as a signal would, part-way through the work
        completed["n"] += 1
        return 1

    monkeypatch.setattr(entrypoints, "dispatch_committed", stop_midway)

    code = run_dispatcher(
        _settings(), factory=object(), start=_never_started, shutdown=stopper  # type: ignore[arg-type]
    )

    assert code == EXIT_OK
    assert completed["n"] == 1, "the pass in flight finished, and no second pass began"


def test_a_stop_before_the_first_pass_dispatches_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stop that arrives during startup is honoured, not raced past."""
    called = {"n": 0}
    monkeypatch.setattr(
        entrypoints,
        "dispatch_committed",
        lambda factory, start, *, limit: (called.__setitem__("n", called["n"] + 1), 0)[1],
    )

    stopper = Shutdown()
    stopper.request_stop()

    assert (
        run_dispatcher(
            _settings(), factory=object(), start=_never_started, shutdown=stopper  # type: ignore[arg-type]
        )
        == EXIT_OK
    )
    assert called["n"] == 0


# ---------------------------------------------------------------------------
# Refusing to start
# ---------------------------------------------------------------------------


def test_the_worker_refuses_to_start_without_a_token(caplog: pytest.LogCaptureFixture) -> None:
    """A worker with no token must not start and pretend to be healthy.

    This is the criterion worth having. `Hatchet()` would refuse anyway, but it refuses with a Pydantic
    error about `ClientConfig` — which reads like a defect in this code rather than a missing environment
    variable, and sends whoever is on call to the wrong place.
    """
    with caplog.at_level(logging.ERROR, logger="gv.workflow.entrypoints"):
        code = run_worker(_settings(hatchet_token=""), factory=object())  # type: ignore[arg-type]

    assert code == EXIT_MISCONFIGURED
    assert code != 0, "a misconfigured process must exit non-zero"
    message = " ".join(r.getMessage() for r in caplog.records)
    assert "GV_HATCHET_TOKEN" in message, "the message names the variable to set"
    assert "runs nothing" in message


def test_a_missing_setting_is_reported_rather_than_raised(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`settings_or_none` turns an incomplete environment into a reportable `None`.

    Returning rather than raising is what lets both entrypoints share one exit code without each one
    catching Pydantic's error type.
    """
    for leftover in ("GV_DATABASE_URL", "GV_HATCHET_TOKEN"):
        monkeypatch.delenv(leftover, raising=False)

    with caplog.at_level(logging.ERROR, logger="gv.workflow.entrypoints"):
        assert settings_or_none() is None

    message = " ".join(r.getMessage() for r in caplog.records)
    assert "database_url" in message, "the missing field is named by Pydantic's own error"


def test_the_misconfigured_exit_code_is_not_a_crash_code() -> None:
    """78 rather than 1, so an operator can tell "you configured me wrong" from "I broke".

    Restarting the first never helps. A supervisor that retries forever on a missing variable is a
    machine burning CPU to produce the same message.
    """
    assert EXIT_MISCONFIGURED == 78
    assert EXIT_OK == 0


# ---------------------------------------------------------------------------
# What the outbox row id is, and is not
# ---------------------------------------------------------------------------


def test_the_row_id_is_recorded_as_metadata_not_as_a_dedupe_key() -> None:
    """The honest version of this module's one subtlety.

    The outbox hands the starter an `idempotency_key` and requires a repeat to be a no-op. The engine
    offers no per-trigger key — `runs.create` takes a name, an input and metadata, and Hatchet's own
    idempotency is a CEL expression declared on the workflow, which `workflow/review.py` does not use.
    So dispatching the same row twice really does create two runs.

    What makes that safe is `workflow/idempotency.py`: each stage claims its work in PostgreSQL first, so
    the second run finds every stage claimed and does nothing. This test pins the metadata key that makes
    a run traceable to its row, and the docstring is where the "not a dedupe guarantee" part is recorded
    so nobody later reads the parameter name and assumes the engine is enforcing something.
    """
    assert OUTBOX_ROW_METADATA_KEY == "gv_outbox_row"

    import workflow.idempotency as stage_level

    assert hasattr(stage_level, "claim"), (
        "the safety of at-least-once dispatch rests on the stage claim; if this moves, the comment in "
        "entrypoints.py explaining why duplicate dispatch is safe is no longer true"
    )


def test_neither_entrypoint_imports_the_verdict_engine() -> None:
    """The isolation guard, asserted here too because these are new import roots.

    `verdict/` must not be reachable from a process that runs extraction and retrieval — `AGENTS.md` §2.
    A new module is exactly where that creeps in, and the repository-wide guard checks `verdict/`'s own
    imports rather than everything that might pull it in.
    """
    import ast
    from pathlib import Path

    for module in ("entrypoints", "worker", "dispatcher"):
        source = Path(f"workflow/{module}.py").read_text()
        tree = ast.parse(source)
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        offending = {name for name in imported if name == "verdict" or name.startswith("verdict.")}
        assert not offending, f"workflow/{module}.py imports {sorted(offending)}"


def _never_started(*, workflow: str, payload: object, idempotency_key: str) -> None:
    """A starter these tests never reach: `dispatch_committed` itself is replaced.

    Raising rather than passing, so a test that accidentally began dispatching for real fails loudly
    instead of quietly doing nothing.
    """
    raise AssertionError("the injected dispatch_committed should have been called instead")
