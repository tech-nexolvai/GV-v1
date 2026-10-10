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
from collections.abc import Iterator, Sequence
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, select
from sqlalchemy import text as sql
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from alembic import command
from app.api.documents import storage_key
from app.db.session import session_factory
from app.evidence.record import open_extraction_run
from app.evidence.sides import CONTENT_VIEW_PREFIX, ReadingSides, has_separate_architect_file
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
from extraction.architect.reader import MEASURED_ARCHITECT_SETTINGS, read_architect_page
from extraction.reader import UnreadablePdf
from storage.local import LocalStore
from tests.app.postgres_fixture import alembic_config
from tests.extraction.architect.architect_sheet import architect_sheet, pasted_sheet
from tests.extraction.architect.combined_sheet import combined_sheet
from vocabulary.semantic_types import DocumentRole
from workflow.architect_page_notes import architect_page_notes_for
from workflow.architect_pairing_records import _eligible, architect_view_tags, architect_views
from workflow.architect_reader import ARCHITECT_EXTRACTOR, persist_architect_pages
from workflow.review import PageResult
from workflow.view_roles import (
    CODE_CONFIRMER,
    CODE_DOCUMENT_CONFIRMER,
    CONTENT_VIEW_SOURCE,
    confirm_view_role,
    content_view_tag,
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


def _extract(
    session: Session, store: LocalStore, revision: PackageRevision
) -> Sequence[PageResult]:
    from tests.workflow.test_architect_reader import _stages

    results = _stages(store).extract_pages(session, revision.id)
    session.commit()
    return results


def _architect_notes(results: Sequence[PageResult], version: DocumentVersion) -> dict[str, Any]:
    (result,) = [
        result for result in results if result.payload.get("document_version_id") == str(version.id)
    ]
    notes = result.payload.get("architect", {})
    assert isinstance(notes, dict)
    return notes


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


def test_an_architect_file_of_pasted_drawings_keeps_the_heading_rule_and_says_so(
    session: Session, store: LocalStore
) -> None:
    """A sheet cut out of a larger set, its drawing pasted with no heading: read without the crop
    crash. A pasted drawing keeps the heading + content rule on the architect's file too (#1163
    review), so it gets no role; it is recorded with its reason and its values stored held."""
    revision, version = _package(session, store, architect=pasted_sheet(origin=(300, 400)))

    results = _extract(session, store, revision)

    page = _page(session, version)
    (view,) = session.scalars(select(DrawingView).where(DrawingView.page_id == page.id)).all()
    assert view.tag == "panel-0" and view.role is None
    assert (
        session.scalars(
            select(ViewRoleConfirmation).where(ViewRoleConfirmation.drawing_view_id == view.id)
        ).all()
        == []
    )
    values = _by_text(_architect_values(session, version))
    held = values["3' - 4\""]
    assert held.value_numerator is None
    assert held.review_reason is not None and "not decided by code" in held.review_reason
    (noted,) = _architect_notes(results, version)["views_without_role"]
    assert noted["view"] == "panel-0"


def test_a_crowded_architect_sheet_records_its_views_and_holds_every_value(
    session: Session, store: LocalStore
) -> None:
    """Never silent: two views not clearly apart are recorded with the reason, no role, and their
    values stored held."""
    revision, version = _package(session, store, architect=architect_sheet(views=2, crowded=True))

    results = _extract(session, store, revision)

    page = _page(session, version)
    views = session.scalars(select(DrawingView).where(DrawingView.page_id == page.id)).all()
    assert sorted(view.tag for view in views) == ["view-1", "view-2"]
    assert all(view.role is None for view in views)
    proposals = session.scalars(
        select(ViewRoleProposal).where(ViewRoleProposal.drawing_view_id.in_([v.id for v in views]))
    ).all()
    assert len(proposals) == 2
    assert all("not clearly apart" in proposal.reason for proposal in proposals)
    stored = _architect_values(session, version)
    assert stored
    assert all(row.value_numerator is None and row.review_reason for row in stored)
    assert architect_views(session, page.id) == set()
    noted = _architect_notes(results, version)["views_without_role"]
    assert sorted(entry["view"] for entry in noted) == ["view-1", "view-2"]


def test_a_page_with_no_view_says_why_and_the_reason_is_stored(
    session: Session, store: LocalStore
) -> None:
    revision, version = _package(session, store, architect=architect_sheet(titled=False))

    results = _extract(session, store, revision)

    (stored,) = architect_page_notes_for(session, revision.id)
    assert stored.kind == "no_view_found"
    assert stored.text.startswith("no view found")
    assert stored.document_version_id == version.id and stored.page_index == 0
    run = session.get(ExtractionRun, stored.extraction_run_id)
    assert run is not None and run.extractor == ARCHITECT_EXTRACTOR
    (note,) = _architect_notes(results, version)["page_notes"]
    assert note["kind"] == "no_view_found"
    assert _architect_values(session, version) == []


def test_a_stamp_holding_no_drawing_is_stored_as_a_note(
    session: Session, store: LocalStore
) -> None:
    revision, _version = _package(session, store, architect=architect_sheet(approval_stamp=True))

    _extract(session, store, revision)

    kinds = [note.kind for note in architect_page_notes_for(session, revision.id)]
    assert "stamp_not_drawing" in kinds


def test_a_page_the_reader_cannot_read_is_stored_as_a_note(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    revision, version = _package(session, store, architect=architect_sheet())
    import workflow.stages as stages_module

    real = read_architect_page

    def refuse(data: bytes, index: int, **kwargs: Any) -> Any:
        if kwargs.get("architect_document"):
            raise UnreadablePdf("a synthetic refusal")
        return real(data, index, **kwargs)

    monkeypatch.setattr(stages_module, "read_architect_page", refuse)

    _extract(session, store, revision)

    (note,) = [
        note
        for note in architect_page_notes_for(session, revision.id)
        if note.document_version_id == version.id
    ]
    assert note.kind == "page_unreadable" and "a synthetic refusal" in note.text


def test_the_notes_are_append_only(session: Session, store: LocalStore) -> None:
    revision, _version = _package(session, store, architect=architect_sheet(titled=False))
    _extract(session, store, revision)
    assert architect_page_notes_for(session, revision.id)

    for statement in (
        "UPDATE architect_page_notes SET text = 'x'",
        "DELETE FROM architect_page_notes",
    ):
        with pytest.raises(DBAPIError, match="append-only"):
            session.execute(sql(statement))
        session.rollback()


def test_a_combined_sheet_uploaded_as_the_architects_file_gives_its_own_drawing_no_code_role(
    session: Session, store: LocalStore
) -> None:
    """A vendor's sheet whose own drawing is page content in feet and inches with an architectural
    scale, beside the architect's drawing pasted under its heading, uploaded in the architect's
    slot with other bytes: no code architect role for the page's own drawing."""
    revision, version = _package(
        session, store, architect=architect_sheet(pasted_with_heading=True)
    )

    _extract(session, store, revision)

    page = _page(session, version)
    views = {
        view.tag: view
        for view in session.scalars(select(DrawingView).where(DrawingView.page_id == page.id))
    }
    assert views["view-1"].role is None
    assert views["panel-1"].role == ViewRole.ARCH.value
    assert architect_view_tags(session, page.id) == {"panel-1"}
    own = [
        row
        for row in _architect_values(session, version)
        if "arch-view-tag:view-1" in (row.ambiguity_flags or [])
    ]
    assert own and all(row.value_numerator is None for row in own)
    assert all("combined sheet" in (row.review_reason or "") for row in own)


def test_a_renumbered_view_never_reuses_another_drawings_stored_view(
    session: Session, store: LocalStore
) -> None:
    """`view-1` stored before somewhere else on the page (the views were renumbered): a new read
    refuses it and reports it, never hanging the old view's role or region on this drawing."""
    data = architect_sheet()
    revision, version = _package(session, store, architect=data)
    _extract(session, store, revision)
    page = _page(session, version)
    (view,) = session.scalars(select(DrawingView).where(DrawingView.page_id == page.id)).all()
    view.region = {
        "space": "stored",
        "points": [["0.80", "0.80"], ["0.95", "0.80"], ["0.95", "0.95"], ["0.80", "0.95"]],
    }
    first = session.get(ExtractionRun, _architect_values(session, version)[0].extraction_run_id)
    assert first is not None
    newer = open_extraction_run(
        session,
        task_run_id=first.task_run_id,
        extractor=ARCHITECT_EXTRACTOR,
        extractor_version="architect-text-test",
        config_hash="a newer read",
        dpi=150,
    )
    reading = read_architect_page(
        data, 0, settings=MEASURED_ARCHITECT_SETTINGS, dpi=150, architect_document=True
    )

    counts = persist_architect_pages(
        session,
        document_version_id=version.id,
        extraction_run_id=newer.id,
        pages=[(page, reading)],
        architect_document=True,
    )

    (refused,) = counts.refused_views
    assert refused["view"] == "view-1" and "renumbered" in str(refused["reason"])
    assert counts.views_confirmed_by_code == 0
    session.refresh(view)
    region = view.region
    points = region.get("points") if isinstance(region, dict) else None
    assert isinstance(points, list) and points[0] == ["0.80", "0.80"]
    # Never silent: a stored note, and the view's values stored held under a tag no view has.
    (note,) = [
        n for n in architect_page_notes_for(session, revision.id) if n.kind == "view_refused"
    ]
    assert "view-1" in note.text and note.extraction_run_id == newer.id
    refused_values = list(
        session.scalars(
            select(ObservationCandidate).where(ObservationCandidate.extraction_run_id == newer.id)
        )
    )
    assert counts.candidates == len(refused_values) > 0
    assert all(row.value_numerator is None for row in refused_values)
    assert all(
        "arch-view-tag:view-1~refused" in (row.ambiguity_flags or []) for row in refused_values
    )
    tags = architect_view_tags(session, page.id) | {"view-1"}
    assert all(_eligible(row, tags).held_reason is not None for row in refused_values)


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


def test_a_panel_and_a_content_view_of_one_number_are_told_apart_by_tag(
    session: Session, store: LocalStore
) -> None:
    """`arch-view:<n>` names either a pasted drawing or a view drawn as content; eligibility goes by
    the very view a value was read in (`arch-view-tag`), so a `panel-1` a person said is the
    vendor's neither hides nor lends its role to `view-1`."""
    revision, version = _package(session, store, architect=architect_sheet())
    _extract(session, store, revision)
    page = _page(session, version)
    clash = DrawingView(page_id=page.id, tag="panel-1", region={"space": "stored", "points": []})
    session.add(clash)
    session.flush()
    confirm_view_role(session, view=clash, role=ViewRole.SHOP, actor="reviewer@example.com")
    session.commit()

    assert architect_view_tags(session, page.id) == {"view-1"}
    # The bare number is ambiguous now: it counts for neither.
    assert architect_views(session, page.id) == set()
    content_span = _by_text(_architect_values(session, version))["3' - 4\""]
    assert _eligible(content_span, {"view-1"}).held_reason is None
    pasted_span = ObservationCandidate(
        document_version_id=version.id,
        page_id=page.id,
        extraction_run_id=content_span.extraction_run_id,
        raw_text="3' - 4\"",
        value_numerator=40,
        value_denominator=1,
        unit="in",
        polygon=[[0, 0], [1, 0], [1, 1], [0, 1]],
        coordinate_space="image",
        ambiguity_flags=["architect-reader", "arch-view:1", "arch-row:9", "arch-slot:0"],
    )
    assert _eligible(pasted_span, {"view-1"}).held_reason is not None


def test_the_sides_prefix_names_the_content_views_tag() -> None:
    assert content_view_tag(7).startswith(CONTENT_VIEW_PREFIX)


def _reading(
    session: Session, version: DocumentVersion, polygon: list[list[int]]
) -> ObservationCandidate:
    """Another extractor's reading on the architect's page, placed by the architect run's frame."""
    run_id = _architect_values(session, version)[0].extraction_run_id
    reading = ObservationCandidate(
        document_version_id=version.id,
        page_id=_page(session, version).id,
        extraction_run_id=run_id,
        raw_text="NOTE",
        polygon=polygon,
        coordinate_space="image",
        ambiguity_flags=[],
    )
    session.add(reading)
    session.flush()
    return reading


#: In the title block (well outside any view) and inside the first drawing, at 150 dpi.
_TITLE_BLOCK = [[80, 1540], [200, 1540], [200, 1570], [80, 1570]]
_IN_DRAWING = [[240, 840], [260, 840], [260, 860], [240, 860]]


@pytest.mark.parametrize("crowded", [False, True])
def test_views_on_the_architects_file_change_no_side_outside_them(
    session: Session, store: LocalStore, crowded: bool
) -> None:
    """A clean sheet and a crowded one behave the same: outside the views, and inside one with no
    role, a reading keeps its document's side, as before the views were read."""
    sheet = architect_sheet(views=2, crowded=True) if crowded else architect_sheet()
    revision, version = _package(session, store, architect=sheet)
    _extract(session, store, revision)
    if crowded:
        # Give it a stored value to place readings by (every value on it is held).
        assert _architect_values(session, version)

    sides = ReadingSides(session)

    assert sides.of(_reading(session, version, _TITLE_BLOCK)) is DocumentRole.ARCH
    assert sides.of(_reading(session, version, _IN_DRAWING)) is DocumentRole.ARCH


def test_a_role_given_to_a_view_on_the_architects_file_decides_inside_it(
    session: Session, store: LocalStore
) -> None:
    revision, version = _package(session, store, architect=architect_sheet())
    _extract(session, store, revision)
    page = _page(session, version)
    (view,) = session.scalars(select(DrawingView).where(DrawingView.page_id == page.id)).all()
    confirm_view_role(session, view=view, role=ViewRole.SHOP, actor="reviewer@example.com")
    session.commit()

    sides = ReadingSides(session)

    assert sides.of(_reading(session, version, _IN_DRAWING)) is DocumentRole.SHOP
    assert sides.of(_reading(session, version, _TITLE_BLOCK)) is DocumentRole.ARCH
