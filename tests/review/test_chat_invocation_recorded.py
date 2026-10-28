"""Reviewer-facing Bedrock narration calls are recorded in the model ledger.

The old reviewer chat was removed on 2026-10-28; the recorder properties its calls proved (#694,
#700) are now proved through the findings wording call, the recorder's remaining caller.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
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


def _composer(
    session: Session, revision_id: UUID, response: Mapping[str, object]
) -> BedrockFindingsComposer:
    return BedrockFindingsComposer(
        NovaConfig(
            model_id="configured-model",
            prompt_id=FINDINGS_PROMPT_ID,
            template_id=FINDINGS_TEMPLATE_ID,
            connect_timeout_seconds=1,
            read_timeout_seconds=2,
            max_attempts=1,
            region_name="us-east-1",
        ),
        _Client(response),
        BedrockConverseInvocationRecorder(session, revision_id),
    )


def _rows(session: Session) -> list[ModelInvocation]:
    return list(session.scalars(select(ModelInvocation).order_by(ModelInvocation.created_at)))


def test_findings_narration_success_writes_one_model_invocation(
    postgres_engine: Engine,
) -> None:
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with factory() as session:
        revision_id, _run_id = _revision_with_run(session)
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
        assert rows[0].extraction_run_id is None
        assert rows[0].package_revision_id == revision_id
        assert rows[0].model_id == "operator-configured-model"
        assert rows[0].prompt_id == FINDINGS_PROMPT_ID
        assert rows[0].template_id == FINDINGS_TEMPLATE_ID
        assert rows[0].input_tokens == 222
        assert rows[0].output_tokens == 33
        # No price file is stated in this test, so the cost of a call that used tokens is unknown —
        # recorded as NULL, never as a free 0 (#700).
        assert rows[0].cost_micros is None
        assert rows[0].outcome == ModelInvocationOutcome.OK


def _revision_without_run(session: Session) -> UUID:
    """A package a reviewer typed values into and nobody ever extracted.

    `scripts/seed_demo.py` produces exactly this without `--with-evidence`, and `CLIENT_FACTS` Q7
    describes it as a supported flow: *"the reviewer types the values into input fields for that
    drawing set."* It is not an exotic state.
    """
    project = Project(name=f"GV typed package {uuid4()}")
    session.add(project)
    session.flush()
    package = Package(project_id=project.id, vendor=None)
    session.add(package)
    session.flush()
    revision = PackageRevision(
        package_id=package.id, revision_number=1, state=PackageState.AWAITING_REVIEW
    )
    session.add(revision)
    session.flush()
    return revision.id


def test_a_call_on_a_package_nobody_extracted_answers_and_is_recorded(
    postgres_engine: Engine,
) -> None:
    """**The defect #694 reproduced**, now the property that must hold.

    The recorder used to anchor a review-time call to the package's *latest* extraction run. On a
    package with none, that lookup raised — and because the model call had succeeded,
    `_record_invocation` re-raised rather than swallowing it. So the reviewer's question failed
    after Bedrock had already been paid for an answer nobody saw.
    """
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with factory() as session:
        revision_id = _revision_without_run(session)
        finding = _finding()
        composer = _composer(session, revision_id, _findings_response(finding.key))

        reply = composer.compose((finding,))

        assert reply is not None, "the call did not answer"
        rows = _rows(session)
        assert len(rows) == 1, "exactly one row per call, whatever the origin"
        assert rows[0].extraction_run_id is None
        assert rows[0].package_revision_id == revision_id


def test_a_row_with_no_origin_cannot_be_built() -> None:
    """Neither origin means a paid call attributable to nothing, which is worse than not recording
    it: the row exists and looks like an answer.
    """
    from extraction.models.invocations import InvocationRecord

    with pytest.raises(ValueError, match="exactly one of"):
        InvocationRecord(
            extraction_run_id=None,
            model_id="m",
            prompt_id="p",
            template_id="t",
            crop_artifact_id=None,
            input_tokens=1,
            output_tokens=1,
            cost_micros=0,
            latency_ms=1,
            outcome="succeeded",
        )


def test_a_row_with_both_origins_cannot_be_built() -> None:
    """Both would let one call be counted against two packages by the cost ceiling."""
    from extraction.models.invocations import InvocationRecord

    with pytest.raises(ValueError, match="exactly one of"):
        InvocationRecord(
            extraction_run_id=uuid4(),
            package_revision_id=uuid4(),
            model_id="m",
            prompt_id="p",
            template_id="t",
            crop_artifact_id=None,
            input_tokens=1,
            output_tokens=1,
            cost_micros=0,
            latency_ms=1,
            outcome="succeeded",
        )


def test_with_a_stated_price_the_call_records_its_real_cost(
    postgres_engine: Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """**#700.** The same call as above, with a price file stated: 321 input and 45 output tokens at
    $0.0008 and $0.0032 per 1,000 is $0.0004008, recorded exactly as 401 millionths."""
    import json

    from app.runs import rates

    price_file = tmp_path / "rates.json"
    price_file.write_text(
        json.dumps(
            {
                "source": "a test price list",
                "retrieved": "2026-09-30",
                "currency": "USD",
                "rates": {
                    "configured-model": {
                        "input_per_1k_tokens": "0.0008",
                        "output_per_1k_tokens": "0.0032",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    rates._cached.cache_clear()
    monkeypatch.setenv(rates.MODEL_RATES_ENV, str(price_file))
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with factory() as session:
        revision_id, _run_id = _revision_with_run(session)
        finding = _finding()
        response = _findings_response(finding.key)
        # The token counts the removed chat test used, so the expected cost below is unchanged.
        response["usage"] = {"inputTokens": 321, "outputTokens": 45}
        _composer(session, revision_id, response).compose((finding,))

        assert _rows(session)[0].cost_micros == 401
