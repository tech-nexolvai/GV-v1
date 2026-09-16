"""Vision reader route wiring for issue #624.

The readers here are scripted. The point is the pipeline contract: bounded crops go to each
configured reader, successful reads become raw candidates under distinct extractor names, and every
attempt is persisted in ``model_invocations``.
"""

from __future__ import annotations

import hashlib
import io
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alembic import command
from app.api.documents import storage_key
from app.db.session import session_factory
from app.models import (
    CanonicalObservation,
    Document,
    DocumentVersion,
    ExtractionRun,
    ModelInvocation,
    ObservationCandidate,
    Package,
    PackageRevision,
    PackageRevisionDocument,
    PackageState,
    Project,
    SourceArtifact,
)
from app.models.runs import TaskRun, WorkflowRun
from evidence.candidate import ObservationCandidate as DomainCandidate
from evidence.coordinates import ImagePoint
from extraction.models.context import AssembledContext
from extraction.models.nova import (
    NovaAdapterError,
    NovaConfig,
    NovaInvocation,
    NovaInvocationOutcome,
    NovaRequest,
)
from extraction.models.validation import ValidationRejection
from storage.local import LocalStore
from tests.app.postgres_fixture import alembic_config
from tests.extraction.test_reader import _pdf
from units.measurement import Unit
from workflow.idempotency import stage_idempotency_key
from workflow.review import ENGINE_VERSION
from workflow.stages import DatabaseStages

pytest_plugins = ("tests.app.postgres_fixture",)


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


def _drawing() -> bytes:
    return _pdf(b'BT /F1 10 Tf 1 0 0 1 20 70 Tm (24 1/2") Tj ET\n')


def _revision(session: Session, store: LocalStore) -> PackageRevision:
    data = _drawing()
    digest = hashlib.sha256(data).hexdigest()
    project = Project(name=f"vision {uuid4()}")
    session.add(project)
    session.flush()
    package = Package(project_id=project.id, vendor="Apex Glass & Stone")
    session.add(package)
    session.flush()
    revision = PackageRevision(
        package_id=package.id, revision_number=1, state=PackageState.EXTRACTING
    )
    session.add(revision)
    session.flush()
    document = Document(package_id=package.id, kind="shop")
    session.add(document)
    session.flush()
    key = storage_key(document.id, digest)
    session.add(SourceArtifact(storage_key=key, sha256=digest, size=len(data)))
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
    workflow_run = WorkflowRun(package_revision_id=revision.id, engine_run_id=str(uuid4()))
    session.add(workflow_run)
    session.flush()
    session.add(
        TaskRun(
            workflow_run_id=workflow_run.id,
            idempotency_key=stage_idempotency_key(
                package_revision_id=revision.id,
                stage="extract_pages",
                engine_version=ENGINE_VERSION,
            ),
            task_type="extract_pages",
            attempt=1,
            outcome="claimed",
        )
    )
    session.flush()
    store.put(key, io.BytesIO(data), content_type="application/pdf")
    return revision


@dataclass(frozen=True, slots=True)
class ScriptedVisionReader:
    config: NovaConfig
    outcome: NovaInvocationOutcome = NovaInvocationOutcome.OK
    reading: str = '24 1/2"'

    def extract(self, request: NovaRequest, recorder: object) -> DomainCandidate:
        assert request.crop.startswith(b"\x89PNG\r\n\x1a\n")
        assert request.context == AssembledContext(nearby_text=(), nearby_geometry=())
        assert request.bound_pt == Decimal(9)
        recorder.record(  # type: ignore[attr-defined]
            NovaInvocation(
                model_id=self.config.model_id,
                prompt_id=self.config.prompt_id,
                template_id=self.config.template_id,
                attempt=1,
                latency_ms=12,
                input_tokens=31,
                output_tokens=7,
                outcome=self.outcome,
                request_id=f"request-{self.config.extractor}",
                context=request.context,
                bound_pt=request.bound_pt,
                injection_attempts=(),
            )
        )
        if self.outcome is NovaInvocationOutcome.OK:
            return DomainCandidate(
                candidate_id=request.candidate_id,
                extractor=self.config.extractor,
                extractor_version=self.config.model_id,
                raw_text=self.reading,
                parsed_value=None,
                unit_guess=Unit.INCH,
                semantic_guess=None,
                page=request.page,
                polygon=(
                    ImagePoint(0, 0),
                    ImagePoint(10, 0),
                    ImagePoint(10, 5),
                    ImagePoint(0, 5),
                ),
                confidence=None,
                ambiguity_flags=(f"{self.config.extractor}_model_reading",),
            )
        if self.outcome is NovaInvocationOutcome.REJECTED:
            recorder.record_rejection(  # type: ignore[attr-defined]
                ValidationRejection(
                    reason="schema_validation_failed",
                    raw_response='{"verdict":"PASS"}',
                    errors=("verdict: extra fields are not permitted",),
                    candidate_id=request.candidate_id,
                    extractor_version=self.config.model_id,
                )
            )
        raise NovaAdapterError(f"{self.config.extractor} did not return a candidate")


def _reader(
    extractor: str,
    model_id: str,
    *,
    outcome: NovaInvocationOutcome = NovaInvocationOutcome.OK,
) -> ScriptedVisionReader:
    return ScriptedVisionReader(
        NovaConfig(
            model_id=model_id,
            prompt_id="dimension-reader-v1",
            template_id="bounded-crop-v1",
            connect_timeout_seconds=1,
            read_timeout_seconds=1,
            max_attempts=1,
            extractor=extractor,
        ),
        outcome=outcome,
    )


def _vision_candidates(session: Session) -> list[tuple[ObservationCandidate, ExtractionRun]]:
    rows = session.execute(
        select(ObservationCandidate, ExtractionRun)
        .join(ExtractionRun, ObservationCandidate.extraction_run_id == ExtractionRun.id)
        .where(ExtractionRun.extractor.like("bedrock-%"))
        .order_by(ExtractionRun.extractor)
    )
    return [(candidate, run) for candidate, run in rows]


def test_two_bedrock_readers_emit_raw_candidates_under_distinct_extractors(
    session: Session, store: LocalStore
) -> None:
    revision = _revision(session, store)
    readers = (
        _reader("bedrock-nova-pro", "amazon.nova-pro-v1:0"),
        _reader("bedrock-claude-haiku-4-5", "anthropic.claude-haiku-4-5-20251001-v1:0"),
    )

    result = DatabaseStages(store, vision_readers=readers).extract_pages(session, revision.id)

    assert result[0].payload["vision_candidates"] == 2
    candidates = _vision_candidates(session)
    assert {run.extractor for _, run in candidates} == {
        "bedrock-nova-pro",
        "bedrock-claude-haiku-4-5",
    }
    assert all(candidate.semantic_guess is None for candidate, _ in candidates)
    assert all(candidate.raw_text == '24 1/2"' for candidate, _ in candidates)
    assert all(candidate.value_numerator == 49 for candidate, _ in candidates)
    assert all(candidate.value_denominator == 2 for candidate, _ in candidates)
    assert session.scalar(select(CanonicalObservation.id)) is None

    invocations = session.scalars(select(ModelInvocation).order_by(ModelInvocation.model_id)).all()
    assert [invocation.outcome for invocation in invocations] == ["ok", "ok"]
    assert all(invocation.candidate_id is not None for invocation in invocations)


def test_a_single_vision_candidate_is_not_sealed(session: Session, store: LocalStore) -> None:
    revision = _revision(session, store)

    DatabaseStages(
        store,
        vision_readers=(_reader("bedrock-nova-pro", "amazon.nova-pro-v1:0"),),
    ).extract_pages(session, revision.id)

    [(candidate, _run)] = _vision_candidates(session)
    assert candidate.corroboration_status is None
    assert candidate.semantic_guess is None
    assert session.scalar(select(CanonicalObservation.id)) is None


@pytest.mark.parametrize(
    ("adapter_outcome", "stored_outcome", "output_tokens"),
    [
        (NovaInvocationOutcome.REFUSED, "refused", 0),
        (NovaInvocationOutcome.TIMEOUT, "timeout", 0),
    ],
)
def test_refusal_or_timeout_records_an_invocation_without_a_candidate(
    session: Session,
    store: LocalStore,
    adapter_outcome: NovaInvocationOutcome,
    stored_outcome: str,
    output_tokens: int,
) -> None:
    revision = _revision(session, store)

    result = DatabaseStages(
        store,
        vision_readers=(
            _reader("bedrock-nova-pro", "amazon.nova-pro-v1:0", outcome=adapter_outcome),
        ),
    ).extract_pages(session, revision.id)

    assert result[0].payload["vision_candidates"] == 0
    assert _vision_candidates(session) == []
    invocation = session.scalars(select(ModelInvocation)).one()
    assert invocation.outcome == stored_outcome
    assert invocation.output_tokens == output_tokens
    assert invocation.candidate_id is None


def test_malformed_model_payload_records_rejection_without_a_candidate(
    session: Session, store: LocalStore
) -> None:
    revision = _revision(session, store)

    DatabaseStages(
        store,
        vision_readers=(
            _reader(
                "bedrock-nova-pro",
                "amazon.nova-pro-v1:0",
                outcome=NovaInvocationOutcome.REJECTED,
            ),
        ),
    ).extract_pages(session, revision.id)

    assert _vision_candidates(session) == []
    invocation = session.scalars(select(ModelInvocation)).one()
    assert invocation.outcome == "rejected"
    assert invocation.candidate_id is None
