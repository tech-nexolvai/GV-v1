"""The drawings on a combined sheet, and a reviewer saying which is which (#710)."""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.api.dependencies import get_artifact_store, get_session
from app.db.session import session_factory
from app.main import create_app
from app.models import DrawingView, Package, Project
from storage.local import LocalStore
from tests.api.test_v1_loop import _settings
from tests.workflow.test_view_roles import _combined_sheet, _extract, _upgrade

pytest_plugins = ("tests.app.postgres_fixture",)


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    _upgrade(postgres_engine)
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
def store() -> Iterator[LocalStore]:
    with tempfile.TemporaryDirectory() as directory:
        yield LocalStore(root=Path(directory), ticket_secret=b"a secret only this test knows")


def _client(session: Session, store: LocalStore, project_id: UUID, *roles: str) -> Any:
    from fastapi.testclient import TestClient

    from app.auth import Principal, Role, authenticate

    principal = Principal(
        id="reviewer@example.com",
        roles=frozenset(Role(role) for role in (roles or ("reviewer",))),
        projects=frozenset({project_id}),
    )
    app = create_app(_settings())
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_artifact_store] = lambda: store
    app.dependency_overrides[authenticate] = lambda: principal
    return TestClient(app, raise_server_exceptions=False)


def _package(session: Session, store: LocalStore) -> tuple[UUID, UUID]:
    revision = _extract(session, store, _combined_sheet())
    package = session.get(Package, revision.package_id)
    assert package is not None
    return package.project_id, package.id


def test_the_drawings_are_listed_with_what_each_is_suggested_to_be(
    session: Session, store: LocalStore
) -> None:
    project_id, package_id = _package(session, store)

    response = _client(session, store, project_id).get(
        f"/api/v1/projects/{project_id}/packages/{package_id}/views"
    )

    assert response.status_code == 200, response.text
    views = response.json()["views"]
    assert len(views) == 2
    assert [view["role"] for view in views] == [None, None]
    assert {view["suggested_role"] for view in views} == {"arch", "shop"}
    assert {view["suggested_from"] for view in views} == {
        "ID SET ELEVATION",
        "VENDOR'S SHOP DRAWING ELEVATION",
    }


def test_a_reviewer_confirms_a_drawing_s_role(session: Session, store: LocalStore) -> None:
    project_id, package_id = _package(session, store)
    client = _client(session, store, project_id)
    base = f"/api/v1/projects/{project_id}/packages/{package_id}/views"
    view = next(v for v in client.get(base).json()["views"] if v["suggested_role"] == "arch")

    response = client.post(f"{base}/{view['view_id']}/role", json={"role": "arch"})

    assert response.status_code == 201, response.text
    assert response.json()["role"] == "arch"
    stored = session.get(DrawingView, UUID(view["view_id"]))
    session.refresh(stored)
    assert stored is not None and stored.role == "arch"


def test_a_drawing_from_another_package_is_not_found(session: Session, store: LocalStore) -> None:
    project_id, _package_id = _package(session, store)
    other = Package(project_id=project_id, vendor="Other")
    session.add(other)
    session.commit()
    view_id = session.scalars(select(DrawingView.id)).first()

    response = _client(session, store, project_id).post(
        f"/api/v1/projects/{project_id}/packages/{other.id}/views/{view_id}/role",
        json={"role": "arch"},
    )

    assert response.status_code == 404


def test_an_unknown_drawing_is_not_found(session: Session, store: LocalStore) -> None:
    project_id, package_id = _package(session, store)

    response = _client(session, store, project_id).post(
        f"/api/v1/projects/{project_id}/packages/{package_id}/views/{uuid4()}/role",
        json={"role": "shop"},
    )

    assert response.status_code == 404


def test_a_role_that_is_neither_is_refused(session: Session, store: LocalStore) -> None:
    project_id, package_id = _package(session, store)
    view_id = session.scalars(select(DrawingView.id)).first()

    response = _client(session, store, project_id).post(
        f"/api/v1/projects/{project_id}/packages/{package_id}/views/{view_id}/role",
        json={"role": "both"},
    )

    assert response.status_code == 422


def test_someone_who_cannot_confirm_evidence_cannot_set_a_role(
    session: Session, store: LocalStore
) -> None:
    project_id, package_id = _package(session, store)
    view_id = session.scalars(select(DrawingView.id)).first()

    response = _client(session, store, project_id, "rule_admin").post(
        f"/api/v1/projects/{project_id}/packages/{package_id}/views/{view_id}/role",
        json={"role": "arch"},
    )

    # 404, not 403: every refusal here looks like an absence, so a caller learns nothing about what
    # exists (`app/auth/dependencies.py:_refuse`).
    assert response.status_code == 404
    assert session.get(DrawingView, view_id).role is None  # type: ignore[union-attr]


def test_a_project_outside_the_callers_is_not_found(session: Session, store: LocalStore) -> None:
    project_id, package_id = _package(session, store)
    stranger = Project(name="someone else's")
    session.add(stranger)
    session.commit()

    response = _client(session, store, stranger.id).get(
        f"/api/v1/projects/{project_id}/packages/{package_id}/views"
    )

    assert response.status_code == 404
