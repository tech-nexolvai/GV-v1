"""The API skeleton: settings, the error contract, request ids and two health questions (#203, C2.1).

Six API groups will hang off this app. What is tested here is the part they all inherit — so a
mistake in it is a mistake in every endpoint that follows, and the tests are mostly about the failure
paths, because those are what a client writes once and never revisits.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings
from app.errors import ErrorEnvelope
from app.main import create_app

DATABASE_URL = "postgresql+psycopg://gv:gv@localhost:5433/gv"


def _settings(**overrides: str) -> Settings:
    values: dict[str, str] = {"database_url": DATABASE_URL}
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


@pytest.fixture
def client() -> TestClient:
    """`raise_server_exceptions=False` so the unhandled-error handler is exercised rather than
    re-raised into the test — the whole point is what a *client* sees."""
    return TestClient(create_app(_settings()), raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Settings fail at startup, not at first use
# ---------------------------------------------------------------------------


def test_a_missing_database_url_fails_immediately() -> None:
    """A service that boots and then 500s on the first upload looks healthy to everything watching
    it, and the failure arrives when somebody is trying to use it."""
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_a_non_postgres_url_is_refused() -> None:
    """SQLite would accept most of the schema and silently lose what the safety argument rests on —
    no JSONB, no deferred constraints, different NUMERIC behaviour."""
    with pytest.raises(ValidationError, match="PostgreSQL"):
        _settings(database_url="sqlite:///./gv.db")


def test_an_unrecognised_setting_is_an_error_not_a_shrug() -> None:
    """A typo in a deployment variable would otherwise leave the setting at its default and the
    operator convinced they had changed it."""
    with pytest.raises(ValidationError):
        Settings(database_url=DATABASE_URL, gv_databse_url="typo")  # type: ignore[call-arg]


def test_settings_are_frozen() -> None:
    """Configuration that can change after startup is configuration nothing validated."""
    settings = _settings()
    with pytest.raises((ValidationError, AttributeError, TypeError)):
        settings.environment = "production"  # type: ignore[misc]


def test_the_factory_builds_isolated_instances() -> None:
    """A module-level `app` would make every test share one configuration, including the database
    URL — the one thing a test most needs to control."""
    first = create_app(_settings(environment="a"))
    second = create_app(_settings(environment="b"))
    assert first is not second
    assert first.state.settings.environment != second.state.settings.environment


# ---------------------------------------------------------------------------
# One error shape, including validation
# ---------------------------------------------------------------------------


def test_a_validation_failure_uses_the_same_envelope(client: TestClient) -> None:
    """FastAPI answers a bad request with its own format by default, so a client would meet two
    different error shapes depending on where the failure happened."""
    app = client.app

    @app.get("/needs-a-number")
    async def needs_a_number(count: int) -> dict[str, int]:  # pragma: no cover - exercised via HTTP
        return {"count": count}

    response = client.get("/needs-a-number", params={"count": "not-a-number"})
    assert response.status_code == 422
    body = ErrorEnvelope.model_validate(response.json())
    assert body.error == "invalid_request"
    assert body.request_id


def test_a_validation_failure_does_not_echo_the_submitted_value(client: TestClient) -> None:
    """Pydantic's error list contains the input, and a rejected request may carry a dimension read
    off a client's drawing. `AGENTS.md` §6 keeps drawing content out of anything forwarded."""
    app = client.app

    @app.get("/echo-check")
    async def echo_check(count: int) -> dict[str, int]:  # pragma: no cover - exercised via HTTP
        return {"count": count}

    secret = "6012mm-from-a-client-drawing"
    response = client.get("/echo-check", params={"count": secret})
    assert secret not in response.text


def test_an_http_error_uses_the_same_envelope(client: TestClient) -> None:
    app = client.app

    @app.get("/missing")
    async def missing() -> None:  # pragma: no cover - exercised via HTTP
        raise HTTPException(status_code=404, detail="no such package")

    response = client.get("/missing")
    assert response.status_code == 404
    body = ErrorEnvelope.model_validate(response.json())
    assert body.error == "http_error" and body.message == "no such package"


def test_an_unexpected_failure_tells_the_caller_nothing_internal(client: TestClient) -> None:
    """A stack trace or a database message in a response body is how an internal detail becomes a
    client's problem to interpret. The request id is the handle for reading the log."""
    app = client.app

    @app.get("/boom")
    async def boom() -> None:  # pragma: no cover - exercised via HTTP
        raise RuntimeError("connection to postgres at 10.83.0.2 refused")

    response = client.get("/boom")
    assert response.status_code == 500
    body = ErrorEnvelope.model_validate(response.json())
    assert body.error == "internal_error"
    assert "postgres" not in response.text and "10.83.0.2" not in response.text
    assert body.request_id


def test_an_unknown_route_still_returns_the_envelope(client: TestClient) -> None:
    """The commonest error in any API, and the one most likely to be handled by a framework default."""
    response = client.get("/no-such-thing")
    assert response.status_code == 404
    assert ErrorEnvelope.model_validate(response.json()).error == "http_error"


# ---------------------------------------------------------------------------
# Request ids
# ---------------------------------------------------------------------------


def test_a_request_id_comes_back_on_the_response(client: TestClient) -> None:
    """So "it failed at 14:32" is answerable."""
    response = client.get("/health")
    assert response.headers["X-Request-ID"]


def test_an_inbound_request_id_is_kept(client: TestClient) -> None:
    """An id that changes at our boundary makes a distributed trace two traces, and the reviewer's
    report cites whichever half we happened to log."""
    supplied = "caller-supplied-id-123"
    response = client.get("/health", headers={"X-Request-ID": supplied})
    assert response.headers["X-Request-ID"] == supplied


def test_the_id_in_an_error_body_matches_the_header(client: TestClient) -> None:
    """The two would be useless if they disagreed: a reviewer quotes one and we search for the other."""
    app = client.app

    @app.get("/fails")
    async def fails() -> None:  # pragma: no cover - exercised via HTTP
        raise RuntimeError("x")

    response = client.get("/fails", headers={"X-Request-ID": "trace-me"})
    assert response.headers["X-Request-ID"] == "trace-me"
    assert ErrorEnvelope.model_validate(response.json()).request_id == "trace-me"


def test_two_requests_get_different_ids(client: TestClient) -> None:
    first = client.get("/health").headers["X-Request-ID"]
    second = client.get("/health").headers["X-Request-ID"]
    assert first != second


# ---------------------------------------------------------------------------
# Alive is not the same question as ready
# ---------------------------------------------------------------------------


def test_health_reports_alive_without_touching_the_database(client: TestClient) -> None:
    """It has to answer while the database is down, or it cannot distinguish the two failures."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "alive"


def test_readiness_refuses_when_the_database_is_unreachable() -> None:
    """503, not 500. A readiness probe that 500s is indistinguishable from a crashed process, and the
    two call for different responses from an orchestrator."""
    unreachable = _settings(database_url="postgresql+psycopg://nobody@127.0.0.1:1/nothing")
    with TestClient(create_app(unreachable), raise_server_exceptions=False) as probe:
        response = probe.get("/ready")
    assert response.status_code == 503
    assert response.json()["status"] == "not ready"


def test_readiness_and_health_are_separate_endpoints() -> None:
    """An orchestrator checking only liveness will route traffic to a process running against a
    schema three migrations behind, and every request it serves will be wrong in a way nothing
    reports."""
    app = create_app(_settings())
    paths = {route.path for route in app.routes if hasattr(route, "path")}
    assert {"/health", "/ready"} <= paths
