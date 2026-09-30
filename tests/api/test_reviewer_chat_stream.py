"""The streaming chat endpoint, asserted through the route.

Three properties matter and each is only real at the route:

1. Events arrive in the order they become known: ``facts`` (no model), then ``narration`` (the
   complete guarded reply), then ``done``.
2. A missing package or a disallowed model is a real HTTP error. A generator endpoint's body does
   not run until the response has started with status 200, so these refusals must come from the
   dependency, or a reviewer would get a broken stream instead of an error.
3. The narration event is the same body ``/chat`` returns, so the two endpoints cannot drift.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID, uuid4

pytest_plugins = ("tests.app.postgres_fixture",)


def _events(body: str) -> list[tuple[str, Any]]:
    """Parse an SSE body into (event, data) pairs. Keep-alive comments are ignored."""
    parsed: list[tuple[str, Any]] = []
    for block in body.split("\n\n"):
        event, data = "message", None
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line.removeprefix("event:").strip()
            elif line.startswith("data:"):
                data = json.loads(line.removeprefix("data:").strip())
        if data is not None:
            parsed.append((event, data))
    return parsed


@contextmanager
def _client_for_unchecked_package(postgres_engine) -> Iterator[tuple[Any, UUID, UUID]]:  # type: ignore[no-untyped-def]
    """A package uploaded but never checked, and a client authorised for its project."""
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import Session

    from alembic import command
    from app.api.dependencies import get_session
    from app.auth import Principal, Role, authenticate
    from app.config import Settings
    from app.db.session import session_factory
    from app.main import create_app
    from app.models import Package, PackageRevision, PackageState, Project
    from tests.app.postgres_fixture import alembic_config

    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")

    project_id = uuid4()
    session: Session = session_factory(postgres_engine)()
    try:
        session.add(Project(id=project_id, name="chat stream test"))
        session.flush()
        package = Package(project_id=project_id, vendor="Apex Glass & Stone")
        session.add(package)
        session.flush()
        session.add(
            PackageRevision(package_id=package.id, revision_number=1, state=PackageState.UPLOADING)
        )
        session.commit()

        app = create_app(
            Settings(
                database_url="postgresql+psycopg://unused@localhost/unused", environment="test"
            )
        )
        app.dependency_overrides[get_session] = lambda: session
        app.dependency_overrides[authenticate] = lambda: Principal(
            id="anant", roles=frozenset({Role.ADMIN}), projects=frozenset({project_id})
        )
        yield TestClient(app, raise_server_exceptions=False), project_id, package.id
    finally:
        session.close()


def test_the_stream_sends_facts_then_the_guarded_reply_then_done(postgres_engine) -> None:  # type: ignore[no-untyped-def]
    from app.review.chat import NOTHING_HAS_RUN

    with _client_for_unchecked_package(postgres_engine) as (client, project_id, package_id):
        streamed = client.post(
            f"/api/v1/projects/{project_id}/packages/{package_id}/chat/stream",
            json={"question": "Why did this fail?"},
        )
        plain = client.post(
            f"/api/v1/projects/{project_id}/packages/{package_id}/chat",
            json={"question": "Why did this fail?"},
        )

    assert streamed.status_code == 200, streamed.text
    assert streamed.headers["content-type"].startswith("text/event-stream")
    events = _events(streamed.text)
    assert [name for name, _ in events] == ["facts", "narration", "done"], events

    facts = events[0][1]
    assert facts["answer"] == NOTHING_HAS_RUN
    assert facts["finding_ids"] == []
    assert facts["narrating"] is False, "nothing has run, so no model may be asked"

    assert events[1][1] == plain.json(), "the narration event is exactly the /chat body"


def test_a_missing_package_is_a_404_not_a_broken_stream(postgres_engine) -> None:  # type: ignore[no-untyped-def]
    with _client_for_unchecked_package(postgres_engine) as (client, project_id, _):
        response = client.post(
            f"/api/v1/projects/{project_id}/packages/{uuid4()}/chat/stream",
            json={"question": "Why did this fail?"},
        )
    assert response.status_code == 404, response.text
    assert not response.headers["content-type"].startswith("text/event-stream")


def test_a_model_that_is_not_allow_listed_is_a_422_not_a_broken_stream(postgres_engine) -> None:  # type: ignore[no-untyped-def]
    with _client_for_unchecked_package(postgres_engine) as (client, project_id, package_id):
        response = client.post(
            f"/api/v1/projects/{project_id}/packages/{package_id}/chat/stream",
            json={"question": "Why did this fail?", "model_id": "not-an-allowed-model"},
        )
    assert response.status_code == 422, response.text
    assert not response.headers["content-type"].startswith("text/event-stream")
