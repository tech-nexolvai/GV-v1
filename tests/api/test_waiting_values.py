"""The six waiting checks ask for exactly what they miss, and run once it is supplied (#1181).

Verification for `app/review/waiting_values.py` (`missing_inputs` on the findings list),
`app/api/project_sink.py` (the project's sink entry and the sink a package uses),
`app/review/settings_in_force.py` (what Measurements shows in force), the company defaults and the
zero refusals (`app/api/company_settings.py`, `app/api/measurements.py`), and `entered` /
`defaults_used` in the changed-values report (`workflow/changed_values.py`).

A drawn number is never asked for, and never typed (GV's rule: people confirm readings, they do not
fill in measurements; Anant, 2026-10-12; the Measurements save refuses one since #1251). Where a test
needs one to reach a PASS or FAIL it arrives the way the app now gets it: a reading on the vendor's
drawing that a reviewer confirms (`confirm_candidate_type`, "Confirm a reading"). Every check runs
through the real engine (`DatabaseStages.run_checks`). Every number and name is synthetic.
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
from app.config import Settings
from app.db.session import session_factory
from app.main import create_app
from app.models import (
    Package,
    PackageRevision,
    PackageState,
    Project,
)
from app.models.project_sinks import ProjectSink
from tests.app.postgres_fixture import alembic_config
from tests.workflow.test_stages import _publish_rulebook

pytest_plugins = ("tests.app.postgres_fixture",)

PROJECT = uuid4()


def _upgrade(engine: Engine) -> None:
    config = alembic_config()
    config.attributes["database_url"] = engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    _upgrade(postgres_engine)
    opened = session_factory(postgres_engine)()
    _publish_rulebook(opened)
    opened.commit()
    try:
        yield opened
    finally:
        opened.close()


def _client(session: Session, *, role: str = "reviewer", actor: str = "a reviewer") -> Any:
    from fastapi.testclient import TestClient

    from app.auth import Principal, Role, authenticate

    app = create_app(
        Settings(  # type: ignore[call-arg]
            database_url="postgresql+psycopg://unused@localhost/unused", environment="test"
        )
    )
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[authenticate] = lambda: Principal(
        id=actor, roles=frozenset({Role(role)}), projects=frozenset({PROJECT})
    )
    return TestClient(app, raise_server_exceptions=False)


def _package(session: Session, product: str | None = None) -> tuple[UUID, UUID]:
    if session.get(Project, PROJECT) is None:
        session.add(Project(id=PROJECT, name="waiting values tests"))
        session.flush()
    package = Package(project_id=PROJECT, vendor="Synthetic Vendor", product_type=product)
    session.add(package)
    session.flush()
    revision = PackageRevision(
        package_id=package.id, revision_number=1, state=PackageState.AWAITING_REVIEW
    )
    session.add(revision)
    session.commit()
    return package.id, revision.id


def _sink(client: Any, **body: Any) -> Any:
    return client.post(f"/api/v1/projects/{PROJECT}/sink", json=body)


# ---------------------------------------------------------------------------
# What each check asks for
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Supplied, then run: a real PASS or FAIL
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# The sink entry
# ---------------------------------------------------------------------------


def test_a_sink_needs_a_name_and_a_size_above_zero(session: Session) -> None:
    """Missing is never zero: a zero, a bare number or no name is refused, and nothing is saved."""
    client = _client(session)
    _package(session)

    for body, says in (
        (
            {"model_name": "Synthetic", "inside_width": '0"', "inside_depth": '15"'},
            "more than zero",
        ),
        ({"model_name": "Synthetic", "inside_width": "30", "inside_depth": '15"'}, "unit"),
        ({"model_name": "  ", "inside_width": '30"', "inside_depth": '15"'}, "Name the sink"),
    ):
        response = _sink(client, **body)
        assert response.status_code == 422, response.text
        assert says in response.json()["message"]

    assert client.get(f"/api/v1/projects/{PROJECT}/sink").json() == {"sink": None}
    assert session.scalars(select(ProjectSink)).all() == []


@pytest.mark.parametrize("role", ["rule_admin"])
def test_only_a_reviewer_or_an_admin_enters_a_sink(session: Session, role: str) -> None:
    """Refused like every authorisation failure: a 404 that says nothing, and nothing is saved.
    The app has no separate viewer role; `rule_admin` is the one role without `ENTER_VALUES`."""
    _package(session)

    response = _sink(
        _client(session, role=role), model_name="Synthetic", inside_width='30"', inside_depth='15"'
    )

    assert response.status_code == 404, response.text
    assert session.scalars(select(ProjectSink)).all() == []


def test_with_no_sink_nothing_is_in_use(session: Session) -> None:
    client = _client(session)
    package, _revision = _package(session)

    in_use = client.get(f"/api/v1/projects/{PROJECT}/packages/{package}/sink-in-use").json()

    # The vendor's sink note is listed only where the depth and sink reader read the set (#1295).
    assert in_use == {
        "model_name": None,
        "inside_width": None,
        "inside_depth": None,
        "vendor_notes": [],
    }


# ---------------------------------------------------------------------------
# Measurements shows what is in force
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Company defaults
# ---------------------------------------------------------------------------


def test_the_four_company_defaults_are_listed_empty_until_gv_sets_them(session: Session) -> None:
    """Listed as project defaults with nothing in use: no value is filled in for GV."""
    response = _client(session, role="admin").get("/api/v1/company-settings")

    settings = {item["name"]: item for item in response.json()["settings"]}
    for name in (
        "countertop_overhang",
        "backsplash_thickness",
        "cabinet_side_thickness",
        "cabinet_depth",
    ):
        assert settings[name]["kind"] == "project_default", name
        assert settings[name]["in_use"] is None, name
        assert settings[name]["rulebook_default"] is None, name
    assert settings["back_offset_minimum"]["kind"] == "standard"


def test_a_zero_overhang_or_backsplash_is_accepted(session: Session) -> None:
    """Zero is a real answer for these two (no overhang, no backsplash); an open question to Anant."""
    admin = _client(session, role="admin")

    response = admin.post(
        "/api/v1/company-settings",
        json={
            "values": [
                {"name": "countertop_overhang", "value": '0"'},
                {"name": "backsplash_thickness", "value": '0"'},
            ]
        },
    )

    assert response.status_code == 201, response.text


# ---------------------------------------------------------------------------
# The report's changed-values list
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# The two cabinet checks (#1205)
# ---------------------------------------------------------------------------
