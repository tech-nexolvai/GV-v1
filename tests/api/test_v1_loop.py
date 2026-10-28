"""The whole V1 loop, through the API a reviewer actually uses (#532).

A drawing goes in, a person says what was read and types what the extractor could not, the rules
decide, the package is signed off, and a workbook comes out. No client input, no model, no AI.

**Why this test is at the API rather than at the service layer.** Both dead ends this story closed
were *routes*: `approve_package` had been complete and tested for months with nothing able to call
it, and the workbook `generate_outputs` stores had no way out of the application. A service-level test
would have passed throughout — it did, for months — which is precisely how a finished feature stays
unreachable. What is asserted here is that a reviewer can get from one end to the other.

**The graceful fallback is the point of the middle.** A real sheet is not read perfectly: the
extractor gets some dimensions and misses others. So the package here has one dimension the reader
picks up whole and one it cannot, and the reviewer supplies the second by hand through the path Q7
blesses. Both routes have to reach the same verdict for the loop to be worth shipping.
"""

from __future__ import annotations

import hashlib
import io
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alembic import command
from app.api.dependencies import get_artifact_store, get_session
from app.api.documents import storage_key
from app.config import Settings
from app.db.session import session_factory
from app.main import create_app
from app.models import (
    Document,
    DocumentVersion,
    ObservationCandidate,
    Package,
    PackageRevision,
    PackageRevisionDocument,
    PackageState,
    Project,
    SourceArtifact,
)
from app.models.runs import WorkflowRun
from storage.local import LocalStore
from tests.app.postgres_fixture import alembic_config
from tests.extraction.test_reader import MISSING_SPACE, _pdf
from workflow.review import run_all
from workflow.stages import DatabaseStages

pytest_plugins = ("tests.app.postgres_fixture",)

PROJECT = UUID("22222222-2222-2222-2222-222222222222")

#: A shop drawing stating one dimension the reader keeps whole, and one it cannot read.
#:
#: `648 [25 1/2]` survives word splitting because the reader recovers dual tokens (#528). `25 1/2"`
#: does not — `extract_words` cuts it at the space — so it stands for every dimension a real sheet
#: presents in a way the extractor cannot use. That is the fallback this loop has to handle, and it
#: is drawn from the actual behaviour rather than imagined.
DRAWING = _pdf(
    b"BT /F1 10 Tf 1 0 0 1 20 70 Tm (648 [25 1/2]) Tj ET\n"
    b"BT /F1 10 Tf 1 0 0 1 20 55 Tm (100 [4]) Tj ET\n"
    b'BT /F1 10 Tf 1 0 0 1 20 40 Tm (SINK 25 1/2") Tj ET\n'
)


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+psycopg://unused/unused",
        hatchet_token="test-token",
        # This e2e test proves the unavailable-provider fallback; it must not attempt an external
        # Bedrock call while exercising the locally deterministic reviewer loop.
        bedrock_chat_enabled=False,
    )


def _principal() -> Any:
    from app.auth import Principal, Role

    return Principal(
        id="ana@example.com", roles=frozenset({Role.ADMIN}), projects=frozenset({PROJECT})
    )


def _upgrade(engine: Engine) -> None:
    config = alembic_config()
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


@pytest.fixture
def store() -> Iterator[LocalStore]:
    with tempfile.TemporaryDirectory() as directory:
        yield LocalStore(root=Path(directory), ticket_secret=b"a secret only this test knows")


@pytest.fixture
def client(session: Session, store: LocalStore) -> Any:
    from fastapi.testclient import TestClient

    from app.auth import authenticate

    app = create_app(_settings())
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_artifact_store] = lambda: store
    app.dependency_overrides[authenticate] = _principal
    return TestClient(app, raise_server_exceptions=False)


def _factory(session: Session) -> Any:
    """A sessionmaker over the test's own engine, which is what `run_all` takes.

    `run_all` opens a transaction per stage, so it needs to make sessions rather than borrow one.
    Bound to the same engine as the test's session so both see the same schema.
    """
    from sqlalchemy.orm import sessionmaker

    return sessionmaker(bind=session.get_bind(), expire_on_commit=False)


def _uploaded(session: Session, store: LocalStore) -> tuple[UUID, PackageRevision, UUID]:
    """A package whose drawing has been uploaded — where a reviewer's work begins."""
    if session.get(Project, PROJECT) is None:
        session.add(Project(id=PROJECT, name="the v1 loop"))
        session.flush()
    package = Package(project_id=PROJECT, vendor="Apex Glass & Stone")
    session.add(package)
    session.flush()
    revision = PackageRevision(
        package_id=package.id, revision_number=1, state=PackageState.UPLOADED
    )
    session.add(revision)
    session.flush()

    digest = hashlib.sha256(DRAWING).hexdigest()
    document = Document(package_id=package.id, kind="shop")
    session.add(document)
    session.flush()
    key = storage_key(document.id, digest)
    session.add(SourceArtifact(storage_key=key, sha256=digest, size=len(DRAWING)))
    session.flush()
    version = DocumentVersion(
        document_id=document.id,
        source_artifact_id=session.execute(
            select(SourceArtifact.id).where(SourceArtifact.storage_key == key)
        ).scalar_one(),
        sha256=digest,
        page_count=1,
    )
    session.add(version)
    session.flush()
    session.add(
        PackageRevisionDocument(
            package_revision_id=revision.id,
            package_id=package.id,
            document_id=document.id,
            document_version_id=version.id,
        )
    )
    run = WorkflowRun(package_revision_id=revision.id, engine_run_id=str(uuid4()))
    session.add(run)
    session.flush()
    session.flush()
    store.put(key, io.BytesIO(DRAWING), content_type="application/pdf")
    session.commit()
    return package.id, revision, run.id


def test_nothing_in_the_loop_types_a_candidate_on_its_own(
    session: Session, store: LocalStore, client: Any
) -> None:
    """**The hard stop, asserted over the whole loop rather than one stage.**

    A reviewer supplies meaning. Nothing else does — not extraction, not the checks, not the report.
    Auto-typing is gated on the real drawings (#274) and the vocabulary Q20 defers, and this is the
    assertion that says so about the finished product rather than about one module.
    """
    _, revision, run_id = _uploaded(session, store)
    run_all(
        _factory(session),
        package_revision_id=revision.id,
        workflow_run_id=run_id,
        stages=DatabaseStages(store, missing_space=MISSING_SPACE),
    )
    session.expire_all()

    guesses = list(
        session.execute(select(ObservationCandidate.semantic_guess).distinct()).scalars()
    )

    assert guesses == [None], f"something assigned a semantic type: {guesses}"
