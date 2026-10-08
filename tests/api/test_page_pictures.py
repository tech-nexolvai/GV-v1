"""A picture of any drawing page for the reviewer's left-hand pane (admin, 2026-10-06)."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.api.dependencies import get_artifact_store, get_session
from app.main import create_app
from app.models.package import Package
from storage.local import LocalStore
from tests.api.test_packages import _settings
from tests.evidence.test_sides import MISSING_SPACE, _read, session, store  # noqa: F401
from workflow.stages import DatabaseStages

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

    # Before the worker has rendered it: not ready, and asking queues the worker's job once.
    assert client.get(f"{base}/1/picture").status_code == 404
    assert client.post(f"{base}/pictures").json() == {"queued": True}

    # The worker renders every page of the revision — no confirmed vendor drawing needed.
    result = DatabaseStages(store, missing_space=MISSING_SPACE).render_vendor_page_pictures(
        session, revision.id
    )
    assert result["rendered"] == 1, result

    shown = client.get(f"{base}/1/picture")
    assert shown.status_code == 200, shown.text
    assert shown.headers["content-type"] == "image/png"
    assert shown.content.startswith(b"\x89PNG")
    assert client.post(f"{base}/pictures").json() == {"queued": False}
    assert client.get(f"{base}/9/picture").status_code == 404
    # An exact finding location must never fall back to a different file's same-numbered page.
    assert client.get(f"{base}/1/picture?document_version_id={uuid4()}").status_code == 404
    from sqlalchemy import select

    from app.models import Page

    page = session.scalars(select(Page)).one()
    exact = client.get(f"{base}/1/picture?document_version_id={page.document_version_id}")
    assert exact.status_code == 200
    assert exact.content == shown.content
