"""The vendor-only view of an evidence region (#952), through the API a reviewer's screen calls.

The sheet is invented (`tests/workflow/test_evidence_crop_gv_marks.py`): a vendor label covered by a
solid red reviewer note, and a second vendor label away from it. The worker reads it, cuts the
evidence crops (both layers, as a person is shown them) and renders the vendor-only page picture
(#948). What is checked:

- the covered label: the original crop shows GV's red note; the vendor-only view of the same region
  shows the vendor's label and no red, and says it is the vendor's drawing without GV markup;
- the far label: the vendor-only view is **pixel-identical** to the original crop, which proves the
  stored region lands on the same pixels through the published transform;
- no vendor-only picture yet: a plain "not available" state, and the picture route says so;
- stored rows that disagree about their page are refused, never shown;
- another project's reading is 404;
- reading these routes changes no row and queues no work.
"""

from __future__ import annotations

from collections.abc import Iterator
from uuid import UUID

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.api.vendor_only_region import VENDOR_ONLY_LABEL
from app.db.session import session_factory
from app.evidence.confirm import confirm_candidate_type
from app.models import (
    CanonicalObservation,
    EvidenceArtifact,
    Finding,
    ObservationCandidate,
    OutboxEntry,
    Package,
    PackageRevision,
    PackageRevisionDocument,
    Page,
    VendorPagePicture,
)
from app.models.evidence import EvidenceSupportingCandidate
from evidence.crop import decode_rgb_png
from storage.local import LocalStore
from tests.api.test_drawing_views import _client
from tests.extraction.test_reader import MISSING_SPACE
from tests.workflow.test_evidence_crop_gv_marks import CLEAN, REVIEWED
from tests.workflow.test_pipeline_end_to_end import _revision, _upgrade, store
from workflow.stages import DatabaseStages

__all__ = ["store"]  # the fixture, re-exported so pytest finds it here

pytest_plugins = ("tests.app.postgres_fixture",)

#: A type the reviewer may give a reading; which one does not matter to a picture.
SEMANTIC_TYPE = "CT010"


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    _upgrade(postgres_engine)
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


def _prepared(
    session: Session, store: LocalStore, *, sheet: bytes = REVIEWED, pictures: bool = True
) -> tuple[PackageRevision, UUID, UUID]:
    """The sheet read, its crops cut and, unless told otherwise, its vendor-only pictures made."""
    revision = _revision(session, store, data=sheet)
    stages = DatabaseStages(store, missing_space=MISSING_SPACE)
    stages.extract_pages(session, revision.id)
    stages.validate_evidence(session, revision.id)
    if pictures:
        rendered = stages.render_vendor_page_pictures(session, revision.id)
        assert rendered["rendered"] == 1, rendered
    session.commit()
    package = session.get_one(Package, revision.package_id)
    return revision, package.project_id, package.id


def _candidate(session: Session, prefix: str) -> ObservationCandidate:
    return session.scalars(
        select(ObservationCandidate)
        .where(ObservationCandidate.raw_text.startswith(prefix))
        .order_by(ObservationCandidate.created_at, ObservationCandidate.id)
        .limit(1)
    ).one()


def _confirmed(session: Session, candidate: ObservationCandidate) -> CanonicalObservation:
    observation = confirm_candidate_type(
        session,
        candidate_id=candidate.id,
        semantic_type=SEMANTIC_TYPE,
        confirmed_by="reviewer@example.com",
    )
    assert isinstance(observation, CanonicalObservation), observation
    session.commit()
    return observation


def _base(project_id: UUID, package_id: UUID) -> str:
    return f"/api/v1/projects/{project_id}/packages/{package_id}"


def _red(png: bytes) -> int:
    width, height, rgb = decode_rgb_png(png)
    return sum(
        1
        for offset in range(0, width * height * 3, 3)
        if rgb[offset] > 200 and rgb[offset + 1] < 60 and rgb[offset + 2] < 60
    )


def _dark(png: bytes) -> int:
    width, height, rgb = decode_rgb_png(png)
    return sum(
        1 for offset in range(0, width * height * 3, 3) if max(rgb[offset : offset + 3]) < 90
    )


def _counts(session: Session) -> dict[str, int]:
    return {
        model.__name__: int(session.scalar(select(func.count()).select_from(model)) or 0)
        for model in (
            ObservationCandidate,
            CanonicalObservation,
            EvidenceArtifact,
            EvidenceSupportingCandidate,
            Finding,
            OutboxEntry,
            VendorPagePicture,
        )
    }


# ---------------------------------------------------------------------------
# A crop under GV's note, and one away from it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", ["evidence", "candidates"])
def test_the_covered_label_is_shown_without_gv_markup(
    session: Session, store: LocalStore, route: str
) -> None:
    _, project_id, package_id = _prepared(session, store)
    candidate = _candidate(session, "648")
    reading = candidate.id if route == "candidates" else _confirmed(session, candidate).id
    client = _client(session, store, project_id)
    base = f"{_base(project_id, package_id)}/{route}/{reading}"

    view = client.get(f"{base}/vendor-only")
    original = client.get(f"{base}/crop")
    picture = client.get(f"{base}/vendor-only/picture")

    assert view.status_code == 200, view.text
    body = view.json()
    assert body["label"] == VENDOR_ONLY_LABEL == "vendor's drawing without GV markup"
    assert body["available"] is True
    assert body["unavailable_reason"] is None
    assert body["crop_shows_gv_mark"] is True
    assert body["page_number"] == 1
    assert body["document_version_id"] == str(candidate.document_version_id)
    assert body["page_id"] == str(candidate.page_id)
    assert original.status_code == 200 and picture.status_code == 200, picture.text
    assert picture.headers["content-type"] == "image/png"
    assert _red(original.content) > 0, "the control: the original crop does show GV's red note"
    assert _red(picture.content) == 0, "GV's note reached the vendor-only view"
    assert _dark(picture.content) > 0, "the vendor's own label is missing from the vendor-only view"
    assert decode_rgb_png(picture.content)[:2] == decode_rgb_png(original.content)[:2]


@pytest.mark.parametrize("route", ["evidence", "candidates"])
def test_the_region_lands_on_the_same_pixels_as_the_original_crop(
    session: Session, store: LocalStore, route: str
) -> None:
    """Where GV wrote nothing the two pictures are the same pixels: the stored region, carried by the
    published transform to the vendor-only picture, is exactly the region the crop was cut from."""
    _, project_id, package_id = _prepared(session, store)
    candidate = _candidate(session, "100")
    reading = candidate.id if route == "candidates" else _confirmed(session, candidate).id
    client = _client(session, store, project_id)
    base = f"{_base(project_id, package_id)}/{route}/{reading}"

    body = client.get(f"{base}/vendor-only").json()
    original = client.get(f"{base}/crop")
    picture = client.get(f"{base}/vendor-only/picture")

    assert body["available"] is True
    assert body["crop_shows_gv_mark"] is False
    assert picture.status_code == 200, picture.text
    assert decode_rgb_png(picture.content) == decode_rgb_png(original.content)
    left, top, right, bottom = body["pixel_box"]
    assert (right - left, bottom - top) == decode_rgb_png(original.content)[:2]


# ---------------------------------------------------------------------------
# Not available, refused, not disclosed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", ["evidence", "candidates"])
def test_no_vendor_only_picture_is_a_plain_unavailable_state(
    session: Session, store: LocalStore, route: str
) -> None:
    _, project_id, package_id = _prepared(session, store, pictures=False)
    candidate = _candidate(session, "648")
    reading = candidate.id if route == "candidates" else _confirmed(session, candidate).id
    client = _client(session, store, project_id)
    base = f"{_base(project_id, package_id)}/{route}/{reading}"
    before = _counts(session)

    view = client.get(f"{base}/vendor-only")
    picture = client.get(f"{base}/vendor-only/picture")

    assert view.status_code == 200, view.text
    body = view.json()
    assert body["available"] is False
    assert (
        body["unavailable_reason"] == "the vendor-only picture of this page has not been made yet"
    )
    assert body["pixel_box"] is None
    assert body["label"] == VENDOR_ONLY_LABEL
    assert picture.status_code == 404
    assert "has not been made yet" in picture.text
    assert _counts(session) == before, "asking for a missing picture must not start any work"


def test_a_picture_of_another_size_is_unavailable_not_stretched(
    session: Session, store: LocalStore
) -> None:
    """A picture whose size is not what the page's transform gives at its dpi cannot be placed."""
    _, project_id, package_id = _prepared(session, store, pictures=False)
    candidate = _candidate(session, "648")
    # The page is 400 x 300 pt, so 1667 x 1250 at 300 dpi; this picture claims one pixel more across.
    session.add(
        VendorPagePicture(
            page_id=candidate.page_id,
            storage_key="vendor-pages/drifted.png",
            sha256="e" * 64,
            media_type="image/png",
            dpi=300,
            width_px=1668,
            height_px=1250,
            snap_points=[],
            snap_tolerance=None,
        )
    )
    session.commit()

    body = (
        _client(session, store, project_id)
        .get(f"{_base(project_id, package_id)}/candidates/{candidate.id}/vendor-only")
        .json()
    )

    assert body["available"] is False
    assert "not the size" in body["unavailable_reason"]


def test_a_crop_recorded_on_another_page_is_refused(session: Session, store: LocalStore) -> None:
    """Stored rows that disagree about which page a reading is on are refused, never shown."""
    _, project_id, package_id = _prepared(session, store)
    _prepared(session, store, sheet=CLEAN)
    near = _candidate(session, "100")
    elsewhere = session.scalars(select(Page).where(Page.id != near.page_id)).one()
    stray = ObservationCandidate(
        document_version_id=near.document_version_id,
        page_id=near.page_id,
        extraction_run_id=near.extraction_run_id,
        raw_text="a reading whose crop row names another page",
        polygon=[list(point) for point in near.polygon],
        coordinate_space=near.coordinate_space,
        ambiguity_flags=[],
    )
    session.add(stray)
    session.flush()
    session.add(
        EvidenceArtifact(
            candidate_id=stray.id,
            canonical_observation_id=None,
            document_version_id=elsewhere.document_version_id,
            page_id=elsewhere.id,
            kind="crop",
            storage_key="evidence-crops/elsewhere.png",
            sha256="d" * 64,
            media_type="image/png",
            coordinate_space="image",
            shows_gv_marks=True,
        )
    )
    session.commit()
    client = _client(session, store, project_id)
    base = f"{_base(project_id, package_id)}/candidates/{stray.id}"

    view = client.get(f"{base}/vendor-only")
    picture = client.get(f"{base}/vendor-only/picture")

    assert view.status_code == 409, view.text
    assert picture.status_code == 409
    assert "do not agree" in view.text


def test_another_projects_reading_is_not_disclosed(session: Session, store: LocalStore) -> None:
    _, project_id, package_id = _prepared(session, store)
    other_revision, other_project, other_package = _prepared(session, store, sheet=CLEAN)
    theirs = session.scalars(
        select(ObservationCandidate)
        .join(
            PackageRevisionDocument,
            PackageRevisionDocument.document_version_id == ObservationCandidate.document_version_id,
        )
        .where(
            PackageRevisionDocument.package_revision_id == other_revision.id,
            ObservationCandidate.raw_text.startswith("100"),
        )
        .limit(1)
    ).one()
    observation = _confirmed(session, theirs)
    # Allowed into the first project only.
    client = _client(session, store, project_id)

    for route, reading in (("candidates", theirs.id), ("evidence", observation.id)):
        # Their reading under my package: not part of it.
        under_mine = f"{_base(project_id, package_id)}/{route}/{reading}/vendor-only"
        # Their own package, asked for by someone without access to their project.
        under_theirs = f"{_base(other_project, other_package)}/{route}/{reading}/vendor-only"
        for path in (under_mine, f"{under_mine}/picture", under_theirs, f"{under_theirs}/picture"):
            response = client.get(path)
            assert response.status_code == 404, (path, response.text)
            assert str(other_project) not in response.text


def test_reading_the_views_changes_nothing(session: Session, store: LocalStore) -> None:
    """A visual aid: no reading, value, decision, finding or job is written by looking at it."""
    _, project_id, package_id = _prepared(session, store)
    observation = _confirmed(session, _candidate(session, "648"))
    candidate = _candidate(session, "100")
    client = _client(session, store, project_id)
    before = _counts(session)
    states = {
        row.id: (row.status, row.value_numerator, row.value_denominator, row.semantic_type)
        for row in session.scalars(select(CanonicalObservation))
    }

    for path in (
        f"evidence/{observation.id}/vendor-only",
        f"evidence/{observation.id}/vendor-only/picture",
        f"candidates/{candidate.id}/vendor-only",
        f"candidates/{candidate.id}/vendor-only/picture",
    ):
        assert client.get(f"{_base(project_id, package_id)}/{path}").status_code == 200

    session.expire_all()
    assert _counts(session) == before
    assert {
        row.id: (row.status, row.value_numerator, row.value_denominator, row.semantic_type)
        for row in session.scalars(select(CanonicalObservation))
    } == states
