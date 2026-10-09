"""A reviewer chat call reaches the Usage page (#1134).

The chat records each Bedrock call in `model_invocations`, the table `/usage` reads. But
`get_session` never commits, so a row the route wrote and nobody committed was thrown away when the
request's session closed: the call was paid for and never counted.

**Why the earlier tests could not see it.** `tests/review/test_chat_invocation_recorded.py` reads the
row back through the same session that wrote it, and the other chat API tests hand every request one
shared session. Both see an uncommitted row. Here the app opens its own session per request, as it
does in production, so `/usage` sees only what the chat request actually committed.

No paid calls: the Bedrock client is a fake that returns a fixed reply.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from alembic import command
from app.auth import Principal, Role, authenticate
from app.config import Settings
from app.db.session import session_factory
from app.main import create_app
from app.models import (
    CheckRun,
    Finding,
    Package,
    PackageRevision,
    PackageState,
    Project,
    RuleDefinition,
    RuleSnapshot,
)
from app.review.chat_bedrock import TOOL_NAME, BedrockReviewerChat
from rules.schema import CheckType, GlobalApplicability, InputSelector, OperationRef, Rule
from rules.semantic_types import OperandSource, ProductType, SemanticType
from rules.snapshot import canonical_json
from tests.app.postgres_fixture import alembic_config
from units.measurement import Unit
from verdict.outcomes import Outcome, Severity

pytest_plugins = ("tests.app.postgres_fixture",)

MODEL = "test-chat-model"


class _FakeBedrock:
    """Answers every converse call with one fixed narration and counts the calls."""

    def __init__(self, finding_id: UUID) -> None:
        self.finding_id = finding_id
        self.calls = 0

    def converse(self, **_: object) -> Mapping[str, object]:
        self.calls += 1
        return {
            "stopReason": "tool_use",
            "usage": {"inputTokens": 321, "outputTokens": 45},
            "output": {
                "message": {
                    "content": [
                        {
                            "toolUse": {
                                "name": TOOL_NAME,
                                "input": {
                                    "summary": "The countertop depth needs a look.",
                                    "findings": [
                                        {
                                            "finding_key": str(self.finding_id),
                                            "explanation": "Compare the vendor depth with the plan.",
                                        }
                                    ],
                                },
                            }
                        }
                    ]
                }
            },
        }


def _rule() -> Rule:
    return Rule(
        id="CHAT-USAGE-001",
        version="1.0.0",
        product_type=ProductType.COUNTERTOP,
        check_type=CheckType.ARCH_VS_SHOP,
        severity=Severity.CRITICAL,
        arithmetic_unit=Unit.INCH,
        inputs={
            "actual": InputSelector(source=OperandSource.SHOP, semantic_type=SemanticType.CT001),
            "expected": InputSelector(source=OperandSource.ARCH, semantic_type=SemanticType.CT001),
        },
        applicability=GlobalApplicability(scope="global"),
        operation=OperationRef(
            type="equals", operands={"actual": "actual", "expected": "expected"}
        ),
    )


def _seed_failed_check(engine: Engine) -> tuple[UUID, UUID, UUID]:
    """A package whose checks ran and failed once, committed so the app's own sessions see it."""
    with session_factory(engine)() as session:
        project = Project(name=f"chat usage {uuid4()}")
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
        rule = _rule()
        body = canonical_json(rule)
        definition = RuleDefinition(rule_id=rule.id)
        session.add(definition)
        session.flush()
        snapshot = RuleSnapshot(
            rule_definition_id=definition.id,
            snapshot_id=f"sha256:{hashlib.sha256(body.encode()).hexdigest()}",
            version=rule.version,
            canonical_json=body,
            product_type=rule.product_type,
            check_type=rule.check_type,
            unconfirmed_tolerance_count=0,
        )
        session.add(snapshot)
        session.flush()
        run = CheckRun(
            package_revision_id=revision.id,
            rule_snapshot_id=snapshot.id,
            engine_version="verdict-test-1",
        )
        session.add(run)
        session.flush()
        finding = Finding(
            check_run_id=run.id,
            package_revision_id=revision.id,
            outcome=Outcome.FAIL,
            severity=Severity.CRITICAL,
            trace={"comparison": "a != b", "outcome": Outcome.FAIL},
            parameter_set_versions={"global": "sha256:parameters"},
        )
        session.add(finding)
        session.commit()
        return project.id, package.id, finding.id


@contextmanager
def _app_with_its_own_sessions(engine: Engine, project_id: UUID) -> Iterator[TestClient]:
    """The app as deployed: it builds its engine from settings and opens one session per request.

    `get_session` is deliberately *not* overridden. Sharing one session across requests is what hid
    this defect: the second request would see the first one's uncommitted row.
    """
    app = create_app(
        Settings(
            database_url=engine.url.render_as_string(hide_password=False),
            environment="test",
            bedrock_chat_enabled=True,
            bedrock_model=MODEL,
        )
    )
    app.dependency_overrides[authenticate] = lambda: Principal(
        id="reviewer-1", roles=frozenset({Role.ADMIN}), projects=frozenset({project_id})
    )
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        own_engine = getattr(app.state, "_gv_read_engine", None)
        if own_engine is not None:
            own_engine.dispose()


@pytest.fixture
def priced(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A stated price for the test model, so the call has an exact cost to show."""
    from app.runs import rates

    price_file = tmp_path / "rates.json"
    price_file.write_text(
        json.dumps(
            {
                "source": "a test price list",
                "retrieved": "2026-10-10",
                "currency": "USD",
                "rates": {
                    MODEL: {"input_per_1k_tokens": "0.0008", "output_per_1k_tokens": "0.0032"}
                },
            }
        ),
        encoding="utf-8",
    )
    rates._cached.cache_clear()
    monkeypatch.setenv(rates.MODEL_RATES_ENV, str(price_file))


def _ask(client: TestClient, path: str, project_id: UUID, package_id: UUID) -> None:
    response = client.post(
        f"/api/v1/projects/{project_id}/packages/{package_id}/{path}",
        json={"question": "Why did this fail?"},
    )
    assert response.status_code == 200, response.text
    if path == "chat/stream":
        assert "event: narration" in response.text, response.text
        assert "event: error" not in response.text, response.text


@pytest.mark.usefixtures("priced")
@pytest.mark.parametrize("path", ["chat", "chat/stream"])
def test_a_chat_call_shows_on_usage_with_its_cost(
    postgres_engine: Engine, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """**Input: one chat question, answered by a fake Bedrock. Outcome: `/usage` shows one call,
    321 + 45 tokens, $0.000401** (321 x $0.0008 + 45 x $0.0032 per 1,000 tokens, in millionths)."""
    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")
    project_id, package_id, finding_id = _seed_failed_check(postgres_engine)
    fake = _FakeBedrock(finding_id)
    monkeypatch.setattr(BedrockReviewerChat, "_client_for_request", lambda _self: fake)

    with _app_with_its_own_sessions(postgres_engine, project_id) as client:
        _ask(client, path, project_id, package_id)
        assert fake.calls == 1, "the fake model was not asked, so nothing was recorded"
        usage = client.get(f"/api/v1/projects/{project_id}/usage")

    assert usage.status_code == 200, usage.text
    totals: dict[str, Any] = usage.json()["totals"]
    assert totals["calls"] == 1, f"the chat call never reached Usage: {totals}"
    assert totals["failed_calls"] == 0
    assert totals["input_tokens"] == 321
    assert totals["output_tokens"] == 45
    assert totals["unpriced_calls"] == 0
    assert totals["cost_usd"] == "0.000401"
    [group] = usage.json()["groups"]
    assert [model["model"] for model in group["models"]] == [MODEL]


class _RefusingBedrock:
    """A guardrail refusal: the call was made and billed for its input, and no answer came back."""

    def __init__(self) -> None:
        self.calls = 0

    def converse(self, **_: object) -> Mapping[str, object]:
        self.calls += 1
        return {
            "stopReason": "guardrail_intervened",
            "usage": {"inputTokens": 19, "outputTokens": 8},
            "output": {"message": {"content": []}},
        }


@pytest.mark.parametrize("path", ["chat", "chat/stream"])
def test_a_refused_chat_call_still_shows_on_usage_as_failed(
    postgres_engine: Engine, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """**Input: the model refuses. Outcome: the reviewer gets the plain answer, and `/usage` shows one
    failed call with its 19 input tokens.** The row is committed when it is recorded, so a call that
    went wrong is counted as well as one that went right."""
    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")
    project_id, package_id, _ = _seed_failed_check(postgres_engine)
    fake = _RefusingBedrock()
    monkeypatch.setattr(BedrockReviewerChat, "_client_for_request", lambda _self: fake)

    with _app_with_its_own_sessions(postgres_engine, project_id) as client:
        _ask(client, path, project_id, package_id)
        assert fake.calls == 1
        usage = client.get(f"/api/v1/projects/{project_id}/usage")

    assert usage.status_code == 200, usage.text
    totals: dict[str, Any] = usage.json()["totals"]
    assert totals["calls"] == 1, f"the refused call never reached Usage: {totals}"
    assert totals["failed_calls"] == 1
    assert totals["input_tokens"] == 19
    assert totals["output_tokens"] == 0
