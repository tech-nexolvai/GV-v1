"""All AI spending so far for a project: its own calls plus earlier runs (#1165).

`GET /projects/{id}/usage/history` adds this project's recorded calls (as `GET /usage` counts them)
to the imported `ai_spend_history` calls, by model and route, and lists the earlier runs. The
provider's own report is a separate endpoint. All values are synthetic.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from alembic import command
from app.api.dependencies import get_session
from app.api.usage import provider_usage_check
from app.auth import Principal, Role, authenticate
from app.config import Settings
from app.db.session import session_factory
from app.main import API_PREFIX, create_app
from app.models import AiSpendHistory, ModelInvocation, Package, PackageRevision, Project
from app.provider_usage import KEY_URL, OpenRouterUsageCheck
from app.schemas.visual_ui import ProviderChecksOut, UsageHistoryOut
from tests.app.postgres_fixture import alembic_config

pytest_plugins = ("tests.app.postgres_fixture",)

T0 = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
SYNTHETIC_KEY = "synthetic-test-key-not-real"
DB_URL = "postgresql+psycopg://gv:gv@localhost:5433/gv"


def _settings(**values: Any) -> Settings:
    """Settings that ignore any real `.env` in the checkout."""
    return Settings(_env_file=None, database_url=DB_URL, **values)  # type: ignore[call-arg]


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
    app = create_app(_settings())
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
    cost: int | None = 500_000,
    priced_later: bool = False,
    label: str = "Earlier runs on this machine",
    call_id: UUID | None = None,
) -> None:
    """`calls` imported calls, each with `cost` (None: unpriced)."""
    for _ in range(calls):
        session.add(
            AiSpendHistory(
                project_id=project_id,
                call_id=call_id or uuid4(),
                occurred_on=day,
                model_id=model,
                route=route,
                purpose=purpose,
                input_tokens=1000,
                output_tokens=100,
                cost_micros=cost,
                priced_later=priced_later,
                source_label=label,
            )
        )
    session.flush()


def _history_of(session: Session, project_id: UUID) -> dict[str, Any]:
    response = _client(session, project_id).get(f"{API_PREFIX}/projects/{project_id}/usage/history")
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
        "priced_later_calls": 0,
        "priced_later_cost_usd": "0.000000",
    }
    assert body["totals"] == body["this_project"]
    assert body["totals"]["calls"] == 2
    assert body["totals"]["cost_usd"] == "0.001500"
    assert body["earlier_runs"] == []
    assert "provider_checks" not in body
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
    _history(session, project_id, calls=4, cost=500_000)
    for calls, cost in ((2, 250_000), (1, None)):
        _history(
            session,
            project_id,
            model="qwen.qwen3-vl-235b-a22b",
            route="bedrock",
            purpose="bake-off",
            calls=calls,
            cost=cost,
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
    qwen_row = body["by_model"][1]
    assert (qwen_row["route"], qwen_row["calls"], qwen_row["unpriced_calls"]) == ("bedrock", 3, 1)


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
        cost=50,
    )
    _history(session, project_id, day=date(2026, 1, 1), purpose="row-choice", calls=1, cost=7)
    _history(session, project_id, day=date(2026, 1, 3), purpose="bake-off", calls=1, cost=9)
    _history(session, project_id, day=date(2026, 1, 3), purpose="findings", calls=1, cost=3)

    runs = _history_of(session, project_id)["earlier_runs"]

    assert [(run["day"], run["purpose"], run["calls"]) for run in runs] == [
        ("2026-01-03", "findings", 1),
        ("2026-01-03", "bake-off", 1),
        ("2026-01-01", "reading", 6),
        ("2026-01-01", "row-choice", 1),
    ]
    assert runs[2]["models"] == ["anthropic.claude-opus-5-5", "anthropic.claude-sonnet-5-5"]
    assert runs[2]["cost_usd"] == "2.000100"
    assert runs[2]["source_label"] == "Earlier runs on this machine"


def test_calls_priced_later_are_counted_and_their_cost_said(session: Session) -> None:
    project_id = _project(session)
    revision_id = _revision(session, project_id)
    # A pre-#754 row of this project: stored as 0 although it used tokens.
    _call(session, revision_id, model="amazon.nova-pro-v1:0", tokens=(1000, 100), cost=0)
    _history(session, project_id, calls=2, cost=40, priced_later=True)

    body = _history_of(session, project_id)

    # Nova Pro: 1000 * 0.0008/1k + 100 * 0.0032/1k = $0.00112, from the published price file.
    assert body["this_project"]["priced_later_calls"] == 1
    assert body["this_project"]["priced_later_cost_usd"] == "0.001120"
    assert body["earlier"]["priced_later_calls"] == 2
    assert body["totals"]["priced_later_calls"] == 3
    assert body["totals"]["priced_later_cost_usd"] == "0.001200"
    assert body["totals"]["cost_usd"] == "0.001200"
    assert body["earlier_runs"][0]["priced_later_calls"] == 2


def test_a_pre_754_zero_for_a_model_with_no_published_price_is_unpriced_not_free(
    session: Session,
) -> None:
    project_id = _project(session)
    revision_id = _revision(session, project_id)
    _call(session, revision_id, model="synthetic.unpublished-model", tokens=(10, 1), cost=0)

    body = _history_of(session, project_id)

    assert body["totals"]["unpriced_calls"] == 1
    assert body["totals"]["priced_later_calls"] == 0


def test_usage_counts_the_same_calls_the_same_way(session: Session) -> None:
    project_id = _project(session)
    revision_id = _revision(session, project_id)
    _call(session, revision_id, model="amazon.nova-pro-v1:0", tokens=(1000, 100), cost=0)
    _call(session, revision_id, tokens=(0, 0), cost=None, outcome="failed")
    _call(session, revision_id, cost=None)

    usage = (
        _client(session, project_id)
        .get(f"{API_PREFIX}/projects/{project_id}/usage")
        .json()["totals"]
    )
    history = _history_of(session, project_id)["this_project"]

    assert (
        (usage["calls"], usage["cost_usd"], usage["unpriced_calls"])
        == (history["calls"], history["cost_usd"], history["unpriced_calls"])
        == (3, "0.001120", 1)
    )


def test_another_projects_history_and_calls_never_show(session: Session) -> None:
    project_id = _project(session)
    other_id = _project(session, "Another synthetic project")
    _history(session, other_id, calls=9, cost=1_000_000)
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


def test_the_existing_usage_response_keeps_its_shape(session: Session) -> None:
    project_id = _project(session)
    revision_id = _revision(session, project_id)
    _call(session, revision_id)
    _history(session, project_id, calls=5)

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
# The OpenRouter key check, with a fake transport only (no request leaves a test)
# ---------------------------------------------------------------------------


class _FakeOpenRouter:
    def __init__(self, replies: Mapping[str, tuple[int, bytes]]) -> None:
        self.replies = replies
        self.asked: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url: str, headers: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
        del timeout
        self.asked.append((url, dict(headers)))
        return self.replies.get(url, (500, b""))


def _provider(session: Session, project_id: UUID, check: OpenRouterUsageCheck | None) -> Any:
    response = _client(session, project_id, check=check).get(
        f"{API_PREFIX}/projects/{project_id}/usage/provider-check"
    )
    assert response.status_code == 200, response.text
    ProviderChecksOut.model_validate(response.json())
    return response.json()


def test_the_keys_own_usage_is_served_on_its_own_endpoint(session: Session) -> None:
    project_id = _project(session)
    fake = _FakeOpenRouter({KEY_URL: (200, b'{"data": {"usage": 12.345678, "limit": null}}')})
    check = OpenRouterUsageCheck(SYNTHETIC_KEY, fetch=fake)

    [provider] = _provider(session, project_id, check)["checks"]

    assert (provider["provider"], provider["status"], provider["used_usd"]) == (
        "openrouter",
        "ok",
        "12.345678",
    )
    # Only the key's own usage is asked for, never the account's credits.
    assert [url for url, _ in fake.asked] == [KEY_URL]
    assert fake.asked[0][1]["Authorization"] == f"Bearer {SYNTHETIC_KEY}"
    assert SYNTHETIC_KEY not in str(provider)
    assert SYNTHETIC_KEY not in repr(check)


def test_with_the_check_off_the_endpoint_answers_nothing(session: Session) -> None:
    project_id = _project(session)

    assert _provider(session, project_id, None) == {"checks": []}


@pytest.mark.parametrize(
    "replies",
    [
        {},
        {KEY_URL: (200, b"not json")},
        {KEY_URL: (200, b'{"data": {"usage": -1}}')},
        {KEY_URL: (200, b'["data"]')},
        {KEY_URL: (302, b"")},
    ],
    ids=["server error", "not json", "negative", "not an object", "redirect"],
)
def test_no_answer_is_unavailable_never_zero(replies: Mapping[str, tuple[int, bytes]]) -> None:
    result = OpenRouterUsageCheck(SYNTHETIC_KEY, fetch=_FakeOpenRouter(replies)).check()

    assert (result.status, result.used_usd) == ("unavailable", None)


@pytest.mark.parametrize("error", [TimeoutError, RuntimeError, KeyError, UnicodeDecodeError])
def test_any_failure_is_unavailable(error: type[Exception]) -> None:
    def broken(url: str, headers: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
        if error is UnicodeDecodeError:
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "synthetic")
        raise error("synthetic failure")

    assert OpenRouterUsageCheck(SYNTHETIC_KEY, fetch=broken).check().status == "unavailable"


def test_a_redirect_is_never_followed() -> None:
    from app.provider_usage import _OPENER, _RefuseRedirects

    handlers = getattr(_OPENER, "handlers", [])
    assert any(isinstance(handler, _RefuseRedirects) for handler in handlers)
    refused: object = _RefuseRedirects().redirect_request()  # type: ignore[func-returns-value]
    assert refused is None


def test_the_answer_is_kept_for_five_minutes() -> None:
    now = [0.0]
    fake = _FakeOpenRouter({KEY_URL: (200, b'{"data": {"usage": 1}}')})
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


def test_the_check_is_off_by_default_and_without_a_key() -> None:
    by_default = _settings(openrouter_api_key=SecretStr(SYNTHETIC_KEY))
    no_key = _settings(usage_provider_check=True)
    switched_on = _settings(usage_provider_check=True, openrouter_api_key=SecretStr(SYNTHETIC_KEY))

    assert provider_usage_check(_FakeRequest(by_default)) is None  # type: ignore[arg-type]
    assert provider_usage_check(_FakeRequest(no_key)) is None  # type: ignore[arg-type]
    request = _FakeRequest(switched_on)
    first = provider_usage_check(request)  # type: ignore[arg-type]
    assert isinstance(first, OpenRouterUsageCheck)
    assert provider_usage_check(request) is first  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The table's own rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "change",
    [
        {"route": "somewhere"},
        {"purpose": "fun"},
        {"cost": -1},
        {"cost": None, "priced_later": True},
        {"label": "  "},
    ],
    ids=["route", "purpose", "negative cost", "priced later with no cost", "blank label"],
)
def test_the_table_refuses_a_row_that_cannot_be_true(
    session: Session, change: dict[str, Any]
) -> None:
    project_id = _project(session)
    with pytest.raises(IntegrityError):
        _history(session, project_id, calls=1, **change)


def test_one_row_per_project_and_call(session: Session) -> None:
    project_id = _project(session)
    call_id = uuid4()
    _history(session, project_id, calls=1, call_id=call_id)
    _history(session, _project(session, "Another synthetic project"), calls=1, call_id=call_id)
    with pytest.raises(IntegrityError):
        _history(session, project_id, calls=1, call_id=call_id)


@pytest.mark.parametrize(
    "statement",
    ["UPDATE ai_spend_history SET cost_micros = 0", "DELETE FROM ai_spend_history"],
    ids=["update", "delete"],
)
def test_an_imported_call_cannot_be_changed_or_removed(session: Session, statement: str) -> None:
    project_id = _project(session)
    _history(session, project_id, calls=1)
    with pytest.raises(DBAPIError, match="append-only"):
        session.execute(text(statement))
