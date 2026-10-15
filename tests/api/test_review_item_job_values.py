"""'Enter this job's values' on the simple review's Not-checked group (#1403). Synthetic values only.

The values a not-checked check was waiting for are saved through the existing per-project settings
path (Company settings → Projects, #1341), never a new one, and then one coalesced re-check is
queued. A person who may not save a project's values is told so by the list (`can_save_job_values`)
and refused by the server.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alembic import command
from app.api.dependencies import get_session
from app.auth import Principal, Role, authenticate
from app.config import Settings
from app.db.session import session_factory
from app.main import create_app
from app.models.parameters import ParameterSet as StoredParameterSet
from app.models.parameters import ParameterValue as StoredParameterValue
from rules.parameters import ParameterLayer
from tests.api.test_review_item_actions import (
    _act,
    _client,
    _held_page,
    _items,
    _queued_runs,
    _revision,
)
from tests.app.postgres_fixture import alembic_config

pytest_plugins = ("tests.app.postgres_fixture",)


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


def _unset(body: Any) -> list[str]:
    """The settings the open not-checked checks wait for, in the list's order."""
    names: list[str] = []
    for entry in body["not_checked"]:
        for setting in entry["settings"]:
            if not setting["set_now"] and setting["key"] not in names:
                names.append(setting["key"])
    return names


def _project_values(session: Session, project_id: UUID) -> dict[str, str]:
    newest = session.execute(
        select(StoredParameterSet)
        .where(
            StoredParameterSet.layer == ParameterLayer.PROJECT.value,
            StoredParameterSet.project_id == project_id,
        )
        .order_by(StoredParameterSet.version.desc())
        .limit(1)
    ).scalar_one_or_none()
    if newest is None:
        return {}
    rows = session.scalars(
        select(StoredParameterValue).where(StoredParameterValue.parameter_set_id == newest.id)
    )
    return {row.name: row.provenance for row in rows}


def _job_values(names: list[str], value: str = '1 1/2"') -> dict[str, object]:
    return {"answer": "job_values", "job_values": [{"name": n, "value": value} for n in names]}


def test_entering_the_jobs_values_saves_them_on_the_project_and_queues_one_recheck(
    session: Session, tmp_path: Path
) -> None:
    project_id, package_id = _held_page(session, tmp_path)
    client = _client(session, project_id)
    body = _items(client, project_id, package_id)
    assert body["can_save_job_values"] is True
    names = _unset(body)
    assert names, "the fixture must give not-checked checks waiting for this job's values"
    revision = _revision(session, package_id)
    assert _queued_runs(session, revision.id) == []

    response = _act(client, project_id, package_id, "not-checked", "answer", _job_values(names))
    assert response.status_code == 201, response.text
    saved = _project_values(session, project_id)
    assert set(names) <= set(saved)
    # The one honest source of these per-job values (#827): the GC or the client.
    assert {saved[name] for name in names} == {"G.C / Client"}
    assert len(_queued_runs(session, revision.id)) == 1
    out = response.json()
    assert out["action"] == "job_values"
    assert out["rechecking"] is True
    # Saved since the run: the list now says so instead of asking again.
    assert _unset(out["review_items"]) == []

    again = _act(client, project_id, package_id, "not-checked", "answer", _job_values(names, '2"'))
    assert again.status_code == 201, again.text
    assert len(_queued_runs(session, revision.id)) == 1, "re-checks coalesce to one waiting run"


def test_a_bare_number_or_an_unknown_name_saves_nothing_and_queues_nothing(
    session: Session, tmp_path: Path
) -> None:
    project_id, package_id = _held_page(session, tmp_path)
    client = _client(session, project_id)
    names = _unset(_items(client, project_id, package_id))
    revision = _revision(session, package_id)

    bare = _act(client, project_id, package_id, "not-checked", "answer", _job_values(names, "2"))
    assert bare.status_code == 422, bare.text
    assert "Add the unit" in bare.text
    unknown = _act(
        client, project_id, package_id, "not-checked", "answer", _job_values(["no_such_value"])
    )
    assert unknown.status_code == 422, unknown.text
    empty = _act(client, project_id, package_id, "not-checked", "answer", _job_values([]))
    assert empty.status_code == 422, empty.text
    assert _project_values(session, project_id) == {}
    assert _queued_runs(session, revision.id) == []


def test_a_person_who_may_not_save_the_projects_values_is_told_and_refused(
    session: Session, tmp_path: Path
) -> None:
    project_id, package_id = _held_page(session, tmp_path)
    names = _unset(_items(_client(session, project_id), project_id, package_id))
    app = create_app(Settings(database_url="postgresql+psycopg://gv:gv@localhost:5434/unused"))
    app.dependency_overrides[authenticate] = lambda: Principal(
        id="ruler", roles=frozenset({Role.RULE_ADMIN}), projects=frozenset({project_id})
    )
    app.dependency_overrides[get_session] = lambda: session
    reader = TestClient(app)

    assert _items(reader, project_id, package_id)["can_save_job_values"] is False
    refused = _act(reader, project_id, package_id, "not-checked", "answer", _job_values(names))
    assert refused.status_code in (403, 404), refused.text
    assert _project_values(session, project_id) == {}
