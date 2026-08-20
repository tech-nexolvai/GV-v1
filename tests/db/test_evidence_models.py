"""Database contract for the immutable persisted evidence plane in issue #195."""

from __future__ import annotations

import hashlib
from fractions import Fraction
from uuid import UUID

import pytest
from alembic.config import Config
from sqlalchemy import Engine, Float, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alembic import command
from app.db.base import Base, Immutable
from app.db.session import session_factory, unit_of_work
from app.models import (
    CanonicalObservation,
    Document,
    DocumentKind,
    DocumentVersion,
    EvidenceArtifact,
    EvidenceArtifactKind,
    EvidenceCandidateRole,
    EvidenceCorroborationLane,
    EvidenceSupportingCandidate,
    ExtractionRun,
    ObservationCandidate,
    Package,
    PackageRevision,
    PackageState,
    Page,
    Project,
    SourceArtifact,
    TaskRun,
    WorkflowRun,
)
from evidence.canonical import Authority, CorroborationLane
from rules.semantic_types import DocumentRole, SemanticType
from units.measurement import Unit
from verdict.operands import EvidenceStatus

pytest_plugins = ("tests.app.postgres_fixture",)

EVIDENCE_TABLES = {
    "observation_candidates",
    "canonical_observations",
    "evidence_supporting_candidates",
    "evidence_corroboration_lanes",
    "evidence_artifacts",
}
HASH = "a" * 64
PAGE_HASH = "b" * 64


def test_candidates_and_canonical_facts_are_distinct_registered_tables() -> None:
    """Input: model registry. Outcome: two tables. Why: promotion must never be a status flip."""

    assert EVIDENCE_TABLES <= set(Base.metadata.tables)
    assert "status" not in Base.metadata.tables["observation_candidates"].columns
    assert "status" in Base.metadata.tables["canonical_observations"].columns


@pytest.mark.parametrize(
    "model",
    [
        ObservationCandidate,
        CanonicalObservation,
        EvidenceSupportingCandidate,
        EvidenceCorroborationLane,
        EvidenceArtifact,
    ],
)
def test_every_evidence_record_is_marked_immutable(model: type) -> None:
    """Input: every evidence model. Outcome: marker. Why: C1.12 must revoke update/delete."""

    assert issubclass(model, Immutable)


def test_no_evidence_column_uses_binary_floating_point() -> None:
    """Input: evidence metadata. Outcome: no Float. Why: persisted evidence must remain exact."""

    for table_name in EVIDENCE_TABLES:
        for column in Base.metadata.tables[table_name].columns:
            assert not isinstance(column.type, Float), f"{table_name}.{column.name} is approximate"


def test_polygons_and_pages_name_their_coordinate_space() -> None:
    """Input: spatial evidence tables. Outcome: page plus space. Why: coordinates need a frame."""

    for table_name in ("observation_candidates", "canonical_observations", "evidence_artifacts"):
        columns = Base.metadata.tables[table_name].columns
        assert {"page_id", "coordinate_space"} <= set(columns.keys())


def test_artifact_hash_detects_changed_retrieved_content() -> None:
    """Input: bytes differing from the stored digest. Outcome: False. Why: SHA-256 is a guard."""

    original = b"review crop bytes"
    artifact = EvidenceArtifact(
        candidate_id=UUID(int=1),
        canonical_observation_id=None,
        document_version_id=UUID(int=2),
        page_id=UUID(int=3),
        kind=EvidenceArtifactKind.CROP,
        storage_key="evidence/crops/one.png",
        sha256=hashlib.sha256(original).hexdigest(),
        media_type="image/png",
        coordinate_space="image",
    )

    assert artifact.content_matches(original) is True
    assert artifact.content_matches(b"changed crop bytes") is False


def _upgrade(engine: Engine) -> None:
    config = Config("alembic.ini")
    config.attributes["database_url"] = engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")


def _persist_context(session: Session) -> tuple[UUID, UUID, UUID]:
    project = Project(name="GV Evidence Test")
    package = Package(project_id=project.id, vendor=None)
    revision = PackageRevision(
        package_id=package.id,
        revision_number=1,
        state=PackageState.CREATED,
    )
    artifact = SourceArtifact(
        storage_key=f"originals/{project.id}/drawing.pdf",
        sha256=HASH,
        size=100,
        backend_version_id=None,
    )
    session.add(project)
    session.flush()
    session.add(package)
    session.flush()
    session.add(revision)
    session.flush()
    document = Document(package_id=revision.package_id, kind=DocumentKind.SHOP)
    session.add_all((artifact, document))
    session.flush()
    version = DocumentVersion(
        document_id=document.id,
        source_artifact_id=artifact.id,
        sha256=HASH,
        page_count=1,
    )
    session.add(version)
    session.flush()
    page = Page(
        document_version_id=version.id,
        index=0,
        content_hash=PAGE_HASH,
        width_pt=612,
        height_pt=792,
        rotation=0,
        has_vector_text=True,
        render_failed=False,
        sheet_number="A-101",
        page_type=None,
        revision_label=None,
        revision_date_raw=None,
        revision_date_interpretations=None,
        revision_sequence_index=None,
    )
    workflow = WorkflowRun(package_revision_id=revision.id, engine_run_id=f"run-{project.id}")
    session.add_all((page, workflow))
    session.flush()
    task = TaskRun(
        workflow_run_id=workflow.id,
        idempotency_key=f"extract-{project.id}",
        task_type="extract_page",
        attempt=1,
        outcome="ok",
    )
    session.add(task)
    session.flush()
    extraction = ExtractionRun(
        task_run_id=task.id,
        extractor="pdfplumber",
        extractor_version="1.0",
        config_hash="config-v1",
    )
    session.add(extraction)
    session.flush()
    return version.id, page.id, extraction.id


def _candidate(
    document_version_id: UUID,
    page_id: UUID,
    extraction_run_id: UUID,
    raw_text: str,
    value: Fraction = Fraction(1, 3),
) -> ObservationCandidate:
    return ObservationCandidate(
        document_version_id=document_version_id,
        page_id=page_id,
        extraction_run_id=extraction_run_id,
        raw_text=raw_text,
        value_numerator=value.numerator,
        value_denominator=value.denominator,
        unit=Unit.INCH,
        unit_guess=Unit.INCH,
        semantic_guess=SemanticType.CT001,
        polygon=[[10, 10], [20, 10], [20, 20]],
        coordinate_space="image",
        confidence=None,
        ambiguity_flags=[],
    )


def _canonical(
    document_version_id: UUID,
    page_id: UUID,
    status: EvidenceStatus,
) -> CanonicalObservation:
    value = Fraction(1, 3)
    return CanonicalObservation(
        document_version_id=document_version_id,
        page_id=page_id,
        document_role=DocumentRole.SHOP,
        polygon=[["0.1", "0.1"], ["0.2", "0.1"], ["0.2", "0.2"]],
        coordinate_space="stored",
        semantic_type=SemanticType.CT001,
        value_numerator=value.numerator,
        value_denominator=value.denominator,
        unit=Unit.INCH,
        status=status,
        authority=Authority.AUTHORITATIVE,
        evidence_crop_uri=None,
    )


def test_fraction_round_trips_as_a_normalized_integer_pair(postgres_engine: Engine) -> None:
    """Input: exact 1/3. Outcome: 1 and 3. Why: NUMERIC expansion would lose exact identity."""

    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    candidate_id: UUID
    with unit_of_work(factory) as session:
        version_id, page_id, extraction_id = _persist_context(session)
        candidate = _candidate(version_id, page_id, extraction_id, "1/3")
        candidate_id = candidate.id
        session.add(candidate)
    with unit_of_work(factory) as session:
        restored = session.get(ObservationCandidate, candidate_id)
        assert restored is not None
        assert restored.value_numerator is not None
        assert restored.value_denominator is not None
        assert Fraction(restored.value_numerator, restored.value_denominator) == Fraction(1, 3)
        assert (restored.value_numerator, restored.value_denominator) == (1, 3)


@pytest.mark.parametrize(
    "status",
    [
        EvidenceStatus.RAW_CANDIDATE,
        EvidenceStatus.CORROBORATED,
        EvidenceStatus.CONFLICTING,
    ],
)
def test_deferred_constraint_rejects_status_without_required_provenance(
    postgres_engine: Engine,
    status: EvidenceStatus,
) -> None:
    """Input: unsupported status. Outcome: rejection at commit. Why: no false evidence fact."""

    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with (
        pytest.raises(IntegrityError, match="provenance is invalid"),
        unit_of_work(factory) as session,
    ):
        version_id, page_id, _ = _persist_context(session)
        session.add(_canonical(version_id, page_id, status))


@pytest.mark.parametrize("status", [EvidenceStatus.HUMAN_CONFIRMED, EvidenceStatus.REJECTED])
def test_status_with_no_required_provenance_needs_no_fabricated_candidate(
    postgres_engine: Engine,
    status: EvidenceStatus,
) -> None:
    """Input: human/rejected fact. Outcome: commit. Why: neither requires an extractor candidate."""

    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    observation_id: UUID
    with unit_of_work(factory) as session:
        version_id, page_id, _ = _persist_context(session)
        observation = _canonical(version_id, page_id, status)
        observation_id = observation.id
        session.add(observation)
    with unit_of_work(factory) as session:
        assert session.get(CanonicalObservation, observation_id) is not None


def test_conflicting_observation_retains_support_and_conflict_relationally(
    postgres_engine: Engine,
) -> None:
    """Input: agreeing and disagreeing readers. Outcome: both links. Why: ORM never picks a winner."""

    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    observation_id: UUID
    with unit_of_work(factory) as session:
        version_id, page_id, extraction_id = _persist_context(session)
        supporting = _candidate(version_id, page_id, extraction_id, "1/3")
        conflicting = _candidate(version_id, page_id, extraction_id, "2/3", value=Fraction(2, 3))
        observation = _canonical(version_id, page_id, EvidenceStatus.CONFLICTING)
        observation_id = observation.id
        session.add_all((supporting, conflicting, observation))
        session.flush()
        session.add_all(
            (
                EvidenceSupportingCandidate(
                    canonical_observation_id=observation.id,
                    candidate_id=supporting.id,
                    role=EvidenceCandidateRole.PRIMARY,
                ),
                EvidenceSupportingCandidate(
                    canonical_observation_id=observation.id,
                    candidate_id=conflicting.id,
                    role=EvidenceCandidateRole.CONFLICTING,
                ),
            )
        )
    with unit_of_work(factory) as session:
        roles = session.scalars(
            select(EvidenceSupportingCandidate.role).where(
                EvidenceSupportingCandidate.canonical_observation_id == observation_id
            )
        ).all()
        assert set(roles) == {
            EvidenceCandidateRole.PRIMARY.value,
            EvidenceCandidateRole.CONFLICTING.value,
        }


def test_one_candidate_plus_dual_unit_satisfies_corroborated_provenance(
    postgres_engine: Engine,
) -> None:
    """Input: one reader plus dual token. Outcome: commit. Why: this is the free corroboration lane."""

    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        version_id, page_id, extraction_id = _persist_context(session)
        candidate = _candidate(version_id, page_id, extraction_id, "984 [38 3/4]")
        observation = _canonical(version_id, page_id, EvidenceStatus.CORROBORATED)
        session.add_all((candidate, observation))
        session.flush()
        session.add_all(
            (
                EvidenceSupportingCandidate(
                    canonical_observation_id=observation.id,
                    candidate_id=candidate.id,
                    role=EvidenceCandidateRole.PRIMARY,
                ),
                EvidenceCorroborationLane(
                    canonical_observation_id=observation.id,
                    lane=CorroborationLane.DUAL_UNIT,
                ),
            )
        )


def test_non_normalized_rational_is_rejected_before_insert(postgres_engine: Engine) -> None:
    """Input: 310/8. Outcome: rejection. Why: 155/4 must have only one stored spelling."""

    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with (
        pytest.raises(ValueError, match="normalized Fraction form"),
        unit_of_work(factory) as session,
    ):
        version_id, page_id, extraction_id = _persist_context(session)
        candidate = _candidate(version_id, page_id, extraction_id, "38 3/4")
        candidate.value_numerator = 310
        candidate.value_denominator = 8
        session.add(candidate)
        session.flush()
