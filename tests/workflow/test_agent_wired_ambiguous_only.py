"""Workflow wiring for the bounded agent on ambiguous regions only."""

from __future__ import annotations

import hashlib
import io
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
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
from extraction.agent.graph import (
    BoundedAgentGraph,
    BoundedRegionContext,
    GraphLimits,
    RetryableToolFailure,
    scripted,
)
from extraction.agent.outcomes import abstain
from extraction.agent.tools import (
    AgentToolbox,
    OcrVerificationArguments,
    ToolCall,
    ToolCallRecord,
)
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
from workflow.stages import DatabaseStages, _shows_a_numeral

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


@dataclass
class _Recorder:
    calls: list[ToolCallRecord] = field(default_factory=list)

    def record(self, call: ToolCallRecord) -> None:
        self.calls.append(call)


@dataclass(frozen=True, slots=True)
class _Reader:
    config: NovaConfig
    reading: str

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
            confidence=None,
            ambiguity_flags=(),
        )


def _revision(session: Session, store: LocalStore, *, data: bytes) -> PackageRevision:
    digest = hashlib.sha256(data).hexdigest()
    project = Project(name=f"agent wiring {uuid4()}")
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


def _limits() -> GraphLimits:
    return GraphLimits(
        max_steps=6,
        max_ocr_retries=2,
        max_primary_vlm_calls=1,
        max_vlm_escalations=0,
        max_nearby_text_items=0,
        max_nearby_geometry_items=0,
    )


def _agent_candidate() -> DomainCandidate:
    return DomainCandidate(
        candidate_id=str(uuid4()),
        extractor="bounded-agent",
        extractor_version="test/1",
        raw_text='984"',
        parsed_value=None,
        unit_guess=Unit.INCH,
        semantic_guess=None,
        page=0,
        polygon=(ImagePoint(10, 10), ImagePoint(20, 10), ImagePoint(20, 20)),
        confidence=Decimal("0.80"),
        ambiguity_flags=(),
    )


def _graph(
    recorder: _Recorder, *, ocr_result: object, vlm_result: object | None = None
) -> BoundedAgentGraph:
    return BoundedAgentGraph(
        limits=_limits(),
        toolbox=AgentToolbox(
            refine_crop=lambda _arguments: RetryableToolFailure("unused"),
            request_ocr_verification=lambda _arguments: ocr_result,
            request_vlm_reading=lambda _arguments: (
                RetryableToolFailure("unused") if vlm_result is None else vlm_result
            ),
            abstain=abstain,
            recorder=recorder,
        ),
    )


def _candidate_runs(session: Session) -> list[tuple[ObservationCandidate, ExtractionRun]]:
    rows = session.execute(
        select(ObservationCandidate, ExtractionRun)
        .join(ExtractionRun, ObservationCandidate.extraction_run_id == ExtractionRun.id)
        .order_by(ExtractionRun.extractor, ObservationCandidate.raw_text)
    )
    return [(candidate, run) for candidate, run in rows]


def test_trigger_permitted_raw_region_runs_agent_and_records_only_a_candidate(
    session: Session, store: LocalStore
) -> None:
    revision = _revision(
        session,
        store,
        data=_pdf(b"BT /F1 10 Tf 1 0 0 1 20 70 Tm (984) Tj ET\n"),
    )
    recorder = _Recorder()

    results = DatabaseStages(
        store,
        bounded_agent=_graph(
            recorder, ocr_result=RetryableToolFailure("unused"), vlm_result=_agent_candidate()
        ),
    ).extract_pages(session, revision.id)

    runs = _candidate_runs(session)
    agent_rows = [row for row, run in runs if run.extractor == "bounded-agent"]
    # **The decision table's first move** (#757): no reading yet, so the primary vision reader —
    # not the fixed script's OCR, OCR, VLM regardless of what each returned.
    assert [call.call_id.rsplit(":", 1)[-1] for call in recorder.calls] == ["vlm-primary"]
    assert len(agent_rows) == 1
    assert agent_rows[0].raw_text == '984"'
    assert agent_rows[0].semantic_guess is None
    assert session.execute(select(CanonicalObservation)).scalars().all() == []
    assert results[0].payload["agent_candidates"] == 1
    assert results[0].payload["agent_abstentions"] == 0


def test_conflicting_fixed_evidence_never_reaches_the_agent(
    session: Session, store: LocalStore
) -> None:
    revision = _revision(
        session,
        store,
        data=_pdf(b'BT /F1 10 Tf 1 0 0 1 20 70 Tm (24") Tj ET\n'),
    )
    recorder = _Recorder()
    reader = _Reader(
        NovaConfig(
            model_id="vision-test/1",
            prompt_id="dimension-reader-v1",
            template_id="bounded-crop-v1",
            connect_timeout_seconds=1,
            read_timeout_seconds=1,
            max_attempts=1,
            extractor="vision-test",
        ),
        reading='25"',
    )

    results = DatabaseStages(
        store,
        vision_readers=(reader,),
        bounded_agent=_graph(recorder, ocr_result=_agent_candidate()),
    ).extract_pages(session, revision.id)

    rows = _candidate_runs(session)
    assert {row.corroboration_status for row, _ in rows} == {"CONFLICTING"}
    assert [run.extractor for _, run in rows] == ["pdfplumber", "vision-test"]
    assert recorder.calls == []
    assert results[0].payload["agent_candidates"] == 0
    assert results[0].payload["agent_abstentions"] == 0


def test_graph_bounds_are_enforced_when_workflow_supplies_too_many_actions(
    session: Session, store: LocalStore
) -> None:
    revision = _revision(
        session,
        store,
        data=_pdf(b"BT /F1 10 Tf 1 0 0 1 20 70 Tm (984) Tj ET\n"),
    )
    recorder = _Recorder()

    def too_many_ocr(context: BoundedRegionContext, _facts: object, _limits: object) -> object:
        return scripted(
            tuple(
                ToolCall(
                    f"ocr-{number}",
                    OcrVerificationArguments(context.region_id, context.crop_artifact_id),
                )
                for number in range(3)
            )
        )

    results = DatabaseStages(
        store,
        bounded_agent=_graph(recorder, ocr_result=RetryableToolFailure("still unreadable")),
        bounded_agent_planner=too_many_ocr,  # type: ignore[arg-type]
    ).extract_pages(session, revision.id)

    assert [call.call_id for call in recorder.calls] == ["ocr-0", "ocr-1"]
    assert [run.extractor for _, run in _candidate_runs(session)] == ["pdfplumber"]
    assert results[0].payload["agent_candidates"] == 0
    assert results[0].payload["agent_abstentions"] == 1


def _vision(reading: str) -> _Reader:
    return _Reader(
        NovaConfig(
            model_id="vision-test/1",
            prompt_id="dimension-reader-v1",
            template_id="bounded-crop-v1",
            connect_timeout_seconds=1,
            read_timeout_seconds=1,
            max_attempts=1,
            extractor="vision-test",
        ),
        reading=reading,
    )


def _run_on_text(
    session: Session, store: LocalStore, text: bytes, *readers: _Reader
) -> tuple[_Recorder, dict[str, object]]:
    """The file's own text `text` at one place, read by `readers`, and the agent on it."""
    revision = _revision(
        session, store, data=_pdf(b"BT /F1 10 Tf 1 0 0 1 20 70 Tm (" + text + b") Tj ET\n")
    )
    recorder = _Recorder()
    results = DatabaseStages(
        store,
        vision_readers=readers,
        bounded_agent=_graph(
            recorder, ocr_result=RetryableToolFailure("unused"), vlm_result=_agent_candidate()
        ),
    ).extract_pages(session, revision.id)
    return recorder, results[0].payload


def test_a_region_no_route_read_a_numeral_in_never_reaches_the_agent(
    session: Session, store: LocalStore
) -> None:
    """**#792 (a), the one that matters most.** The file says `SINK` and a vision reader reads
    `SINK`, which it flags unparsed. On AI_Set_2, the only values the agent's readers found in such
    regions belonged to a neighbouring label: `Vendor` was read as `24"`. That is a value in the wrong
    place, and two readers of different vendors could agree on it. Outcome: no agent call, and the
    page counts the region it set aside."""
    recorder, payload = _run_on_text(session, store, b"SINK", _vision("SINK"))

    assert recorder.calls == []
    assert payload["agent_regions"] == 0
    assert payload["agent_regions_without_a_numeral"] == 1


def test_a_numeral_any_route_read_keeps_the_region_the_agents(
    session: Session, store: LocalStore
) -> None:
    """**Every route's text counts.** The file says `SINK` and the vision reader reads `24` at the
    same place, with no unit. Outcome: the agent runs on the region once, because a reader saw a digit
    there."""
    recorder, payload = _run_on_text(session, store, b"SINK", _vision("24"))

    assert recorder.calls != []
    assert payload["agent_regions"] == 1
    assert payload["agent_regions_without_a_numeral"] == 0


def test_the_files_own_text_that_does_not_parse_is_no_reason_to_ask_a_model(
    session: Session, store: LocalStore
) -> None:
    """**#792 (b).** The file's own `B24` (a cabinet tag: a numeral, not a dimension) is unparsed.
    A model shown its picture can only read what the file already says. Outcome: no agent call."""
    recorder, payload = _run_on_text(session, store, b"B24")

    assert recorder.calls == []
    assert payload["agent_regions"] == 0
    assert payload["agent_regions_without_a_numeral"] == 0


def test_a_models_reading_that_does_not_parse_still_is(session: Session, store: LocalStore) -> None:
    """**The rule is the route's, not the string's.** The same `B24`, read by a vision reader at the
    same place, is a reading of pixels, and one look may read it differently. Outcome: the agent
    runs."""
    recorder, payload = _run_on_text(session, store, b"B24", _vision("B24"))

    assert recorder.calls != []
    assert payload["agent_regions"] == 1


@pytest.mark.parametrize(
    ("text", "numeral"),
    [("SINK", False), ('"', False), ("", False), (None, False), ("B24", True), ("½", True)],
)
def test_a_numeral_is_any_digit_a_vulgar_fraction_included(text: str | None, numeral: bool) -> None:
    """`½` is a numeral that `str.isdigit` does not count, and a label can be nothing else."""
    assert _shows_a_numeral(text) is numeral
