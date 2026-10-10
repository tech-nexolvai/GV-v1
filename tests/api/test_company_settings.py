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


def test_the_list_holds_gvs_standards_and_nothing_per_project(session: Session) -> None:
    """**#817, the admin's decision of 2026-10-02.** Outcome: exactly the six settings the rulebook
    gives a default — the back-offset minimum with its 2.5" in use, and so on, and since #991 the
    field cut at GV's 1" (admin, 2026-10-07). Not the per-project ones (side panel, overhang,
    backsplash, cabinet depth, the cabinet width bounds), and not the sink's cut-sheet size, which
    is one review's."""
    response = _client(session).get(URL)

    assert response.status_code == 200, response.text
    settings = _by_name(response)
    assert set(settings) == {
        "back_offset_minimum",
        "front_offset_required",
        "sink_cutout_clearance",
        "filler_min",
        "filler_max",
        "field_cut",
    }
    field_cut = settings["field_cut"]
    assert (field_cut["scope"], field_cut["rulebook_default"], field_cut["in_use_from"]) == (
        "project",
        "1 in",
        "rulebook",
    )
    assert (field_cut["rulebook_note"] or "").startswith("company standard 1 in")
    offset = settings["back_offset_minimum"]
    assert (offset["rulebook_default"], offset["in_use"], offset["in_use_from"]) == (
        "2 1/2 in",
        "2 1/2 in",
        "rulebook",
    )
    assert "#674" in (settings["filler_max"]["rulebook_note"] or "")


def test_saving_one_standard_keeps_the_ones_saved_before(session: Session) -> None:
    """**The point of #812.** Monday the front offset; Tuesday the clearance alone. Outcome: both are
    company values in use, with who set them, and the version moved on."""
    client = _client(session)

    first = client.post(URL, json={"values": [{"name": "front_offset_required", "value": '4"'}]})
    second = client.post(URL, json={"values": [{"name": "sink_cutout_clearance", "value": '1/8"'}]})

    assert (first.status_code, second.status_code) == (201, 201), second.text
    assert second.json()["version"] > first.json()["version"]
    settings = _by_name(second)
    for name, value in (("front_offset_required", "4 in"), ("sink_cutout_clearance", "1/8 in")):
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
        ({"name": "back_offset_minimum", "value": "2"}, "with its unit"),
        ({"name": "back_offset_minumum", "value": '2"'}, "not a company standard"),
        ({"name": "sink_interior_width", "value": '28"'}, "not a company standard"),
        # Per project by the client lead's checklist (#817): entered for each job, never for the company.
        ({"name": "cabinet_side_thickness", "value": '3/4"'}, "per-project setting"),
        ({"name": "single_door_cab_width_min", "value": '9"'}, "per-project setting"),
    ],
)
def test_a_value_with_no_unit_or_a_name_no_check_uses_is_refused(
    session: Session, entry: dict[str, str], says: str
) -> None:
    """A bare number is never guessed at; a misspelt or run-only name would be a standard no check
    ever reads; and a per-project setting is not GV's to fix for every job (#817)."""
    response = _client(session).post(URL, json={"values": [entry]})

    assert response.status_code == 422, response.text
    assert says in response.json()["message"]


def test_a_reviewer_may_read_the_standards_but_not_change_them(session: Session) -> None:
    """Refused the way every authorisation failure is (`app/auth/dependencies._refuse`): a 404 that
    says nothing. Outcome: and nothing was stored."""
    client = _client(session, role="reviewer")

    assert client.get(URL).status_code == 200
    refused = client.post(URL, json={"values": [{"name": "back_offset_minimum", "value": '2"'}]})
    assert refused.status_code == 404, refused.text
    assert _by_name(_client(session).get(URL))["back_offset_minimum"]["company_value"] is None


def test_a_save_is_audited_under_the_person_who_made_it(session: Session) -> None:
    _client(session).post(URL, json={"values": [{"name": "front_offset_required", "value": '4"'}]})

    events = [
        event
        for event in session.execute(select(AuditEvent)).scalars()
        if event.target_type == "company_standards"
    ]
    assert [event.actor for event in events] == ["a admin"]
