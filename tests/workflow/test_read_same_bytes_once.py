"""One file in two upload slots is read once, and its slot still never decides a role (#961).

#963/#964 refuse a second copy of the same bytes at upload and at `start_extraction`. A revision can
still reach the reader holding one file twice without passing either door: a revision built by
`app/lifecycle/supersede.supersede()`, a revision assembled before #964 whose extraction is retried,
or one written straight to the database by a script. These tests cover the reader-side backstop:
`ingest` and `extract_pages` read each distinct confirmed SHA-256 once, every upload row is kept, and
the matching role boundary still refuses the slot when one file is both drawings.

Every drawing here is invented (`tests/workflow/test_view_roles._combined_sheet`,
`tests/extraction/architect/combined_sheet`); no client value appears.
"""

from __future__ import annotations

import hashlib
import io
import tempfile
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from alembic import command
from app.api.documents import storage_key
from app.db.session import session_factory
from app.lifecycle.states import begin
from app.lifecycle.supersede import supersede
from app.models import (
    Document,
    DocumentKind,
    DocumentVersion,
    DrawingView,
    MatchCandidate,
    Package,
    PackageRevision,
    PackageRevisionDocument,
    PackageState,
    Page,
    Project,
    SourceArtifact,
    TaskRun,
    ViewRole,
    ViewRoleProposal,
    WorkflowRun,
)
from app.models.evidence import ObservationCandidate
from app.models.runs import ExtractionRun
from storage.hashing import ArtifactCorrupt
from storage.local import LocalStore
from tests.app.postgres_fixture import alembic_config
from tests.extraction.test_reader import MISSING_SPACE
from workflow.review import ENGINE_VERSION
from workflow.stages import DatabaseStages
from workflow.view_roles import confirm_view_role

pytest_plugins = ("tests.app.postgres_fixture",)

ARCH = DocumentKind.ARCHITECTURAL
SHOP = DocumentKind.SHOP


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


@pytest.fixture
def store() -> Iterator[LocalStore]:
    with tempfile.TemporaryDirectory() as directory:
        yield LocalStore(root=Path(directory), ticket_secret=b"a secret only this test knows")


class CountingOcr:
    """An OCR engine that finds nothing and counts every page it is asked to read.

    One call is one page-reading invocation; a file read twice would be asked twice.
    """

    name = "counting-ocr"
    version = "counting/1"

    def __init__(self) -> None:
        self.calls = 0

    def read(self, rgb: bytes, *, width: int, height: int) -> tuple[()]:
        del rgb, width, height
        self.calls += 1
        return ()


def _sheet() -> bytes:
    """A one-page sheet with two labelled drawings that takes the OCR route."""
    from tests.workflow.test_view_roles import _combined_sheet

    return _combined_sheet()


def _stages(store: LocalStore, ocr: CountingOcr, **extra: object) -> DatabaseStages:
    return DatabaseStages(
        store,
        dpi=150,
        ocr_engine=ocr,  # type: ignore[arg-type]
        missing_space=MISSING_SPACE,
        **extra,  # type: ignore[arg-type]
    )


def _new_revision(session: Session) -> PackageRevision:
    project = Project(name=f"read once {uuid4().hex[:8]}")
    session.add(project)
    session.flush()
    package = Package(project_id=project.id, vendor="Apex")
    session.add(package)
    session.flush()
    revision = PackageRevision(package_id=package.id, revision_number=1, state=PackageState.CREATED)
    session.add(revision)
    session.flush()
    begin(session, revision.id, actor="test")
    return revision


def _upload(
    session: Session, store: LocalStore, package_id: UUID, *, kind: DocumentKind, data: bytes
) -> tuple[Document, DocumentVersion]:
    """One stored copy under its own document, written the way `confirm_upload` writes it."""
    digest = hashlib.sha256(data).hexdigest()
    document = Document(package_id=package_id, kind=kind.value)
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
    store.put(key, io.BytesIO(data), content_type="application/pdf")
    return document, version


def _join(session: Session, revision: PackageRevision, version: DocumentVersion) -> None:
    session.add(
        PackageRevisionDocument(
            package_revision_id=revision.id,
            package_id=revision.package_id,
            document_id=version.document_id,
            document_version_id=version.id,
        )
    )
    session.flush()


def _claim_extraction(session: Session, revision_id: UUID) -> None:
    """The task run `run_stage` claims before `extract_pages` runs."""
    from workflow.idempotency import stage_idempotency_key

    workflow_run = WorkflowRun(package_revision_id=revision_id, engine_run_id=str(uuid4()))
    session.add(workflow_run)
    session.flush()
    session.add(
        TaskRun(
            workflow_run_id=workflow_run.id,
            idempotency_key=stage_idempotency_key(
                package_revision_id=revision_id,
                stage="extract_pages",
                engine_version=ENGINE_VERSION,
            ),
            task_type="extract_pages",
            attempt=1,
            outcome="claimed",
        )
    )
    session.flush()


def _package(
    session: Session, store: LocalStore, files: list[tuple[DocumentKind, bytes]]
) -> tuple[PackageRevision, list[DocumentVersion]]:
    revision = _new_revision(session)
    versions: list[DocumentVersion] = []
    for kind, data in files:
        _, version = _upload(session, store, revision.package_id, kind=kind, data=data)
        _join(session, revision, version)
        versions.append(version)
    _claim_extraction(session, revision.id)
    session.commit()
    return revision, versions


def _pages_by_version(session: Session, versions: list[DocumentVersion]) -> dict[UUID, int]:
    return {
        version.id: session.scalar(
            select(func.count()).select_from(Page).where(Page.document_version_id == version.id)
        )
        or 0
        for version in versions
    }


def _candidates_by_version(session: Session, versions: list[DocumentVersion]) -> dict[UUID, int]:
    return {
        version.id: session.scalar(
            select(func.count())
            .select_from(ObservationCandidate)
            .where(ObservationCandidate.document_version_id == version.id)
        )
        or 0
        for version in versions
    }


def _single_copy_baseline(session: Session, store: LocalStore, data: bytes) -> tuple[int, int]:
    """(OCR calls, pages) when the file is uploaded once: what one read costs."""
    ocr = CountingOcr()
    revision, _ = _package(session, store, [(SHOP, data)])
    results = _stages(store, ocr).extract_pages(session, revision.id)
    session.commit()
    return ocr.calls, len(results)


# --- the same bytes in both slots are read once ---------------------------------------------------


def test_the_same_file_in_both_slots_is_read_once(session: Session, store: LocalStore) -> None:
    """Input: one file as the architect's AND the shop drawing. Outcome: pages, candidates and OCR
    calls for one copy only, exactly what uploading it once costs. Why: reading it twice doubled the
    work and wrote a twin of every reading — the material for a drawing compared with itself."""
    data = _sheet()
    baseline_calls, baseline_pages = _single_copy_baseline(session, store, data)
    assert baseline_calls > 0 and baseline_pages == 1

    ocr = CountingOcr()
    revision, (arch_copy, shop_copy) = _package(session, store, [(ARCH, data), (SHOP, data)])
    results = _stages(store, ocr).extract_pages(session, revision.id)
    session.commit()

    assert ocr.calls == baseline_calls, "each page of the file is read once"
    assert len(results) == baseline_pages
    pages = _pages_by_version(session, [arch_copy, shop_copy])
    assert pages == {arch_copy.id: 0, shop_copy.id: baseline_pages}
    candidates = _candidates_by_version(session, [arch_copy, shop_copy])
    assert candidates[arch_copy.id] == 0, "no twin readings on the second copy"
    assert {result.payload["document_version_id"] for result in results} == {str(shop_copy.id)}


@pytest.mark.parametrize("order", [(SHOP, ARCH), (ARCH, SHOP)])
def test_the_copy_read_is_the_shop_slot_whichever_was_uploaded_first(
    session: Session, store: LocalStore, order: tuple[DocumentKind, DocumentKind]
) -> None:
    """The copy read is stable: the shop slot's, as if the combined set had been uploaded once as
    shop (the #963 convention), never whichever row happens to come first."""
    data = _sheet()
    revision, versions = _package(session, store, [(order[0], data), (order[1], data)])
    shop_copy = versions[order.index(SHOP)]

    _stages(store, CountingOcr()).extract_pages(session, revision.id)
    session.commit()

    pages = session.scalars(
        select(Page.document_version_id)
        .join(
            PackageRevisionDocument,
            PackageRevisionDocument.document_version_id == Page.document_version_id,
        )
        .where(PackageRevisionDocument.package_revision_id == revision.id)
    ).all()
    assert set(pages) == {shop_copy.id}


def test_two_shop_copies_of_one_file_are_read_once(session: Session, store: LocalStore) -> None:
    """Identity is the hash, not the slot: two copies in the same slot kind are one file too."""
    data = _sheet()
    ocr = CountingOcr()
    revision, (first, second) = _package(session, store, [(SHOP, data), (SHOP, data)])

    _stages(store, ocr).extract_pages(session, revision.id)
    session.commit()

    pages = _pages_by_version(session, [first, second])
    assert sorted(pages.values()) == [0, 1]


def test_the_architect_reader_stores_one_files_values_once(
    session: Session, store: LocalStore
) -> None:
    """Type 1 (#1052): the architect reader reads every shop-slot copy. Two shop-slot copies of one
    file wrote every architect value twice, one set per copy."""
    from extraction.architect.reader import MEASURED_ARCHITECT_SETTINGS
    from tests.extraction.architect.combined_sheet import combined_sheet
    from workflow.architect_reader import ARCHITECT_EXTRACTOR

    data = combined_sheet()
    once, _ = _package(session, store, [(SHOP, data)])
    _stages(store, CountingOcr(), architect_reader=MEASURED_ARCHITECT_SETTINGS).extract_pages(
        session, once.id
    )
    session.commit()

    def architect_values(revision_id: UUID) -> int:
        return (
            session.scalar(
                select(func.count())
                .select_from(ObservationCandidate)
                .join(ExtractionRun, ExtractionRun.id == ObservationCandidate.extraction_run_id)
                .join(
                    PackageRevisionDocument,
                    PackageRevisionDocument.document_version_id
                    == ObservationCandidate.document_version_id,
                )
                .where(
                    ExtractionRun.extractor == ARCHITECT_EXTRACTOR,
                    PackageRevisionDocument.package_revision_id == revision_id,
                )
            )
            or 0
        )

    expected = architect_values(once.id)
    assert expected > 0

    twice, _ = _package(session, store, [(SHOP, data), (SHOP, data)])
    _stages(store, CountingOcr(), architect_reader=MEASURED_ARCHITECT_SETTINGS).extract_pages(
        session, twice.id
    )
    session.commit()

    assert architect_values(twice.id) == expected


def test_a_revision_built_by_supersede_reads_one_file_once(
    session: Session, store: LocalStore
) -> None:
    """The path no upload door guards: a re-issued shop drawing whose bytes equal the architect
    drawing carried forward from revision 1. Revision 2 holds one file in both slots."""
    data = _sheet()
    other = _sheet() + b"\n% a different file\n"
    revision_one = _new_revision(session)
    _, arch_copy = _upload(session, store, revision_one.package_id, kind=ARCH, data=data)
    shop_document, first_shop = _upload(
        session, store, revision_one.package_id, kind=SHOP, data=other
    )
    _join(session, revision_one, arch_copy)
    _join(session, revision_one, first_shop)
    # The re-issue: a new version of the shop document, with the architect's bytes.
    digest = hashlib.sha256(data).hexdigest()
    key = storage_key(shop_document.id, digest)
    artifact = SourceArtifact(storage_key=key, sha256=digest, size=len(data))
    session.add(artifact)
    session.flush()
    reissued = DocumentVersion(
        document_id=shop_document.id, source_artifact_id=artifact.id, sha256=digest, page_count=1
    )
    session.add(reissued)
    session.flush()
    store.put(key, io.BytesIO(data), content_type="application/pdf")
    revision_two = supersede(
        session, package_id=revision_one.package_id, new_document_versions=[reissued.id], actor="t"
    )
    _claim_extraction(session, revision_two.id)
    session.commit()

    ocr = CountingOcr()
    results = _stages(store, ocr).extract_pages(session, revision_two.id)
    session.commit()

    assert len(results) == 1
    assert _pages_by_version(session, [arch_copy, reissued]) == {arch_copy.id: 0, reissued.id: 1}
    # Nothing was merged or rewritten to get there: revision 2 still names both drawings.
    members = session.scalars(
        select(PackageRevisionDocument.document_version_id).where(
            PackageRevisionDocument.package_revision_id == revision_two.id
        )
    ).all()
    assert set(members) == {arch_copy.id, reissued.id}


# --- different files, even nearly identical ones, are unchanged ----------------------------------


def test_two_different_files_are_each_read(session: Session, store: LocalStore) -> None:
    """A near copy (the same sheet re-saved with one extra line) is a different file: both read."""
    data = _sheet()
    near = data + b"\n% re-saved\n"
    assert hashlib.sha256(near).hexdigest() != hashlib.sha256(data).hexdigest()
    ocr = CountingOcr()
    revision, (arch_copy, shop_copy) = _package(session, store, [(ARCH, data), (SHOP, near)])

    results = _stages(store, ocr).extract_pages(session, revision.id)
    session.commit()

    assert len(results) == 2
    assert _pages_by_version(session, [arch_copy, shop_copy]) == {
        arch_copy.id: 1,
        shop_copy.id: 1,
    }


# --- nothing is deleted or merged ---------------------------------------------------------------


def test_every_upload_row_is_kept(session: Session, store: LocalStore) -> None:
    """Read once is not stored once: both documents, versions, artifacts and memberships remain."""
    data = _sheet()
    revision, versions = _package(session, store, [(ARCH, data), (SHOP, data)])

    def rows() -> tuple[int, int, int, int]:
        version_ids = [version.id for version in versions]
        document_ids = [version.document_id for version in versions]
        return (
            session.scalar(
                select(func.count()).select_from(Document).where(Document.id.in_(document_ids))
            )
            or 0,
            session.scalar(
                select(func.count())
                .select_from(DocumentVersion)
                .where(DocumentVersion.id.in_(version_ids))
            )
            or 0,
            session.scalar(
                select(func.count())
                .select_from(SourceArtifact)
                .join(DocumentVersion, DocumentVersion.source_artifact_id == SourceArtifact.id)
                .where(DocumentVersion.id.in_(version_ids))
            )
            or 0,
            session.scalar(
                select(func.count())
                .select_from(PackageRevisionDocument)
                .where(PackageRevisionDocument.package_revision_id == revision.id)
            )
            or 0,
        )

    before = rows()
    stages = _stages(store, CountingOcr())
    stages.ingest(session, revision.id)
    stages.extract_pages(session, revision.id)
    session.commit()

    assert before == (2, 2, 2, 2)
    assert rows() == before


# --- ingest: every copy still verified, each file parsed once ----------------------------------


def test_ingest_parses_one_file_once(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    import workflow.stages as stages_module

    data = _sheet()
    revision, _ = _package(session, store, [(ARCH, data), (SHOP, data)])
    parsed = 0
    real = stages_module.read_pages

    def counting(source: bytes) -> object:
        nonlocal parsed
        parsed += 1
        return real(source)

    monkeypatch.setattr(stages_module, "read_pages", counting)

    report = _stages(store, CountingOcr()).ingest(session, revision.id)

    assert parsed == 1
    assert report["documents"] == 2 and report["verified"] == 2


def test_ingest_still_checks_the_bytes_of_the_copy_it_does_not_parse(
    session: Session, store: LocalStore
) -> None:
    """Each copy has its own stored object; a corrupted second copy still halts the package."""
    data = _sheet()
    revision, (_, shop_copy) = _package(session, store, [(ARCH, data), (SHOP, data)])
    # Changed on disk behind the store's back, the way storage corruption arrives.
    store._path(storage_key(shop_copy.document_id, shop_copy.sha256)).write_bytes(data + b"x")

    with pytest.raises(ArtifactCorrupt):
        _stages(store, CountingOcr()).ingest(session, revision.id)


# --- roles: the slot still never decides; a person's confirmation still does ------------------


def _part_on(session: Session, view: DrawingView) -> None:
    from app.models import ItemIdentifier
    from tests.workflow.test_view_roles import _confirmed_part

    item = _confirmed_part(session, view)
    session.add(
        ItemIdentifier(drawing_item_id=item.id, kind="vendor_unique", value_as_printed="B24")
    )
    session.flush()


def test_one_file_read_once_still_takes_no_role_from_its_slot(
    session: Session, store: LocalStore
) -> None:
    """After read-once, the views exist once, on the shop copy. With no person's confirmation the
    slot must not make one of them the architect's: nothing is matched, so nothing is compared."""
    data = _sheet()
    revision, _ = _package(session, store, [(ARCH, data), (SHOP, data)])
    stages = _stages(store, CountingOcr())
    stages.extract_pages(session, revision.id)
    views = list(session.scalars(select(DrawingView).order_by(DrawingView.tag)))
    assert len(views) == 2
    for view in views:
        _part_on(session, view)

    result = stages.match(session, revision.id)

    assert result["candidates"] == 0
    assert session.scalars(select(MatchCandidate)).all() == []


def test_a_persons_confirmed_roles_still_match_one_file_read_once(
    session: Session, store: LocalStore
) -> None:
    data = _sheet()
    revision, _ = _package(session, store, [(ARCH, data), (SHOP, data)])
    stages = _stages(store, CountingOcr())
    stages.extract_pages(session, revision.id)
    views = list(session.scalars(select(DrawingView).order_by(DrawingView.tag)))
    assert len(views) == 2
    for view in views:
        proposed = session.scalars(
            select(ViewRoleProposal.proposed_role).where(
                ViewRoleProposal.drawing_view_id == view.id
            )
        ).one()
        confirm_view_role(session, view=view, role=ViewRole(proposed), actor="reviewer-1")
        _part_on(session, view)

    result = stages.match(session, revision.id)

    assert result["candidates"] == 1
