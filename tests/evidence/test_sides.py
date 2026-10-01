"""A reading's side comes from the confirmed drawing it sits in, not from the upload (#795).

Verification for: `app/evidence/sides.py`, and its four callers — the reviewer's label
(`app/evidence/confirm.py`), the readings the Measure page offers (`app/api/confirmations.py`), the
form-filler (`workflow/propose.py`) and, through the same function, the exact-tag lane.

The one that matters most is
`test_a_label_on_the_architects_half_of_a_sheet_uploaded_as_shop_is_the_architects`: the #795 hazard.
A combined sheet is uploaded as `shop`. Before #795 every reading on it was the vendor's, so the
architect's right number could pass the vendor's wrong drawing.

The sheet is 200 x 100 pt with `24"` on its left half and `36"` on its right; the two halves are
recorded as drawing views the way the panel step records a stamp (`record_panel_view`). A third
reading, `30"`, sits across the line between them.
"""

from __future__ import annotations

import hashlib
import io
import tempfile
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.api.documents import storage_key
from app.db.session import session_factory
from app.evidence.confirm import ConfirmationRefused, RefusalReason, confirm_candidate_type
from app.evidence.sides import MARKUP_ROUTE, ReadingSides, SideRefusal, SideRefusalReason
from app.models import (
    CanonicalObservation,
    Document,
    DocumentVersion,
    DrawingView,
    ObservationCandidate,
    Package,
    PackageRevision,
    PackageRevisionDocument,
    PackageState,
    Page,
    Project,
    SourceArtifact,
    ViewRole,
)
from app.models.runs import TaskRun, WorkflowRun
from storage.local import LocalStore
from tests.api.test_drawing_views import _client
from tests.evidence.test_bridge import _upgrade
from tests.extraction.test_annotations import _free_text
from tests.extraction.test_annotations import _pdf as _annotated_pdf
from tests.extraction.test_reader import _pdf
from vocabulary.semantic_types import DocumentRole
from workflow.idempotency import stage_idempotency_key
from workflow.propose import assignment_readings
from workflow.review import ENGINE_VERSION
from workflow.stages import DatabaseStages
from workflow.view_roles import confirm_view_role, record_panel_view

pytest_plugins = ("tests.app.postgres_fixture",)

SHEET = _pdf(
    b'BT /F1 10 Tf 1 0 0 1 20 70 Tm (24") Tj ET\n'
    b'BT /F1 10 Tf 1 0 0 1 140 70 Tm (36") Tj ET\n'
    b'BT /F1 10 Tf 1 0 0 1 93 30 Tm (30") Tj ET\n'
)

LEFT_HALF = (
    (Decimal(0), Decimal(0)),
    (Decimal("0.5"), Decimal(0)),
    (Decimal("0.5"), Decimal(1)),
    (Decimal(0), Decimal(1)),
)
RIGHT_HALF = (
    (Decimal("0.5"), Decimal(0)),
    (Decimal(1), Decimal(0)),
    (Decimal(1), Decimal(1)),
    (Decimal("0.5"), Decimal(1)),
)


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


def _read(
    session: Session,
    store: LocalStore,
    *,
    kind: str,
    also: str | None = None,
    data: bytes = SHEET,
) -> PackageRevision:
    """The sheet uploaded as one document of `kind`, and read — with, given `also`, a second
    drawing of that kind in the same package, the way a two-PDF package arrives."""
    digest = hashlib.sha256(data).hexdigest()
    project = Project(name=f"sides {uuid4()}")
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
    if also is not None:
        other = Document(package_id=package.id, kind=also)
        session.add(other)
        session.flush()
        other_data = _pdf(b"BT /F1 10 Tf 1 0 0 1 20 70 Tm (ELEVATION) Tj ET\n")
        other_digest = hashlib.sha256(other_data).hexdigest()
        other_key = storage_key(other.id, other_digest)
        store.put(other_key, io.BytesIO(other_data), content_type="application/pdf")
        other_artifact = SourceArtifact(
            storage_key=other_key, sha256=other_digest, size=len(other_data)
        )
        session.add(other_artifact)
        session.flush()
        other_version = DocumentVersion(
            document_id=other.id,
            source_artifact_id=other_artifact.id,
            sha256=other_digest,
            page_count=1,
        )
        session.add(other_version)
        session.flush()
        session.add(
            PackageRevisionDocument(
                package_revision_id=revision.id,
                package_id=package.id,
                document_id=other.id,
                document_version_id=other_version.id,
            )
        )
        session.flush()
    DatabaseStages(store).extract_pages(session, revision.id)
    return revision


def _page(session: Session, revision: PackageRevision, data: bytes = SHEET) -> Page:
    """The sheet's page — not the second drawing's, in a two-PDF package."""
    return session.execute(
        select(Page)
        .join(DocumentVersion, DocumentVersion.id == Page.document_version_id)
        .join(
            PackageRevisionDocument,
            PackageRevisionDocument.document_version_id == Page.document_version_id,
        )
        .where(
            PackageRevisionDocument.package_revision_id == revision.id,
            DocumentVersion.sha256 == hashlib.sha256(data).hexdigest(),
        )
    ).scalar_one()


def _halves(
    session: Session, revision: PackageRevision, *, left: ViewRole | None, right: ViewRole | None
) -> None:
    """Record the sheet's two halves as drawings, and confirm each role given."""
    page = _page(session, revision)
    for index, (region, role) in enumerate(((LEFT_HALF, left), (RIGHT_HALF, right))):
        view = record_panel_view(
            session,
            page_id=page.id,
            annotation_index=index,
            stored_points=region,
            proposed_role=None,
            heading=None,
            reason="a half of the test sheet",
        )
        if role is not None:
            confirm_view_role(session, view=view, role=role, actor="a reviewer")


def _one_drawing(
    session: Session, revision: PackageRevision, role: ViewRole | None, data: bytes = SHEET
) -> None:
    """The whole sheet recorded as one drawing, as a `/Stamp` filling the page is."""
    view = record_panel_view(
        session,
        page_id=_page(session, revision, data).id,
        annotation_index=0,
        stored_points=(
            (Decimal(0), Decimal(0)),
            (Decimal(1), Decimal(0)),
            (Decimal(1), Decimal(1)),
            (Decimal(0), Decimal(1)),
        ),
        proposed_role=None,
        heading=None,
        reason="the whole test sheet",
    )
    if role is not None:
        confirm_view_role(session, view=view, role=role, actor="a reviewer")


def _reading(
    session: Session, revision: PackageRevision, text: str, data: bytes = SHEET
) -> ObservationCandidate:
    page = _page(session, revision, data)
    candidate = (
        session.execute(
            select(ObservationCandidate)
            .where(
                ObservationCandidate.page_id == page.id,
                ObservationCandidate.raw_text == text,
                ObservationCandidate.value_numerator.is_not(None),
            )
            .order_by(ObservationCandidate.created_at, ObservationCandidate.id)
        )
        .scalars()
        .first()
    )
    assert candidate is not None, text
    return candidate


def _side(session: Session, revision: PackageRevision, text: str) -> DocumentRole | SideRefusal:
    return ReadingSides(session).of(_reading(session, revision, text))


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


def test_on_a_sheet_with_drawings_each_reading_takes_its_drawings_confirmed_role(
    session: Session, store: LocalStore
) -> None:
    """Uploaded as `shop`; the left half confirmed the architect's. Outcome: `24"` is ARCH and `36"`
    SHOP — the upload's kind decides neither."""
    revision = _read(session, store, kind="shop")
    _halves(session, revision, left=ViewRole.ARCH, right=ViewRole.SHOP)

    assert _side(session, revision, '24"') is DocumentRole.ARCH
    assert _side(session, revision, '36"') is DocumentRole.SHOP


def test_a_reading_on_a_drawing_nobody_has_confirmed_has_no_side(
    session: Session, store: LocalStore
) -> None:
    """Outcome: refused, saying what to do — not the upload's kind as a stand-in."""
    revision = _read(session, store, kind="shop")
    _halves(session, revision, left=None, right=ViewRole.SHOP)

    side = _side(session, revision, '24"')
    assert isinstance(side, SideRefusal)
    assert side.reason is SideRefusalReason.VIEW_ROLE_UNCONFIRMED
    assert "confirm whether this drawing is the architect's or the vendor's" in side.detail


def test_a_reading_across_two_drawings_has_no_side(session: Session, store: LocalStore) -> None:
    """`30"` sits across the line between the halves. Outcome: refused, never given either side."""
    revision = _read(session, store, kind="shop")
    _halves(session, revision, left=ViewRole.ARCH, right=ViewRole.SHOP)

    side = _side(session, revision, '30"')
    assert isinstance(side, SideRefusal)
    assert side.reason is SideRefusalReason.NOT_IN_ONE_VIEW


@pytest.mark.parametrize(
    ("kind", "expected"),
    [("architectural", DocumentRole.ARCH), ("shop", DocumentRole.SHOP)],
)
def test_a_page_with_no_drawings_takes_the_uploads_kind(
    session: Session, store: LocalStore, kind: str, expected: DocumentRole
) -> None:
    """**A two-PDF package is unchanged.** Outcome: the upload's kind, as before #795."""
    revision = _read(session, store, kind=kind)

    assert _side(session, revision, '24"') is expected


def test_a_schedule_reading_takes_no_part(session: Session, store: LocalStore) -> None:
    revision = _read(session, store, kind="schedule")

    side = _side(session, revision, '24"')
    assert isinstance(side, SideRefusal)
    assert side.reason is SideRefusalReason.NOT_COMPARED


def test_a_one_drawing_page_in_a_two_pdf_package_takes_the_uploads_side_until_confirmed(
    session: Session, store: LocalStore
) -> None:
    """**The admin's fallback (2026-10-01).** A genuine two-PDF package's drawings sit in `/Stamp`s,
    so they are views; asking for a confirmation of each would leave the form empty. Outcome: the
    shop file's one drawing is SHOP while nobody has said otherwise."""
    revision = _read(session, store, kind="shop", also="architectural")
    _one_drawing(session, revision, None)

    assert _side(session, revision, '24"') is DocumentRole.SHOP


def test_a_confirmation_wins_over_the_uploads_side(session: Session, store: LocalStore) -> None:
    revision = _read(session, store, kind="shop", also="architectural")
    _one_drawing(session, revision, ViewRole.ARCH)

    assert _side(session, revision, '24"') is DocumentRole.ARCH


def test_a_two_drawing_page_never_takes_the_uploads_side_even_in_a_two_pdf_package(
    session: Session, store: LocalStore
) -> None:
    """**What keeps the fallback safe.** A combined sheet mixed into a two-PDF package still holds
    the architect's drawing beside the vendor's. Outcome: refused until a person says which."""
    revision = _read(session, store, kind="shop", also="architectural")
    _halves(session, revision, left=None, right=None)

    side = _side(session, revision, '24"')
    assert isinstance(side, SideRefusal)
    assert side.reason is SideRefusalReason.VIEW_ROLE_UNCONFIRMED


def test_a_one_drawing_page_of_a_lone_combined_set_does_not_take_the_uploads_side(
    session: Session, store: LocalStore
) -> None:
    """A package with only one document has no second file to say this one is the other side.
    Outcome: refused — a combined set's vendor-only sheet is confirmed like any other."""
    revision = _read(session, store, kind="shop")
    _one_drawing(session, revision, None)

    side = _side(session, revision, '24"')
    assert isinstance(side, SideRefusal)
    assert side.reason is SideRefusalReason.VIEW_ROLE_UNCONFIRMED


# ---------------------------------------------------------------------------
# Its callers
# ---------------------------------------------------------------------------


def test_a_label_on_the_architects_half_of_a_sheet_uploaded_as_shop_is_the_architects(
    session: Session, store: LocalStore
) -> None:
    """**The point.** Outcome: the reviewer's label records ARCH, so no SHOP check can use it."""
    revision = _read(session, store, kind="shop")
    _halves(session, revision, left=ViewRole.ARCH, right=ViewRole.SHOP)

    observation = confirm_candidate_type(
        session,
        candidate_id=_reading(session, revision, '24"').id,
        semantic_type="cabinet_width",
        confirmed_by="a reviewer",
    )

    assert isinstance(observation, CanonicalObservation)
    assert observation.document_role == DocumentRole.ARCH.value


def test_labelling_a_reading_on_an_unconfirmed_drawing_is_refused_with_the_reason(
    session: Session, store: LocalStore
) -> None:
    revision = _read(session, store, kind="shop")
    _halves(session, revision, left=None, right=None)

    refused = confirm_candidate_type(
        session,
        candidate_id=_reading(session, revision, '24"').id,
        semantic_type="cabinet_width",
        confirmed_by="a reviewer",
    )

    assert isinstance(refused, ConfirmationRefused)
    assert refused.reason is RefusalReason.VIEW_ROLE_UNCONFIRMED
    assert session.execute(select(CanonicalObservation)).scalars().all() == []


def test_the_form_filler_uses_each_readings_drawing_and_never_an_unconfirmed_one(
    session: Session, store: LocalStore
) -> None:
    """**The path #795 would otherwise leave open.** An accepted proposal reaches the checks even
    when its confirmation fails, so the form-filler must not put a reading in a field by a guessed
    side. Outcome: `24"` on the unconfirmed left half is not offered; `36"` on the vendor's is SHOP.
    """
    revision = _read(session, store, kind="architectural")
    _halves(session, revision, left=None, right=ViewRole.SHOP)

    offered = {reading.value: reading.source for reading in assignment_readings(session, revision)}

    assert offered == {"36 in": "SHOP"}


def test_the_measure_page_is_told_each_readings_side_and_why_one_has_none(
    session: Session, store: LocalStore
) -> None:
    """Outcome: the candidates list carries the drawing's side, and for a reading on an unconfirmed
    drawing no side and the sentence that says what to do."""
    revision = _read(session, store, kind="shop")
    _halves(session, revision, left=None, right=ViewRole.SHOP)
    session.commit()
    package = session.get(Package, revision.package_id)
    assert package is not None

    response = _client(session, store, package.project_id).get(
        f"/api/v1/projects/{package.project_id}/packages/{package.id}/candidates"
    )

    assert response.status_code == 200, response.text
    by_text: dict[str, Any] = {item["raw_text"]: item for item in response.json()["candidates"]}
    assert (by_text['36"']["source"], by_text['36"']["source_refusal"]) == ("SHOP", None)
    assert by_text['24"']["source"] is None
    assert "confirm whether this drawing" in by_text['24"']["source_refusal"]
    assert session.execute(select(DrawingView)).scalars().all()


@pytest.mark.parametrize(
    ("also", "expected"),
    [("architectural", "shop"), (None, None)],
)
def test_the_drawings_list_says_which_drawings_the_upload_decides(
    session: Session, store: LocalStore, also: str | None, expected: str | None
) -> None:
    """Outcome: in a two-PDF package the shop file's one drawing is listed as decided by the upload;
    alone, it is not — so the Measure page asks only about the drawings that need a person."""
    revision = _read(session, store, kind="shop", also=also)
    _one_drawing(session, revision, None)
    session.commit()
    package = session.get(Package, revision.package_id)
    assert package is not None

    response = _client(session, store, package.project_id).get(
        f"/api/v1/projects/{package.project_id}/packages/{package.id}/views"
    )

    assert response.status_code == 200, response.text
    (view,) = response.json()["views"]
    assert (view["role"], view["upload_side"]) == (None, expected)


# ---------------------------------------------------------------------------
# A reviewer's markup (#802)
# ---------------------------------------------------------------------------

#: A sheet whose only reading is a reviewer's note: a box writing a number over the drawing, the
#: way the client's reviewer writes the architect's number over the vendor's.
MARKUP_SHEET = _annotated_pdf(annotations=[_free_text('25 1/2"', rect=b"[40 40 120 60]")])


def _markup_on_a_confirmed_vendor_drawing(
    session: Session, store: LocalStore
) -> tuple[PackageRevision, ObservationCandidate]:
    revision = _read(session, store, kind="shop", data=MARKUP_SHEET)
    _one_drawing(session, revision, ViewRole.SHOP, data=MARKUP_SHEET)
    return revision, _reading(session, revision, '25 1/2"', data=MARKUP_SHEET)


def test_a_reviewers_markup_has_no_side_even_on_a_confirmed_vendor_drawing(
    session: Session, store: LocalStore
) -> None:
    """**#802, the one that matters most.** The note sits on a drawing a person confirmed as the
    vendor's. Outcome: no side — a reviewer's correction is never the vendor's reading, so no check
    can PASS the vendor's drawing on the number the reviewer wrote over it."""
    _revision, markup = _markup_on_a_confirmed_vendor_drawing(session, store)

    side = ReadingSides(session).of(markup)

    assert isinstance(side, SideRefusal)
    assert side.reason is SideRefusalReason.MARKUP


def test_labelling_a_reviewers_markup_is_refused(session: Session, store: LocalStore) -> None:
    _revision, markup = _markup_on_a_confirmed_vendor_drawing(session, store)

    refused = confirm_candidate_type(
        session, candidate_id=markup.id, semantic_type="CT010", confirmed_by="a reviewer"
    )

    assert isinstance(refused, ConfirmationRefused)
    assert refused.reason is RefusalReason.REVIEWER_MARKUP
    assert session.execute(select(CanonicalObservation)).scalars().all() == []


def test_the_form_filler_never_offers_a_reviewers_markup(
    session: Session, store: LocalStore
) -> None:
    revision, _markup = _markup_on_a_confirmed_vendor_drawing(session, store)

    assert assignment_readings(session, revision) == ()


def test_the_measure_page_says_a_markup_reading_is_neither_drawing(
    session: Session, store: LocalStore
) -> None:
    revision, _markup = _markup_on_a_confirmed_vendor_drawing(session, store)
    session.commit()
    package = session.get(Package, revision.package_id)
    assert package is not None

    response = _client(session, store, package.project_id).get(
        f"/api/v1/projects/{package.project_id}/packages/{package.id}/candidates"
    )

    assert response.status_code == 200, response.text
    (item,) = response.json()["candidates"]
    assert item["source"] is None
    assert "reviewer's markup" in item["source_refusal"]


def test_the_markup_route_named_here_is_the_one_the_stage_records() -> None:
    """**The drift guard.** `sides.py` restates the route name because it may not import the stage;
    a renamed route would otherwise quietly hand markup a side again."""
    from workflow.stages import MARKUP_EXTRACTOR

    assert MARKUP_ROUTE == MARKUP_EXTRACTOR


def test_every_reason_a_reading_has_no_side_is_a_reason_a_label_is_refused() -> None:
    """A reason with no mapping would raise inside a reviewer's click rather than refuse it."""
    from app.evidence.confirm import _SIDE_REFUSAL

    assert set(_SIDE_REFUSAL) == set(SideRefusalReason)
