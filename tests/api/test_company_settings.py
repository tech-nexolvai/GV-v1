"""GV's company standards: read by anyone, saved by an admin, kept across saves (#812).

Verification for: `app/api/company_settings.py`.

The one that matters most is `test_saving_one_standard_keeps_the_ones_saved_before`: a company save is
a new version of the company layer, and it must carry the earlier values forward — the trap #799 fixed
for the project layer, one layer up.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alembic import command
from app.api.dependencies import get_session
from app.audit.events import AuditEvent
from app.config import Settings
from app.db.session import session_factory
from app.main import create_app
from tests.app.postgres_fixture import alembic_config
from tests.workflow.test_stages import _publish_rulebook

pytest_plugins = ("tests.app.postgres_fixture",)

URL = "/api/v1/company-settings"


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")
    opened = session_factory(postgres_engine)()
    _publish_rulebook(opened)
    opened.commit()
    try:
        yield opened
    finally:
        opened.close()


def _client(session: Session, role: str = "admin") -> Any:
    from fastapi.testclient import TestClient

    from app.auth import Principal, Role, authenticate

    app = create_app(
        Settings(  # type: ignore[call-arg]
            database_url="postgresql+psycopg://unused@localhost/unused", environment="test"
        )
    )
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[authenticate] = lambda: Principal(
        id=f"a {role}", roles=frozenset({Role(role)}), projects=frozenset()
    )
    return TestClient(app, raise_server_exceptions=False)


def _by_name(response: Any) -> dict[str, dict[str, Any]]:
    return {setting["name"]: setting for setting in response.json()["settings"]}


def test_the_list_holds_company_and_project_settings_and_no_run_ones(session: Session) -> None:
    """Outcome: the back-offset minimum is listed with its rulebook 2.5" in use; the cabinet depth,
    a project setting, is listed with nothing in use; the sink's cut-sheet size, true for one review,
    is not listed at all."""
    response = _client(session).get(URL)

    assert response.status_code == 200, response.text
    settings = _by_name(response)
    offset = settings["back_offset_minimum"]
    assert (offset["rulebook_default"], offset["in_use"], offset["in_use_from"]) == (
        "2 1/2 in",
        "2 1/2 in",
        "rulebook",
    )
    depth = settings["cabinet_depth"]
    assert (depth["scope"], depth["in_use"], depth["in_use_from"]) == ("project", None, None)
    assert "CT-DEPTH-001" in depth["rule_ids"]
    assert "#674" in (settings["filler_max"]["rulebook_note"] or "")
    assert "sink_interior_width" not in settings
    assert "sink_interior_depth" not in settings


def test_saving_one_standard_keeps_the_ones_saved_before(session: Session) -> None:
    """**The point.** Monday the cabinet depth; Tuesday the side thickness alone. Outcome: both are
    company values in use, with who set them, and the version moved on."""
    client = _client(session)

    first = client.post(URL, json={"values": [{"name": "cabinet_depth", "value": '24"'}]})
    second = client.post(
        URL, json={"values": [{"name": "cabinet_side_thickness", "value": '3/4"'}]}
    )

    assert (first.status_code, second.status_code) == (201, 201), second.text
    assert second.json()["version"] > first.json()["version"]
    settings = _by_name(second)
    for name, value in (("cabinet_depth", "24 in"), ("cabinet_side_thickness", "3/4 in")):
        assert (settings[name]["company_value"], settings[name]["in_use_from"]) == (
            value,
            "company",
        ), name
        assert settings[name]["company_set_by"] == "a admin"


def test_a_company_value_replaces_the_rulebook_default_and_says_so(session: Session) -> None:
    response = _client(session).post(
        URL, json={"values": [{"name": "back_offset_minimum", "value": '2 3/8"'}]}
    )

    offset = _by_name(response)["back_offset_minimum"]
    assert (offset["in_use"], offset["in_use_from"], offset["rulebook_default"]) == (
        "2 3/8 in",
        "company",
        "2 1/2 in",
    )


@pytest.mark.parametrize(
    ("entry", "says"),
    [
        ({"name": "cabinet_depth", "value": "24"}, "with its unit"),
        ({"name": "cabinet_dpeth", "value": '24"'}, "not a setting"),
        ({"name": "sink_interior_width", "value": '28"'}, "not a setting"),
    ],
)
def test_a_value_with_no_unit_or_a_name_no_check_uses_is_refused(
    session: Session, entry: dict[str, str], says: str
) -> None:
    """A bare number is never guessed at, and a misspelt or run-only name would be a standard no
    check ever reads, saved as though it mattered."""
    response = _client(session).post(URL, json={"values": [entry]})

    assert response.status_code == 422, response.text
    assert says in response.json()["message"]


def test_a_reviewer_may_read_the_standards_but_not_change_them(session: Session) -> None:
    """Refused the way every authorisation failure is (`app/auth/dependencies._refuse`): a 404 that
    says nothing. Outcome: and nothing was stored."""
    client = _client(session, role="reviewer")

    assert client.get(URL).status_code == 200
    refused = client.post(URL, json={"values": [{"name": "cabinet_depth", "value": '24"'}]})
    assert refused.status_code == 404, refused.text
    assert _by_name(_client(session).get(URL))["cabinet_depth"]["company_value"] is None


def test_a_save_is_audited_under_the_person_who_made_it(session: Session) -> None:
    _client(session).post(URL, json={"values": [{"name": "cabinet_depth", "value": '24"'}]})

    events = [
        event
        for event in session.execute(select(AuditEvent)).scalars()
        if event.target_type == "company_standards"
    ]
    assert [event.actor for event in events] == ["a admin"]
