"""Readings on different drawings are never summed into one check (#826).

Verification for: `workflow/evidence_operands.py` (`evidence_operands`, `_spanning`), the engine's
found-but-ambiguous step (`verdict/engine.py`), and the run that joins them (`DatabaseStages.run_checks`).

The one that matters most is `test_a_cabinet_from_another_drawing_is_never_summed_in`: the wrong
FAIL #826 describes, end to end. One vendor drawing's countertop adds up exactly — 2 + 18 + 20 + 3,
plus a 1" field cut at each end, is 45 — and labelled alone it passes. Label one more cabinet, from a
different drawing, and the old sum was 60: a FAIL that read as the vendor's error.
"""

from __future__ import annotations

import hashlib
import io
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.api.documents import storage_key
from app.db.session import session_factory
from app.evidence.confirm import ConfirmationRefused, confirm_candidate_type
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
from app.models.parameters import to_rows
from app.models.runs import TaskRun, WorkflowRun
from rules.parameters import ParameterLayer, ParameterSet, ParameterValue, Provenance
from rules.schema import Quantity
from storage.local import LocalStore
from tests.evidence.test_bridge import _upgrade
from tests.extraction.test_reader import MISSING_SPACE, _pdf
from tests.workflow.test_identifier_pairing import _finding, _rules
from tests.workflow.test_stages import _publish_rulebook
from units.measurement import Unit
from verdict.outcomes import Outcome
from workflow.evidence_operands import DRAWINGS_SPANNED, evidence_operands
from workflow.idempotency import stage_idempotency_key
from workflow.review import ENGINE_VERSION
from workflow.stages import DatabaseStages

pytest_plugins = ("tests.app.postgres_fixture",)

RULE = "CT-WIDTH-001"
WIDTH_INPUTS = {"countertop_width", "cabinet_widths", "filler_widths"}


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


def _drawing(*labels: str) -> bytes:
    """One vendor drawing stating `labels` left to right, far enough apart to read separately."""
    return _pdf(
        b"".join(
            f"BT /F1 10 Tf 1 0 0 1 {20 + 70 * i} 70 Tm ({label}) Tj ET\n".encode()
            for i, label in enumerate(labels)
        ),
        box=b"[0 0 400 100]",
    )


def _revision(session: Session, store: LocalStore, *drawings: bytes) -> PackageRevision:
    """One package holding each of `drawings` as its own shop PDF: one page, no views, so each page
    is one drawing."""
    project = Project(name=f"one drawing {uuid4()}")
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
    for data in drawings:
        digest = hashlib.sha256(data).hexdigest()
        document = Document(package_id=package.id, kind="shop")
        session.add(document)
        session.flush()
        key = storage_key(document.id, digest)
        artifact = SourceArtifact(storage_key=key, sha256=digest, size=len(data))
        session.add(artifact)
        session.flush()
        version = DocumentVersion(
            document_id=document.id, source_artifact_id=artifact.id, sha256=digest, page_count=1
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
        store.put(key, io.BytesIO(data), content_type="application/pdf")
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
    # The field cut a reviewer set for this job: 1" per end, Raj's typical (Q1).
    field_cut = ParameterSet(
        project_id=str(project.id),
        layer=ParameterLayer.PROJECT,
        version=1,
        parameters={
            "field_cut": ParameterValue(
                value=Quantity(value=Fraction(1), unit=Unit.INCH),
                provenance=Provenance.COMPANY_STANDARD,
                set_by="a reviewer",
                set_at=datetime(2026, 10, 3, tzinfo=UTC),
            )
        },
    )
    stored, rows = to_rows(field_cut)
    session.add(stored)
    session.add_all(rows)
    session.flush()
    return revision


def _label(session: Session, revision: PackageRevision, text: str, semantic_type: str) -> None:
    """A reviewer names the reading `text` — the only one with that text in the package."""
    (candidate,) = session.execute(
        select(ObservationCandidate)
        .join(
            PackageRevisionDocument,
            PackageRevisionDocument.document_version_id == ObservationCandidate.document_version_id,
        )
        .where(
            PackageRevisionDocument.package_revision_id == revision.id,
            ObservationCandidate.raw_text == text,
            ObservationCandidate.value_numerator.is_not(None),
        )
    ).scalars()
    confirmed = confirm_candidate_type(
        session, candidate_id=candidate.id, semantic_type=semantic_type, confirmed_by="a reviewer"
    )
    assert not isinstance(confirmed, ConfirmationRefused), confirmed


def _labelled(
    session: Session, store: LocalStore, *, with_the_other_drawing: bool
) -> PackageRevision:
    """Drawing one: a 45" countertop over 18" and 20" cabinets, with 2" and 3" fillers. Drawing two:
    a 15" cabinet of some other countertop, labelled only when asked."""
    revision = _revision(
        session,
        store,
        _drawing('45"', '2"', '18"', '20"', '3"'),
        _drawing('15"'),
    )
    DatabaseStages(store, missing_space=MISSING_SPACE).extract_pages(session, revision.id)
    _label(session, revision, '45"', "countertop_overall_width")
    _label(session, revision, '18"', "cabinet_width")
    _label(session, revision, '20"', "cabinet_width")
    _label(session, revision, '2"', "filler_width")
    _label(session, revision, '3"', "filler_width")
    if with_the_other_drawing:
        _label(session, revision, '15"', "cabinet_width")
    return revision


def _checked(session: Session, store: LocalStore, revision: PackageRevision) -> object:
    _publish_rulebook(session)
    DatabaseStages(
        store, discriminators={"wall_config": "back_left_right"}, missing_space=MISSING_SPACE
    ).run_checks(session, revision.id)
    return _finding(session, revision, RULE)


def test_one_drawings_run_is_checked_as_before_and_passes(
    session: Session, store: LocalStore
) -> None:
    """**The control.** All five readings on one drawing: no change from before #826. Outcome: PASS —
    45 = 18 + 20 + 2 + 3 + 1 + 1, exactly."""
    revision = _labelled(session, store, with_the_other_drawing=False)

    evidence = evidence_operands(session, revision.id, _rules())
    finding = _checked(session, store, revision)

    assert WIDTH_INPUTS <= evidence.operands[RULE].keys()
    assert RULE not in evidence.ambiguous
    assert finding.outcome == Outcome.PASS.value, finding.reason


def test_a_cabinet_from_another_drawing_is_never_summed_in(
    session: Session, store: LocalStore
) -> None:
    """**The point.** One more cabinet, from another drawing. Outcome: REVIEW_REQUIRED saying the
    readings are on two drawings — never the FAIL of 45 against 60 that the old sum gave."""
    revision = _labelled(session, store, with_the_other_drawing=True)

    finding = _checked(session, store, revision)

    assert finding.outcome == Outcome.REVIEW_REQUIRED.value, finding.reason
    assert finding.reason is not None
    assert DRAWINGS_SPANNED.format(count=2) in finding.reason


def test_every_one_assembly_input_of_that_side_is_withheld_not_only_the_cabinets(
    session: Session, store: LocalStore
) -> None:
    """A countertop from one drawing judged against cabinets from two is the same mixture, so the
    countertop and fillers go too, each with the reason."""
    revision = _labelled(session, store, with_the_other_drawing=True)

    evidence = evidence_operands(session, revision.id, _rules())

    assert set(evidence.ambiguous[RULE]) == WIDTH_INPUTS
    assert not WIDTH_INPUTS & set(evidence.operands.get(RULE, {}))
    assert set(evidence.ambiguous[RULE].values()) == {DRAWINGS_SPANNED.format(count=2)}
