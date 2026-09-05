"""A real PDF through the real stages: pages, candidates, crops — and no meaning attached.

Verification for: `DatabaseStages.ingest`, `.validate_evidence` and `.match` in `workflow/stages.py`
(#517).

The components these stages call have been finished and tested for months with no production caller
at all — `evidence/crop.py` had never cut a crop outside its own unit tests, and no `match_candidates`
row had ever been written by anything but a test. The gap was connection, so what this file tests is
the connection: a document goes in one end and pages, readings and pictures of those readings come
out the other, through the actual pipeline rather than a stub.

**The most important test here is `test_nothing_in_the_pipeline_gives_a_candidate_a_meaning`.** The
pipeline deliberately stops at untyped candidates. A crop is a picture of a region, not a claim about
what the region means, and the value-to-meaning association needs the real GV drawings (#274) and the
vocabulary Q20 defers. That test is what keeps a later change from quietly crossing the line.
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
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from alembic import command
from app.api.documents import storage_key
from app.db.session import session_factory
from app.models import (
    CanonicalObservation,
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
from app.models.drawing import DrawingItem, DrawingView, ItemIdentifier
from app.models.evidence import EvidenceArtifact
from app.models.matching import MatchCandidate as MatchCandidateRow
from app.models.runs import ExtractionRun, TaskRun, WorkflowRun
from extraction.reader import read_pages
from storage.local import LocalStore
from tests.app.postgres_fixture import alembic_config
from tests.extraction.test_reader import _pdf
from workflow.idempotency import stage_idempotency_key
from workflow.review import ENGINE_VERSION
from workflow.stages import DatabaseStages

pytest_plugins = ("tests.app.postgres_fixture",)

#: A real PDF, committed to this repository, with seventeen pages of vector text.
#:
#: Not a drawing — it is our own design document — and that is the point rather than a compromise.
#: The client's drawings are proprietary and never enter the repository (`tests/test_repo_hygiene.py`
#: enforces it), and #274 has not landed, so no real drawing is available to any test. What this
#: proves is what the stages actually claim: the mechanism is drawing-agnostic. It reads pages, finds
#: text, and cuts a picture of each reading, and none of that asks what the document is about.
REAL_PDF = Path(__file__).resolve().parents[2] / "docs" / "GV_V1_Agentic_systemDesign.pdf"

#: The PNG magic number. A crop that is not a PNG is not a crop a reviewer can open.
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

#: A unit square, as the region of a view and the extent of an item. Both columns are JSONB and this
#: test does not exercise geometry — the matcher works on identifiers and scope, not on shape.
BOX = {"points": [[0, 0], [1, 0], [1, 1], [0, 1]], "space": "stored"}


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


@pytest.fixture(scope="module")
def pdf_bytes() -> bytes:
    """The real PDF, read once for the whole module rather than per test."""
    assert REAL_PDF.exists(), f"the sample PDF this suite runs on is missing: {REAL_PDF}"
    return REAL_PDF.read_bytes()


def _revision(
    session: Session,
    store: LocalStore,
    *,
    data: bytes,
    stored: bytes | None = None,
    kind: str = "shop",
) -> PackageRevision:
    """A revision with one document attached, its bytes in the store, and a claimed task run.

    `stored` exists for one test: it puts *different* bytes in the store than the ones the database
    recorded a digest for, which is the corruption `ingest` is there to notice. Everywhere else it is
    the same bytes and the parameter is not passed.
    """
    digest = hashlib.sha256(data).hexdigest()
    project = Project(name="pipeline test")
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

    document = Document(package_id=package.id, kind=kind)
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
        # The real count, read from the file rather than asserted, so `ingest` is checking the
        # document against a fact and not against a number this fixture invented.
        page_count=len(read_pages(data)),
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

    store.put(key, io.BytesIO(data if stored is None else stored), content_type="application/pdf")
    return revision


def _count(session: Session, model: type, **where: object) -> int:
    statement = select(func.count()).select_from(model)
    for column, value in where.items():
        statement = statement.where(getattr(model, column) == value)
    return int(session.execute(statement).scalar_one())


# ---------------------------------------------------------------------------
# The whole spine, on a real file
# ---------------------------------------------------------------------------


def test_a_real_pdf_runs_the_whole_mechanical_pipeline(
    session: Session, store: LocalStore, pdf_bytes: bytes
) -> None:
    """The acceptance criterion: a real PDF in, pages and readings and pictures out.

    All four stages are run in the order the workflow runs them, against one revision, with nothing
    stubbed. The assertions are deliberately about *existence and consistency* rather than exact
    counts: this file is committed to the repository and could be replaced, and a test asserting
    "exactly 1,133 candidates" would then fail for a reason that has nothing to do with the pipeline.
    """
    revision = _revision(session, store, data=pdf_bytes)
    stages = DatabaseStages(store)

    ingested = stages.ingest(session, revision.id)
    assert ingested["ran"] is True
    assert ingested["verified"] == 1
    assert ingested["digest_mismatched"] == []
    assert ingested["unreadable"] == []
    assert ingested["page_count_changed"] == []

    pages = stages.extract_pages(session, revision.id)
    assert len(pages) == len(read_pages(pdf_bytes)), "every page of the document was visited"
    assert sum(int(page.payload["candidates"]) for page in pages) > 0, "the reader found text"

    evidence = stages.validate_evidence(session, revision.id)
    assert evidence["ran"] is True
    assert int(evidence["crops"]) > 0, "candidates were read but nothing was cut"

    matched = stages.match(session, revision.id)
    assert matched["ran"] is True

    # And the rows are actually there, which is the half a returned mapping cannot prove.
    assert _count(session, Page) == len(read_pages(pdf_bytes)), "a page row per page"
    assert _count(session, ExtractionRun) >= 1
    candidates = _count(session, ObservationCandidate)
    crops = _count(session, EvidenceArtifact)
    assert candidates > 0
    assert crops > 0
    assert crops <= candidates, "a crop belongs to a candidate; there cannot be more of them"


def test_every_crop_is_a_real_png_of_a_real_region(
    session: Session, store: LocalStore, pdf_bytes: bytes
) -> None:
    """The stored bytes are an image a reviewer could open, not a row claiming one exists.

    An `evidence_artifacts` row whose key points at nothing, or at something that is not an image, is
    worse than no row: the reviewer follows the link and finds the evidence missing at the moment
    they need it most.
    """
    revision = _revision(session, store, data=pdf_bytes)
    stages = DatabaseStages(store)
    stages.extract_pages(session, revision.id)
    stages.validate_evidence(session, revision.id)

    artifacts = list(session.execute(select(EvidenceArtifact).limit(25)).scalars())
    assert artifacts, "no crops to check"
    for artifact in artifacts:
        content = store.get(artifact.storage_key).read()
        assert content.startswith(PNG_SIGNATURE), f"{artifact.storage_key} is not a PNG"
        assert artifact.content_matches(content), "the stored digest does not describe these bytes"
        assert artifact.media_type == "image/png"
        assert artifact.kind == "crop"
        assert artifact.candidate_id is not None
        assert artifact.canonical_observation_id is None


# ---------------------------------------------------------------------------
# The line this pipeline stops at
# ---------------------------------------------------------------------------


def test_nothing_in_the_pipeline_gives_a_candidate_a_meaning(
    session: Session, store: LocalStore, pdf_bytes: bytes
) -> None:
    """**The hard stop.** Readings and pictures, and not one claim about what any of it means.

    Every candidate stays untyped, no canonical observation is minted, and therefore nothing has
    become eligible to be a verdict operand — `evidence/gate.py` takes a canonical observation, and
    there are none. This is the state the system should be in until the real drawings land (#274) and
    the vocabulary is settled (Q20), and it is asserted rather than trusted because the failure mode
    is silent: a `semantic_guess` filled in by a well-meaning heuristic would look like progress on a
    dashboard and be a fabricated fact in a review.
    """
    revision = _revision(session, store, data=pdf_bytes)
    stages = DatabaseStages(store)
    stages.extract_pages(session, revision.id)
    stages.validate_evidence(session, revision.id)
    stages.match(session, revision.id)

    guesses = list(
        session.execute(select(ObservationCandidate.semantic_guess).distinct()).scalars()
    )
    assert guesses == [None], f"a candidate was given a semantic type: {guesses}"
    assert _count(session, CanonicalObservation) == 0, "the pipeline minted a canonical observation"


def test_match_finds_nothing_and_says_why_rather_than_reporting_success(
    session: Session, store: LocalStore, pdf_bytes: bytes
) -> None:
    """Zero matches, with the reason — because nothing detects a drawing item on a page.

    A stage returning `{"candidates": 0}` and nothing else would be indistinguishable from a package
    whose drawings genuinely share no identifiers. The reason is the difference between "we looked
    and there was nothing" and "we cannot look yet".
    """
    revision = _revision(session, store, data=pdf_bytes)
    stages = DatabaseStages(store)
    stages.extract_pages(session, revision.id)

    result = stages.match(session, revision.id)

    assert result["items"] == 0
    assert result["candidates"] == 0
    assert "#274" in str(result["reason"]) and "Q20" in str(result["reason"])


# ---------------------------------------------------------------------------
# `ingest` notices what it is there to notice
# ---------------------------------------------------------------------------


def test_ingest_reports_a_digest_mismatch_rather_than_raising(
    session: Session, store: LocalStore, pdf_bytes: bytes
) -> None:
    """A document whose stored bytes are not the bytes that were uploaded.

    Reported and not raised: a corrupt artifact is not transient, and raising would roll the claim
    back and retry the same broken file for ever (#491). The mismatch names the version so a person
    can act on it.
    """
    revision = _revision(
        session, store, data=pdf_bytes, stored=pdf_bytes.replace(b"%PDF-1.", b"%PDF-9.", 1)
    )

    result = DatabaseStages(store).ingest(session, revision.id)

    assert result["ran"] is True
    assert result["verified"] == 0
    assert len(list(result["digest_mismatched"])) == 1
    # The unreadable list stays empty: a file that fails its digest is not read at all, because
    # counting the pages of a file that is not the one under review describes the wrong document.
    assert result["unreadable"] == []


def test_ingest_reports_an_unreadable_document(session: Session, store: LocalStore) -> None:
    """A file whose digest is right and which still will not parse.

    Both facts are recorded separately, because they call for different actions: a digest mismatch is
    a storage problem and an unparseable file is a submission problem.
    """
    broken = b"%PDF-1.4\nthis will not parse\n"
    project = Project(name="unreadable")
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
    digest = hashlib.sha256(broken).hexdigest()
    key = storage_key(document.id, digest)
    session.add(SourceArtifact(storage_key=key, sha256=digest, size=len(broken)))
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
    session.flush()
    store.put(key, io.BytesIO(broken), content_type="application/pdf")

    result = DatabaseStages(store).ingest(session, revision.id)

    assert result["verified"] == 0
    assert result["digest_mismatched"] == []
    assert len(list(result["unreadable"])) == 1


# ---------------------------------------------------------------------------
# Running it twice
# ---------------------------------------------------------------------------


def test_running_the_stages_twice_does_not_double_the_rows(
    session: Session, store: LocalStore, pdf_bytes: bytes
) -> None:
    """A redelivered message is the same work arriving twice, not a second reading.

    `evidence_artifacts` is append-only and unique on (storage_key, sha256), and a crop's key is
    content-addressed — so a second pass over the same page regenerates byte-identical crops. Without
    the guard this raises an `IntegrityError` and takes the whole transaction with it.
    """
    revision = _revision(session, store, data=pdf_bytes)
    stages = DatabaseStages(store)
    stages.extract_pages(session, revision.id)
    first = stages.validate_evidence(session, revision.id)
    crops_after_one_pass = _count(session, EvidenceArtifact)

    second = stages.validate_evidence(session, revision.id)

    assert int(first["crops"]) > 0
    assert int(second["crops"]) == 0, "the second pass cut crops that already existed"
    assert int(second["already_had_one"]) > 0
    assert _count(session, EvidenceArtifact) == crops_after_one_pass


# ---------------------------------------------------------------------------
# `match` when there is something to match
# ---------------------------------------------------------------------------


def _drawn_item(
    session: Session,
    revision: PackageRevision,
    *,
    kind: str,
    tag: str,
    item_type: str,
    mark: str | None,
    identifier_kind: str = "mark",
) -> DrawingItem:
    """One drawing item on its own document, with an optional printed identifier.

    Seeded rather than extracted, because nothing extracts one: detecting a view and typing an item
    are both semantic and both wait on #274 and Q20. That is precisely why this test exists — it
    proves the wiring downstream of detection is real, so that when detection lands the stage does
    not also need writing.
    """
    data = _pdf(f"BT /F1 10 Tf 1 0 0 1 20 70 Tm ({tag} {item_type} {mark}) Tj ET\n".encode())
    digest = hashlib.sha256(data).hexdigest()
    package_id = session.execute(
        select(PackageRevision.package_id).where(PackageRevision.id == revision.id)
    ).scalar_one()
    document = Document(package_id=package_id, kind=kind)
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
            package_id=package_id,
            document_id=document.id,
            document_version_id=version.id,
        )
    )
    page = Page(
        document_version_id=version.id,
        index=0,
        content_hash=digest,
        width_pt=Decimal(612),
        height_pt=Decimal(792),
        rotation=0,
        has_vector_text=True,
    )
    session.add(page)
    session.flush()
    view = DrawingView(page_id=page.id, tag=tag, region=BOX)
    session.add(view)
    session.flush()
    item = DrawingItem(drawing_view_id=view.id, item_type=item_type, extent=BOX)
    session.add(item)
    session.flush()
    if mark is not None:
        session.add(
            ItemIdentifier(drawing_item_id=item.id, kind=identifier_kind, value_as_printed=mark)
        )
        session.flush()
    return item


def test_match_writes_real_candidates_when_items_exist(session: Session, store: LocalStore) -> None:
    """The wiring downstream of item detection, proven with items supplied.

    Without this, `match` would only ever be observed returning zero, and a stage that returns zero
    because it is broken looks exactly like a stage that returns zero because there is nothing to
    find. One architectural item and one shop item carrying the same mark must produce one persisted
    `match_candidates` row on the exact lane.
    """
    revision = _revision(session, store, data=_pdf(b"BT /F1 10 Tf 1 0 0 1 20 70 Tm (base) Tj ET\n"))
    left = _drawn_item(
        session, revision, kind="architectural", tag="D", item_type="CT001", mark="C-12"
    )
    right = _drawn_item(session, revision, kind="shop", tag="E", item_type="CT001", mark="C-12")

    result = DatabaseStages(store).match(session, revision.id)

    assert int(result["items"]) >= 2
    assert int(result["candidates"]) == 1
    rows = list(session.execute(select(MatchCandidateRow)).scalars())
    assert len(rows) == 1
    assert rows[0].left_item_id == left.id
    assert rows[0].right_item_id == right.id
    assert rows[0].lane == "exact"


def test_match_does_not_pair_items_of_different_types(session: Session, store: LocalStore) -> None:
    """The same mark on a countertop and on a cabinet is not a match.

    `MatchableItem.category` is the item type, and the matcher filters on it. Asserted because the
    failure would be quiet and plausible: marks repeat across item types on a real sheet, and a
    countertop proposed as the same object as a cabinet would be an obvious error to a reviewer and
    an invisible one to a test that only counted candidates.
    """
    revision = _revision(session, store, data=_pdf(b"BT /F1 10 Tf 1 0 0 1 20 70 Tm (base) Tj ET\n"))
    _drawn_item(session, revision, kind="architectural", tag="D", item_type="CT001", mark="C-12")
    _drawn_item(session, revision, kind="shop", tag="E", item_type="CT002", mark="C-12")

    result = DatabaseStages(store).match(session, revision.id)

    assert int(result["candidates"]) == 0
    assert _count(session, MatchCandidateRow) == 0


def test_an_item_whose_only_identifier_is_a_catalogue_number_is_still_reported(
    session: Session, store: LocalStore
) -> None:
    """It cannot be matched, and it must not disappear.

    A catalogue number names a model, not a unit, so it cannot establish that two drawn items are
    the same item. An earlier version of `_matchable_items` filtered those rows out one at a time,
    which silently dropped any item whose *only* identifier was a catalogue number — the item stopped
    existing as far as matching was concerned, with nothing reported.
    """
    revision = _revision(session, store, data=_pdf(b"BT /F1 10 Tf 1 0 0 1 20 70 Tm (base) Tj ET\n"))
    _drawn_item(
        session,
        revision,
        kind="architectural",
        tag="D",
        item_type="CT001",
        mark="SKU-9",
        identifier_kind="catalogue",
    )
    _drawn_item(session, revision, kind="shop", tag="E", item_type="CT001", mark="SKU-9")

    result = DatabaseStages(store).match(session, revision.id)

    assert int(result["items"]) == 2, "the catalogue-only item vanished from the projection"
    assert int(result["candidates"]) == 0, "a catalogue number was used to establish identity"
