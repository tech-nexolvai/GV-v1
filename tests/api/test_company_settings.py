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


def test_the_list_holds_gvs_standards_and_the_four_project_defaults(session: Session) -> None:
    """**#817, the admin's decision of 2026-10-02, and #1181 (2026-10-11).** Outcome: the six
    settings the rulebook gives a default — the back-offset minimum with its 2.5" in use, and so on,
    and since #991 the field cut at GV's 1" (admin, 2026-10-07) — and, since #1181, the four
    per-project settings GV may give a default (overhang, backsplash, side panel, cabinet depth),
    each empty. Not the cabinet width bounds (mandatory per project), and not the sink's cut-sheet
    size, which is the sink entry's."""
    response = _client(session).get(URL)

    assert response.status_code == 200, response.text
    settings = _by_name(response)
    standards = {
        "back_offset_minimum",
        "front_offset_required",
        "sink_cutout_clearance",
        "filler_min",
        "filler_max",
        "field_cut",
    }
    defaults = {
        "countertop_overhang",
        "backsplash_thickness",
        "cabinet_side_thickness",
        "cabinet_depth",
    }
    assert set(settings) == standards | defaults
    assert {name for name, item in settings.items() if item["kind"] == "standard"} == standards
    for name in defaults:
        assert (settings[name]["in_use"], settings[name]["rulebook_default"]) == (None, None), name
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


def test_a_company_default_for_a_project_setting_is_saved_and_in_use(session: Session) -> None:
    """#1181: GV's usual side panel, saved once. Outcome: in use as GV's, set by the admin."""
    response = _client(session).post(
        URL, json={"values": [{"name": "cabinet_side_thickness", "value": '3/4"'}]}
    )

    assert response.status_code == 201, response.text
    side = _by_name(response)["cabinet_side_thickness"]
    assert (side["in_use"], side["in_use_from"], side["company_set_by"], side["kind"]) == (
        "3/4 in",
        "company",
        "a admin",
        "project_default",
    )


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
        # Mandatory per project by the client lead's checklist (#817): never a company value.
        ({"name": "single_door_cab_width_min", "value": '9"'}, "per-project setting"),
    ],
)
def test_a_value_with_no_unit_or_a_name_no_check_uses_is_refused(
    session: Session, entry: dict[str, str], says: str
) -> None:
    """A bare number is never guessed at; a misspelt or run-only name would be a standard no check
    ever reads; and a cabinet width bound is not GV's to fix for every job (#817)."""
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


@pytest.mark.parametrize(
    ("role", "may_save"), [("admin", True), ("reviewer", False), ("rule_admin", False)]
)
def test_can_save_says_what_the_save_route_allows(
    session: Session, role: str, may_save: bool
) -> None:
    """#1369: the page hides Save for people who cannot save. `can_save` comes from the same check the
    save route uses, so it never promises a save the server then refuses (or hides one it allows).
    The server stays the authority: the save below is still checked as before."""
    client = _client(session, role=role)

    read = client.get(URL)
    assert read.status_code == 200, read.text
    assert read.json()["can_save"] is may_save

    saved = client.post(URL, json={"values": [{"name": "back_offset_minimum", "value": '2"'}]})
    assert (saved.status_code == 201) is may_save, saved.text
    if may_save:
        assert saved.json()["can_save"] is True
