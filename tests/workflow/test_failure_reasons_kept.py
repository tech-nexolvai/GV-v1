"""Never silent: a reader or wording call that failed or was never sent keeps why (#1215).

Verification for `workflow.stages._reader_invocation` (the reader attempt's row) and
`app.runs.invocations.BedrockConverseInvocationRecorder` (the findings wording call's row), stored
through a migrated database. Synthetic attempts and a fake Bedrock client only: no model is called.

Before #1215 a reader question refused before sending (a close-up wider than the readers' picture
limit) was stored as a bare `failed`, and a wording call that failed outright stored no error at
all. The cause now sits in the row's `rejection_reason`; successful, malformed and rejected rows are
stored exactly as before.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alembic import command
from app.db.session import session_factory
from app.models import ModelInvocation, ModelInvocationOutcome
from app.runs.invocations import BedrockConverseInvocationRecorder, record
from extraction.form_reader.bedrock import AttemptUsage
from extraction.models.nova import NovaConfig
from tests.app.postgres_fixture import alembic_config
from tests.db.test_invocation_failure_reason_migration import _run
from tests.review.test_chat_invocation_recorded import (
    _finding,
    _findings_response,
    _revision_without_run,
)
from workflow.findings_bedrock import PROMPT_ID, TEMPLATE_ID, BedrockFindingsComposer
from workflow.findings_composer import CompositionMode, compose_findings
from workflow.stages import DatabaseStages

pytest_plugins = ("tests.app.postgres_fixture",)

#: Made-up credential shapes a provider error could echo. None is real.
FAKE_ACCESS_KEY = "AKIAEXAMPLEFAKE12345"
FAKE_SECRET = "wJalrXUtnFEMI0K7MDENG0bPxRfiCYEXAMPLEKEY"
PROMPT_LINE = "You are reading a made-up drawing: report every width"


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.rollback()
        opened.close()


def _stages() -> Any:
    stages: Any = object.__new__(DatabaseStages)
    stages._form_reader = SimpleNamespace(rates=None)
    return stages


def _attempt(**changes: Any) -> AttemptUsage:
    values: dict[str, Any] = {
        "model_id": "made-up-reader",
        "prompt_id": "slot-span-test",
        "template_id": "slot-span-test",
        "input_tokens": None,
        "output_tokens": None,
        "latency_ms": 0,
        "malformed": False,
        "page_index": 2,
    }
    values.update(changes)
    return AttemptUsage(**values)


def _stored(session: Session, attempt: AttemptUsage) -> ModelInvocation:
    run = _run(session)
    return record(session, _stages()._reader_invocation(run.id, attempt))


# --- reader attempts --------------------------------------------------------------------------------


def test_a_question_refused_for_its_picture_size_keeps_that_cause(session: Session) -> None:
    """The client-set case: never sent, because the close-up was wider than the picture limit."""
    row = _stored(session, _attempt(failure_kind="PictureWouldBeResized"))

    assert row.outcome == ModelInvocationOutcome.FAILED
    assert row.rejection_reason == "PictureWouldBeResized"
    assert (row.input_tokens, row.output_tokens, row.cost_micros) == (0, 0, 0)


@pytest.mark.parametrize(
    ("kind", "outcome"),
    [
        ("SpendCapExceeded", ModelInvocationOutcome.BUDGET_REFUSED),
        ("ProviderAccountRefused", ModelInvocationOutcome.ACCOUNT_REFUSED),
        ("ProviderInFlightLimited", ModelInvocationOutcome.ACCOUNT_REFUSED),
        ("ThrottlingException", ModelInvocationOutcome.FAILED),
        ("TimeoutError", ModelInvocationOutcome.FAILED),
    ],
)
def test_every_failed_or_unsent_reader_attempt_keeps_its_cause(
    session: Session, kind: str, outcome: ModelInvocationOutcome
) -> None:
    row = _stored(session, _attempt(failure_kind=kind))

    assert (row.outcome, row.rejection_reason) == (outcome.value, kind)


def test_a_cause_that_is_not_a_class_name_is_not_stored_as_written(session: Session) -> None:
    row = _stored(session, _attempt(failure_kind=f"Bearer {FAKE_SECRET}"))

    assert row.outcome == ModelInvocationOutcome.FAILED
    assert row.rejection_reason == "unnamed_failure"


def test_a_successful_reader_attempt_is_stored_as_before(session: Session) -> None:
    row = _stored(
        session,
        _attempt(input_tokens=120, output_tokens=8, latency_ms=900, raw_response_text="{}"),
    )

    assert (row.outcome, row.rejection_reason) == ("ok", None)
    assert row.private_raw_response == "{}"


def test_a_malformed_reader_answer_is_stored_as_before(session: Session) -> None:
    row = _stored(
        session,
        _attempt(input_tokens=120, output_tokens=8, malformed=True, raw_response_text="not json"),
    )

    assert (row.outcome, row.rejection_reason) == ("rejected", "malformed_form_answer")


# --- the findings wording call ----------------------------------------------------------------------


class _Raises:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def converse(self, **kwargs: object) -> Mapping[str, object]:
        raise self.error


class _Answers:
    def __init__(self, response: Mapping[str, object]) -> None:
        self.response = response

    def converse(self, **kwargs: object) -> Mapping[str, object]:
        return self.response


def _composer(client: object, session: Session, revision_id: UUID) -> BedrockFindingsComposer:
    return BedrockFindingsComposer(
        NovaConfig(
            model_id="made-up-wording-model",
            prompt_id=PROMPT_ID,
            template_id=TEMPLATE_ID,
            connect_timeout_seconds=1,
            read_timeout_seconds=2,
            max_attempts=1,
            region_name="us-east-1",
        ),
        client,  # type: ignore[arg-type]
        BedrockConverseInvocationRecorder(session, revision_id),
    )


def _rows(session: Session, revision_id: UUID) -> list[ModelInvocation]:
    return list(
        session.scalars(
            select(ModelInvocation).where(ModelInvocation.package_revision_id == revision_id)
        )
    )


class ModelErrorException(Exception):
    """Named like a Bedrock service error."""


def test_a_failed_wording_call_keeps_its_error_class_and_a_safe_short_message(
    session: Session,
) -> None:
    revision_id = _revision_without_run(session)
    error = ModelErrorException(
        f"The model could not answer (key {FAKE_ACCESS_KEY}, "
        f"x-amz-security-token={FAKE_SECRET}) at https://user:{FAKE_SECRET}@example.invalid/x\n"
        f"{PROMPT_LINE}"
    )

    result = compose_findings((_finding(),), _composer(_Raises(error), session, revision_id))

    assert result.mode is CompositionMode.FALLBACK
    [row] = _rows(session, revision_id)
    assert row.outcome == ModelInvocationOutcome.FAILED
    reason = row.rejection_reason
    assert reason is not None
    assert reason.startswith("ModelErrorException: The model could not answer")
    assert len(reason) <= 200 and "\n" not in reason
    for leaked in (FAKE_ACCESS_KEY, FAKE_SECRET, "example.invalid", PROMPT_LINE):
        assert leaked not in reason


def test_a_wording_call_that_timed_out_keeps_why(session: Session) -> None:
    revision_id = _revision_without_run(session)

    compose_findings(
        (_finding(),),
        _composer(_Raises(TimeoutError("read timed out after 34 s")), session, revision_id),
    )

    [row] = _rows(session, revision_id)
    assert row.outcome == ModelInvocationOutcome.TIMEOUT
    assert row.rejection_reason == "TimeoutError: read timed out after 34 s"
    assert (row.input_tokens, row.output_tokens) == (0, 0)


def test_a_rejected_wording_answer_is_stored_as_before(session: Session) -> None:
    """An answer came back but broke the contract: the long-standing `rejected` reason, unchanged
    (class and message, up to 500 characters)."""
    from time import monotonic_ns

    revision_id = _revision_without_run(session)
    message = "the answer named a finding that was not asked about"

    row = BedrockConverseInvocationRecorder(session, revision_id).record(
        model_id="made-up-wording-model",
        prompt_id=PROMPT_ID,
        template_id=TEMPLATE_ID,
        started_ns=monotonic_ns(),
        response={"stopReason": "tool_use", "usage": {"inputTokens": 9, "outputTokens": 3}},
        error=ValueError(message),
    )

    assert row.outcome == ModelInvocationOutcome.REJECTED
    assert row.rejection_reason == f"ValueError: {message}"


def test_a_successful_wording_call_is_stored_as_before(session: Session) -> None:
    revision_id = _revision_without_run(session)
    finding = _finding()

    result = compose_findings(
        (finding,), _composer(_Answers(_findings_response(finding.key)), session, revision_id)
    )

    assert result.mode is CompositionMode.LLM
    [row] = _rows(session, revision_id)
    assert (row.outcome, row.rejection_reason) == ("ok", None)


@pytest.mark.parametrize(
    ("response", "outcome"),
    [
        (None, ModelInvocationOutcome.FAILED),
        (
            {"stopReason": "tool_use", "usage": {"inputTokens": 9, "outputTokens": 3}},
            ModelInvocationOutcome.REJECTED,
        ),
    ],
)
def test_a_nul_byte_in_the_error_does_not_lose_the_row(
    session: Session, response: Mapping[str, Any] | None, outcome: ModelInvocationOutcome
) -> None:
    """Review of #1218: PostgreSQL refuses a NUL in text, so the insert failed and the call's row
    was lost. Both the failed and the rejected path store the cause with the NUL replaced."""
    from time import monotonic_ns

    revision_id = _revision_without_run(session)

    row = BedrockConverseInvocationRecorder(session, revision_id).record(
        model_id="made-up-wording-model",
        prompt_id=PROMPT_ID,
        template_id=TEMPLATE_ID,
        started_ns=monotonic_ns(),
        response=response,
        error=ValueError("the stream broke\x00 here \x1b[31mred\x1b[0m \u202eend"),
    )
    session.flush()

    assert row.outcome == outcome
    assert row.rejection_reason is not None
    assert "\x00" not in row.rejection_reason and "\x1b" not in row.rejection_reason
    assert "\u202e" not in row.rejection_reason
    assert row.rejection_reason.startswith("ValueError: the stream broke")
