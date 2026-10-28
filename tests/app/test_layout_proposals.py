"""Layout discriminator proposals stay proposals until a reviewer confirms them (#655)."""

from __future__ import annotations

import importlib.util
import pathlib
from collections.abc import Iterator
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
import yaml
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from alembic import command
from app.db.session import session_factory
from app.models import (
    Document,
    DocumentVersion,
    EvidenceArtifact,
    EvidenceArtifactKind,
    ObservationCandidate,
    Package,
    PackageRevision,
    PackageRevisionDocument,
    PackageState,
    Project,
    SourceArtifact,
)
from app.models.document import DocumentKind, Page
from app.models.evidence import LayoutProposal
from app.models.runs import ExtractionRun, TaskRun, WorkflowRun
from rules.schema import Rule
from tests.app.postgres_fixture import alembic_config
from workflow.layout_proposals import record_layout_proposal

pytest_plugins = ("tests.app.postgres_fixture",)

PROJECT = uuid4()
RULEBOOK = pathlib.Path(__file__).resolve().parents[2] / "rules" / "rulebook"


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


def _package_with_crop(session: Session, *, crop_sha: str = "2" * 64) -> tuple[UUID, UUID, UUID]:
    if session.get(Project, PROJECT) is None:
        session.add(Project(id=PROJECT, name="layout proposal tests"))
        session.flush()

    package = Package(project_id=PROJECT, vendor="Apex Glass & Stone")
    session.add(package)
    session.flush()
    revision = PackageRevision(
        package_id=package.id, revision_number=1, state=PackageState.NEEDS_INPUT
    )
    session.add(revision)
    session.flush()

    source = SourceArtifact(storage_key=f"s/{uuid4()}", sha256="1" * 64, size=1)
    session.add(source)
    session.flush()
    document = Document(package_id=package.id, kind=DocumentKind.SHOP.value)
    session.add(document)
    session.flush()
    version = DocumentVersion(
        document_id=document.id, source_artifact_id=source.id, sha256="1" * 64, page_count=1
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
    page = Page(
        document_version_id=version.id,
        index=0,
        content_hash="1" * 64,
        width_pt=Decimal(612),
        height_pt=Decimal(792),
        rotation=0,
        has_vector_text=True,
    )
    session.add(page)
    session.flush()

    workflow_run = WorkflowRun(package_revision_id=revision.id, engine_run_id=str(uuid4()))
    session.add(workflow_run)
    session.flush()
    task_run = TaskRun(
        workflow_run_id=workflow_run.id,
        idempotency_key=str(uuid4()),
        task_type="extract",
        attempt=1,
        outcome="SUCCEEDED",
    )
    session.add(task_run)
    session.flush()
    run = ExtractionRun(
        task_run_id=task_run.id,
        extractor="layout-test",
        extractor_version="1",
        config_hash="layout-test",
    )
    session.add(run)
    session.flush()
    candidate = ObservationCandidate(
        document_version_id=version.id,
        page_id=page.id,
        extraction_run_id=run.id,
        raw_text="layout crop anchor",
        polygon=[[0, 0], [10, 0], [10, 10], [0, 10]],
        ambiguity_flags=[],
    )
    session.add(candidate)
    session.flush()
    crop = EvidenceArtifact(
        candidate_id=candidate.id,
        canonical_observation_id=None,
        document_version_id=version.id,
        page_id=page.id,
        kind=EvidenceArtifactKind.CROP.value,
        storage_key=f"evidence-crops/{uuid4()}.png",
        sha256=crop_sha,
        media_type="image/png",
        coordinate_space="image",
    )
    session.add(crop)
    session.commit()
    return package.id, revision.id, crop.id


def _record_wall_config_proposal(
    session: Session, revision_id: UUID, crop_id: UUID
) -> LayoutProposal:
    return record_layout_proposal(
        session,
        package_revision_id=revision_id,
        discriminator_name="wall_config",
        proposed_value="back_only",
        crop_artifact_id=crop_id,
        model_id="amazon.nova-lite-v1:0",
        prompt_id="layout-discriminator-v1",
    )


def test_unchanged_layout_evidence_does_not_mint_a_duplicate_proposal(
    session: Session,
) -> None:
    _package_id, revision_id, crop_id = _package_with_crop(session)

    first = _record_wall_config_proposal(session, revision_id, crop_id)
    second = _record_wall_config_proposal(session, revision_id, crop_id)

    assert second.id == first.id
    count = session.execute(select(func.count()).select_from(LayoutProposal)).scalar_one()
    assert count == 1


def test_the_recorded_crop_artifact_is_retrievable(session: Session) -> None:
    _package_id, revision_id, crop_id = _package_with_crop(session)

    proposal = _record_wall_config_proposal(session, revision_id, crop_id)

    artifact = session.get(EvidenceArtifact, proposal.crop_artifact_id)
    assert artifact is not None
    assert artifact.id == crop_id
    assert artifact.media_type == "image/png"


def test_layout_tables_are_declared_append_only_in_their_migration() -> None:
    path = pathlib.Path(__file__).resolve().parents[2] / "alembic/versions/0047_layout_proposals.py"
    spec = importlib.util.spec_from_file_location("layout_proposal_migration", path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    assert set(migration.IMMUTABLE_TABLES) == {"layout_proposals", "layout_confirmations"}


# ---------------------------------------------------------------------------------------------
# The wall layout two readers agreed on (#992)
# ---------------------------------------------------------------------------------------------

READERS = "us.moonshotai.kimi-k3 + qwen.qwen3-vl-235b-a22b"


def test_the_finding_note_names_the_readers_only_when_they_chose() -> None:
    from workflow.layout_proposals import discriminator_note

    note = discriminator_note("wall_config", "back_left_right", "two AI readers agreed: a + b")
    assert note is not None and "two AI readers" in note and "back_left_right" in note
    assert discriminator_note("wall_config", "back_only", "reviewer") is None
    assert discriminator_note("wall_config", "back_only", None) is None


def test_a_finding_whose_layout_the_readers_chose_carries_the_note() -> None:
    from workflow.stages import DatabaseStages

    width = Rule.model_validate(
        yaml.safe_load((RULEBOOK / "ct_width_001.yaml").read_text(encoding="utf-8"))
    )
    readers = DatabaseStages(
        discriminators={"wall_config": "back_left_right"},
        discriminator_sources={"wall_config": f"two AI readers agreed: {READERS}"},
    )
    (note,) = readers._discriminator_notes(width)
    assert "two AI readers" in note
    reviewer = DatabaseStages(
        discriminators={"wall_config": "back_left_right"},
        discriminator_sources={"wall_config": "reviewer"},
    )
    assert reviewer._discriminator_notes(width) == ()
