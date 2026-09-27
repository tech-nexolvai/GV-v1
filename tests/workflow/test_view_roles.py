"""Role resolution for matching when one sheet carries both drawing roles (#705)."""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alembic import command
from app.db.session import session_factory
from app.models import (
    Document,
    DocumentKind,
    DocumentVersion,
    DrawingItem,
    DrawingView,
    ItemIdentifier,
    MatchCandidate,
    Package,
    PackageRevision,
    PackageRevisionDocument,
    PackageState,
    Page,
    Project,
    SourceArtifact,
    ViewRole,
)
from retrieval.matching import MatchDocumentRole
from tests.app.postgres_fixture import alembic_config
from workflow.stages import DatabaseStages, _matchable_items

pytest_plugins = ("tests.app.postgres_fixture",)

BOX = {"space": "pdf_points", "polygon": [0, 0, 100, 100]}


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


def _revision(session: Session) -> PackageRevision:
    project = Project(name="view role matching")
    session.add(project)
    session.flush()
    package = Package(project_id=project.id, vendor="Apex")
    session.add(package)
    session.flush()
    revision = PackageRevision(
        package_id=package.id,
        revision_number=1,
        state=PackageState.MATCHING,
    )
    session.add(revision)
    session.flush()
    return revision


def _document_version(
    session: Session,
    revision: PackageRevision,
    *,
    document_kind: DocumentKind,
) -> DocumentVersion:
    n = len(session.execute(select(DocumentVersion)).all()) + 1
    digest = f"{n:064x}"
    document = Document(package_id=revision.package_id, kind=document_kind)
    session.add(document)
    session.flush()
    artifact = SourceArtifact(
        storage_key=f"documents/{document.id}/{digest}.pdf",
        sha256=digest,
        size=1,
        backend_version_id=None,
    )
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
            package_id=revision.package_id,
            document_id=document.id,
            document_version_id=version.id,
        )
    )
    session.flush()
    return version


def _item(
    session: Session,
    version: DocumentVersion,
    *,
    view_role: ViewRole | None,
    tag: str,
    mark: str = "B24",
) -> DrawingItem:
    page_count = len(
        session.execute(select(Page).where(Page.document_version_id == version.id)).all()
    )
    digest = f"{page_count + 1:064x}"
    page = Page(
        document_version_id=version.id,
        index=page_count,
        content_hash=digest,
        width_pt=Decimal(612),
        height_pt=Decimal(792),
        rotation=0,
        has_vector_text=True,
        render_failed=False,
        sheet_number=tag,
        page_type=None,
        revision_label=None,
    )
    session.add(page)
    session.flush()
    view = DrawingView(
        page_id=page.id,
        tag=tag,
        region=BOX,
        role=None if view_role is None else view_role.value,
    )
    session.add(view)
    session.flush()
    item = DrawingItem(drawing_view_id=view.id, item_type="CT001", extent=BOX)
    session.add(item)
    session.flush()
    session.add(
        ItemIdentifier(
            drawing_item_id=item.id,
            kind="vendor_unique",
            value_as_printed=mark,
        )
    )
    session.flush()
    return item


def test_combined_sheet_matches_by_confirmed_view_roles(session: Session) -> None:
    """Both roles can come from views inside one uploaded document kind."""
    revision = _revision(session)
    combined = _document_version(session, revision, document_kind=DocumentKind.SHOP)
    arch = _item(
        session,
        combined,
        view_role=ViewRole.ARCH,
        tag="ID SET",
    )
    shop = _item(
        session,
        combined,
        view_role=ViewRole.SHOP,
        tag="SHOP",
    )

    result = DatabaseStages().match(session, revision.id)

    assert result["candidates"] == 1
    candidate = session.scalars(select(MatchCandidate)).one()
    assert candidate.left_item_id == arch.id
    assert candidate.right_item_id == shop.id


def test_combined_sheet_with_unconfirmed_roles_abstains(session: Session) -> None:
    """A null view role on one combined document must not silently become the document kind."""
    revision = _revision(session)
    combined = _document_version(session, revision, document_kind=DocumentKind.SHOP)
    _item(session, combined, view_role=None, tag="ID SET")
    _item(session, combined, view_role=None, tag="SHOP")

    result = DatabaseStages().match(session, revision.id)

    assert result["candidates"] == 0
    assert result["reason"] == (
        "matching needs confirmed architectural and shop views; missing: architectural, shop"
    )
    assert session.scalars(select(MatchCandidate)).all() == []


def test_two_pdf_package_still_falls_back_to_document_kind(session: Session) -> None:
    """Existing packages with separate arch and shop PDFs do not need view roles to keep matching."""
    revision = _revision(session)
    arch_version = _document_version(session, revision, document_kind=DocumentKind.ARCHITECTURAL)
    shop_version = _document_version(session, revision, document_kind=DocumentKind.SHOP)
    arch = _item(
        session,
        arch_version,
        view_role=None,
        tag="A-101",
    )
    shop = _item(
        session,
        shop_version,
        view_role=None,
        tag="S-101",
    )

    result = DatabaseStages().match(session, revision.id)

    assert result["candidates"] == 1
    candidate = session.scalars(select(MatchCandidate)).one()
    assert candidate.left_item_id == arch.id
    assert candidate.right_item_id == shop.id


def test_view_role_takes_precedence_over_document_kind(session: Session) -> None:
    """The document kind is no longer the source of truth when the view has a confirmed role."""
    revision = _revision(session)
    shop_version = _document_version(session, revision, document_kind=DocumentKind.SHOP)
    arch_version = _document_version(session, revision, document_kind=DocumentKind.ARCHITECTURAL)
    arch = _item(
        session,
        shop_version,
        view_role=ViewRole.ARCH,
        tag="ID SET",
    )
    _item(
        session,
        arch_version,
        view_role=ViewRole.SHOP,
        tag="SHOP",
    )

    projected = _matchable_items(session, revision.id)
    arch_projection = next(item for item, _ in projected if item.item_id == arch.id)

    assert arch_projection.document_role is MatchDocumentRole.ARCH
