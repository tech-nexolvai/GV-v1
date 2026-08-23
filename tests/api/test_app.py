"""The API skeleton: settings, the error contract, request ids and two health questions (#203, C2.1).

Six API groups will hang off this app. What is tested here is the part they all inherit — so a
mistake in it is a mistake in every endpoint that follows, and the tests are mostly about the failure
paths, because those are what a client writes once and never revisits.
"""

from __future__ import annotations

import re
from uuid import UUID

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings
from app.errors import ErrorEnvelope
from app.main import ACCEPTED_REQUEST_ID, create_app
from app.telemetry.tracing import TRACE_ID_HEADER
from tests.app.postgres_fixture import alembic_config

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
    # The shared envelope, not a bespoke body — see
    # `test_readiness_failures_use_the_shared_error_envelope`.
    assert ErrorEnvelope.model_validate(response.json()).error == "not_ready"


def test_readiness_and_health_are_separate_endpoints() -> None:
    """An orchestrator checking only liveness will route traffic to a process running against a
    schema three migrations behind, and every request it serves will be wrong in a way nothing
    reports."""
    app = create_app(_settings())
    paths = {route.path for route in app.routes if hasattr(route, "path")}
    assert {"/health", "/ready"} <= paths


# ---------------------------------------------------------------------------
# Readiness means at head, not merely migrated (found by review on #336)
# ---------------------------------------------------------------------------


def test_readiness_failures_use_the_shared_error_envelope() -> None:
    """ "One error shape, for every failure" is the headline claim of this module, and the first
    version of `/ready` answered with a bespoke body — so a client handling errors through
    `ErrorEnvelope` could not parse a not-ready response."""
    unreachable = _settings(database_url="postgresql+psycopg://nobody@127.0.0.1:1/nothing")
    with TestClient(create_app(unreachable), raise_server_exceptions=False) as probe:
        response = probe.get("/ready")
    assert response.status_code == 503
    body = ErrorEnvelope.model_validate(response.json())
    assert body.error == "not_ready"
    assert body.request_id


def test_a_schema_behind_head_is_not_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """The failure this endpoint exists for, and the one the first version missed.

    It asked only whether `alembic_version` was non-empty, so a database three migrations behind
    answered `200 ready` — a process serving requests against an outdated schema, which is not
    degraded but wrong, reported as healthy.
    """
    from app import main

    monkeypatch.setattr(
        main,
        "_readiness_problems",
        lambda url: ["the schema is at 0007_x but the code expects head"],
    )
    with TestClient(create_app(_settings()), raise_server_exceptions=False) as probe:
        response = probe.get("/ready")
    assert response.status_code == 503
    assert "0007_x" in ErrorEnvelope.model_validate(response.json()).message


def test_readiness_at_head_reports_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """The check has to be able to say yes, or the refusals above prove nothing."""
    from app import main

    monkeypatch.setattr(main, "_readiness_problems", lambda url: [])
    with TestClient(create_app(_settings()), raise_server_exceptions=False) as probe:
        response = probe.get("/ready")
    assert response.status_code == 200 and response.json()["status"] == "ready"


def test_the_expected_head_is_read_from_the_migrations_not_hardcoded() -> None:
    """A hardcoded revision would be correct until the next migration and wrong silently after."""
    from alembic.script import ScriptDirectory

    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    assert head, "alembic must report a head revision for the readiness check to compare against"


# ---------------------------------------------------------------------------
# A 5xx detail is written for us, not for the caller
# ---------------------------------------------------------------------------


def test_a_server_side_http_detail_is_not_forwarded(client: TestClient) -> None:
    """A 4xx detail tells a client what they got wrong and is safe. A 5xx detail may name a host, a
    table or a driver error — the first version forwarded both."""
    app = client.app

    @app.get("/upstream-broke")
    async def upstream_broke() -> None:  # pragma: no cover - exercised via HTTP
        raise HTTPException(status_code=502, detail="postgres at 10.83.0.2 refused the connection")

    response = client.get("/upstream-broke")
    assert response.status_code == 502
    assert "10.83.0.2" not in response.text and "postgres" not in response.text
    assert ErrorEnvelope.model_validate(response.json()).error == "internal_error"


def test_a_client_side_http_detail_is_still_forwarded(client: TestClient) -> None:
    """Otherwise every 404 becomes "something went wrong", which is useless to the caller."""
    app = client.app

    @app.get("/nope")
    async def nope() -> None:  # pragma: no cover - exercised via HTTP
        raise HTTPException(status_code=404, detail="no such package revision")

    response = client.get("/nope")
    assert ErrorEnvelope.model_validate(response.json()).message == "no such package revision"


def test_the_readiness_check_does_not_block_the_event_loop() -> None:
    """`create_engine` and a synchronous session would stall the loop, making every other request slow
    while the probe reports on health. The work runs in a worker thread."""
    import inspect

    from app import main

    source = inspect.getsource(main)
    assert "run_in_threadpool(_readiness_problems" in source


# ---------------------------------------------------------------------------
# The request id is caller-supplied, so it is caller-controlled
# ---------------------------------------------------------------------------


def test_an_implausible_request_id_is_replaced_rather_than_echoed(client: TestClient) -> None:
    """A caller-supplied id is only kept when it looks like an id.

    The first version read the header with no constraint at all, and that value was echoed in a response
    header, written to logs, and put on a span. So a caller could choose a 200 KB value or embed a data
    URI — and the span attribute guard in `app/telemetry/tracing.py`, which refuses exactly that, was
    bypassed because the request span set its attribute directly.

    Replaced rather than refused: a malformed header is a cosmetic client bug, and failing the request
    would turn it into an outage. The caller gets a working response and an id we generated.
    """
    hostile = "data:application/pdf;base64," + "A" * 400
    response = client.get("/health", headers={"X-Request-ID": hostile})

    assert response.status_code == 200
    returned = response.headers["X-Request-ID"]
    assert returned != hostile, "an implausible id was echoed straight back"
    assert "data:" not in returned and len(returned) <= 128
    UUID(returned)  # a generated one, which is what "no usable id supplied" means


@pytest.mark.parametrize(
    "supplied",
    [
        "x" * 129,  # over the length limit
        "has spaces",
        "with\nnewline",  # header injection shape
        "",
    ],
)
def test_only_id_shaped_values_are_kept(supplied: str, client: TestClient) -> None:
    """Each of these is not an id, and none of them should come back."""
    response = client.get("/health", headers={"X-Request-ID": supplied})
    assert response.headers["X-Request-ID"] != supplied


def test_an_error_response_carries_the_trace_id_too(client: TestClient) -> None:
    """The case the trace header exists for, which the first version missed.

    An unhandled exception is handled *outside* the middleware that opened the span, so the code that
    stamps the header never runs and the span has already ended. `app/errors.py` had already learned this
    for the request id — its docstring says so — and the trace id repeated the mistake, leaving the
    responses somebody is actually complaining about as the only ones with no trace to quote.
    """
    app = client.app

    @app.get("/explodes")
    async def explodes() -> None:  # pragma: no cover - exercised via HTTP
        raise RuntimeError("x")

    response = client.get("/explodes")

    assert response.status_code == 500
    assert response.headers["X-Request-ID"]
    trace_id = response.headers.get(TRACE_ID_HEADER)
    assert trace_id is not None, "the failing response is the one with no trace id"
    assert re.fullmatch(r"[0-9a-f]{32}", trace_id), f"not a trace id: {trace_id!r}"


def test_a_successful_response_and_a_failing_one_use_the_same_header(client: TestClient) -> None:
    """One header name for both paths, so a client reads one field whatever happened."""
    ok = client.get("/health")
    assert TRACE_ID_HEADER in ok.headers


def test_a_maximum_length_request_id_is_actually_usable(client: TestClient) -> None:
    """An id this app accepts must survive the span guard, not 500 halfway through.

    **This is the test for a bug I introduced while fixing the one above.** Routing the request span
    through `traced()` meant the request id met the attribute checks — correct, and the point of the fix.
    But those checks refuse an unbroken run of 120+ base64-shaped characters, while the accepted pattern
    allowed 128. So a 126-character alphanumeric token, which is an entirely ordinary opaque id, passed the
    boundary and was then refused inside the middleware: a valid request answered with a 500.

    **The length is discovered, not written down here.** My first attempt at this test hardcoded a
    100-character value, which passes whether the limit is 100 or 128 — so it asserted nothing about the
    two limits agreeing, which is the only thing it exists to check. Probing for the longest value the
    pattern accepts means widening the pattern past the span guard's threshold fails this test.
    """
    longest = ""
    while ACCEPTED_REQUEST_ID.fullmatch(longest + "a"):
        longest += "a"
    assert longest, "the pattern accepts nothing, so this test cannot say anything"

    response = client.get("/health", headers={"X-Request-ID": longest})

    assert response.status_code == 200, (
        f"the longest acceptable id ({len(longest)} characters) was not usable — the boundary accepts a "
        "value the span guard refuses"
    )
    assert response.headers["X-Request-ID"] == longest
    assert response.headers[TRACE_ID_HEADER]
