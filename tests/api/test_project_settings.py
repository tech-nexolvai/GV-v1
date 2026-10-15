"""Company settings → Projects: each project's own values, set by its reviewers (#1341).

Verification for: `app/api/company_settings.py` (the Projects section).

The decision behind it (2026-10-15): per-project values live in Company settings → Projects and a
reviewer may edit them; GV's standards stay admin-only. So these tests hold three things:

- a reviewer saves a project value and it is the one in use for that project, carried across saves;
- a reviewer still cannot touch GV's standards, and nobody outside a project sees or changes it;
- every value the review's Settings step asks for can be set here (the sink through the sink card).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alembic import command
from app.api.dependencies import get_session
from app.audit.events import AuditEvent
from app.config import Settings
from app.db.session import session_factory
from app.main import create_app
from app.models import Package, Project
from app.models.parameters import ParameterValue
from rules.required_inputs import required_inputs
from tests.app.postgres_fixture import alembic_config
from tests.workflow.test_stages import _publish_rulebook

pytest_plugins = ("tests.app.postgres_fixture",)

URL = "/api/v1/company-settings/projects"
COMPANY = "/api/v1/company-settings"

CABINET_LIMITS = {
    "single_door_cab_width_min",
    "single_door_cab_width_max",
    "double_door_cab_width_min",
    "double_door_cab_width_max",
    "drawer_cab_width_min",
    "drawer_cab_width_max",
}


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


def _project(session: Session, name: str = "Test job", product: str | None = None) -> UUID:
    project = Project(name=name)
    session.add(project)
    session.flush()
    if product is not None:
        session.add(Package(project_id=project.id, vendor="Test vendor", product_type=product))
    session.commit()
    return project.id


def _client(session: Session, role: str = "reviewer", projects: tuple[UUID, ...] = ()) -> Any:
    from fastapi.testclient import TestClient

    from app.auth import Principal, Role, authenticate

    app = create_app(
        Settings(  # type: ignore[call-arg]
            database_url="postgresql+psycopg://unused@localhost/unused", environment="test"
        )
    )
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[authenticate] = lambda: Principal(
        id=f"a {role}", roles=frozenset({Role(role)}), projects=frozenset(projects)
    )
    return TestClient(app, raise_server_exceptions=False)


def _one(response: Any, project_id: UUID) -> dict[str, Any]:
    projects: list[dict[str, Any]] = [
        item for item in response.json()["projects"] if item["id"] == str(project_id)
    ]
    assert len(projects) == 1, response.text
    return projects[0]


def _settings(project: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {setting["name"]: setting for setting in project["settings"]}


def test_every_value_the_review_settings_step_asks_for_is_listed(session: Session) -> None:
    """Outcome: each setting the published rules declare is a row here, except the sink's two
    inside sizes, which the project's sink card (in the same project block) sets. The cabinet width
    limits are there; each row says what it is for and which checks use it."""
    project_id = _project(session)
    response = _client(session, projects=(project_id,)).get(URL)

    assert response.status_code == 200, response.text
    project = _one(response, project_id)
    from app.api.measurements import _published_rules

    asked = {parameter.name for parameter in required_inputs(_published_rules(session)).parameters}
    listed = set(_settings(project))
    assert listed == asked - {"sink_interior_width", "sink_interior_depth"}
    assert CABINET_LIMITS <= listed
    assert "sink" in project and project["sink"] is None
    for name, setting in _settings(project).items():
        assert setting["purpose"], name
        assert setting["checks"] and all(check["name"] for check in setting["checks"]), name


def test_a_project_starts_from_the_rulebook_then_gvs_standard(session: Session) -> None:
    """Outcome: the clearance is in use at the checklist default (1/4"); once GV saves its own
    standard the project uses that, and says so; the limits nobody set are in use as nothing."""
    project_id = _project(session)
    client = _client(session, projects=(project_id,))

    before = _settings(_one(client.get(URL), project_id))
    assert (
        before["sink_cutout_clearance"]["in_use"],
        before["sink_cutout_clearance"]["in_use_from"],
    ) == (
        "1/4 in",
        "rulebook",
    )
    assert (
        before["drawer_cab_width_max"]["in_use"],
        before["drawer_cab_width_max"]["in_use_from"],
    ) == (
        None,
        None,
    )
    saved = _client(session, role="admin").post(
        COMPANY, json={"values": [{"name": "cabinet_depth", "value": '24"'}]}
    )
    assert saved.status_code == 201, saved.text
    after = _settings(_one(client.get(URL), project_id))
    assert (after["cabinet_depth"]["in_use"], after["cabinet_depth"]["in_use_from"]) == (
        "24 in",
        "company",
    )


def test_a_reviewer_sets_project_values_and_earlier_ones_are_kept(session: Session) -> None:
    """**The point of #1341.** A reviewer saves the overhang, then a cabinet width limit. Outcome:
    both are this project's values in use, with who set them; GV's standard is untouched."""
    project_id = _project(session)
    client = _client(session, projects=(project_id,))

    first = client.post(
        f"{URL}/{project_id}", json={"values": [{"name": "countertop_overhang", "value": '1"'}]}
    )
    second = client.post(
        f"{URL}/{project_id}",
        json={
            "values": [
                {"name": "drawer_cab_width_max", "value": '36"', "source": "G.C / Client"},
                {"name": "filler_min", "value": '1/2"'},
            ]
        },
    )

    assert (first.status_code, second.status_code) == (201, 201), second.text
    settings = _settings(second.json())
    for name, text in (
        ("countertop_overhang", "1 in"),
        ("drawer_cab_width_max", "36 in"),
        ("filler_min", "1/2 in"),
    ):
        assert (settings[name]["in_use"], settings[name]["in_use_from"]) == (text, "project"), name
        assert settings[name]["project_value"]["set_by"] == "a reviewer", name
    # A project value is not GV's standard: the company list still shows the rulebook's 1".
    company = {s["name"]: s for s in _client(session).get(COMPANY).json()["settings"]}
    assert company["filler_min"]["company_value"] is None


def test_a_project_value_wins_over_gvs_standard(session: Session) -> None:
    project_id = _project(session)
    _client(session, role="admin").post(
        COMPANY, json={"values": [{"name": "front_offset_required", "value": '4"'}]}
    )
    response = _client(session, projects=(project_id,)).post(
        f"{URL}/{project_id}", json={"values": [{"name": "front_offset_required", "value": '3"'}]}
    )

    assert response.status_code == 201, response.text
    offset = _settings(response.json())["front_offset_required"]
    assert (offset["in_use"], offset["in_use_from"], offset["company_value"]) == (
        "3 in",
        "project",
        "4 in",
    )


@pytest.mark.parametrize(
    ("entry", "says"),
    [
        ({"name": "countertop_overhang", "value": "1"}, "Add the unit"),
        ({"name": "sink_interior_width", "value": '28"'}, "sink"),
        ({"name": "no_such_setting", "value": '1"'}, "not a setting"),
        # Two sources are honest for the clearance; the person says which (#827).
        ({"name": "sink_cutout_clearance", "value": '1/8"'}, "came from"),
        ({"name": "cabinet_depth", "value": '0"'}, "more than zero"),
    ],
)
def test_a_wrong_project_value_is_refused_and_nothing_is_stored(
    session: Session, entry: dict[str, str], says: str
) -> None:
    project_id = _project(session)
    response = _client(session, projects=(project_id,)).post(
        f"{URL}/{project_id}", json={"values": [entry]}
    )

    assert response.status_code == 422, response.text
    assert says in response.json()["message"]
    assert list(session.execute(select(ParameterValue.id)).scalars()) == []


def test_a_named_source_is_kept(session: Session) -> None:
    project_id = _project(session)
    response = _client(session, projects=(project_id,)).post(
        f"{URL}/{project_id}",
        json={
            "values": [{"name": "sink_cutout_clearance", "value": '1/8"', "source": "Fabricator"}]
        },
    )

    assert response.status_code == 201, response.text
    clearance = _settings(response.json())["sink_cutout_clearance"]
    assert clearance["project_value"]["source"] == "Fabricator"
    assert [source["value"] for source in clearance["sources"]] == [
        "Company standard",
        "Fabricator",
    ]


def test_outside_the_project_nothing_is_seen_or_saved(session: Session) -> None:
    """The isolation boundary: a reviewer of another job does not see this one, and a save is the
    same 404 as a project that does not exist. Outcome: nothing stored."""
    mine, theirs = _project(session, "Mine"), _project(session, "Theirs")
    client = _client(session, projects=(mine,))

    assert [item["id"] for item in client.get(URL).json()["projects"]] == [str(mine)]
    refused = client.post(
        f"{URL}/{theirs}", json={"values": [{"name": "countertop_overhang", "value": '1"'}]}
    )
    assert refused.status_code == 404, refused.text
    missing = _client(session, projects=(uuid4(),)).post(
        f"{URL}/{uuid4()}", json={"values": [{"name": "countertop_overhang", "value": '1"'}]}
    )
    assert missing.status_code == 404
    assert list(session.execute(select(ParameterValue.id)).scalars()) == []


def test_a_role_that_may_not_enter_values_cannot_save(session: Session) -> None:
    """A rule admin may read but not enter a project's values (`Action.ENTER_VALUES`)."""
    project_id = _project(session)
    client = _client(session, role="rule_admin", projects=(project_id,))

    assert client.get(URL).status_code == 200
    refused = client.post(
        f"{URL}/{project_id}", json={"values": [{"name": "countertop_overhang", "value": '1"'}]}
    )
    assert refused.status_code == 404
    assert list(session.execute(select(ParameterValue.id)).scalars()) == []


def test_a_reviewer_still_cannot_change_gvs_standards(session: Session) -> None:
    project_id = _project(session)
    refused = _client(session, projects=(project_id,)).post(
        COMPANY, json={"values": [{"name": "filler_min", "value": '1/2"'}]}
    )
    assert refused.status_code == 404


def test_a_project_save_is_audited_under_the_person_who_made_it(session: Session) -> None:
    project_id = _project(session)
    _client(session, projects=(project_id,)).post(
        f"{URL}/{project_id}", json={"values": [{"name": "countertop_overhang", "value": '1"'}]}
    )

    events = [
        event
        for event in session.execute(select(AuditEvent)).scalars()
        if event.target_type == "project_settings"
    ]
    assert [event.actor for event in events] == ["a reviewer"]


def test_cabinet_width_limits_apply_only_to_cabinet_jobs(session: Session) -> None:
    """Outcome: on a countertop-only job the six limits are listed as not used by its drawing
    sets (still settable); on a cabinet job they apply."""
    countertop = _project(session, "Countertop job", product="countertop")
    cabinet = _project(session, "Cabinet job", product="cabinet")
    response = _client(session, projects=(countertop, cabinet)).get(URL)

    on_countertop = _settings(_one(response, countertop))
    on_cabinet = _settings(_one(response, cabinet))
    assert {name for name in CABINET_LIMITS if on_countertop[name]["applies"]} == set()
    assert {name for name in CABINET_LIMITS if on_cabinet[name]["applies"]} == CABINET_LIMITS
    assert on_countertop["countertop_overhang"]["applies"] is True
    assert _one(response, cabinet)["product_types"] == ["cabinet"]


def test_the_projects_sink_card_is_shown_with_its_project(session: Session) -> None:
    project_id = _project(session)
    client = _client(session, projects=(project_id,))
    saved = client.post(
        f"/api/v1/projects/{project_id}/sink",
        json={"model_name": "Test sink", "inside_width": '28"', "inside_depth": '16"'},
    )
    assert saved.status_code == 201, saved.text

    sink = _one(client.get(URL), project_id)["sink"]
    assert (sink["model_name"], sink["inside_width"]["text"]) == ("Test sink", '28"')


@pytest.mark.parametrize(
    ("role", "may_save"), [("admin", True), ("reviewer", True), ("rule_admin", False)]
)
def test_can_save_says_what_the_project_save_route_allows(
    session: Session, role: str, may_save: bool
) -> None:
    """#1369: each project says whether the caller may save it, from the same checks the save route
    uses (project access and `Action.ENTER_VALUES`). The save is still checked by the server."""
    project_id = _project(session)
    client = _client(session, role=role, projects=(project_id,))

    assert _one(client.get(URL), project_id)["can_save"] is may_save

    saved = client.post(
        f"{URL}/{project_id}", json={"values": [{"name": "countertop_overhang", "value": '1"'}]}
    )
    assert (saved.status_code == 201) is may_save, saved.text
    if may_save:
        assert saved.json()["can_save"] is True
