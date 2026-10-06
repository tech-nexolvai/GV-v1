"""A picture of any drawing page for the reviewer's left-hand pane (admin, 2026-10-06)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.api.dependencies import get_artifact_store, get_session
from app.main import create_app
from app.models.package import Package
from storage.local import LocalStore
from tests.api.test_packages import _settings
from tests.evidence.test_sides import _read, session, store  # noqa: F401 — fixtures

pytest_plugins = ("tests.app.postgres_fixture",)


def _client(db: Session, artifacts: LocalStore, project_id: UUID) -> Any:
    from fastapi.testclient import TestClient

    from app.auth import Principal, Role, authenticate

    app = create_app(_settings())
    app.dependency_overrides[get_session] = lambda: db
    app.dependency_overrides[get_artifact_store] = lambda: artifacts
    app.dependency_overrides[authenticate] = lambda: Principal(
        id="anant", roles=frozenset({Role.ADMIN}), projects=frozenset({project_id})
    )
    return TestClient(app, raise_server_exceptions=False)


def test_any_page_of_a_package_can_be_shown_as_a_picture(
    session: Session, store: LocalStore  # noqa: F811
) -> None:
    """No confirmed vendor drawing is needed: a combined set has none before review."""
    revision = _read(session, store, kind="shop")
    package = session.get(Package, revision.package_id)
    assert package is not None
    client = _client(session, store, package.project_id)
    base = f"/api/v1/projects/{package.project_id}/packages/{package.id}/pages"

    shown = client.get(f"{base}/1/picture")
    assert shown.status_code == 200, shown.text
    assert shown.headers["content-type"] == "image/png"
    assert shown.content.startswith(b"\x89PNG")

    assert client.get(f"{base}/9/picture").status_code == 404
    assert client.get(f"{base}/1/picture?dpi=999").status_code == 422
