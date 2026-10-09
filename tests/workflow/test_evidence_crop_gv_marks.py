"""The evidence picture's "shows GV's markup" flag counts GV's own annotation layer (#952).

An evidence crop is cut from the page rendered with **both** layers (`vendor_only=False`): a person
reviewing evidence is shown what the sheet shows, GV's notes included (#742). The flag recorded with
it (`EvidenceArtifact.shows_gv_marks`) used to ask only about GV marks baked into the vendor's drawing
(`crop_shows_a_gv_mark`, #901/#929), never about GV's own annotation layer, which is exactly what the
both-layer picture paints on. So a crop with GV's note over the vendor's label said "no GV mark".

These go through `DatabaseStages.validate_evidence` on an invented sheet: a vendor stamp drawing a
small label, and a reviewer's solid red note laid over it (`REVIEWED_SHEET`, #742's own sheet), plus
a second sheet with the same vendor drawing and no note at all.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.db.session import session_factory
from app.models import EvidenceArtifact, ObservationCandidate
from evidence.coordinates import ImagePoint, StoredPoint
from evidence.polygon import Polygon
from extraction.annotations import DrawingLayer, MarkupNote
from storage.local import LocalStore
from tests.extraction.test_annotations import _appearance
from tests.extraction.test_reader import HELVETICA, MISSING_SPACE, _pdf_from, _stream
from tests.workflow.test_pipeline_end_to_end import _revision, _upgrade, store
from workflow.stages import DatabaseStages, crop_shows_the_reviewer_layer

__all__ = ["store"]  # the fixture, re-exported so pytest finds it here

pytest_plugins = ("tests.app.postgres_fixture",)

#: A vendor label covered by a solid red reviewer note, and a second label well away from it.
#: Page space: the covered label starts at (40, 200), the note covers (35..125, 195..215), the other
#: label starts at (250, 60).
_LABELS = (
    b"BT /F1 10 Tf 1 0 0 1 40 200 Tm (648 [25 1/2]) Tj ET\n"
    b"BT /F1 10 Tf 1 0 0 1 250 60 Tm (100 [4]) Tj ET\n"
)
_NOTE = (
    b"<< /Type /Annot /Subtype /FreeText /Rect [35 195 125 215] /Contents (46 1/2) "
    b"/DA (/Helv 10 Tf 1 0 0 rg) /AP << /N 7 0 R >> >>"
)


def _sheet(*, with_note: bool) -> bytes:
    annots = b" /Annots [6 0 R]" if with_note else b""
    return _pdf_from(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 400 300]"
            + annots
            + b" /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
            _stream(_LABELS),
            HELVETICA,
            _NOTE,
            _appearance(b"1 0 0 rg 0 0 90 20 re f\n", bbox=b"[0 0 90 20]", matrix=b"[1 0 0 1 0 0]"),
        ]
    )


REVIEWED = _sheet(with_note=True)
CLEAN = _sheet(with_note=False)


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    _upgrade(postgres_engine)
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


def _crops(session: Session, store: LocalStore, sheet: bytes) -> list[EvidenceArtifact]:
    revision = _revision(session, store, data=sheet)
    stages = DatabaseStages(store, missing_space=MISSING_SPACE)
    stages.extract_pages(session, revision.id)
    stages.validate_evidence(session, revision.id)
    return list(
        session.scalars(
            select(EvidenceArtifact)
            .join(ObservationCandidate, ObservationCandidate.id == EvidenceArtifact.candidate_id)
            .where(EvidenceArtifact.kind == "crop")
            .order_by(EvidenceArtifact.created_at, EvidenceArtifact.id)
        )
    )


def _flags(crops: list[EvidenceArtifact], session: Session) -> dict[str, bool | None]:
    return {
        session.get_one(ObservationCandidate, crop.candidate_id).raw_text: crop.shows_gv_marks
        for crop in crops
    }


def test_a_crop_under_a_gv_note_says_it_shows_gv_markup(
    session: Session, store: LocalStore
) -> None:
    """The bug: GV's note is painted into the crop, and the flag said it was not there."""
    flags = _flags(_crops(session, store, REVIEWED), session)

    covered = [text for text in flags if text.startswith("648")]
    assert covered, f"the covered label was not read, so this test proves nothing: {flags}"
    assert {flags[text] for text in covered} == {True}
    # GV's own note, read as a markup reading, is a picture of GV's markup too.
    assert flags.get("46 1/2") is True


def test_a_crop_away_from_the_note_still_says_it_shows_none(
    session: Session, store: LocalStore
) -> None:
    """The note is counted only where it is in the picture."""
    flags = _flags(_crops(session, store, REVIEWED), session)

    far = [text for text in flags if text.startswith("100")]
    assert far, f"the far label was not read, so this test proves nothing: {flags}"
    assert {flags[text] for text in far} == {False}


def test_a_sheet_with_no_gv_note_says_no_crop_shows_one(
    session: Session, store: LocalStore
) -> None:
    """The control: the vendor's drawing alone is not called GV's markup."""
    flags = _flags(_crops(session, store, CLEAN), session)

    assert flags, "no crop was cut, so this test proves nothing"
    assert set(flags.values()) == {False}


def _note(subtype: str, corners: tuple[tuple[int, int], tuple[int, int]]) -> MarkupNote:
    (left, top), (right, bottom) = corners
    return MarkupNote(
        layer=DrawingLayer.OTHER,
        subtype=subtype,
        text="",
        author=None,
        extent=Polygon(
            points=tuple(
                _stored(x, y)
                for x, y in ((left, top), (right, top), (right, bottom), (left, bottom))
            ),
            space="stored",
            document_version_id=UUID(int=1),
            page=0,
        ),
        image_extent=(
            ImagePoint(left, top),
            ImagePoint(right, top),
            ImagePoint(right, bottom),
            ImagePoint(left, bottom),
        ),
        rotation_degrees=0,
        annotation_index=0,
    )


def _stored(x: int, y: int) -> StoredPoint:
    return StoredPoint(Decimal(x) / Decimal(1000), Decimal(y) / Decimal(1000))


@pytest.mark.parametrize(
    ("corners", "expected"),
    [
        (((10, 10), (20, 20)), True),  # inside the crop
        (((0, 0), (10, 10)), True),  # touching its corner counts, as for every other GV mark
        (((200, 200), (300, 300)), False),  # elsewhere on the page
    ],
)
def test_the_reviewer_layer_counts_where_it_overlaps_the_crop(
    corners: tuple[tuple[int, int], tuple[int, int]], expected: bool
) -> None:
    assert crop_shows_the_reviewer_layer((10, 10, 100, 100), [_note("Square", corners)]) is expected


def test_a_popup_is_never_counted_because_no_render_paints_it() -> None:
    """A `/Popup` is the closed bubble of a note; the page render never draws it."""
    assert (
        crop_shows_the_reviewer_layer((10, 10, 100, 100), [_note("Popup", ((0, 0), (500, 500)))])
        is False
    )
