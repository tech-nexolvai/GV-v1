"""Writing down what the reader saw: pages, an extraction run, and observation candidates.

Verification for: `app/evidence/record.py` and `DatabaseStages.extract_pages` in `workflow/stages.py`.

These are the first `observation_candidates` rows the system has ever written. Until now the reader
could read a drawing and every reading evaporated when the function returned.

The test that matters most is `test_a_bare_number_is_recorded_without_a_value`. It guards a bug this
code actually had: `984 mm` was recorded as 984 **inches** — 82 feet — because word splitting
separates a number from its unit marker.
"""

from __future__ import annotations

import hashlib
import io
import tempfile
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alembic import command
from app.api.documents import storage_key
from app.db.session import session_factory
from app.evidence.record import UNKNOWN_UNIT_FLAG, UNPARSED_FLAG
from app.models import (
    Document,
    DocumentVersion,
    ObservationCandidate,
    Package,
    PackageRevision,
    PackageRevisionDocument,
    PackageState,
    Page,
    Project,
    SourceArtifact,
)
from app.models.runs import ExtractionRun, TaskRun, WorkflowRun
from storage.local import LocalStore
from tests.app.postgres_fixture import alembic_config
from tests.extraction.test_reader import _pdf
from workflow.idempotency import stage_idempotency_key
from workflow.review import ENGINE_VERSION
from workflow.stages import DatabaseStages

pytest_plugins = ("tests.app.postgres_fixture",)

#: One page carrying: a fraction with an inch mark, a millimetre dimension, plain text that is not a
#: dimension at all, and a line. Everything a reader meets on a real sheet, in miniature.
DRAWING = _pdf(
    b'BT /F1 10 Tf 1 0 0 1 20 70 Tm (38 3/4") Tj ET\n'
    b"BT /F1 10 Tf 1 0 0 1 20 50 Tm (984 mm) Tj ET\n"
    b"BT /F1 10 Tf 1 0 0 1 20 20 Tm (TITLE BLOCK) Tj ET\n"
    b"1 w 20 40 m 120 40 l S\n"
)
SHA = hashlib.sha256(DRAWING).hexdigest()


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


def _revision(session: Session, store: LocalStore, *, data: bytes = DRAWING) -> PackageRevision:
    """A package revision with one document attached, and its bytes in the store.

    Built the long way on purpose — project, package, revision, document, artifact, version, the
    revision-to-version join, then the workflow and task runs. Every one of those is a `NOT NULL`
    foreign key somewhere in the chain this stage writes, so a shorter fixture would be testing a
    shape the database does not permit.
    """
    digest = hashlib.sha256(data).hexdigest()
    project = Project(name="extraction test")
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


def _candidates(session: Session) -> dict[str, ObservationCandidate]:
    return {row.raw_text: row for row in session.execute(select(ObservationCandidate)).scalars()}


# ---------------------------------------------------------------------------
# The bug this code actually had
# ---------------------------------------------------------------------------


def test_a_bare_number_is_recorded_without_a_value(session: Session, store: LocalStore) -> None:
    """**`984 mm` was recorded as 984 inches. That is 82 feet.**

    `extract_words` splits at the space, so a dimension and its unit marker arrive as two separate
    tokens — and the bare one is indistinguishable from a number that never had a unit. An earlier
    version let the caller declare the sheet's unit, which turned every such fragment into an inch
    value: a real number, correctly parsed, and wrong by a factor of 25.4.

    A caller can know what a sheet is drawn in. It cannot know whether *this* token's unit was
    tokenised away. So a bare number keeps its reading and claims no value.
    """
    revision = _revision(session, store)
    DatabaseStages(store).extract_pages(session, revision.id)

    bare = _candidates(session)["984"]

    assert bare.value_numerator is None
    assert bare.value_denominator is None
    assert bare.unit is None
    assert UNKNOWN_UNIT_FLAG in bare.ambiguity_flags
    assert bare.raw_text == "984", "the reading itself must survive; only the value is withheld"


def test_a_token_carrying_its_own_unit_is_parsed(session: Session, store: LocalStore) -> None:
    """The control. Without it, a recorder that valued nothing would pass the test above."""
    revision = _revision(session, store)
    DatabaseStages(store).extract_pages(session, revision.id)

    inches = _candidates(session)['3/4"']

    assert inches.value_numerator == 3
    assert inches.value_denominator == 4
    assert inches.unit == "in"
    assert inches.ambiguity_flags == []


# ---------------------------------------------------------------------------
# Nothing is dropped
# ---------------------------------------------------------------------------


def test_text_that_is_not_a_dimension_is_still_recorded(
    session: Session, store: LocalStore
) -> None:
    """**A dropped token leaves no trace, and that is the problem with dropping it.**

    Which text on a drawing is a dimension is decided by association, which is not built. A recorder
    that filtered would be making that decision by guess and making it invisibly — the title block
    would be gone, and so would any dimension the filter got wrong.
    """
    revision = _revision(session, store)
    DatabaseStages(store).extract_pages(session, revision.id)

    found = _candidates(session)

    assert "TITLE" in found
    assert UNPARSED_FLAG in found["TITLE"].ambiguity_flags
    assert found["TITLE"].value_numerator is None


def test_the_two_failures_are_told_apart(session: Session, store: LocalStore) -> None:
    """A number with no unit and a word that is not a number mean different things to whoever reads
    the row: one is a dimension worth chasing, the other is a title block."""
    revision = _revision(session, store)
    DatabaseStages(store).extract_pages(session, revision.id)

    found = _candidates(session)

    assert found["984"].ambiguity_flags == [UNKNOWN_UNIT_FLAG]
    assert found["TITLE"].ambiguity_flags == [UNPARSED_FLAG]


# ---------------------------------------------------------------------------
# The rows the database demands
# ---------------------------------------------------------------------------


def test_the_polygon_is_integer_image_pixels(session: Session, store: LocalStore) -> None:
    """`coordinate_space` is checked against the literal `'image'`, and the points must be integers.

    The reader carries these alongside its stored-space polygon precisely because they cannot be
    recovered afterwards: `dpi`, `media_box` and `crop_box` are not persisted anywhere, so a
    candidate written in stored space would have geometry nothing could place.
    """
    revision = _revision(session, store)
    DatabaseStages(store).extract_pages(session, revision.id)

    for candidate in _candidates(session).values():
        assert candidate.coordinate_space == "image"
        assert len(candidate.polygon) == 4
        for x, y in candidate.polygon:
            assert isinstance(x, int) and not isinstance(x, bool)
            assert isinstance(y, int) and not isinstance(y, bool)


def test_a_page_manifest_and_an_extraction_run_are_written(
    session: Session, store: LocalStore
) -> None:
    """Both are `NOT NULL` foreign keys on a candidate, and nothing had ever created either.

    Without the page there is nothing for a candidate to point at; without the run there is no way to
    say what read the number, and a re-read by a newer extractor would be indistinguishable from the
    first.
    """
    revision = _revision(session, store)
    DatabaseStages(store).extract_pages(session, revision.id)

    pages = list(session.execute(select(Page)).scalars())
    runs = list(session.execute(select(ExtractionRun)).scalars())

    assert len(pages) == 1
    assert pages[0].index == 0
    assert pages[0].has_vector_text is True
    assert pages[0].width_pt == Decimal(200)
    assert len(runs) == 1
    assert runs[0].extractor == "pdfplumber"


def test_running_twice_reuses_the_pages_rather_than_failing(
    session: Session, store: LocalStore
) -> None:
    """**A workflow redelivery is ordinary, and `pages` is append-only and unique per index.**

    A second attempt cannot rewrite a page and must not raise on the constraint either. The
    candidates *are* written again, because the table is append-only and a re-read is a new reading —
    which is the behaviour, not a leak.
    """
    revision = _revision(session, store)
    stages = DatabaseStages(store)

    stages.extract_pages(session, revision.id)
    first = len(list(session.execute(select(ObservationCandidate)).scalars()))
    stages.extract_pages(session, revision.id)

    assert len(list(session.execute(select(Page)).scalars())) == 1, "the page was written twice"
    assert len(list(session.execute(select(ExtractionRun)).scalars())) == 1
    assert len(list(session.execute(select(ObservationCandidate)).scalars())) == first * 2


def test_the_page_result_reports_what_was_written(session: Session, store: LocalStore) -> None:
    """`run_stage` reduces these to a count, but a stage that returned nothing about its work would
    make a page that read nothing indistinguishable from a page nobody read."""
    revision = _revision(session, store)

    results = DatabaseStages(store).extract_pages(session, revision.id)

    assert len(results) == 1
    assert results[0].index == 0
    assert results[0].payload["has_vector_text"] is True
    assert int(results[0].payload["candidates"]) > 0  # type: ignore[call-overload]


# ---------------------------------------------------------------------------
# What it declines to do
# ---------------------------------------------------------------------------


def test_no_candidate_claims_a_semantic_type(session: Session, store: LocalStore) -> None:
    """**Nothing in the system assigns one, and normalisation refuses to infer one from position.**

    `docs/DESIGN.md` names text-to-item association as one of four things it will not specify until
    real drawings exist, and `CLIENT_FACTS` Q20 records the vocabulary as provisional. A recorder
    that guessed here would produce a correctly-read number attached to the wrong item — which is
    internally consistent and completely wrong.
    """
    revision = _revision(session, store)
    DatabaseStages(store).extract_pages(session, revision.id)

    assert all(row.semantic_guess is None for row in _candidates(session).values())


def test_without_an_artifact_store_it_does_nothing_rather_than_failing(
    session: Session,
) -> None:
    """No settings-driven store factory exists for a worker yet. Reporting nothing read is a fact a
    caller can act on; crashing on a missing dependency would look like a broken document."""
    assert DatabaseStages(None).extract_pages(session, uuid4()) == ()


def test_a_revision_with_no_documents_reads_nothing(session: Session, store: LocalStore) -> None:
    """An empty result, and no page or run written for a revision that has nothing attached."""
    assert DatabaseStages(store).extract_pages(session, uuid4()) == ()
    assert list(session.execute(select(Page)).scalars()) == []


def test_an_unreadable_document_does_not_produce_empty_pages(
    session: Session, store: LocalStore
) -> None:
    """**A document that will not parse is not a document with no dimensions.**

    It is skipped, leaving a version with no pages rather than pages that all read empty — the second
    would travel downstream as a drawing that showed nothing.
    """
    revision = _revision(session, store, data=b"%PDF-1.4\nthis will not parse\n")

    results = DatabaseStages(store).extract_pages(session, revision.id)

    assert results == ()
    assert list(session.execute(select(Page)).scalars()) == []
