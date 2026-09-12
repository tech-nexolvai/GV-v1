"""How the chat endpoint projects a stored finding into the facts a model may see.

These are the pure helpers in `app/api/reviewer_chat.py`. They had no tests, which is how the page
convention drifted away from the rest of the product without anything noticing.
"""

from __future__ import annotations

import json

from app.api.reviewer_chat import _evidence_page

pytest_plugins = ("tests.app.postgres_fixture",)

DOCUMENT = "11111111-1111-4111-8111-111111111111"


def _reference(**overrides: object) -> str:
    payload: dict[str, object] = {"page": 0, "document_version_id": DOCUMENT}
    payload.update(overrides)
    return json.dumps(payload)


def test_a_page_is_counted_the_way_a_reviewer_counts_sheets() -> None:
    """**Input: stored index 0. Outcome: "1".**

    `pages.index` is zero-based, the way the reader addresses a document. A reviewer counts sheets
    from one, and so does every other surface in the product — `EnterValuesPage` and
    `ConfirmReadingsPage` both render `page_index + 1`.

    This endpoint was the exception, and it showed: asked which sheet had the failure, the AI
    answered *"Sheet 0 has the failure"* — a sheet that exists on no drawing. The number reaching a
    reviewer has to be the number printed on the paper in front of them.
    """
    assert _evidence_page(_reference(page=0)) == "1"
    assert _evidence_page(_reference(page=12)) == "13"


def test_a_reference_that_is_not_structurally_complete_is_not_labelled() -> None:
    """Outcome: `None`, so nothing downstream prints a page it cannot stand behind.

    A partial reference is not a page 1. Returning `None` keeps the evidence line off the finding
    entirely, which is honest; inventing a default would put a reviewer on the wrong sheet.
    """
    assert _evidence_page(json.dumps({"page": 0})) is None
    assert _evidence_page(json.dumps({"document_version_id": DOCUMENT})) is None
    assert _evidence_page(_reference(document_version_id="  ")) is None
    assert _evidence_page("not json") is None
    assert _evidence_page("") is None
    assert _evidence_page(None) is None


def test_a_page_that_is_not_a_whole_count_is_refused() -> None:
    """Outcome: `None` for a negative, a float, or a bool.

    `True` is an `int` in Python and would otherwise label evidence as sheet 2. The bool check is
    why this reads `isinstance(page, bool)` before the integer test rather than after it.
    """
    assert _evidence_page(_reference(page=-1)) is None
    assert _evidence_page(_reference(page=1.5)) is None
    assert _evidence_page(_reference(page=True)) is None


# ---------------------------------------------------------------------------
# Whether anything was checked at all
# ---------------------------------------------------------------------------


def test_the_endpoint_reports_a_package_nobody_has_checked(postgres_engine) -> None:  # type: ignore[no-untyped-def]
    """**Input: an uploaded package with no check run. Outcome: chat says nothing has run.**

    The endpoint has to *look*. `answer_question` cannot see the difference — an empty finding list
    is all it gets — so the distinction is only real if this module queries for it. Asked "Why did
    this fail?" about a package uploaded minutes earlier, chat answered *"No FAIL findings in this
    run (0 total)"*, which reads as a clean bill of health for a drawing nothing had looked at.

    Asserted through the route rather than on the helper, because the helper being right and the
    route not passing its answer along is exactly the shape of the original bug.
    """
    from uuid import uuid4

    from fastapi.testclient import TestClient
    from sqlalchemy.orm import Session

    from alembic import command
    from app.api.dependencies import get_session
    from app.auth import Principal, Role, authenticate
    from app.config import Settings
    from app.db.session import session_factory
    from app.main import create_app
    from app.models import Package, PackageRevision, PackageState, Project
    from app.review.chat import NOTHING_HAS_RUN
    from tests.app.postgres_fixture import alembic_config

    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")

    project_id = uuid4()
    session: Session = session_factory(postgres_engine)()
    try:
        session.add(Project(id=project_id, name="chat state test"))
        session.flush()
        package = Package(project_id=project_id, vendor="Apex Glass & Stone")
        session.add(package)
        session.flush()
        session.add(
            PackageRevision(package_id=package.id, revision_number=1, state=PackageState.UPLOADING)
        )
        session.commit()

        app = create_app(
            Settings(  # type: ignore[call-arg]
                database_url="postgresql+psycopg://unused@localhost/unused", environment="test"
            )
        )
        app.dependency_overrides[get_session] = lambda: session
        app.dependency_overrides[authenticate] = lambda: Principal(
            id="anant", roles=frozenset({Role.ADMIN}), projects=frozenset({project_id})
        )
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post(
            f"/api/v1/projects/{project_id}/packages/{package.id}/chat",
            json={"question": "Why did this fail?"},
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["answer"] == NOTHING_HAS_RUN
        assert "No FAIL findings" not in body["answer"]
        assert body["findings"] == []
    finally:
        session.close()
