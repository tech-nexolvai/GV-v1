"""The architect's drawings uploaded as their own file are read, view by view (#1163).

Verification for: the routing in `workflow/stages.DatabaseStages._read_architect_drawings`
(`_architect_reader_versions_for`), `workflow/architect_reader.persist_architect_pages` for views
drawn as page content, `workflow/view_roles.CODE_DOCUMENT_CONFIRMER` and
`workflow/architect_pairing_records.architect_views`. The sheets are the invented ones in
`tests/extraction/architect/`; no client value appears here.

Matching these views with the vendor's (Phase 3) is not built: the values are stored for it, and the
package still says plainly that the separate file is not compared (#1161).
"""

from __future__ import annotations

import hashlib
import io
import tempfile
from collections.abc import Iterator
from fractions import Fraction
from pathlib import Path

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alembic import command
from app.api.documents import storage_key
from app.db.session import session_factory
from app.evidence.sides import ReadingSides, has_separate_architect_file
from app.models import (
    Document,
    DocumentVersion,
    DrawingView,
    PackageRevision,
    PackageRevisionDocument,
    Page,
    SourceArtifact,
    ViewRole,
    ViewRoleConfirmation,
    ViewRoleProposal,
)
from app.models.evidence import ObservationCandidate
from app.models.runs import ExtractionRun
from app.review import approval
from storage.local import LocalStore
from tests.app.postgres_fixture import alembic_config
from tests.extraction.architect.architect_sheet import architect_sheet, pasted_sheet
from tests.extraction.architect.combined_sheet import combined_sheet
from vocabulary.semantic_types import DocumentRole
from workflow.architect_pairing_records import architect_views
from workflow.architect_reader import ARCHITECT_EXTRACTOR
from workflow.view_roles import (
    CODE_CONFIRMER,
    CODE_DOCUMENT_CONFIRMER,
    CONTENT_VIEW_SOURCE,
    confirm_view_role,
)

pytest_plugins = ("tests.app.postgres_fixture",)


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


def _add_architectural(
    session: Session, store: LocalStore, revision: PackageRevision, data: bytes
) -> DocumentVersion:
    """The architect's drawings, uploaded in the architectural slot as their own file."""
    digest = hashlib.sha256(data).hexdigest()
    document = Document(package_id=revision.package_id, kind="architectural")
    session.add(document)
    session.flush()
    key = storage_key(document.id, digest)
    artifact = SourceArtifact(storage_key=key, sha256=digest, size=len(data))
    session.add(artifact)
    session.flush()
    store.put(key, io.BytesIO(data), content_type="application/pdf")
    version = DocumentVersion(
        document_id=document.id, source_artifact_id=artifact.id, sha256=digest, page_count=1
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
    session.commit()
    return version


def _package(
    session: Session, store: LocalStore, *, architect: bytes
) -> tuple[PackageRevision, DocumentVersion]:
    from tests.workflow.test_architect_reader import _upload

    revision = _upload(session, store, combined_sheet())
    return revision, _add_architectural(session, store, revision, architect)


def _extract(session: Session, store: LocalStore, revision: PackageRevision) -> None:
    from tests.workflow.test_architect_reader import _stages

    _stages(store).extract_pages(session, revision.id)
    session.commit()


def _architect_values(session: Session, version: DocumentVersion) -> list[ObservationCandidate]:
    return list(
        session.scalars(
            select(ObservationCandidate)
            .join(ExtractionRun, ExtractionRun.id == ObservationCandidate.extraction_run_id)
            .where(
                ExtractionRun.extractor == ARCHITECT_EXTRACTOR,
                ObservationCandidate.document_version_id == version.id,
            )
            .order_by(ObservationCandidate.created_at, ObservationCandidate.id)
        )
    )


def _by_text(rows: list[ObservationCandidate]) -> dict[str, ObservationCandidate]:
    return {row.raw_text.replace("’", "'"): row for row in rows}


def _page(session: Session, version: DocumentVersion) -> Page:
    return session.scalars(select(Page).where(Page.document_version_id == version.id)).one()


def test_a_separate_architect_file_is_read_and_its_view_confirmed_by_kind_and_content(
    session: Session, store: LocalStore
) -> None:
    revision, version = _package(session, store, architect=architect_sheet())

    _extract(session, store, revision)

    page = _page(session, version)
    (view,) = session.scalars(select(DrawingView).where(DrawingView.page_id == page.id)).all()
    assert view.tag == "view-1"
    assert view.role == ViewRole.ARCH.value
    (confirmation,) = session.scalars(
        select(ViewRoleConfirmation).where(ViewRoleConfirmation.drawing_view_id == view.id)
    ).all()
    assert confirmation.confirmed_by == CODE_DOCUMENT_CONFIRMER
    (proposal,) = session.scalars(
        select(ViewRoleProposal).where(ViewRoleProposal.drawing_view_id == view.id)
    ).all()
    assert proposal.source == CONTENT_VIEW_SOURCE
    assert proposal.heading is not None and "SYNTHETIC ELEVATION" in proposal.heading
    assert architect_views(session, page.id) == {1}


def test_its_values_are_stored_like_the_pasted_paths_with_both_judgments(
    session: Session, store: LocalStore
) -> None:
    revision, version = _package(session, store, architect=architect_sheet())

    _extract(session, store, revision)

    values = _by_text(_architect_values(session, version))
    for text, inches in (("3' - 4\"", 40), ("2' - 2\"", 26)):
        row = values[text]
        assert Fraction(row.value_numerator or 0, row.value_denominator or 1) == inches
        flags = row.ambiguity_flags or []
        assert "arch-view:1" in flags and "arch-view-tag:view-1" in flags
        assert "arch-ticks-on-outline:yes" in flags
        assert any(flag.startswith("arch-scale:1.5") for flag in flags)
        assert row.review_reason is None
    held = values["2' - 7\""]
    assert held.value_numerator is None
    assert held.review_reason is not None and "drawn length" in held.review_reason
    assert any(flag.startswith("arch-held:") for flag in held.ambiguity_flags or [])
    # Inside the view the architect's role was confirmed for: the architect's side.
    assert ReadingSides(session).of(values["3' - 4\""]) is DocumentRole.ARCH


def test_the_shop_file_is_still_read_as_before_beside_it(
    session: Session, store: LocalStore
) -> None:
    revision, _version = _package(session, store, architect=architect_sheet())

    _extract(session, store, revision)

    shop_views = {
        view.tag: view
        for view in session.scalars(
            select(DrawingView)
            .join(Page, Page.id == DrawingView.page_id)
            .join(DocumentVersion, DocumentVersion.id == Page.document_version_id)
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(Document.kind == "shop")
        )
    }
    assert shop_views["panel-1"].role == ViewRole.ARCH.value
    assert shop_views["panel-3"].role == ViewRole.SHOP.value
    confirmers = {
        row.confirmed_by
        for row in session.scalars(
            select(ViewRoleConfirmation).where(
                ViewRoleConfirmation.drawing_view_id.in_([v.id for v in shop_views.values()])
            )
        )
    }
    assert confirmers == {CODE_CONFIRMER}


def test_an_architect_file_of_pasted_drawings_on_a_shifted_page_is_read(
    session: Session, store: LocalStore
) -> None:
    """A sheet cut out of a larger set, its drawing pasted (no heading): read without the crop
    crash, its role from the document's kind and its content."""
    revision, version = _package(session, store, architect=pasted_sheet(origin=(300, 400)))

    _extract(session, store, revision)

    page = _page(session, version)
    (view,) = session.scalars(select(DrawingView).where(DrawingView.page_id == page.id)).all()
    assert view.tag == "panel-0" and view.role == ViewRole.ARCH.value
    (confirmation,) = session.scalars(
        select(ViewRoleConfirmation).where(ViewRoleConfirmation.drawing_view_id == view.id)
    ).all()
    assert confirmation.confirmed_by == CODE_DOCUMENT_CONFIRMER
    values = _by_text(_architect_values(session, version))
    assert values["3' - 4\""].value_numerator == 40
    assert not any(f.startswith("arch-view-tag:") for f in values["3' - 4\""].ambiguity_flags or [])


def test_a_crowded_architect_sheet_confirms_nothing_and_stores_no_value(
    session: Session, store: LocalStore
) -> None:
    revision, version = _package(session, store, architect=architect_sheet(views=2, crowded=True))

    _extract(session, store, revision)

    page = _page(session, version)
    views = session.scalars(select(DrawingView).where(DrawingView.page_id == page.id)).all()
    assert all(view.role is None for view in views)
    assert _architect_values(session, version) == []
    assert architect_views(session, page.id) == set()


def test_two_views_on_one_architect_sheet_are_two_stored_views(
    session: Session, store: LocalStore
) -> None:
    revision, version = _package(session, store, architect=architect_sheet(views=2))

    _extract(session, store, revision)

    page = _page(session, version)
    tags = sorted(
        view.tag
        for view in session.scalars(select(DrawingView).where(DrawingView.page_id == page.id))
    )
    assert tags == ["view-1", "view-2"]
    assert architect_views(session, page.id) == {1, 2}
    by_view: dict[str, list[str]] = {}
    for row in _architect_values(session, version):
        tag = next(f for f in row.ambiguity_flags or [] if f.startswith("arch-view-tag:"))
        by_view.setdefault(tag, []).append(row.raw_text)
    assert set(by_view) == {"arch-view-tag:view-1", "arch-view-tag:view-2"}


def test_a_rerun_neither_repeats_nor_overrides_a_persons_choice(
    session: Session, store: LocalStore
) -> None:
    revision, version = _package(session, store, architect=architect_sheet())
    _extract(session, store, revision)
    page = _page(session, version)
    (view,) = session.scalars(select(DrawingView).where(DrawingView.page_id == page.id)).all()
    confirm_view_role(session, view=view, role=ViewRole.SHOP, actor="reviewer@example.com")
    session.commit()

    _extract(session, store, revision)

    session.refresh(view)
    assert view.role == ViewRole.SHOP.value
    assert architect_views(session, page.id) == set()
    assert (
        session.scalars(
            select(ViewRoleConfirmation).where(
                ViewRoleConfirmation.confirmed_by == CODE_DOCUMENT_CONFIRMER
            )
        ).all()
        != []
    )


def test_the_package_still_says_the_separate_file_is_not_compared_yet(
    session: Session, store: LocalStore
) -> None:
    """Reading is not matching (Phase 3): the Phase 0 notice keeps its ground (#1161)."""
    revision, _version = _package(session, store, architect=architect_sheet())

    _extract(session, store, revision)

    assert has_separate_architect_file(session, revision.id)


def test_a_code_confirmation_by_kind_is_not_a_reviewers_input_at_sign_off() -> None:
    assert CODE_DOCUMENT_CONFIRMER in approval._CODE_CONFIRMERS


def test_a_page_holding_both_a_panel_and_a_content_view_of_one_number_counts_neither(
    session: Session, store: LocalStore
) -> None:
    """`arch-view:<n>` names either; with both on one page, which one a value came from would be a
    guess, so neither counts."""
    revision, version = _package(session, store, architect=architect_sheet())
    _extract(session, store, revision)
    page = _page(session, version)
    clash = DrawingView(page_id=page.id, tag="panel-1", region={"space": "stored", "points": []})
    session.add(clash)
    session.flush()
    confirm_view_role(session, view=clash, role=ViewRole.ARCH, actor="reviewer@example.com")
    session.commit()

    assert architect_views(session, page.id) == set()
