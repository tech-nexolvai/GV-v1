"""Cross-route second-reader wiring for issue #622."""

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
    Document,
    DocumentVersion,
    ExtractionRun,
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
    NovaConfig,
    NovaInvocation,
    NovaInvocationOutcome,
    NovaRequest,
)
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


def _drawing(text: bytes = b'BT /F1 10 Tf 1 0 0 1 20 70 Tm (24") Tj ET\n') -> bytes:
    return _pdf(text)


def _revision(session: Session, store: LocalStore) -> PackageRevision:
    data = _drawing()
    digest = hashlib.sha256(data).hexdigest()
    project = Project(name=f"cross-route {uuid4()}")
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
    artifact = SourceArtifact(storage_key=key, sha256=digest, size=len(data))
    session.add(artifact)
    session.flush()
    version = DocumentVersion(
        document_id=document.id,
        source_artifact_id=artifact.id,
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
class _Reader:
    config: NovaConfig
    reading: str = '24"'
    confidence: Decimal | None = None

    def extract(self, request: NovaRequest, recorder: object) -> DomainCandidate:
        recorder.record(  # type: ignore[attr-defined]
            NovaInvocation(
                model_id=self.config.model_id,
                prompt_id=self.config.prompt_id,
                template_id=self.config.template_id,
                attempt=1,
                latency_ms=1,
                input_tokens=10,
                output_tokens=2,
                outcome=NovaInvocationOutcome.OK,
                request_id=f"request-{self.config.extractor}",
                context=AssembledContext(nearby_text=(), nearby_geometry=()),
                bound_pt=Decimal(9),
                injection_attempts=(),
            )
        )
        return DomainCandidate(
            candidate_id=request.candidate_id,
            extractor=self.config.extractor,
            extractor_version=self.config.model_id,
            raw_text=self.reading,
            parsed_value=None,
            unit_guess=Unit.INCH,
            semantic_guess=None,
            page=request.page,
            polygon=(ImagePoint(0, 0), ImagePoint(1, 0), ImagePoint(1, 1)),
            confidence=self.confidence,
            ambiguity_flags=(),
        )


def _reader(
    extractor: str,
    model_id: str,
    *,
    reading: str = '24"',
    confidence: Decimal | None = None,
) -> _Reader:
    return _Reader(
        NovaConfig(
            model_id=model_id,
            prompt_id="dimension-reader-v1",
            template_id="bounded-crop-v1",
            connect_timeout_seconds=1,
            read_timeout_seconds=1,
            max_attempts=1,
            extractor=extractor,
        ),
        reading=reading,
        confidence=confidence,
    )


def _candidate_runs(session: Session) -> list[tuple[ObservationCandidate, ExtractionRun]]:
    rows = session.execute(
        select(ObservationCandidate, ExtractionRun)
        .join(ExtractionRun, ObservationCandidate.extraction_run_id == ExtractionRun.id)
        .order_by(ExtractionRun.extractor, ObservationCandidate.raw_text)
    )
    return [(candidate, run) for candidate, run in rows]


def test_two_routes_agreeing_on_one_region_get_the_second_reader_lane(
    session: Session, store: LocalStore
) -> None:
    revision = _revision(session, store)

    DatabaseStages(
        store,
        vision_readers=(_reader("bedrock-nova-pro", "amazon.nova-pro-v1:0"),),
    ).extract_pages(session, revision.id)

    rows = _candidate_runs(session)
    assert {run.extractor for _, run in rows} == {"pdfplumber", "bedrock-nova-pro"}
    assert {row.raw_text for row, _ in rows} == {'24"'}
    assert all(row.corroboration_status == "RAW_CANDIDATE" for row, _ in rows)
    assert all(row.corroboration_lane == "SECOND_READER" for row, _ in rows)


def test_two_routes_disagreeing_on_one_region_become_conflicting_without_a_winner(
    session: Session, store: LocalStore
) -> None:
    revision = _revision(session, store)

    DatabaseStages(
        store,
        vision_readers=(
            _reader(
                "bedrock-nova-pro",
                "amazon.nova-pro-v1:0",
                reading='25"',
                confidence=Decimal("0.99"),
            ),
        ),
    ).extract_pages(session, revision.id)

    rows = _candidate_runs(session)
    assert {row.raw_text for row, _ in rows} == {'24"', '25"'}
    assert all(row.corroboration_status == "CONFLICTING" for row, _ in rows)
    assert all(row.corroboration_lane == "SECOND_READER" for row, _ in rows)


def test_single_route_region_keeps_no_corroboration_lane(
    session: Session, store: LocalStore
) -> None:
    revision = _revision(session, store)

    DatabaseStages(store).extract_pages(session, revision.id)

    rows = _candidate_runs(session)
    assert {run.extractor for _, run in rows} == {"pdfplumber"}
    assert all(row.corroboration_status is None for row, _ in rows)
    assert all(row.corroboration_lane is None for row, _ in rows)


def test_two_versions_of_the_same_extractor_do_not_count_as_independent(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    revision = _revision(session, store)
    monkeypatch.setattr("workflow.stages.EXTRACTOR", "same-reader")

    DatabaseStages(
        store,
        vision_readers=(_reader("same-reader", "same-reader/v2"),),
    ).extract_pages(session, revision.id)

    rows = _candidate_runs(session)
    assert {run.extractor for _, run in rows} == {"same-reader"}
    assert all(row.raw_text == '24"' for row, _ in rows)
    assert all(row.corroboration_status is None for row, _ in rows)
    assert all(row.corroboration_lane is None for row, _ in rows)
