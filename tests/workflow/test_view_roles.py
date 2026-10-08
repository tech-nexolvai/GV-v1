"""Role resolution for matching when one sheet carries both drawing roles (#705)."""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alembic import command
from app.audit.events import AuditEvent
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
    ViewRoleConfirmation,
    ViewRoleProposal,
)
from retrieval.matching import MatchDocumentRole
from storage.local import LocalStore
from tests.app.postgres_fixture import alembic_config
from tests.extraction.test_annotations import _appearance, _free_text, _pdf, _stamp
from tests.extraction.test_reader import MISSING_SPACE
from vocabulary.part_kinds import PartKind
from workflow.parts import confirm_part, record_part_proposal
from workflow.stages import (
    DatabaseStages,
    _architect_candidate_filter_boxes_by_page,
    _matchable_items,
)
from workflow.view_roles import confirm_view_role

pytest_plugins = ("tests.app.postgres_fixture",)

BOX = {"space": "pdf_points", "polygon": [0, 0, 100, 100]}

#: A part's outline in stored page space, which a suggestion requires.
STORED_BOX = ((Decimal(0), Decimal(0)), (Decimal(1), Decimal(0)), (Decimal(1), Decimal(1)))


def _confirmed_part(
    session: Session, view: DrawingView, kind: PartKind = PartKind.CABINET
) -> DrawingItem:
    """A part a person confirmed on `view`: outside tests, the only way an item is made (#852).

    Matching reads only items whose confirmation is current (#882), so an item inserted by hand
    would never be read.
    """
    proposal = record_part_proposal(
        session,
        drawing_view_id=view.id,
        kind=kind,
        extent=STORED_BOX,
        defining_line=(STORED_BOX[0], STORED_BOX[1]),
        code_as_printed=None,
        code_candidate_id=None,
        reason="seeded by the test",
        source="test",
        source_version="1",
    )
    confirmation = confirm_part(
        session, proposal=proposal, kind=kind, code=None, actor="reviewer-1"
    )
    return session.get_one(DrawingItem, confirmation.drawing_item_id)


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
    digest: str | None = None,
) -> DocumentVersion:
    n = len(session.execute(select(DocumentVersion)).all()) + 1
    digest = digest or f"{n:064x}"
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
    item = _confirmed_part(session, view)
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

    result = DatabaseStages(missing_space=MISSING_SPACE).match(session, revision.id)

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

    result = DatabaseStages(missing_space=MISSING_SPACE).match(session, revision.id)

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

    result = DatabaseStages(missing_space=MISSING_SPACE).match(session, revision.id)

    assert result["candidates"] == 1
    candidate = session.scalars(select(MatchCandidate)).one()
    assert candidate.left_item_id == arch.id
    assert candidate.right_item_id == shop.id


def test_one_file_uploaded_as_both_kinds_never_matches_itself(session: Session) -> None:
    """**#963.** The same bytes as the architectural AND the shop document is one combined set. With
    the slot deciding, the vendor's item on the "architectural" copy matched its own twin on the shop
    copy — a vendor-vs-architect comparison of a drawing with itself. Outcome: no role from the slot,
    so nothing is matched until a person confirms whose drawings they are."""
    revision = _revision(session)
    same = "ab" * 32
    arch_copy = _document_version(
        session, revision, document_kind=DocumentKind.ARCHITECTURAL, digest=same
    )
    shop_copy = _document_version(session, revision, document_kind=DocumentKind.SHOP, digest=same)
    _item(session, arch_copy, view_role=None, tag="S-101")
    _item(session, shop_copy, view_role=None, tag="S-101")

    DatabaseStages(missing_space=MISSING_SPACE).match(session, revision.id)

    assert session.scalars(select(MatchCandidate)).all() == []


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


# ---------------------------------------------------------------------------
# A combined sheet: each drawing becomes a view, its label gives a suggestion,
# and only a reviewer's confirmation sets the role (#710)
# ---------------------------------------------------------------------------


def _combined_sheet(*, labelled: bool = True) -> bytes:
    """Two drawings on one 400 x 300 page, labelled the client's way: `ID SET ELEVATION` at the top,
    `VENDOR'S SHOP DRAWING ELEVATION` halfway down as a divider with the vendor's drawing under it.
    """
    labels = (
        [
            _free_text("ID SET ELEVATION ", rect=b"[10 285 150 298]"),
            _free_text("VENDOR'S SHOP DRAWING ELEVATION ", rect=b"[10 142 220 156]"),
        ]
        if labelled
        else []
    )
    first = 5 + len(labels) + 2  # the two appearances follow the annotations
    return _pdf(
        annotations=[
            *labels,
            _stamp(rect=b"[60 160 350 290]", appearance_object=first),
            _stamp(rect=b"[60 20 350 140]", appearance_object=first + 1),
        ],
        extra_objects=[_appearance(), _appearance()],
    )


@pytest.fixture
def store() -> Iterator[LocalStore]:
    with tempfile.TemporaryDirectory() as directory:
        yield LocalStore(root=Path(directory), ticket_secret=b"a secret only this test knows")


def _extract(session: Session, store: LocalStore, data: bytes) -> PackageRevision:
    from tests.workflow.test_association import _revision as _stored_revision
    from tests.workflow.test_markup_route import _SilentOcr

    revision = _stored_revision(session, store, data=data)
    session.commit()
    DatabaseStages(store, dpi=150, ocr_engine=_SilentOcr(), missing_space=MISSING_SPACE).extract_pages(  # type: ignore[arg-type]
        session, revision.id
    )
    session.commit()
    return revision


def _views(session: Session) -> list[DrawingView]:
    return list(session.scalars(select(DrawingView).order_by(DrawingView.tag)))


def _suggestion(session: Session, view: DrawingView) -> ViewRoleProposal:
    return session.scalars(
        select(ViewRoleProposal).where(ViewRoleProposal.drawing_view_id == view.id)
    ).one()


def test_each_drawing_on_a_combined_sheet_becomes_a_view_with_a_suggestion(
    session: Session, store: LocalStore
) -> None:
    """**Acceptance 1.** The suggestion is recorded, not applied: both views have no role."""
    _extract(session, store, _combined_sheet())

    views = _views(session)

    assert len(views) == 2
    assert [view.role for view in views] == [None, None]
    suggested = {_suggestion(session, view).proposed_role for view in views}
    assert suggested == {"arch", "shop"}


def test_the_suggestion_follows_the_label_not_the_position(
    session: Session, store: LocalStore
) -> None:
    _extract(session, store, _combined_sheet())

    by_role = {_suggestion(session, view).proposed_role: view for view in _views(session)}

    top = by_role["arch"].region["points"]
    bottom = by_role["shop"].region["points"]
    assert max(Decimal(y) for _x, y in top) < min(Decimal(y) for _x, y in bottom)
    assert _suggestion(session, by_role["arch"]).heading == "ID SET ELEVATION"


def test_a_sheet_with_no_labels_still_reads_and_suggests_nothing(
    session: Session, store: LocalStore
) -> None:
    """**Acceptance 3.** No model is involved, so nothing can be missing — but with no labels there is
    nothing to suggest, and each drawing records that, with the reason."""
    _extract(session, store, _combined_sheet(labelled=False))

    views = _views(session)

    assert len(views) == 2
    assert all(_suggestion(session, view).proposed_role is None for view in views)
    assert all("label" in _suggestion(session, view).reason for view in views)


def test_reading_the_sheet_again_adds_nothing(session: Session, store: LocalStore) -> None:
    revision = _extract(session, store, _combined_sheet())
    from tests.workflow.test_markup_route import _SilentOcr

    DatabaseStages(store, dpi=150, ocr_engine=_SilentOcr(), missing_space=MISSING_SPACE).extract_pages(  # type: ignore[arg-type]
        session, revision.id
    )
    session.commit()

    assert len(_views(session)) == 2
    assert len(session.scalars(select(ViewRoleProposal)).all()) == 2


def test_only_a_reviewer_confirmation_sets_the_role(session: Session, store: LocalStore) -> None:
    """**Acceptance 2.** Reading the sheet never sets it; confirming does, is recorded, and is
    audited with who did it."""
    _extract(session, store, _combined_sheet())
    view = _views(session)[0]
    assert view.role is None

    confirmation = confirm_view_role(session, view=view, role=ViewRole.ARCH, actor="reviewer-1")
    session.commit()

    assert session.get(DrawingView, view.id).role == "arch"  # type: ignore[union-attr]
    assert session.scalars(select(ViewRoleConfirmation)).one().confirmed_by == "reviewer-1"
    audited = session.scalars(
        select(AuditEvent).where(AuditEvent.target_id == confirmation.id)
    ).one()
    assert audited.actor == "reviewer-1"


def test_a_correction_is_a_new_confirmation_and_the_latest_counts(
    session: Session, store: LocalStore
) -> None:
    _extract(session, store, _combined_sheet())
    view = _views(session)[0]

    confirm_view_role(session, view=view, role=ViewRole.ARCH, actor="reviewer-1")
    confirm_view_role(session, view=view, role=ViewRole.SHOP, actor="reviewer-2")
    session.commit()

    assert session.get(DrawingView, view.id).role == "shop"  # type: ignore[union-attr]
    assert len(session.scalars(select(ViewRoleConfirmation)).all()) == 2


def test_match_says_what_is_missing_once_drawings_are_found(
    session: Session, store: LocalStore
) -> None:
    """The old message said matching needed "the real drawings (#274)", which had arrived. It now
    names how many drawings were found, how many roles are confirmed, and that items are missing."""
    revision = _extract(session, store, _combined_sheet())
    for view in _views(session):
        role = ViewRole(_suggestion(session, view).proposed_role)
        confirm_view_role(session, view=view, role=role, actor="reviewer-1")
    session.commit()

    result = DatabaseStages(missing_space=MISSING_SPACE).match(session, revision.id)

    assert result["candidates"] == 0
    reason = str(result["reason"])
    assert "Drawings found: 2" in reason
    assert "1 architect, 1 vendor" in reason
    assert "#748" in reason and "#274" not in reason


def test_after_both_roles_are_confirmed_match_stops_abstaining_once_items_exist(
    session: Session, store: LocalStore
) -> None:
    """**Acceptance 4, as far as this issue reaches.** Run without association settings, the stage
    suggests no parts, so the items here are confirmed by hand on the views the sheet produced. With
    both roles confirmed, the matcher runs and proposes the match instead of abstaining."""
    revision = _extract(session, store, _combined_sheet())
    views = {_suggestion(session, view).proposed_role: view for view in _views(session)}
    for role, view in views.items():
        confirm_view_role(session, view=view, role=ViewRole(role), actor="reviewer-1")
        item = _confirmed_part(session, view)
        session.add(
            ItemIdentifier(drawing_item_id=item.id, kind="vendor_unique", value_as_printed="B24")
        )
    session.commit()

    result = DatabaseStages(missing_space=MISSING_SPACE).match(session, revision.id)

    assert result["candidates"] == 1


def test_the_document_kind_never_decides_a_combined_sheet_s_views(
    session: Session, store: LocalStore
) -> None:
    """**Acceptance 5.** The sheet was uploaded as one document of one kind; its views still carry no
    role until a person says, whatever that kind is."""
    _extract(session, store, _combined_sheet())

    assert all(view.role is None for view in _views(session))


def test_slot_reader_receives_architect_role_step_regions_only(session: Session) -> None:
    revision = _revision(session)
    version = _document_version(session, revision, document_kind=DocumentKind.SHOP)
    page = Page(
        document_version_id=version.id,
        index=0,
        content_hash="a" * 64,
        width_pt=Decimal(612),
        height_pt=Decimal(792),
        rotation=0,
        has_vector_text=True,
        render_failed=False,
        sheet_number=None,
        page_type=None,
        revision_label=None,
    )
    session.add(page)
    session.flush()
    arch = DrawingView(
        page_id=page.id,
        tag="A",
        region={"space": "stored", "points": [["0.1", "0.2"], ["0.8", "0.2"], ["0.8", "0.6"]]},
        role=ViewRole.ARCH.value,
    )
    unknown = DrawingView(
        page_id=page.id,
        tag="B",
        region={"space": "stored", "points": [["0.1", "0.6"], ["0.8", "0.6"], ["0.8", "0.9"]]},
        role=None,
    )
    proposed_arch = DrawingView(
        page_id=page.id,
        tag="C",
        region={"space": "stored", "points": [["0.2", "0.1"], ["0.4", "0.1"], ["0.4", "0.3"]]},
        role=None,
    )
    session.add_all((arch, unknown, proposed_arch))
    session.flush()
    session.add(
        ViewRoleProposal(
            drawing_view_id=proposed_arch.id,
            proposed_role=ViewRole.ARCH.value,
            heading="synthetic architect view",
            reason="synthetic role-step proposal",
            source="test",
        )
    )
    session.flush()

    result = _architect_candidate_filter_boxes_by_page(session, (page.id,))

    assert len(result[page.id]) == 2
    assert {(box.left, box.top, box.right, box.bottom) for box in result[page.id]} == {
        (Decimal("0.1"), Decimal("0.2"), Decimal("0.8"), Decimal("0.6")),
        (Decimal("0.2"), Decimal("0.1"), Decimal("0.4"), Decimal("0.3")),
    }
