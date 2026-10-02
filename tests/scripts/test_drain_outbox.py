"""The local demo worker must hand an extraction result to a reviewer, even when it read nothing."""

from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alembic import command
from app.db.session import session_factory
from app.lifecycle.states import begin, transition
from app.models import Package, PackageRevision, PackageState, Project
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


class _NoCandidatesStages:
    """A completed extraction whose honest result is no AI proposals."""

    def ingest(self, session: Session, package_revision_id: object) -> dict[str, object]:
        del session, package_revision_id
        return {"implemented": True}

    def extract_pages(self, session: Session, package_revision_id: object) -> tuple[object, ...]:
        del session, package_revision_id
        return ()

    def match(self, session: Session, package_revision_id: object) -> dict[str, object]:
        del session, package_revision_id
        return {"proposed": 0}

    def validate_evidence(self, session: Session, package_revision_id: object) -> dict[str, object]:
        del session, package_revision_id
        return {"crops": 0}


def test_zero_candidate_extraction_hands_off_to_reviewer_input(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zero proposals are an abstention, not a processing state that never completes."""
    import scripts.drain_outbox as worker

    project = Project(name="zero proposal handoff")
    session.add(project)
    session.flush()
    package = Package(project_id=project.id)
    session.add(package)
    session.flush()
    revision = PackageRevision(package_id=package.id, revision_number=1, state=PackageState.CREATED)
    session.add(revision)
    session.flush()
    begin(session, revision.id, actor="reviewer")
    transition(session, revision.id, PackageState.UPLOADING, actor="reviewer")
    transition(session, revision.id, PackageState.UPLOADED, actor="reviewer")

    monkeypatch.setattr(worker, "_stages", lambda **_: _NoCandidatesStages())
    worker._extract_package(session, revision.id, str(uuid4()))
    session.commit()

    state = session.scalar(select(PackageRevision.state).where(PackageRevision.id == revision.id))
    assert state == PackageState.NEEDS_INPUT.value


def test_reviewer_input_resumes_the_validation_handoff_before_checks(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A zero-result handoff must not make a later check request violate the resume guard."""
    import scripts.drain_outbox as worker

    project = Project(name="resume zero proposal handoff")
    session.add(project)
    session.flush()
    package = Package(project_id=project.id)
    session.add(package)
    session.flush()
    revision = PackageRevision(package_id=package.id, revision_number=1, state=PackageState.CREATED)
    session.add(revision)
    session.flush()
    begin(session, revision.id, actor="reviewer")
    transition(session, revision.id, PackageState.UPLOADING, actor="reviewer")
    transition(session, revision.id, PackageState.UPLOADED, actor="reviewer")

    monkeypatch.setattr(worker, "_stages", lambda **_: _NoCandidatesStages())
    worker._extract_package(session, revision.id, str(uuid4()))
    worker._resume_from_reviewer_input(session, revision)
    session.commit()

    state = session.scalar(select(PackageRevision.state).where(PackageRevision.id == revision.id))
    assert state == PackageState.VALIDATING_EVIDENCE.value


def _uploaded_revision(session: Session, name: str) -> PackageRevision:
    project = Project(name=name)
    session.add(project)
    session.flush()
    package = Package(project_id=project.id)
    session.add(package)
    session.flush()
    revision = PackageRevision(package_id=package.id, revision_number=1, state=PackageState.CREATED)
    session.add(revision)
    session.flush()
    begin(session, revision.id, actor="reviewer")
    transition(session, revision.id, PackageState.UPLOADING, actor="reviewer")
    transition(session, revision.id, PackageState.UPLOADED, actor="reviewer")
    return revision


def test_extraction_builds_the_phrase_index_when_the_gap_is_stated(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hook runs after the stages and reports counts only (#836)."""
    import scripts.drain_outbox as worker

    revision = _uploaded_revision(session, "phrase index built")
    monkeypatch.setenv(worker.PHRASE_GAP_VARIABLE, "0.3")
    monkeypatch.setattr(worker, "_stages", lambda **_: _NoCandidatesStages())

    results = worker._extract_package(session, revision.id, str(uuid4()))

    assert results["package_text"] == {
        "built": True,
        "written": False,
        "phrases": 0,
        "runs": 0,
        "markup": 0,
        "dimension": 0,
        "note": 0,
    }


def test_a_failed_phrase_build_takes_back_its_own_rows_and_nothing_else(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**It never fails the extraction** (#836). A build that writes and then fails leaves no row
    behind, and the revision still reaches the reviewer exactly as it would have without it."""
    import scripts.drain_outbox as worker
    from retrieval import package_text

    revision = _uploaded_revision(session, "phrase index failed")

    def _writes_then_fails(build_session: Session, *_: object) -> object:
        build_session.add(Project(name="written by a failing phrase build"))
        build_session.flush()
        raise RuntimeError("the build failed part-way through")

    monkeypatch.setenv(worker.PHRASE_GAP_VARIABLE, "0.3")
    monkeypatch.setattr(worker, "_stages", lambda **_: _NoCandidatesStages())
    monkeypatch.setattr(package_text, "build_package_phrases", _writes_then_fails)

    results = worker._extract_package(session, revision.id, str(uuid4()))
    session.commit()

    assert results["package_text"] == {
        "built": False,
        "reason": "the phrase build failed: RuntimeError",
    }
    state = session.scalar(select(PackageRevision.state).where(PackageRevision.id == revision.id))
    assert state == PackageState.NEEDS_INPUT.value
    assert (
        session.scalar(
            select(Project.id).where(Project.name == "written by a failing phrase build")
        )
        is None
    )
