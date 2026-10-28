"""Cross-route second-reader wiring for issue #622.

Verification for: `DatabaseStages._apply_cross_route_corroboration`, the agreement gate every page's
readings go through.

**Since 2026-10-28** the second route here is a reading recorded beside the drawing's own, as the
gate receives one, not a scripted vision reader inside `extract_pages`: the vision readers were
retired with the label-first route. What the gate decides is unchanged, and so is every outcome
below.
"""

from __future__ import annotations

import hashlib
import io
import tempfile
from collections.abc import Iterator
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alembic import command
from app.api.documents import storage_key
from app.db.session import session_factory
from app.evidence.record import open_extraction_run
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
from storage.local import LocalStore
from tests.app.postgres_fixture import alembic_config
from tests.extraction.test_reader import MISSING_SPACE, _pdf
from workflow.idempotency import stage_idempotency_key
from workflow.review import ENGINE_VERSION
from workflow.stages import DatabaseStages, _AgreementGuard, _MixedFractionGuard

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


def _revision(session: Session, store: LocalStore, data: bytes | None = None) -> PackageRevision:
    data = _drawing() if data is None else data
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


def _candidate_runs(session: Session) -> list[tuple[ObservationCandidate, ExtractionRun]]:
    rows = session.execute(
        select(ObservationCandidate, ExtractionRun)
        .join(ExtractionRun, ObservationCandidate.extraction_run_id == ExtractionRun.id)
        .order_by(ExtractionRun.extractor, ObservationCandidate.raw_text)
    )
    return [(candidate, run) for candidate, run in rows]


class _HoldsNothingBack(_AgreementGuard):
    """A guard that lets every agreement through, so only `corroborate` decides here."""

    def _reason(self, region: ObservationCandidate) -> str | None:
        del region
        return None


def _two_routes(
    session: Session,
    store: LocalStore,
    *,
    extractor: str,
    version: str,
    label: str = '24"',
    reading: str = '24"',
    inches: Fraction | int = 24,
    confidence: Decimal | None = None,
    mixed_fraction: _MixedFractionGuard | None = None,
) -> list[tuple[ObservationCandidate, ExtractionRun]]:
    """The drawing's own `label` and a second route's `reading` of the same region, through the gate.

    Both readings are new, as the gate receives a page's readings: the drawing's under the vector
    reader's run, the other under `extractor`'s. Returns them with their runs.
    """
    revision = _revision(
        session, store, data=_drawing(f"BT /F1 10 Tf 1 0 0 1 20 70 Tm ({label}) Tj ET\n".encode())
    )
    DatabaseStages(store, missing_space=MISSING_SPACE).extract_pages(session, revision.id)
    read = session.execute(select(ObservationCandidate)).scalars().one()
    first = session.get(ExtractionRun, read.extraction_run_id)
    assert first is not None
    second = open_extraction_run(
        session,
        task_run_id=first.task_run_id,
        extractor=extractor,
        extractor_version=version,
        config_hash="test",
        dpi=first.dpi,
    )
    rows = [
        (
            ObservationCandidate(
                document_version_id=read.document_version_id,
                page_id=read.page_id,
                extraction_run_id=run.id,
                raw_text=text,
                value_numerator=Fraction(value).numerator,
                value_denominator=Fraction(value).denominator,
                unit="in",
                unit_guess="in",
                semantic_guess=None,
                polygon=[list(point) for point in read.polygon],
                coordinate_space="image",
                confidence=certainty,
                ambiguity_flags=[],
            ),
            run,
        )
        for run, text, value, certainty in (
            (
                first,
                read.raw_text,
                Fraction(read.value_numerator, read.value_denominator),
                read.confidence,
            ),
            (second, reading, inches, confidence),
        )
    ]
    session.add_all(row for row, _ in rows)
    DatabaseStages._apply_cross_route_corroboration(
        session,
        page_index=0,
        candidates=[row for row, _ in rows],
        gv_mark=_HoldsNothingBack(),  # type: ignore[arg-type]
        cut_label=_HoldsNothingBack(),  # type: ignore[arg-type]
        mixed_fraction=_MixedFractionGuard() if mixed_fraction is None else mixed_fraction,
    )
    return rows


def test_two_routes_agreeing_on_one_region_get_the_second_reader_lane(
    session: Session, store: LocalStore
) -> None:
    rows = _two_routes(session, store, extractor="bedrock-nova-pro", version="amazon.nova-pro-v1:0")

    assert {run.extractor for _, run in rows} == {"pdfplumber", "bedrock-nova-pro"}
    assert {row.raw_text for row, _ in rows} == {'24"'}
    assert all(row.corroboration_status == "RAW_CANDIDATE" for row, _ in rows)
    assert all(row.corroboration_lane == "SECOND_READER" for row, _ in rows)


def test_two_routes_disagreeing_on_one_region_become_conflicting_without_a_winner(
    session: Session, store: LocalStore
) -> None:
    rows = _two_routes(
        session,
        store,
        extractor="bedrock-nova-pro",
        version="amazon.nova-pro-v1:0",
        reading='25"',
        inches=25,
        confidence=Decimal("0.99"),
    )

    assert {row.raw_text for row, _ in rows} == {'24"', '25"'}
    assert all(row.corroboration_status == "CONFLICTING" for row, _ in rows)
    assert all(row.corroboration_lane == "SECOND_READER" for row, _ in rows)


def test_single_route_region_keeps_no_corroboration_lane(
    session: Session, store: LocalStore
) -> None:
    revision = _revision(session, store)

    DatabaseStages(store, missing_space=MISSING_SPACE).extract_pages(session, revision.id)

    rows = _candidate_runs(session)
    assert {run.extractor for _, run in rows} == {"pdfplumber"}
    assert all(row.corroboration_status is None for row, _ in rows)
    assert all(row.corroboration_lane is None for row, _ in rows)


def test_two_versions_of_the_same_extractor_do_not_count_as_independent(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("workflow.stages.EXTRACTOR", "same-reader")

    rows = _two_routes(session, store, extractor="same-reader", version="same-reader/v2")

    assert {run.extractor for _, run in rows} == {"same-reader"}
    assert all(row.raw_text == '24"' for row, _ in rows)
    assert all(row.corroboration_status is None for row, _ in rows)
    assert all(row.corroboration_lane is None for row, _ in rows)


# ---------------------------------------------------------------------------
# A reading with no value abstains (#924)
# ---------------------------------------------------------------------------


AGREED = ((10, 10), (20, 10), (20, 20), (10, 20))
CONFLICTED = ((30, 10), (40, 10), (40, 20), (30, 20))

#: The #907 pair and the reading agent's forced-tool reader: Qwen, Amazon, and Amazon again.
QWEN = ("bedrock-qwen3-vl-235b", "qwen.qwen3-vl-235b-a22b")
NOVA_TAUGHT = ("bedrock-nova-2-lite-taught", "us.amazon.nova-2-lite-v1:0")
NOVA_FORCED = ("bedrock-nova-2-lite", "amazon.nova-2-lite-v1:0")


def _dual_region(session: Session, store: LocalStore) -> list[ObservationCandidate]:
    """Unsaved readings of two regions: the pair agreeing on `914 [36]` beside a forced-tool `914`
    with no value, and the pair disagreeing — `914 [36]` against `895 [35]` — beside the same."""
    revision = _revision(session, store)
    DatabaseStages(store, missing_space=MISSING_SPACE).extract_pages(session, revision.id)
    template = session.execute(select(ObservationCandidate)).scalars().one()
    first = session.get(ExtractionRun, template.extraction_run_id)
    assert first is not None
    runs = {
        extractor: open_extraction_run(
            session,
            task_run_id=first.task_run_id,
            extractor=extractor,
            extractor_version=model_id,
            config_hash="test",
            dpi=first.dpi,
        )
        for extractor, model_id in (QWEN, NOVA_TAUGHT, NOVA_FORCED)
    }

    def row(
        reader: tuple[str, str], region: tuple[tuple[int, int], ...], text: str, inches: int | None
    ) -> ObservationCandidate:
        return ObservationCandidate(
            document_version_id=template.document_version_id,
            page_id=template.page_id,
            extraction_run_id=runs[reader[0]].id,
            raw_text=text,
            value_numerator=inches,
            value_denominator=None if inches is None else 1,
            unit=None if inches is None else "in",
            unit_guess=None if inches is None else "in",
            semantic_guess=None,
            polygon=[list(point) for point in region],
            coordinate_space="image",
            confidence=None,
            ambiguity_flags=[] if inches is not None else ["unparsed"],
        )

    rows = [
        row(QWEN, AGREED, "914 [36]", 36),
        row(NOVA_TAUGHT, AGREED, "914 [36]", 36),
        row(NOVA_FORCED, AGREED, "914", None),
        row(QWEN, CONFLICTED, "914 [36]", 36),
        row(NOVA_TAUGHT, CONFLICTED, "895 [35]", 35),
        row(NOVA_FORCED, CONFLICTED, "914", None),
    ]
    session.add_all(rows)
    return rows


def test_an_agreement_is_marked_on_the_readings_that_agreed_and_a_conflict_on_the_region(
    session: Session, store: LocalStore
) -> None:
    """**#924 in the stage.** Two vendors agreeing on both halves of `914 [36]` are given the
    second-reader lane; the forced-tool reader's `914`, with no value, abstained and keeps none, so
    it is grouped again with the reading agent's looks. Where the two disagree, the conflict is the
    region's: every reading in it is marked, the empty one too, as the agent's contradiction check
    marks a region."""
    rows = _dual_region(session, store)

    DatabaseStages._apply_cross_route_corroboration(
        session,
        page_index=0,
        candidates=rows,
        gv_mark=_HoldsNothingBack(),  # type: ignore[arg-type]
        cut_label=_HoldsNothingBack(),  # type: ignore[arg-type]
        mixed_fraction=_MixedFractionGuard(),
    )

    marks = [(row.raw_text, row.corroboration_status, row.corroboration_lane) for row in rows]
    assert marks == [
        ("914 [36]", "RAW_CANDIDATE", "SECOND_READER"),
        ("914 [36]", "RAW_CANDIDATE", "SECOND_READER"),
        ("914", None, None),
        ("914 [36]", "CONFLICTING", "SECOND_READER"),
        ("895 [35]", "CONFLICTING", "SECOND_READER"),
        ("914", "CONFLICTING", "SECOND_READER"),
    ]
