"""All AI spending so far for a project: its own calls plus earlier runs (#1165).

`GET /projects/{id}/usage/history` adds this project's recorded calls (as `GET /usage` counts them)
to the imported `ai_spend_history` rows, by model and route, and lists the earlier runs. All values
are synthetic.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alembic import command
from app.api.dependencies import get_session
from app.api.usage import provider_usage_check
from app.auth import Principal, Role, authenticate
from app.config import Settings
from app.db.session import session_factory
from app.main import API_PREFIX, create_app
from app.models import AiSpendHistory, ModelInvocation, Package, PackageRevision, Project
from app.provider_usage import CREDITS_URL, KEY_URL, OpenRouterUsageCheck
from app.schemas.visual_ui import UsageHistoryOut
from tests.app.postgres_fixture import alembic_config

pytest_plugins = ("tests.app.postgres_fixture",)

T0 = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
SYNTHETIC_KEY = "synthetic-test-key-not-real"


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


def _client(
    session: Session, project_id: UUID, *, check: OpenRouterUsageCheck | None = None
) -> TestClient:
    app = create_app(Settings(database_url="postgresql+psycopg://gv:gv@localhost:5433/gv"))
    app.dependency_overrides[authenticate] = lambda: Principal(
        id="reviewer", roles=frozenset({Role.REVIEWER}), projects=frozenset({project_id})
    )
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[provider_usage_check] = lambda: check
    return TestClient(app)


def _project(session: Session, name: str = "Synthetic project") -> UUID:
    project = Project(name=name)
    session.add(project)
    session.flush()
    return project.id


def _revision(session: Session, project_id: UUID) -> UUID:
    package = Package(project_id=project_id, vendor="Synthetic vendor", product_type="countertop")
    session.add(package)
    session.flush()
    revision = PackageRevision(package_id=package.id, revision_number=1, state="AWAITING_REVIEW")
    session.add(revision)
    session.flush()
    return revision.id


def _call(
    session: Session,
    revision_id: UUID,
    *,
    model: str = "anthropic.claude-opus-5-5",
    prompt: str = "claude-slot-span-v2",
    tokens: tuple[int, int] = (100, 10),
    cost: int | None = 1_000,
    outcome: str = "ok",
) -> None:
    session.add(
        ModelInvocation(
            package_revision_id=revision_id,
            extraction_run_id=None,
            model_id=model,
            prompt_id=prompt,
            template_id="synthetic-template",
            input_tokens=tokens[0],
            output_tokens=tokens[1],
            cost_micros=cost,
            latency_ms=1,
            outcome=outcome,
            created_at=T0,
        )
    )
    session.flush()


def _history(
    session: Session,
    project_id: UUID,
    *,
    day: date = date(2026, 1, 1),
    model: str = "anthropic.claude-opus-5-5",
    route: str = "unknown",
    purpose: str = "reading",
    calls: int = 4,
    cost: int = 2_000_000,
    unpriced: int = 0,
    label: str = "Earlier runs on this machine",
) -> None:
    session.add(
        AiSpendHistory(
            project_id=project_id,
            occurred_on=day,
            model_id=model,
            route=route,
            purpose=purpose,
            calls=calls,
            input_tokens=calls * 1000,
            output_tokens=calls * 100,
            cost_micros=cost,
            unpriced_calls=unpriced,
            source_label=label,
            source_key=f"{day.isoformat()}|{model}|{purpose}",
        )
    )
    session.flush()


def _history_of(
    session: Session, project_id: UUID, check: OpenRouterUsageCheck | None = None
) -> dict[str, Any]:
    response = _client(session, project_id, check=check).get(
        f"{API_PREFIX}/projects/{project_id}/usage/history"
    )
    assert response.status_code == 200, response.text
    UsageHistoryOut.model_validate(response.json())
    body: dict[str, Any] = response.json()
    return body


def test_with_no_history_the_totals_are_this_projects_calls(session: Session) -> None:
    project_id = _project(session)
    revision_id = _revision(session, project_id)
    _call(session, revision_id)
    _call(session, revision_id, model="anthropic.claude-sonnet-5-5", cost=500)

    body = _history_of(session, project_id)

    assert body["earlier"] == {
        "calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": "0.000000",
        "unpriced_calls": 0,
    }
    assert body["totals"] == body["this_project"]
    assert body["totals"]["calls"] == 2
    assert body["totals"]["cost_usd"] == "0.001500"
    assert body["earlier_runs"] == []
    assert body["provider_checks"] == []
    assert [(row["model"], row["route"]) for row in body["by_model"]] == [
        ("anthropic.claude-opus-5-5", "unknown"),
        ("anthropic.claude-sonnet-5-5", "unknown"),
    ]


def test_an_empty_project_answers_zero_not_an_error(session: Session) -> None:
    project_id = _project(session)

    body = _history_of(session, project_id)

    assert body["totals"]["calls"] == 0
    assert body["by_model"] == [] and body["earlier_runs"] == []


def test_history_is_in_the_all_time_total_and_by_model(session: Session) -> None:
    project_id = _project(session)
    revision_id = _revision(session, project_id)
    _call(session, revision_id, cost=1_000_000)
    _history(session, project_id, calls=4, cost=2_000_000)
    _history(
        session,
        project_id,
        model="qwen.qwen3-vl-235b-a22b",
        route="bedrock",
        purpose="bake-off",
        calls=3,
        cost=500_000,
        unpriced=1,
    )

    body = _history_of(session, project_id)

    assert body["this_project"]["calls"] == 1
    assert body["earlier"]["calls"] == 7
    assert body["earlier"]["unpriced_calls"] == 1
    assert body["totals"]["calls"] == 8
    assert body["totals"]["cost_usd"] == "3.500000"
    assert body["totals"]["unpriced_calls"] == 1
    opus = body["by_model"][0]
    assert (opus["model"], opus["route"], opus["calls"], opus["cost_usd"]) == (
        "anthropic.claude-opus-5-5",
        "unknown",
        5,
        "3.000000",
    )
    qwen = body["by_model"][1]
    assert (qwen["route"], qwen["calls"], qwen["unpriced_calls"]) == ("bedrock", 3, 1)


def test_earlier_runs_are_grouped_by_day_purpose_and_source_newest_first(
    session: Session,
) -> None:
    project_id = _project(session)
    _history(session, project_id, day=date(2026, 1, 1), purpose="reading")
    _history(
        session,
        project_id,
        day=date(2026, 1, 1),
        model="anthropic.claude-sonnet-5-5",
        purpose="reading",
        calls=2,
        cost=100,
    )
    _history(session, project_id, day=date(2026, 1, 1), purpose="row-choice", calls=1, cost=7)
    _history(session, project_id, day=date(2026, 1, 3), purpose="bake-off", calls=1, cost=9)

    runs = _history_of(session, project_id)["earlier_runs"]

    assert [(run["day"], run["purpose"], run["calls"]) for run in runs] == [
        ("2026-01-03", "bake-off", 1),
        ("2026-01-01", "reading", 6),
        ("2026-01-01", "row-choice", 1),
    ]
    assert runs[1]["models"] == ["anthropic.claude-opus-5-5", "anthropic.claude-sonnet-5-5"]
    assert runs[1]["cost_usd"] == "2.000100"
    assert runs[1]["source_label"] == "Earlier runs on this machine"


def test_another_projects_history_and_calls_never_show(session: Session) -> None:
    project_id = _project(session)
    other_id = _project(session, "Another synthetic project")
    _history(session, other_id, calls=9, cost=9_000_000)
    _call(session, _revision(session, other_id), cost=5_000_000)
    _history(session, project_id, calls=1, cost=10)

    body = _history_of(session, project_id)

    assert body["totals"]["calls"] == 1
    assert body["totals"]["cost_usd"] == "0.000010"
    assert body["this_project"]["calls"] == 0


def test_a_project_the_reader_cannot_see_is_refused(session: Session) -> None:
    project_id = _project(session)
    other_id = _project(session, "Another synthetic project")
    _history(session, other_id)

    response = _client(session, project_id).get(f"{API_PREFIX}/projects/{other_id}/usage/history")

    assert response.status_code in (403, 404)


def test_a_failed_call_with_no_tokens_costs_nothing_and_an_unpriced_call_is_counted(
    session: Session,
) -> None:
    project_id = _project(session)
    revision_id = _revision(session, project_id)
    _call(session, revision_id, tokens=(0, 0), cost=None, outcome="failed")
    _call(session, revision_id, cost=None)

    body = _history_of(session, project_id)

    assert body["totals"]["calls"] == 2
    assert body["totals"]["unpriced_calls"] == 1
    assert body["totals"]["cost_usd"] == "0.000000"


def test_the_assistant_went_through_openrouter(session: Session) -> None:
    project_id = _project(session)
    revision_id = _revision(session, project_id)
    _call(session, revision_id, model="anthropic.claude-sonnet-5-5", prompt="review-assistant-v3")
    _call(session, revision_id, model="us.amazon.nova-pro-v1:0", prompt="findings-composer-v3")

    routes = {row["model"]: row["route"] for row in _history_of(session, project_id)["by_model"]}

    assert routes == {
        "anthropic.claude-sonnet-5-5": "openrouter",
        "us.amazon.nova-pro-v1:0": "bedrock",
    }


def test_the_existing_usage_response_is_unchanged(session: Session) -> None:
    project_id = _project(session)
    revision_id = _revision(session, project_id)
    _call(session, revision_id)
    _history(session, project_id, calls=50)

    response = _client(session, project_id).get(f"{API_PREFIX}/projects/{project_id}/usage")

    assert response.status_code == 200
    assert response.json()["totals"]["calls"] == 1
    assert set(response.json()) == {
        "from",
        "to",
        "group_by",
        "totals",
        "groups",
        "package_reading_times",
        "reading_times",
    }


# ---------------------------------------------------------------------------
# The OpenRouter cross-check, with a fake transport only (no request leaves a test)
# ---------------------------------------------------------------------------


class _FakeOpenRouter:
    def __init__(self, replies: Mapping[str, tuple[int, bytes]]) -> None:
        self.replies = replies
        self.asked: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url: str, headers: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
        del timeout
        self.asked.append((url, dict(headers)))
        return self.replies.get(url, (500, b""))


def test_the_provider_total_is_shown_beside_the_recorded_total(session: Session) -> None:
    project_id = _project(session)
    fake = _FakeOpenRouter(
        {CREDITS_URL: (200, b'{"data": {"total_credits": 50, "total_usage": 12.345678}}')}
    )
    check = OpenRouterUsageCheck(SYNTHETIC_KEY, fetch=fake)

    body = _history_of(session, project_id, check)

    [provider] = body["provider_checks"]
    assert provider["provider"] == "openrouter"
    assert (provider["status"], provider["scope"], provider["used_usd"]) == (
        "ok",
        "account",
        "12.345678",
    )
    assert fake.asked[0][1]["Authorization"] == f"Bearer {SYNTHETIC_KEY}"
    assert SYNTHETIC_KEY not in str(body)
    assert SYNTHETIC_KEY not in repr(check)


def test_a_key_that_cannot_read_the_account_reports_its_own_usage() -> None:
    fake = _FakeOpenRouter(
        {CREDITS_URL: (403, b""), KEY_URL: (200, b'{"data": {"usage": 3.5, "limit": null}}')}
    )

    result = OpenRouterUsageCheck(SYNTHETIC_KEY, fetch=fake).check()

    assert (result.status, result.scope, result.used_usd) == ("ok", "key", "3.500000")


@pytest.mark.parametrize(
    "replies",
    [{}, {CREDITS_URL: (200, b"not json")}, {CREDITS_URL: (200, b'{"data": {"total_usage": -1}}')}],
    ids=["server error", "not json", "negative"],
)
def test_no_answer_is_unavailable_never_zero(replies: Mapping[str, tuple[int, bytes]]) -> None:
    result = OpenRouterUsageCheck(SYNTHETIC_KEY, fetch=_FakeOpenRouter(replies)).check()

    assert (result.status, result.used_usd) == ("unavailable", None)


def test_a_transport_failure_is_unavailable() -> None:
    def broken(url: str, headers: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
        raise TimeoutError

    assert OpenRouterUsageCheck(SYNTHETIC_KEY, fetch=broken).check().status == "unavailable"


def test_the_answer_is_kept_for_five_minutes() -> None:
    now = [0.0]
    fake = _FakeOpenRouter({CREDITS_URL: (200, b'{"data": {"total_usage": 1}}')})
    check = OpenRouterUsageCheck(SYNTHETIC_KEY, fetch=fake, clock=lambda: now[0])

    check.check()
    now[0] = 299.0
    check.check()
    assert len(fake.asked) == 1
    now[0] = 301.0
    check.check()
    assert len(fake.asked) == 2


class _FakeRequest:
    def __init__(self, settings: Settings) -> None:
        self.app = type("App", (), {})()
        self.app.state = type("State", (), {})()
        self.app.state.settings = settings


def test_the_check_is_off_without_a_key_or_when_switched_off() -> None:
    url = "postgresql+psycopg://gv:gv@localhost:5433/gv"
    no_key = Settings(database_url=url)
    switched_off = Settings(
        database_url=url,
        usage_provider_check=False,
        openrouter_api_key=SecretStr(SYNTHETIC_KEY),
    )
    with_key = Settings(database_url=url, openrouter_api_key=SecretStr(SYNTHETIC_KEY))

    assert provider_usage_check(_FakeRequest(no_key)) is None  # type: ignore[arg-type]
    assert provider_usage_check(_FakeRequest(switched_off)) is None  # type: ignore[arg-type]
    request = _FakeRequest(with_key)
    first = provider_usage_check(request)  # type: ignore[arg-type]
    assert isinstance(first, OpenRouterUsageCheck)
    assert provider_usage_check(request) is first  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The table's own rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "change",
    [
        {"calls": 0},
        {"unpriced": 5},
        {"route": "somewhere"},
        {"purpose": "fun"},
        {"cost": -1},
        {"label": "  "},
    ],
    ids=["no calls", "more unpriced than calls", "route", "purpose", "negative cost", "blank"],
)
def test_the_table_refuses_a_row_that_cannot_be_true(
    session: Session, change: dict[str, Any]
) -> None:
    project_id = _project(session)
    with pytest.raises(IntegrityError):
        _history(session, project_id, **change)


def test_one_row_per_project_and_source_key(session: Session) -> None:
    project_id = _project(session)
    _history(session, project_id)
    _history(session, _project(session, "Another synthetic project"))
    with pytest.raises(IntegrityError):
        _history(session, project_id)
