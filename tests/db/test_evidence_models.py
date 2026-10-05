"""Database contract for the immutable persisted evidence plane in issue #195."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from fractions import Fraction
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, Float, select, text
from sqlalchemy import inspect as sa_inspect
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
from tests.app.postgres_fixture import alembic_config
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


def test_crop_mark_state_is_explicitly_tristate() -> None:
    """A legacy crop is unknown, not clean; new checks may record either answer."""

    assert "shows_gv_marks" in EvidenceArtifact.__table__.columns
    assert (
        EvidenceArtifact(
            candidate_id=UUID(int=1),
            document_version_id=UUID(int=2),
            page_id=UUID(int=3),
            kind=EvidenceArtifactKind.CROP.value,
            storage_key="evidence/crops/unknown.png",
            sha256=HASH,
            media_type="image/png",
            coordinate_space="image",
        ).shows_gv_marks
        is None
    )


def _upgrade(engine: Engine) -> None:
    config = alembic_config()
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


def test_evidence_crop_sharing_migration_downgrades_cleanly(
    postgres_engine: Engine,
) -> None:
    """No shared digest means the old uniqueness rule can be restored without data loss."""

    _upgrade(postgres_engine)
    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)

    command.downgrade(config, "0065_check_run_defaults_citation")

    inspector = sa_inspect(postgres_engine)
    assert "shows_gv_marks" not in {
        column["name"] for column in inspector.get_columns("evidence_artifacts")
    }
    assert any(
        set(item["column_names"]) == {"storage_key", "sha256"}
        for item in inspector.get_unique_constraints("evidence_artifacts")
    )
    command.upgrade(config, "head")


def test_evidence_crop_migration_leaves_legacy_rows_untouched(
    postgres_engine: Engine,
) -> None:
    """A crop row inserted under the old schema survives upgrade with an unknown mark state."""

    _upgrade(postgres_engine)
    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.downgrade(config, "0065_check_run_defaults_citation")

    factory = session_factory(postgres_engine)
    artifact_id = uuid4()
    crop_key = "evidence/crops/legacy.png"
    with unit_of_work(factory) as session:
        version_id, page_id, extraction_id = _persist_context(session)
        candidate = _candidate(version_id, page_id, extraction_id, "legacy crop")
        session.add(candidate)
        session.flush()
        session.execute(
            text(
                "INSERT INTO evidence_artifacts "
                "(id, created_at, candidate_id, canonical_observation_id, "
                "document_version_id, page_id, kind, storage_key, sha256, media_type, "
                "coordinate_space) VALUES (:id, :created_at, :candidate_id, NULL, "
                ":document_version_id, :page_id, 'crop', :storage_key, :sha256, "
                "'image/png', 'image')"
            ),
            {
                "id": artifact_id,
                "created_at": datetime.now(UTC),
                "candidate_id": candidate.id,
                "document_version_id": version_id,
                "page_id": page_id,
                "storage_key": crop_key,
                "sha256": HASH,
            },
        )

    command.upgrade(config, "head")
    with session_factory(postgres_engine)() as session:
        row = session.get(EvidenceArtifact, artifact_id)
        assert row is not None
        assert row.storage_key == crop_key
        assert row.sha256 == HASH
        assert row.shows_gv_marks is None


def test_evidence_crop_sharing_migration_refuses_downgrade_without_touching_rows(
    postgres_engine: Engine,
) -> None:
    """Shared objects block restoration of uniqueness; no row may be merged or removed."""

    from sqlalchemy import func

    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    shared_key = "evidence/crops/shared.png"
    with unit_of_work(factory) as session:
        version_id, page_id, extraction_id = _persist_context(session)
        first = _candidate(version_id, page_id, extraction_id, "first")
        second = _candidate(version_id, page_id, extraction_id, "second")
        session.add_all((first, second))
        session.flush()
        session.add_all(
            EvidenceArtifact(
                candidate_id=candidate.id,
                canonical_observation_id=None,
                document_version_id=version_id,
                page_id=page_id,
                kind=EvidenceArtifactKind.CROP.value,
                storage_key=shared_key,
                sha256=HASH,
                media_type="image/png",
                coordinate_space="image",
                shows_gv_marks=None,
            )
            for candidate in (first, second)
        )

    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    with pytest.raises(RuntimeError, match="cannot downgrade.*shared evidence crop"):
        command.downgrade(config, "0065_check_run_defaults_citation")

    with session_factory(postgres_engine)() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(EvidenceArtifact)
                .where(EvidenceArtifact.storage_key == shared_key)
            )
            == 2
        )
    command.upgrade(config, "head")


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


# ---------------------------------------------------------------------------
# Who wrote the annotation a candidate was read from (#543)
# ---------------------------------------------------------------------------


def test_the_annotation_author_round_trips(postgres_engine: Engine) -> None:
    """Input: a candidate read from a reviewer's annotation. Outcome: the author survives.

    `/T` is the only place the file records who corrected a drawing, and a reviewer's correction of a
    vendor's dimension outranks it partly by virtue of who wrote it.
    """
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    candidate_id: UUID
    with unit_of_work(factory) as session:
        version_id, page_id, extraction_id = _persist_context(session)
        candidate = _candidate(version_id, page_id, extraction_id, '185 1/4"')
        candidate.source_author = "GVI-007"
        candidate_id = candidate.id
        session.add(candidate)
    with unit_of_work(factory) as session:
        restored = session.get(ObservationCandidate, candidate_id)
        assert restored is not None
        assert restored.source_author == "GVI-007"


def test_a_reading_with_no_author_stores_null(postgres_engine: Engine) -> None:
    """Outcome: null, which is the ordinary case — a number read off a drawing has no author."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    candidate_id: UUID
    with unit_of_work(factory) as session:
        version_id, page_id, extraction_id = _persist_context(session)
        candidate = _candidate(version_id, page_id, extraction_id, '38 3/4"')
        candidate_id = candidate.id
        session.add(candidate)
    with unit_of_work(factory) as session:
        restored = session.get(ObservationCandidate, candidate_id)
        assert restored is not None
        assert restored.source_author is None


@pytest.mark.parametrize("author", ["", "   ", "\t"])
def test_a_blank_author_is_refused(postgres_engine: Engine, author: str) -> None:
    """Input: an author that is present but empty. Outcome: the database refuses it.

    A `/T` written without a value is a tool filling in a key, not a person. Stored as `''` it would
    make "nobody said" and "somebody said nothing" the same row — and the second reads as an
    attribution.
    """
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with pytest.raises(IntegrityError, match="source_author"), unit_of_work(factory) as session:
        version_id, page_id, extraction_id = _persist_context(session)
        candidate = _candidate(version_id, page_id, extraction_id, '185 1/4"')
        candidate.source_author = author
        session.add(candidate)
