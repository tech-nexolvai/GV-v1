"""Database contract for document pinning and page manifests in issue #193."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.base import Base, Immutable
from app.db.session import session_factory, unit_of_work
from app.models import (
    Document,
    DocumentKind,
    DocumentVersion,
    GoldCase,
    GoldSet,
    Package,
    PackageRevision,
    PackageState,
    Page,
    PageType,
    Project,
    SourceArtifact,
)

pytest_plugins = ("tests.app.postgres_fixture",)

DOCUMENT_TABLES = ("source_artifacts", "documents", "document_versions", "pages")
HASH_A = "a" * 64
HASH_B = "b" * 64
PAGE_HASH = "c" * 64


@pytest.mark.parametrize("table", DOCUMENT_TABLES)
def test_every_document_table_is_registered(table: str) -> None:
    """Input: imported models. Outcome: table registered. Why: Alembic must see it."""

    assert table in Base.metadata.tables


@pytest.mark.parametrize("model", [SourceArtifact, DocumentVersion, Page])
def test_pinned_records_are_marked_immutable(model: type) -> None:
    """Input: historical model. Outcome: marker. Why: C1.12 will revoke update/delete."""

    assert issubclass(model, Immutable)


def test_page_schema_contains_the_complete_b6_and_b11_fields() -> None:
    """Input: Page metadata. Outcome: all manifest fields. Why: persistence must round-trip it."""

    expected = {
        "index",
        "content_hash",
        "width_pt",
        "height_pt",
        "rotation",
        "has_vector_text",
        "render_failed",
        "sheet_number",
        "page_type",
        "revision_label",
        "revision_date_raw",
        "revision_date_interpretations",
        "revision_sequence_index",
    }
    assert expected <= set(Base.metadata.tables["pages"].columns.keys())


def test_gold_cases_now_have_a_restricting_document_version_foreign_key() -> None:
    """Input: gold-case schema. Outcome: restrictive FK. Why: annotations cannot be orphaned."""

    matching = [
        foreign_key
        for foreign_key in Base.metadata.tables["gold_cases"].foreign_keys
        if foreign_key.column.table.name == "document_versions"
    ]
    assert len(matching) == 1
    assert matching[0].ondelete == "RESTRICT"


def _persist_package_revision(session: Session) -> PackageRevision:
    project = Project(name="GV Document Test")
    package = Package(project_id=project.id, vendor=None)
    revision = PackageRevision(
        package_id=package.id,
        revision_number=1,
        state=PackageState.CREATED,
    )
    session.add(project)
    session.flush()
    session.add(package)
    session.flush()
    session.add(revision)
    session.flush()
    return revision


def _persist_version(
    session: Session,
    *,
    sha256: str = HASH_A,
    storage_key: str = "originals/project/package.pdf",
) -> DocumentVersion:
    revision = _persist_package_revision(session)
    document = Document(
        package_revision_id=revision.id,
        kind=DocumentKind.SHOP,
    )
    artifact = SourceArtifact(
        storage_key=storage_key,
        sha256=sha256,
        size=1024,
        backend_version_id=None,
    )
    session.add_all((document, artifact))
    session.flush()
    version = DocumentVersion(
        document_id=document.id,
        source_artifact_id=artifact.id,
        sha256=sha256,
        page_count=1,
    )
    session.add(version)
    session.flush()
    return version


def _page(version_id: UUID, **changes: object) -> Page:
    values: dict[str, object] = {
        "document_version_id": version_id,
        "index": 0,
        "content_hash": PAGE_HASH,
        "width_pt": Decimal("612.125"),
        "height_pt": Decimal("792.5"),
        "rotation": 90,
        "has_vector_text": True,
        "render_failed": False,
        "sheet_number": "A-101",
        "page_type": PageType.PLAN,
        "revision_label": "Rev C",
        "revision_date_raw": "03/04/26",
        "revision_date_interpretations": ["2026-03-04", "2026-04-03"],
        "revision_sequence_index": 3,
    }
    values.update(changes)
    return Page(**values)


def test_same_bytes_cannot_create_two_versions_of_one_document(postgres_engine: Engine) -> None:
    """Input: same document and SHA twice. Outcome: rejection. Why: duplicate is not a version."""

    Base.metadata.create_all(postgres_engine)
    factory = session_factory(postgres_engine)
    with pytest.raises(IntegrityError), unit_of_work(factory) as session:
        first = _persist_version(session)
        second_artifact = SourceArtifact(
            storage_key="originals/project/duplicate.pdf",
            sha256=HASH_A,
            size=1024,
            backend_version_id=None,
        )
        session.add(second_artifact)
        session.flush()
        session.add(
            DocumentVersion(
                document_id=first.document_id,
                source_artifact_id=second_artifact.id,
                sha256=HASH_A,
                page_count=1,
            )
        )
        session.flush()


def test_different_bytes_create_a_new_version_without_updating_the_first(
    postgres_engine: Engine,
) -> None:
    """Input: same document with new bytes. Outcome: two immutable rows with distinct hashes."""

    Base.metadata.create_all(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        first = _persist_version(session)
        first_id = first.id
        document_id = first.document_id
        second_artifact = SourceArtifact(
            storage_key="originals/project/revised.pdf",
            sha256=HASH_B,
            size=2048,
            backend_version_id="s3-version-2",
        )
        session.add(second_artifact)
        session.flush()
        second = DocumentVersion(
            document_id=document_id,
            source_artifact_id=second_artifact.id,
            sha256=HASH_B,
            page_count=2,
        )
        session.add(second)
        second_id = second.id
    with unit_of_work(factory) as session:
        versions = session.scalars(
            select(DocumentVersion)
            .where(DocumentVersion.document_id == document_id)
            .order_by(DocumentVersion.created_at)
        ).all()
        assert {version.id for version in versions} == {first_id, second_id}
        assert {version.sha256 for version in versions} == {HASH_A, HASH_B}


def test_version_hash_must_match_the_source_artifact(postgres_engine: Engine) -> None:
    """Input: version hash B pointing at artifact A. Outcome: FK rejection, never false pinning."""

    Base.metadata.create_all(postgres_engine)
    factory = session_factory(postgres_engine)
    with pytest.raises(IntegrityError), unit_of_work(factory) as session:
        revision = _persist_package_revision(session)
        document = Document(package_revision_id=revision.id, kind=DocumentKind.ARCHITECTURAL)
        artifact = SourceArtifact(
            storage_key="originals/project/architectural.pdf",
            sha256=HASH_A,
            size=500,
            backend_version_id=None,
        )
        session.add_all((document, artifact))
        session.flush()
        session.add(
            DocumentVersion(
                document_id=document.id,
                source_artifact_id=artifact.id,
                sha256=HASH_B,
                page_count=1,
            )
        )
        session.flush()


def test_page_round_trip_preserves_unknowns_failure_and_exact_revision_data(
    postgres_engine: Engine,
) -> None:
    """Input: failed unclassified page. Outcome: retained exact geometry and ambiguous date."""

    Base.metadata.create_all(postgres_engine)
    factory = session_factory(postgres_engine)
    page_id: UUID
    with unit_of_work(factory) as session:
        version = _persist_version(session)
        page = _page(version.id, page_type=None, render_failed=True)
        page_id = page.id
        session.add(page)
    with unit_of_work(factory) as session:
        restored = session.get(Page, page_id)
        assert restored is not None
        assert restored.page_type is None
        assert restored.render_failed is True
        assert restored.width_pt == Decimal("612.125")
        assert restored.height_pt == Decimal("792.5")
        assert restored.revision_date_raw == "03/04/26"
        assert restored.revision_date_interpretations == ["2026-03-04", "2026-04-03"]


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"index": -1}, "negative internal index"),
        ({"rotation": 45}, "unsupported rotation"),
        ({"page_type": "probably_plan"}, "guessed page classification"),
    ],
)
def test_invalid_page_manifest_values_are_rejected(
    postgres_engine: Engine,
    changes: dict[str, object],
    reason: str,
) -> None:
    """Input: malformed manifest value. Outcome: rejection. Why: recorded page must be truthful."""

    del reason
    Base.metadata.create_all(postgres_engine)
    factory = session_factory(postgres_engine)
    with pytest.raises(IntegrityError), unit_of_work(factory) as session:
        version = _persist_version(session)
        session.add(_page(version.id, **changes))
        session.flush()


def test_gold_case_cannot_reference_an_unstored_document_version(postgres_engine: Engine) -> None:
    """Input: gold annotation with random version UUID. Outcome: rejection, never orphaned truth."""

    Base.metadata.create_all(postgres_engine)
    factory = session_factory(postgres_engine)
    with pytest.raises(IntegrityError), unit_of_work(factory) as session:
        gold_set = GoldSet(name="Document FK", version="1", notes=None)
        session.add(gold_set)
        session.flush()
        session.add(
            GoldCase(
                gold_set_id=gold_set.id,
                document_version_id=uuid4(),
                content_hash=HASH_A,
                annotations={},
                annotated_by="reviewer@example.test",
                annotated_on=None,
            )
        )
        session.flush()


def test_referenced_document_version_cannot_be_deleted(postgres_engine: Engine) -> None:
    """Input: delete a version with a page. Outcome: rejection. Why: evidence cannot be orphaned."""

    Base.metadata.create_all(postgres_engine)
    factory = session_factory(postgres_engine)
    version_id: UUID
    with unit_of_work(factory) as session:
        version = _persist_version(session)
        version_id = version.id
        session.add(_page(version.id))
    with pytest.raises(IntegrityError), unit_of_work(factory) as session:
        session.execute(delete(DocumentVersion).where(DocumentVersion.id == version_id))
