"""Review sessions and actions over HTTP (#229).

The service has done this work since D4.1 and nothing exposed it. This layer adds one thing —
turning a service refusal into a status code — and takes away one thing that matters more: the
reviewer's name never comes from the request body.

That is what most of these assert. An audit trail whose author is client-supplied answers "who says
so?" with "whoever was asked", which is the only question it exists to answer.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from alembic import command
from app.api import review
from app.auth import Principal, Role, authenticate
from app.config import Settings
from app.db.session import session_factory
from app.main import create_app
from app.models import (
    CheckRun,
    Finding,
    Package,
    PackageRevision,
    Project,
    RuleDefinition,
    RuleSnapshot,
)
from app.models.package import PackageState
from app.models.review import ReviewAction
from verdict.outcomes import Outcome, Severity

pytest_plugins = ("tests.app.postgres_fixture",)

DATABASE_URL = "postgresql+psycopg://gv:gv@localhost:5433/gv"
REVIEWER = "anant"


def _settings() -> Settings:
    return Settings(database_url=DATABASE_URL)  # type: ignore[call-arg]


def _principal(*projects: UUID, name: str = REVIEWER) -> Principal:
    return Principal(id=name, roles=frozenset(Role), projects=frozenset(projects))


def _app(principal: Principal, session: Session) -> FastAPI:
    app = create_app(_settings())
    app.dependency_overrides[authenticate] = lambda: principal
    app.dependency_overrides[review.get_session] = lambda: session
    return app


def _client(session: Session, *projects: UUID, name: str = REVIEWER) -> TestClient:
    return TestClient(
        _app(_principal(*projects, name=name), session), raise_server_exceptions=False
    )


def _upgrade(engine: Engine) -> None:
    config = Config("alembic.ini")
    config.attributes["database_url"] = engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    _upgrade(postgres_engine)
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


def _revision(session: Session, project_id: UUID) -> PackageRevision:
    session.add(Project(id=project_id, name=f"p-{project_id}"))
    session.flush()
    return _revision_in_project(session, project_id)


def _revision_in_project(session: Session, project_id: UUID) -> PackageRevision:
    """Create another package revision without creating a second project."""
    package = Package(project_id=project_id)
    session.add(package)
    session.flush()
    revision = PackageRevision(
        package_id=package.id, revision_number=1, state=PackageState.AWAITING_REVIEW
    )
    session.add(revision)
    session.flush()
    return revision


def _finding(session: Session, revision: PackageRevision) -> Finding:
    definition = RuleDefinition(rule_id=f"CT-WIDTH-{uuid4().hex[:8]}")
    session.add(definition)
    session.flush()
    canonical = json.dumps({"id": definition.rule_id, "version": "1.0.0"}, separators=(",", ":"))
    snapshot = RuleSnapshot(
        rule_definition_id=definition.id,
        snapshot_id=f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}",
        version="1.0.0",
        canonical_json=canonical,
        product_type="countertop",
        check_type="internal",
        unconfirmed_tolerance_count=0,
    )
    session.add(snapshot)
    session.flush()
    run = CheckRun(
        package_revision_id=revision.id,
        rule_snapshot_id=snapshot.id,
        engine_version="verdict-1.2.3",
    )
    session.add(run)
    session.flush()
    finding = Finding(
        check_run_id=run.id,
        package_revision_id=revision.id,
        outcome=Outcome.FAIL,
        severity=Severity.CRITICAL,
        parameter_set_versions={},
        # NOT NULL: a finding without its arithmetic is a verdict nobody can check.
        trace={"operation": "equals", "comparison": "96 == 98 1/2"},
    )
    session.add(finding)
    session.flush()
    return finding


# ---------------------------------------------------------------------------
# The reviewer is the caller
# ---------------------------------------------------------------------------


def test_every_review_note_and_reason_limit_is_its_column_length() -> None:
    """The structural guard behind the test above: each free-text field a reviewer types is
    accepted only up to what its column holds, so validation, not the database, says no."""
    from app.models.review import ReviewException
    from app.schemas.review import GrantException, RecordAction

    def limit(model: type, field: str) -> object:
        return next(
            getattr(rule, "max_length", None)
            for rule in model.model_fields[field].metadata
            if getattr(rule, "max_length", None) is not None
        )

    assert limit(RecordAction, "note") == ReviewAction.__table__.c.note.type.length
    assert limit(GrantException, "reason") == ReviewException.__table__.c.reason.type.length


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# The list the sidebar reads
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# The way into the exceptions table, which had none (#547)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# The way into the correction ledger, which had none (#547)
# ---------------------------------------------------------------------------
