"""After sign-off nothing about a review changes (#1182).

Two halves, like the authorisation audit: a walk over every write route under a package or a review
session, which fails on any that neither refuses a closed revision nor is named in `EXEMPT_WRITES`
with its reason; and a request to every guarded route on a signed-off package, which must answer 409
with the plain sentence and write nothing.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from alembic import command
from app.api.dependencies import get_artifact_store, get_session
from app.api.guards import mounted_routes
from app.api.signed_off import EXEMPT_WRITES, REFUSES_CLOSED_MARKER
from app.auth import Principal, Role, authenticate
from app.config import Settings
from app.db.session import session_factory
from app.main import create_app
from app.models import (
    Document,
    DocumentKind,
    OutboxEntry,
    Package,
    PackageRevision,
    PackageState,
    Project,
)
from app.models.parameters import ParameterValue
from app.models.review import ReviewSession
from storage.local import LocalStore
from tests.app.postgres_fixture import alembic_config
from tests.workflow.test_stages import _publish_rulebook

pytest_plugins = ("tests.app.postgres_fixture",)

PROJECT = UUID("6f0c4a52-1182-4e0b-9a3e-5d1c0e7f1182")
SIGNED_OFF = "This review is signed off. Start a new revision to change it."
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _app() -> Any:
    return create_app(Settings(database_url="postgresql+psycopg://unused/unused"))  # type: ignore[call-arg]


def _guarded(dependant: Dependant) -> bool:
    """Whether a closed-revision refusal is anywhere among this route's dependencies."""
    return any(
        getattr(child.call, REFUSES_CLOSED_MARKER, False) or _guarded(child)
        for child in dependant.dependencies
    )


def _revision_writes() -> dict[tuple[str, str], APIRoute]:
    """Every write route that names a package or a review session, by method and served path."""
    return {
        (next(iter(sorted(mounted.route.methods & WRITE_METHODS))), mounted.path): mounted.route
        for mounted in mounted_routes(_app())
        if mounted.route.methods & WRITE_METHODS
        and ("{package_id}" in mounted.path or "{review_session_id}" in mounted.path)
    }


GUARDED = sorted(
    (key for key, route in _revision_writes().items() if _guarded(route.dependant)),
    key=lambda key: key[1],
)


# ---------------------------------------------------------------------------
# The audit
# ---------------------------------------------------------------------------


def test_every_write_under_a_package_refuses_a_closed_review_or_says_why_not() -> None:
    """Outcome: no route is left unaccounted for. A new write route under a package fails here
    until it takes `open_revision` (or `open_session_revision`) or is named in `EXEMPT_WRITES`."""
    unaccounted = [
        key
        for key, route in _revision_writes().items()
        if not _guarded(route.dependant) and key not in EXEMPT_WRITES
    ]

    assert unaccounted == []


def test_no_exemption_outlives_its_route_or_is_also_guarded() -> None:
    routes = _revision_writes()

    assert set(EXEMPT_WRITES) <= set(routes), set(EXEMPT_WRITES) - set(routes)
    assert [key for key in EXEMPT_WRITES if _guarded(routes[key].dependant)] == []
    assert all(reason.strip() for reason in EXEMPT_WRITES.values())


def test_the_writes_named_in_the_audit_are_all_guarded() -> None:
    """The inputs and decisions the audit (2026-10-11) named, by path. A refactor that drops one of
    the guards fails here by name, not only as a count."""
    paths = {path for _, path in GUARDED}
    base = "/api/v1/projects/{project_id}/packages/{package_id}"
    for suffix in (
        "/measurements",
        "/checks",
        "/measurements/propose",
        "/slot-rows/{row_id}/review",
        "/slot-rows/{row_id}/architect-pairing",
        "/views/{view_id}/role",
        "/candidates/{candidate_id}/confirm",
        "/review-sessions",
    ):
        assert base + suffix in paths, suffix
    assert any("architect" in path and "match" in path for path in paths), paths
    sessions = "/api/v1/projects/{project_id}/review-sessions/{review_session_id}"
    for suffix in ("/actions", "/evidence", "/exceptions"):
        assert sessions + suffix in paths, suffix


# ---------------------------------------------------------------------------
# The behaviour
# ---------------------------------------------------------------------------


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


def _client(session: Session, principal: Principal, store: LocalStore | None = None) -> TestClient:
    app = _app()
    app.dependency_overrides[authenticate] = lambda: principal
    app.dependency_overrides[get_session] = lambda: session
    if store is not None:
        app.dependency_overrides[get_artifact_store] = lambda: store
    return TestClient(app, raise_server_exceptions=False)


def _admin(projects: frozenset[UUID] = frozenset({PROJECT})) -> Principal:
    return Principal(id="an admin", roles=frozenset({Role.ADMIN}), projects=projects)


def _package(session: Session, state: PackageState) -> tuple[UUID, UUID, UUID]:
    """A package whose one revision is in `state`, with a review session and a document."""
    if session.get(Project, PROJECT) is None:
        session.add(Project(id=PROJECT, name="signed-off writes"))
        session.flush()
    package = Package(project_id=PROJECT, vendor="Synthetic vendor", product_type="countertop")
    session.add(package)
    session.flush()
    revision = PackageRevision(package_id=package.id, revision_number=1, state=state.value)
    session.add(revision)
    session.flush()
    review = ReviewSession(package_revision_id=revision.id, reviewer="an admin")
    session.add(review)
    document = Document(package_id=package.id, kind=DocumentKind.SHOP.value)
    session.add(document)
    session.commit()
    return package.id, review.id, document.id


def _url(path: str, package_id: UUID, review_session_id: UUID) -> str:
    """The route's path with this package and session, and a random id for everything else: the
    refusal comes before anything else on the route is looked up."""
    filled = path.replace("{project_id}", str(PROJECT)).replace("{package_id}", str(package_id))
    filled = filled.replace("{review_session_id}", str(review_session_id))
    while "{" in filled:
        start = filled.index("{")
        filled = filled[:start] + str(uuid4()) + filled[filled.index("}", start) + 1 :]
    return filled


def _counts(session: Session) -> tuple[int, int]:
    session.expire_all()
    return (
        session.scalar(select(func.count()).select_from(OutboxEntry)) or 0,
        session.scalar(select(func.count()).select_from(ParameterValue)) or 0,
    )


@pytest.mark.parametrize(("method", "path"), GUARDED, ids=[path for _, path in GUARDED])
def test_every_guarded_write_is_refused_on_a_signed_off_review(
    session: Session, method: str, path: str
) -> None:
    """Outcome: 409 with the plain sentence, for an admin, before the request body is even read;
    nothing is stored and nothing is queued."""
    package_id, review_id, _ = _package(session, PackageState.APPROVED)
    before = _counts(session)

    response = _client(session, _admin()).request(
        method, _url(path, package_id, review_id), json={}
    )

    assert response.status_code == 409, response.text
    assert response.json()["message"] == SIGNED_OFF
    assert _counts(session) == before


def test_the_screens_two_writes_from_the_audit_are_refused_with_real_bodies(
    session: Session,
) -> None:
    """The audit's own repro: Save values (201 before) and Run checks (202 before) on an approved
    package. Outcome: both 409, no parameter set written, no run queued."""
    package_id, _, _ = _package(session, PackageState.APPROVED)
    client = _client(session, _admin())
    base = f"/api/v1/projects/{PROJECT}/packages/{package_id}"

    saved = client.post(
        f"{base}/measurements",
        json={"parameters": [{"name": "countertop_overhang", "value": '1 1/2"'}]},
    )
    checked = client.post(f"{base}/checks")

    assert (saved.status_code, checked.status_code) == (409, 409)
    assert saved.json()["message"] == checked.json()["message"] == SIGNED_OFF
    assert _counts(session) == (0, 0)


@pytest.mark.parametrize(
    ("state", "words"),
    [
        (PackageState.CHANGES_REQUESTED, "Changes were requested on this review"),
        (PackageState.SUPERSEDED, "A newer revision replaced this one"),
        (PackageState.CANCELLED, "This review was cancelled"),
    ],
    ids=["changes-requested", "superseded", "cancelled"],
)
def test_every_closed_review_is_refused_in_its_own_words(
    session: Session, state: PackageState, words: str
) -> None:
    package_id, _, _ = _package(session, state)

    response = _client(session, _admin()).post(
        f"/api/v1/projects/{PROJECT}/packages/{package_id}/checks"
    )

    assert response.status_code == 409
    assert response.json()["message"].startswith(words)


def test_an_open_review_still_takes_the_same_writes(session: Session) -> None:
    """Outcome: the guard lets an open review through (Run checks is accepted and queued)."""
    package_id, _, _ = _package(session, PackageState.AWAITING_REVIEW)

    response = _client(session, _admin()).post(
        f"/api/v1/projects/{PROJECT}/packages/{package_id}/checks"
    )

    assert response.status_code == 202, response.text
    assert _counts(session)[0] == 1


def test_someone_outside_the_project_still_gets_not_found_not_signed_off(
    session: Session,
) -> None:
    """The guard runs before the endpoint's own dependencies, so it checks membership itself: a
    409 would confirm the package exists."""
    package_id, review_id, _ = _package(session, PackageState.APPROVED)
    outsider = _client(session, _admin(projects=frozenset({uuid4()})))

    package_write = outsider.post(f"/api/v1/projects/{PROJECT}/packages/{package_id}/checks")
    session_write = outsider.post(
        f"/api/v1/projects/{PROJECT}/review-sessions/{review_id}/actions", json={}
    )

    assert (package_write.status_code, session_write.status_code) == (404, 404)


@pytest.mark.parametrize(
    "route",
    ["register", "confirm", "extract"],
)
def test_the_drawing_routes_refuse_a_signed_off_review_in_the_same_words(
    session: Session, route: str, tmp_path: Path
) -> None:
    """The document routes lock the revision themselves, so they check after their own lock."""
    package_id, _, document_id = _package(session, PackageState.APPROVED)
    client = _client(session, _admin(), LocalStore(root=tmp_path, ticket_secret=b"test secret"))
    sha = "b" * 64
    response = {
        "register": lambda: client.post(
            f"/api/v1/projects/{PROJECT}/packages/{package_id}/documents",
            json={"kind": "shop", "sha256": sha},
        ),
        "confirm": lambda: client.post(
            f"/api/v1/projects/{PROJECT}/documents/{document_id}/confirm",
            json={"sha256": sha, "page_count": 1},
        ),
        "extract": lambda: client.post(f"/api/v1/projects/{PROJECT}/packages/{package_id}/extract"),
    }[route]()

    assert response.status_code == 409, response.text
    assert response.json()["message"] == SIGNED_OFF
