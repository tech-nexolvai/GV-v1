"""The chat's uploads carry the vendor's previous round beside its re-issued file (#1728), for a set
uploaded as separate files only. Database-backed; made-up hashes and names, no PDF read, $0."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from app.api.assistant_chats import PREVIOUS_ROUND_WORDS, _upload_sources


def _setup(postgres_engine: Any, *, architect: bool, same_shop: bool = False) -> tuple[Any, Any]:
    from sqlalchemy.orm import sessionmaker

    from alembic import command
    from app.models import Package, PackageRevision, PackageState, Project
    from app.models.document import (
        Document,
        DocumentKind,
        DocumentVersion,
        PackageRevisionDocument,
        SourceArtifact,
    )
    from tests.app.postgres_fixture import alembic_config

    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")
    factory = sessionmaker(bind=postgres_engine, expire_on_commit=False)
    with factory() as session:
        project = Project(id=uuid4(), name="rounds test")
        session.add(project)
        session.flush()
        package = Package(project_id=project.id, vendor="Sample Vendor")
        session.add(package)
        session.flush()

        def version(document: Any, digest: str, name: str) -> Any:
            artifact = SourceArtifact(
                storage_key=f"originals/{uuid4().hex}.pdf",
                sha256=digest,
                size=10,
                backend_version_id=None,
            )
            session.add(artifact)
            session.flush()
            made = DocumentVersion(
                document_id=document.id,
                source_artifact_id=artifact.id,
                sha256=digest,
                page_count=2,
                file_name=name,
            )
            session.add(made)
            session.flush()
            return made

        shop = Document(package_id=package.id, kind=DocumentKind.SHOP)
        arch = Document(package_id=package.id, kind=DocumentKind.ARCHITECTURAL)
        session.add_all([shop, arch])
        session.flush()
        first_shop = version(shop, "1" * 64, "vendor.pdf")
        arch_version = version(arch, "2" * 64, "architect.pdf")
        second_shop = first_shop if same_shop else version(shop, "3" * 64, "vendor.pdf")
        revisions = []
        for number, shop_version in ((1, first_shop), (2, second_shop)):
            revision = PackageRevision(
                package_id=package.id, revision_number=number, state=PackageState.UPLOADING
            )
            session.add(revision)
            session.flush()
            members = [(shop, shop_version)] + ([(arch, arch_version)] if architect else [])
            for document, made in members:
                session.add(
                    PackageRevisionDocument(
                        package_revision_id=revision.id,
                        package_id=package.id,
                        document_id=document.id,
                        document_version_id=made.id,
                    )
                )
            revisions.append(revision)
        session.commit()
        return factory, (package.id, revisions[0].id, revisions[1].id)


def test_round_two_of_separate_files_lists_round_one_beside_it(postgres_engine: Any) -> None:
    factory, (package, first, second) = _setup(postgres_engine, architect=True)
    listed = _upload_sources(factory, object(), package, second)()
    assert [(s.name, s.kind, s.sha256[:1]) for s in listed] == [
        ("vendor.pdf", listed[0].kind, "3"),
        ("architect.pdf", "the architect's drawings", "2"),
        ("Round 1 - vendor.pdf", PREVIOUS_ROUND_WORDS.format(round=1), "1"),
    ]
    assert listed[0].kind.startswith("the vendor's shop drawings")
    # Round 1 itself has no earlier round.
    assert [s.name for s in _upload_sources(factory, object(), package, first)()] == [
        "vendor.pdf",
        "architect.pdf",
    ]


def test_no_earlier_file_without_an_architects_file_or_a_changed_vendor_file(
    postgres_engine: Any,
) -> None:
    factory, (package, _first, second) = _setup(postgres_engine, architect=False)
    assert [s.name for s in _upload_sources(factory, object(), package, second)()] == ["vendor.pdf"]


def test_the_same_vendor_file_again_adds_nothing(postgres_engine: Any) -> None:
    factory, (package, _first, second) = _setup(postgres_engine, architect=True, same_shop=True)
    assert [s.name for s in _upload_sources(factory, object(), package, second)()] == [
        "vendor.pdf",
        "architect.pdf",
    ]
