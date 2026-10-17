"""Only this machine's address, and no write from another site's page (`app/host_guard.py`).

DNS rebinding: a page on another site points its own domain at 127.0.0.1 and talks to the API
through the browser. Its requests carry that domain as `Host` (refused, 400) and its page as
`Origin` (a write is refused, 403). The screen's development proxy sends `127.0.0.1:8000` as `Host`
and the screen's own page as `Origin`, and goes through. No database, no model call: $0.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.host_guard import allowed_hosts, host_name, origin_host
from app.main import create_app
from app.telemetry.tracing import configure_tracing

DATABASE_URL = "postgresql+psycopg://gv:gv@localhost:5433/gv"


def _client(**overrides: object) -> TestClient:
    configure_tracing()
    settings = Settings(database_url=DATABASE_URL, **overrides)  # type: ignore[arg-type]
    return TestClient(create_app(settings), raise_server_exceptions=False)


@pytest.mark.parametrize(
    "host", ["localhost:8000", "127.0.0.1:8000", "[::1]:8000", "LOCALHOST", "testserver"]
)
def test_this_machines_own_addresses_are_served(host: str) -> None:
    answer = _client().get("/health", headers={"Host": host})
    assert answer.status_code == 200


@pytest.mark.parametrize(
    "host", ["evil.example", "evil.example:8000", "127.0.0.1.evil.example", "localhost.evil"]
)
def test_any_other_host_is_refused_with_the_one_error_shape(host: str) -> None:
    answer = _client().get("/health", headers={"Host": host})
    assert answer.status_code == 400
    body = answer.json()
    assert body["error"] == "host_not_allowed" and body["request_id"]
    assert "evil" not in answer.text  # the caller's host is never echoed


def test_a_deployments_own_host_is_served_when_listed() -> None:
    client = _client(trusted_hosts=("gv.example.com",))
    assert client.get("/health", headers={"Host": "gv.example.com"}).status_code == 200
    assert client.get("/health", headers={"Host": "other.example.com"}).status_code == 400


def test_the_setting_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GV_TRUSTED_HOSTS", '["gv.example.com"]')
    assert Settings(database_url=DATABASE_URL).trusted_hosts == ("gv.example.com",)


def test_a_write_from_another_sites_page_is_refused() -> None:
    client = _client()
    answer = client.post(
        "/health", headers={"Host": "127.0.0.1:8000", "Origin": "https://evil.example"}
    )
    assert answer.status_code == 403 and answer.json()["error"] == "origin_not_allowed"
    for origin in ("null", "file://", "not a url"):
        refused = client.delete("/health", headers={"Origin": origin})
        assert refused.status_code == 403, origin


def test_the_screens_proxy_and_scripts_go_through() -> None:
    """The vite proxy (`changeOrigin`) sends `Host: 127.0.0.1:8000` and the screen's page as
    `Origin`; curl and tests send no `Origin`. Past the guard, `/health` takes no POST (405)."""
    client = _client()
    for origin in ("http://127.0.0.1:5173", "http://localhost:5173", None):
        headers = {"Host": "127.0.0.1:8000"}
        if origin is not None:
            headers["Origin"] = origin
        assert client.post("/health", headers=headers).status_code == 405, origin
    # A read from another site's page is answered only for a host that is ours, and the browser
    # does not show another site's page the answer (no CORS).
    read = client.get("/health", headers={"Origin": "https://evil.example"})
    assert read.status_code == 200


def test_the_helpers() -> None:
    assert host_name("Example.COM:443") == "example.com"
    assert host_name("[::1]:8000") == "::1"
    assert host_name("") is None and host_name(None) is None
    assert origin_host("http://localhost:5173") == "localhost"
    assert origin_host("null") is None
    assert "gv.example.com" in allowed_hosts([" GV.example.com. "])
