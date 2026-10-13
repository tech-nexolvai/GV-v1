"""The reading assignment's calls are recorded, and recording never changes the answer (#1221).

Verification for: `workflow/model_call_recorder.py` and its wiring in `workflow/propose.py`.

Before #1221 the assignment recorded nothing in `model_invocations`: its calls were missing from
Usage, cost and every scorecard. Each call now writes exactly one row through the same path as the
findings narration and the reviewer chat (`BedrockConverseInvocationRecorder`), scoped to the
package revision (ADR-0019).

No provider is contacted: every Bedrock client here is a fake, and every value is authored.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alembic import command
from app.db.session import session_factory
from app.models import ModelInvocation, PackageRevision
from app.models.evidence import MeasurementProposal
from tests.app.postgres_fixture import alembic_config
from workflow.assignment import AssignmentContext, Field, Reading
from workflow.assignment_bedrock import (
    PROMPT_ID,
    TEMPLATE_ID,
    TOOL_NAME,
    BedrockAssignmentModel,
    _Config,
)
from workflow.model_call_recorder import ModelCallRecorder

pytest_plugins = ("tests.app.postgres_fixture",)

MODEL = "test-assignment-model"


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture(autouse=True)
def rates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A stated price for the test model: $1 per 1k input tokens, $2 per 1k output tokens."""
    path = tmp_path / "rates.json"
    path.write_text(
        json.dumps(
            {
                "source": "test prices, authored",
                "retrieved": "2026-10-14",
                "currency": "USD",
                "rates": {MODEL: {"input_per_1k_tokens": "1", "output_per_1k_tokens": "2"}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("GV_MODEL_RATES_FILE", str(path))


def _revision(session: Session) -> tuple[UUID, AssignmentContext]:
    """A revision with one real, attached reading, and the context the step would assemble."""
    from tests.api.test_measurement_proposal import _package_with_readings

    package, ids = _package_with_readings(session, readings=(("25 1/2", 51, 2, None, None),))
    revision_id = session.execute(
        select(PackageRevision.id).where(PackageRevision.package_id == package)
    ).scalar_one()
    context = AssignmentContext(
        fields=(Field(key="SHOP:CT010", name="countertop_depth", source="SHOP", many=False),),
        readings=(
            Reading(
                candidate_id=str(ids["25 1/2"]),
                value="25 1/2 in",
                source="SHOP",
                page=1,
                line_key="l",
            ),
        ),
    )
    return revision_id, context


def _answer() -> dict[str, Any]:
    return {
        "stopReason": "tool_use",
        "usage": {"inputTokens": 1000, "outputTokens": 100},
        "output": {
            "message": {
                "content": [
                    {
                        "toolUse": {
                            "name": TOOL_NAME,
                            "input": {
                                "assignments": [{"field_key": "SHOP:CT010", "candidate_ids": ["r1"]}]
                            },
                        }
                    }
                ]
            }
        },
    }


class _Client:
    def __init__(self, response: dict[str, Any] | None = None, error: Exception | None = None):
        self.response = response
        self.error = error
        self.calls = 0

    def converse(self, **_: Any) -> dict[str, Any]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


def _model(client: _Client, model_id: str = MODEL) -> BedrockAssignmentModel:
    return BedrockAssignmentModel(
        config=_Config(
            model_id=model_id,
            region_name="us-east-1",
            connect_timeout_seconds=1,
            read_timeout_seconds=2,
        ),
        client=client,
    )


def _propose(
    session: Session, client: _Client, monkeypatch: pytest.MonkeyPatch, **kw: Any
) -> tuple[UUID, dict[str, object]]:
    from workflow import propose

    revision_id, context = _revision(session)
    monkeypatch.setattr(propose, "assignment_context", lambda _session, _revision: (context, 1))
    return revision_id, propose.propose_for_revision(session, revision_id, _model(client, **kw))


def _rows(session: Session) -> list[ModelInvocation]:
    return list(session.execute(select(ModelInvocation)).scalars())


def test_the_worker_writes_one_ok_row_per_call_against_the_revision(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**Outcome: one `ok` row, priced, with tokens, identity and the revision as its scope.**"""
    client = _Client(_answer())

    revision_id, summary = _propose(session, client, monkeypatch)

    assert summary["filled"] == 1
    rows = _rows(session)
    assert len(rows) == client.calls == 1
    row = rows[0]
    assert row.package_revision_id == revision_id
    assert row.extraction_run_id is None
    assert (row.model_id, row.prompt_id, row.template_id) == (MODEL, PROMPT_ID, TEMPLATE_ID)
    assert (row.input_tokens, row.output_tokens) == (1000, 100)
    assert row.cost_micros == 1_200_000
    assert row.outcome == "ok"
    assert row.rejection_reason is None


def test_a_malformed_answer_writes_one_rejected_row_and_still_fails_closed(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    malformed = {
        "usage": {"inputTokens": 1000, "outputTokens": 100},
        "output": {"message": {"content": [{"text": "free text, no tool"}]}},
    }

    _, summary = _propose(session, _Client(malformed), monkeypatch)

    assert summary["filled"] == 0
    assert summary["failure"] == "answer-malformed"
    [row] = _rows(session)
    assert row.outcome == "rejected"
    assert row.rejection_reason is not None
    assert row.rejection_reason.startswith("UnusableAnswer:")
    assert row.cost_micros == 1_200_000, "the model answered, so its tokens were paid for"


def test_a_failed_call_writes_one_failed_row_with_its_reason_and_still_fails_closed(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from botocore.exceptions import ClientError  # type: ignore[import-untyped]

    error = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}}, "Converse"
    )

    _, summary = _propose(session, _Client(error=error), monkeypatch)

    assert summary["filled"] == 0
    assert summary["failure"] == "throttled"
    [row] = _rows(session)
    assert row.outcome == "failed"
    assert row.rejection_reason == "ClientError: ThrottlingException"
    assert (row.input_tokens, row.output_tokens, row.cost_micros) == (0, 0, 0)


def test_a_timed_out_call_writes_one_timeout_row_and_still_fails_closed(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from botocore.exceptions import ReadTimeoutError  # type: ignore[import-untyped]

    error = ReadTimeoutError(endpoint_url="https://bedrock.invalid")

    _, summary = _propose(session, _Client(error=error), monkeypatch)

    assert summary["filled"] == 0
    assert summary["failure"] == "unreachable"
    [row] = _rows(session)
    assert row.outcome == "timeout"
    assert row.rejection_reason is not None
    assert row.output_tokens == 0


def test_the_worker_row_lives_in_the_workers_transaction(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**Decision: the worker's row commits or rolls back with the work it explains.**

    The worker commits once the revision enters `NEEDS_INPUT`. The recorder does not commit on its
    own there: that would also commit the half-finished extraction around it. So a worker that
    fails after the call loses the row with the rest of its work, exactly as the findings
    narration's row does today (a known gap, written down in `docs/decisions/SUTRA_HARNESS.md`).
    """
    _propose(session, _Client(_answer()), monkeypatch)
    assert len(_rows(session)) == 1
    session.rollback()

    assert _rows(session) == []


def test_a_row_the_database_refuses_never_changes_the_answer(
    session: Session, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """**Outcome: the proposal is filed as before; the lost row is logged, not raised.**

    A blank model id is refused by the table's own check. The write is in a savepoint, so the
    refusal leaves the session usable and the proposal that follows is still filed.
    """
    with caplog.at_level(logging.WARNING, logger="gv.workflow.model_call_recorder"):
        _, summary = _propose(session, _Client(_answer()), monkeypatch, model_id="")

    assert summary["filled"] == 1
    assert _rows(session) == []
    assert session.execute(select(MeasurementProposal)).scalars().first() is not None
    assert "model call not recorded" in caplog.text


def test_the_committing_recorder_keeps_its_row_when_the_request_rolls_back(
    session: Session, postgres_engine: Engine
) -> None:
    """**Decision for the API path: the row is committed at once, as the reviewer chat's is.**

    `get_session` never commits, so an uncommitted row would be thrown away when the request ended:
    the call paid for and never counted (#1134).
    """
    from time import monotonic_ns

    revision_id, _ = _revision(session)
    ModelCallRecorder(session, package_revision_id=revision_id, commit=True).record(
        model_id=MODEL,
        prompt_id=PROMPT_ID,
        template_id=TEMPLATE_ID,
        started_ns=monotonic_ns(),
        response=_answer(),
        error=None,
    )
    session.rollback()

    with session_factory(postgres_engine)() as other:
        [row] = list(other.execute(select(ModelInvocation)).scalars())
    assert row.package_revision_id == revision_id
    assert row.outcome == "ok"


def test_the_recorder_needs_exactly_one_scope() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        ModelCallRecorder(object(), package_revision_id=uuid4(), extraction_run_id=uuid4())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="exactly one"):
        ModelCallRecorder(object())  # type: ignore[arg-type]
