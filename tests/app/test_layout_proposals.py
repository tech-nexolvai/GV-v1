"""Layout discriminator proposals stay proposals until a reviewer confirms them (#655)."""

from __future__ import annotations

import importlib.util
import pathlib
from collections.abc import Iterator
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
import yaml
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from alembic import command
from app.api.dependencies import get_session
from app.config import Settings
from app.db.session import session_factory
from app.main import create_app
from app.models import (
    Document,
    DocumentVersion,
    EvidenceArtifact,
    EvidenceArtifactKind,
    ObservationCandidate,
    OutboxEntry,
    Package,
    PackageRevision,
    PackageRevisionDocument,
    PackageState,
    Project,
    RuleDefinition,
    RuleSnapshot,
    SourceArtifact,
)
from app.models.document import DocumentKind, Page
from app.models.evidence import LayoutConfirmation, LayoutProposal
from app.models.runs import ExtractionRun, TaskRun, WorkflowRun
from rules.schema import Rule
from rules.snapshot import publish
from tests.app.postgres_fixture import alembic_config
from workflow.layout_proposals import record_layout_proposal

pytest_plugins = ("tests.app.postgres_fixture",)

PROJECT = uuid4()
RULEBOOK = pathlib.Path(__file__).resolve().parents[2] / "rules" / "rulebook"


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        database_url="postgresql+psycopg://unused@localhost/unused",
        environment="test",
    )


def _principal() -> Any:
    from app.auth import Principal, Role

    return Principal(id="anant", roles=frozenset({Role.ADMIN}), projects=frozenset({PROJECT}))


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


def _client(session: Session) -> Any:
    from fastapi.testclient import TestClient

    from app.auth import authenticate

    app = create_app(_settings())
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[authenticate] = _principal
    return TestClient(app, raise_server_exceptions=False)


def _publish_rulebook(session: Session) -> None:
    for path in sorted(RULEBOOK.glob("*.yaml")):
        rule = Rule.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        snapshot = publish(rule)
        definition = RuleDefinition(rule_id=rule.id)
        session.add(definition)
        session.flush()
        session.add(
            RuleSnapshot(
                rule_definition_id=definition.id,
                snapshot_id=snapshot.snapshot_id,
                version=rule.version,
                canonical_json=snapshot.canonical_json,
                product_type=rule.product_type.value,
                check_type=rule.check_type.value,
                unconfirmed_tolerance_count=0,
            )
        )
    session.flush()


def _package_with_crop(session: Session) -> tuple[UUID, UUID, UUID]:
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
        sha256="2" * 64,
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


def test_a_proposal_without_confirmation_cannot_reach_run_checks(session: Session) -> None:
    _publish_rulebook(session)
    package_id, revision_id, crop_id = _package_with_crop(session)
    _record_wall_config_proposal(session, revision_id, crop_id)
    session.commit()

    response = _client(session).post(f"/api/v1/projects/{PROJECT}/packages/{package_id}/checks")

    assert response.status_code == 202, response.text
    entry = session.execute(select(OutboxEntry)).scalar_one()
    assert entry.payload["discriminators"] == {}


def test_required_inputs_returns_the_layout_proposal_beside_choices(session: Session) -> None:
    _publish_rulebook(session)
    package_id, revision_id, crop_id = _package_with_crop(session)
    proposal = _record_wall_config_proposal(session, revision_id, crop_id)
    session.commit()

    response = _client(session).get(
        f"/api/v1/projects/{PROJECT}/packages/{package_id}/required-inputs"
    )

    assert response.status_code == 200, response.text
    wall_config = next(
        discriminator
        for discriminator in response.json()["discriminators"]
        if discriminator["name"] == "wall_config"
    )
    assert "back_only" in wall_config["choices"]
    assert wall_config["proposal"] == {
        "value": "back_only",
        "crop_artifact_id": str(proposal.crop_artifact_id),
        "model_id": "amazon.nova-lite-v1:0",
        "prompt_id": "layout-discriminator-v1",
        "confirmed": False,
    }


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


def test_confirming_a_discriminator_records_who_and_when(session: Session) -> None:
    _publish_rulebook(session)
    package_id, revision_id, crop_id = _package_with_crop(session)
    proposal = _record_wall_config_proposal(session, revision_id, crop_id)
    session.commit()

    response = _client(session).post(
        f"/api/v1/projects/{PROJECT}/packages/{package_id}/checks",
        json={"discriminators": {"wall_config": "back_only"}},
    )

    assert response.status_code == 202, response.text
    confirmation = session.execute(select(LayoutConfirmation)).scalar_one()
    assert confirmation.package_revision_id == revision_id
    assert confirmation.discriminator_name == "wall_config"
    assert confirmation.value == "back_only"
    assert confirmation.confirmed_by == "anant"
    assert confirmation.created_at is not None
    assert session.get(LayoutProposal, proposal.id).proposed_value == "back_only"
    entry = session.execute(select(OutboxEntry)).scalar_one()
    assert entry.payload["discriminators"] == {"wall_config": "back_only"}


def test_layout_tables_are_declared_append_only_in_their_migration() -> None:
    path = pathlib.Path(__file__).resolve().parents[2] / "alembic/versions/0047_layout_proposals.py"
    spec = importlib.util.spec_from_file_location("layout_proposal_migration", path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    assert set(migration.IMMUTABLE_TABLES) == {"layout_proposals", "layout_confirmations"}
