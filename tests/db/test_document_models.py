"""Database contract for document pinning and page manifests in issue #193."""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Engine, delete, func, select, update
from sqlalchemy.exc import DatabaseError, IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
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
    PackageRevisionDocument,
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
        package_id=revision.package_id,
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
        document = Document(package_id=revision.package_id, kind=DocumentKind.ARCHITECTURAL)
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


# ---------------------------------------------------------------------------
# A document belongs to the package; a revision names what it includes (#366, ADR-0018)
# ---------------------------------------------------------------------------
#
# The properties the ADR claims, each asserted against a migrated database rather than against the
# ORM metadata. `create_all` builds from the models, so it would compare the models with themselves —
# the lesson #313 cost a CI round-trip to learn.


@pytest.fixture
def migrated(postgres_engine: Engine) -> sessionmaker[Session]:
    """A session factory against a database migrated to head."""
    config = Config("alembic.ini")
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")
    return session_factory(postgres_engine)


def _package(session: Session, name: str = "GV Identity Test") -> Package:
    project = Project(name=f"{name} {uuid4().hex[:8]}")
    session.add(project)
    session.flush()
    package = Package(project_id=project.id, vendor=None)
    session.add(package)
    session.flush()
    return package


def _revision(
    session: Session, package: Package, number: int, state: PackageState
) -> PackageRevision:
    revision = PackageRevision(package_id=package.id, revision_number=number, state=state)
    session.add(revision)
    session.flush()
    return revision


def _version(session: Session, document: Document, digest: str) -> DocumentVersion:
    artifact = SourceArtifact(
        storage_key=f"originals/{uuid4().hex}.pdf",
        sha256=digest,
        size=2048,
        backend_version_id=None,
    )
    session.add(artifact)
    session.flush()
    version = DocumentVersion(
        document_id=document.id, source_artifact_id=artifact.id, sha256=digest, page_count=3
    )
    session.add(version)
    session.flush()
    return version


def _include(
    session: Session, revision: PackageRevision, document: Document, version: DocumentVersion
) -> PackageRevisionDocument:
    membership = PackageRevisionDocument(
        package_revision_id=revision.id,
        package_id=document.package_id,
        document_id=document.id,
        document_version_id=version.id,
    )
    session.add(membership)
    session.flush()
    return membership


def test_one_drawing_is_one_document_across_two_revisions(
    migrated: sessionmaker[Session],
) -> None:
    """Input: two revisions including the same version. Outcome: both. Why: this was impossible.

    **The property #211 was blocked on.** `uq_document_versions_source_artifact_id` refuses a second
    version over the same bytes, so before ADR-0018 a superseding revision could not include a sheet
    that had not changed — and a revision holding only the changed sheet runs its checks against a
    partial drawing set, where the absent drawings produce no failures.
    """
    with unit_of_work(migrated) as session:
        package = _package(session)
        first = _revision(session, package, 1, PackageState.CREATED)
        second = _revision(session, package, 2, PackageState.CREATED)
        document = Document(package_id=package.id, kind=DocumentKind.SHOP)
        session.add(document)
        session.flush()
        version = _version(session, document, HASH_A)

        _include(session, first, document, version)
        _include(session, second, document, version)

        shared = session.scalars(
            select(PackageRevisionDocument.package_revision_id).where(
                PackageRevisionDocument.document_version_id == version.id
            )
        ).all()
        assert set(shared) == {first.id, second.id}


def test_the_same_bytes_are_stored_once_however_many_revisions_include_them(
    migrated: sessionmaker[Session],
) -> None:
    """The acceptance criterion. Carrying a drawing forward costs one link row and no bytes."""
    with unit_of_work(migrated) as session:
        package = _package(session)
        first = _revision(session, package, 1, PackageState.CREATED)
        second = _revision(session, package, 2, PackageState.CREATED)
        document = Document(package_id=package.id, kind=DocumentKind.SHOP)
        session.add(document)
        session.flush()
        version = _version(session, document, HASH_A)
        _include(session, first, document, version)
        _include(session, second, document, version)

        artifacts = session.scalar(select(func.count()).select_from(SourceArtifact))
        versions = session.scalar(select(func.count()).select_from(DocumentVersion))
        assert (artifacts, versions) == (1, 1), "two revisions, one artifact, one version"


def test_a_revision_cannot_include_two_versions_of_one_drawing(
    migrated: sessionmaker[Session],
) -> None:
    """Otherwise a revision holds v1 and v2 of the same sheet, its checks run against both, and no
    reader can say which version a finding meant."""
    with pytest.raises(IntegrityError), unit_of_work(migrated) as session:
        package = _package(session)
        revision = _revision(session, package, 1, PackageState.CREATED)
        document = Document(package_id=package.id, kind=DocumentKind.SHOP)
        session.add(document)
        session.flush()
        _include(session, revision, document, _version(session, document, HASH_A))
        _include(session, revision, document, _version(session, document, HASH_B))


def test_a_revision_cannot_include_another_packages_drawing(
    migrated: sessionmaker[Session],
) -> None:
    """**The hole the prototype found while ADR-0018 was still in draft.**

    Every value in the refused row is individually true: the document really does belong to the other
    package, and the revision really does exist. The *combination* is the lie, and resolving only the
    document side let it through. Naming the same `package_id` in the revision's key too is what
    refuses it.
    """
    with pytest.raises(IntegrityError), unit_of_work(migrated) as session:
        mine = _package(session, "mine")
        theirs = _package(session, "theirs")
        revision = _revision(session, mine, 1, PackageState.CREATED)
        document = Document(package_id=theirs.id, kind=DocumentKind.SHOP)
        session.add(document)
        session.flush()
        version = _version(session, document, HASH_A)
        session.add(
            PackageRevisionDocument(
                package_revision_id=revision.id,
                package_id=theirs.id,
                document_id=document.id,
                document_version_id=version.id,
            )
        )
        session.flush()


def test_a_prior_revision_still_resolves_to_the_bytes_it_was_reviewed_against(
    migrated: sessionmaker[Session],
) -> None:
    """**The property that makes a six-month-old review defensible**, and the one that must not weaken.

    Revision 2 includes a newer version of the same drawing. Revision 1 must still resolve to the
    version — and therefore the artifact, and therefore the bytes — that it was checked against.
    """
    with unit_of_work(migrated) as session:
        package = _package(session)
        first = _revision(session, package, 1, PackageState.CREATED)
        second = _revision(session, package, 2, PackageState.CREATED)
        document = Document(package_id=package.id, kind=DocumentKind.SHOP)
        session.add(document)
        session.flush()
        old_version = _version(session, document, HASH_A)
        new_version = _version(session, document, HASH_B)
        _include(session, first, document, old_version)
        _include(session, second, document, new_version)

        def version_for(revision_id: UUID) -> UUID | None:
            return session.scalar(
                select(PackageRevisionDocument.document_version_id).where(
                    PackageRevisionDocument.package_revision_id == revision_id
                )
            )

        assert version_for(first.id) == old_version.id, "March's answer changed"
        assert version_for(second.id) == new_version.id


def test_the_set_may_change_while_the_revision_is_being_assembled(
    migrated: sessionmaker[Session],
) -> None:
    """Drawings arrive one at a time, and a mis-uploaded sheet re-uploaded a minute later is ordinary
    use rather than tampering. Anant's call: mutable while assembling, frozen once read."""
    for state in (PackageState.CREATED, PackageState.UPLOADING, PackageState.UPLOADED):
        with unit_of_work(migrated) as session:
            package = _package(session, f"assembling {state.value}")
            revision = _revision(session, package, 1, state)
            document = Document(package_id=package.id, kind=DocumentKind.SHOP)
            session.add(document)
            session.flush()
            membership = _include(session, revision, document, _version(session, document, HASH_A))
            replacement = _version(session, document, HASH_B)

            membership.document_version_id = replacement.id
            session.flush()

            assert (
                session.scalar(
                    select(PackageRevisionDocument.document_version_id).where(
                        PackageRevisionDocument.id == membership.id
                    )
                )
                == replacement.id
            ), state.value


def test_the_set_is_frozen_once_something_has_read_it(migrated: sessionmaker[Session]) -> None:
    """**From `INGESTING` onward the set is evidence.** A set that can change after it has been read is
    a set nobody can be held to — *"that drawing wasn't in the set we reviewed"* must not be a row
    anybody can edit. Changing it means superseding the revision (#211).
    """
    for state in (PackageState.INGESTING, PackageState.RUNNING_CHECKS, PackageState.APPROVED):
        with pytest.raises(DatabaseError), unit_of_work(migrated) as session:
            package = _package(session, f"frozen {state.value}")
            revision = _revision(session, package, 1, PackageState.CREATED)
            document = Document(package_id=package.id, kind=DocumentKind.SHOP)
            session.add(document)
            session.flush()
            membership = _include(session, revision, document, _version(session, document, HASH_A))
            replacement = _version(session, document, HASH_B)

            # Moved on, so the set is now evidence rather than a work in progress.
            session.execute(
                update(PackageRevision)
                .where(PackageRevision.id == revision.id)
                .values(state=state.value)
            )
            session.flush()

            membership.document_version_id = replacement.id
            session.flush()


def test_a_frozen_set_cannot_have_a_member_removed(migrated: sessionmaker[Session]) -> None:
    """Deleting is the other way to change what a revision was reviewed against."""
    with pytest.raises(DatabaseError), unit_of_work(migrated) as session:
        package = _package(session, "frozen delete")
        revision = _revision(session, package, 1, PackageState.CREATED)
        document = Document(package_id=package.id, kind=DocumentKind.SHOP)
        session.add(document)
        session.flush()
        membership = _include(session, revision, document, _version(session, document, HASH_A))
        session.execute(
            update(PackageRevision)
            .where(PackageRevision.id == revision.id)
            .values(state=PackageState.AWAITING_REVIEW.value)
        )
        session.flush()
        session.delete(membership)
        session.flush()


def test_the_migration_and_the_lifecycle_agree_on_the_assembly_states() -> None:
    """Input: both lists. Outcome: identical. Why: they are two copies of one rule.

    The trigger in `0017` hardcodes the assembly states, because a migration describes one fixed
    historical state and must not import live code. `ASSEMBLY_STATES` in `app/lifecycle/states.py` is
    the source. #313 was exactly this shape — an enum and a migration's literal drifting apart with
    nothing comparing them — so it gets the same guard.
    """
    from app.lifecycle.states import ASSEMBLY_STATES

    source = Path("alembic/versions/0017_document_per_package.py").read_text(encoding="utf-8")
    trigger = re.search(r"revision_state NOT IN \(([^)]*)\)", source)
    assert trigger is not None, "the freeze trigger no longer names its states inline"
    in_migration = frozenset(value.strip().strip("'") for value in trigger.group(1).split(","))
    declared = frozenset(state.value for state in ASSEMBLY_STATES)

    assert in_migration == declared, (
        f"0017's trigger allows {sorted(in_migration)} but ASSEMBLY_STATES declares "
        f"{sorted(declared)}. A state added to one and not the other either freezes a revision that "
        "is still being assembled, or leaves a reviewed set editable."
    )


def test_a_document_names_its_package_not_a_revision() -> None:
    """The docstring and the schema agree now, which is the acceptance criterion."""
    columns = set(Document.__table__.columns.keys())
    assert "package_id" in columns
    assert "package_revision_id" not in columns
    assert "across every revision" in (Document.__doc__ or "")
