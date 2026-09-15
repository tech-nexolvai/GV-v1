"""Reviewer-facing Bedrock narration calls are recorded in the model ledger."""

from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alembic import command
from app.db.session import session_factory
from app.models import (
    ExtractionRun,
    ModelInvocation,
    ModelInvocationOutcome,
    Package,
    PackageRevision,
    PackageState,
    Project,
    TaskRun,
    WorkflowRun,
)
from app.review.chat_bedrock import PROMPT_ID as CHAT_PROMPT_ID
from app.review.chat_bedrock import TEMPLATE_ID as CHAT_TEMPLATE_ID
from app.review.chat_bedrock import TOOL_NAME as CHAT_TOOL_NAME
from app.review.chat_bedrock import BedrockReviewerChat, _Config
from app.runs.invocations import BedrockConverseInvocationRecorder
from extraction.models.nova import NovaConfig
from tests.app.postgres_fixture import alembic_config
from workflow.findings_bedrock import (
    PROMPT_ID as FINDINGS_PROMPT_ID,
)
from workflow.findings_bedrock import (
    TEMPLATE_ID as FINDINGS_TEMPLATE_ID,
)
from workflow.findings_bedrock import (
    TOOL_NAME as FINDINGS_TOOL_NAME,
)
from workflow.findings_bedrock import BedrockFindingsComposer
from workflow.findings_composer import ComposerFinding

pytest_plugins = ("tests.app.postgres_fixture",)


class _Client:
    def __init__(self, response: Mapping[str, object]) -> None:
        self.response = response
        self.requests: list[dict[str, object]] = []

    def converse(self, **kwargs: object) -> Mapping[str, object]:
        self.requests.append(kwargs)
        return self.response


def _upgrade(engine: Engine) -> None:
    config = alembic_config()
    config.attributes["database_url"] = engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")


def _revision_with_run(session: Session) -> tuple[UUID, UUID]:
    project = Project(name=f"GV chat invocation {uuid4()}")
    session.add(project)
    session.flush()
    package = Package(project_id=project.id, vendor=None)
    revision = PackageRevision(
        package_id=package.id,
        revision_number=1,
        state=PackageState.AWAITING_REVIEW,
    )
    session.add(package)
    session.flush()
    session.add(revision)
    session.flush()
    workflow = WorkflowRun(package_revision_id=revision.id, engine_run_id=f"run-{uuid4()}")
    session.add(workflow)
    session.flush()
    task = TaskRun(
        workflow_run_id=workflow.id,
        idempotency_key=f"chat-ledger-{uuid4()}",
        task_type="generate_outputs",
        attempt=1,
        outcome="completed",
    )
    session.add(task)
    session.flush()
    run = ExtractionRun(
        task_run_id=task.id,
        extractor="test",
        extractor_version="test/1",
        config_hash="config-v1",
    )
    session.add(run)
    session.flush()
    return revision.id, run.id


def _finding() -> ComposerFinding:
    return ComposerFinding(
        key=str(uuid4()),
        check="CT-DEPTH-001",
        check_name="Countertop depth",
        outcome="FAIL",
        severity="FLAG",
        reason="The values differ.",
        comparison="25 1/2 in vs 25 in",
        difference=None,
        tolerance=None,
        arithmetic_unit="in",
        operands=(),
        evidence_pages=("13",),
        notes=(),
    )


def _chat_response(finding_key: str) -> dict[str, object]:
    return {
        "stopReason": "tool_use",
        "usage": {"inputTokens": 321, "outputTokens": 45},
        "output": {
            "message": {
                "content": [
                    {
                        "toolUse": {
                            "name": CHAT_TOOL_NAME,
                            "input": {
                                "summary": "1 FAIL finding needs review.",
                                "findings": [
                                    {
                                        "finding_key": finding_key,
                                        "explanation": "Review the vendor depth before approval.",
                                    }
                                ],
                            },
                        }
                    }
                ]
            }
        },
    }


def _findings_response(finding_key: str) -> dict[str, object]:
    return {
        "stopReason": "tool_use",
        "usage": {"inputTokens": 222, "outputTokens": 33},
        "output": {
            "message": {
                "content": [
                    {
                        "toolUse": {
                            "name": FINDINGS_TOOL_NAME,
                            "input": {
                                "summary": "1 FAIL finding needs review.",
                                "findings": [
                                    {
                                        "finding_key": finding_key,
                                        "explanation": "Review the vendor depth before approval.",
                                    }
                                ],
                            },
                        }
                    }
                ]
            }
        },
    }


def _rows(session: Session) -> list[ModelInvocation]:
    return list(session.scalars(select(ModelInvocation).order_by(ModelInvocation.created_at)))


def test_reviewer_chat_success_writes_one_model_invocation(
    postgres_engine: Engine,
) -> None:
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with factory() as session:
        revision_id, run_id = _revision_with_run(session)
        finding = _finding()
        chat = BedrockReviewerChat(
            _Config("configured-model", "us-east-1", 1, 2),
            _Client(_chat_response(finding.key)),
            BedrockConverseInvocationRecorder(session, revision_id),
        )

        chat.compose((finding,), question="Why did this fail?")

        rows = _rows(session)
        assert len(rows) == 1
        assert rows[0].extraction_run_id == run_id
        assert rows[0].model_id == "configured-model"
        assert rows[0].prompt_id == CHAT_PROMPT_ID
        assert rows[0].template_id == CHAT_TEMPLATE_ID
        assert rows[0].input_tokens == 321
        assert rows[0].output_tokens == 45
        assert rows[0].cost_micros == 0
        assert rows[0].outcome == ModelInvocationOutcome.OK


def test_reviewer_chat_refusal_is_recorded_with_zero_output_tokens(
    postgres_engine: Engine,
) -> None:
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with factory() as session:
        revision_id, _ = _revision_with_run(session)
        chat = BedrockReviewerChat(
            _Config("configured-model", "us-east-1", 1, 2),
            _Client(
                {
                    "stopReason": "guardrail_intervened",
                    "usage": {"inputTokens": 19, "outputTokens": 8},
                    "output": {"message": {"content": []}},
                }
            ),
            BedrockConverseInvocationRecorder(session, revision_id),
        )

        with pytest.raises(RuntimeError, match="one tool call"):
            chat.compose((_finding(),), question="Why did this fail?")

        rows = _rows(session)
        assert len(rows) == 1
        assert rows[0].input_tokens == 19
        assert rows[0].output_tokens == 0
        assert rows[0].outcome == ModelInvocationOutcome.REFUSED


def test_findings_narration_success_writes_one_model_invocation(
    postgres_engine: Engine,
) -> None:
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with factory() as session:
        revision_id, run_id = _revision_with_run(session)
        finding = _finding()
        composer = BedrockFindingsComposer(
            NovaConfig(
                model_id="operator-configured-model",
                prompt_id=FINDINGS_PROMPT_ID,
                template_id=FINDINGS_TEMPLATE_ID,
                connect_timeout_seconds=1,
                read_timeout_seconds=2,
                max_attempts=1,
                region_name="us-east-1",
            ),
            _Client(_findings_response(finding.key)),
            BedrockConverseInvocationRecorder(session, revision_id),
        )

        composer.compose((finding,))

        rows = _rows(session)
        assert len(rows) == 1
        assert rows[0].extraction_run_id == run_id
        assert rows[0].model_id == "operator-configured-model"
        assert rows[0].prompt_id == FINDINGS_PROMPT_ID
        assert rows[0].template_id == FINDINGS_TEMPLATE_ID
        assert rows[0].input_tokens == 222
        assert rows[0].output_tokens == 33
        assert rows[0].cost_micros == 0
        assert rows[0].outcome == ModelInvocationOutcome.OK
