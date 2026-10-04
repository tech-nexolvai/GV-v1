"""The agreement gate does not confirm a reading whose crop shows a GV mark in colour (#901).

Verification for: `workflow/stages.py` (`crop_shows_a_gv_mark`, `gv_mark_in_crop`, `_GvMarkGuard`
and `DatabaseStages._apply_cross_route_corroboration`).

**The admin's decision, 2026-10-03.** In every scorecard run on the 51-crop key the two readers
agreed on GV's own number, baked into the vendor's drawing in colour (#851). An agreement whose crop
shows markup drawn in colour, wholly or in part, stays a pre-fill a person ticks; the same pair on a
crop with no mark is confirmed as before. The guard only ever takes a confirmation away.

The drawings are made up here. No model is called and no client drawing is read.
"""

from __future__ import annotations

import hashlib
import io
import tempfile
from collections.abc import Callable, Iterator
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alembic import command
from app.api.documents import storage_key
from app.db.session import session_factory
from app.models import (
    Document,
    DocumentVersion,
    ExtractionRun,
    ObservationCandidate,
    Package,
    PackageRevision,
    PackageRevisionDocument,
    PackageState,
    Project,
    SourceArtifact,
)
from app.models.runs import TaskRun, WorkflowRun
from evidence.coordinates import PageTransform, StoredPoint
from evidence.crop import RenderedPage
from evidence.polygon import Polygon
from extraction.annotations import PathSegment, SegmentKind, VectorPath
from storage.local import LocalStore
from tests.app.postgres_fixture import alembic_config
from tests.extraction.test_reader import MISSING_SPACE
from tests.workflow.test_cross_route_corroboration import _Reader, _reader
from tests.workflow.test_cut_label_guard import stated_geometry
from workflow.idempotency import stage_idempotency_key
from workflow.review import ENGINE_VERSION
from workflow.stages import (
    GV_MARK_REASON,
    GV_MARK_UNCHECKED_REASON,
    ColouredMarkup,
    DatabaseStages,
    _GvMarkGuard,
    crop_shows_a_gv_mark,
    gv_mark_in_crop,
)

pytest_plugins = ("tests.app.postgres_fixture",)

DOCUMENT = UUID("33333333-3333-4333-8333-333333333333")

RED: tuple[int, int, int, int] = (255, 0, 0, 255)
BLACK: tuple[int, int, int, int] = (0, 0, 0, 255)

#: A crop's page pixels: 100–200 across, 50–150 down.
CROP = (100, 50, 200, 150)

#: A 200 × 100 pt page at 72 dpi, so a pixel is a point and only the y axis turns over.
TRANSFORM = PageTransform(
    dpi=72,
    rotation=0,
    media_box=(Decimal(0), Decimal(0), Decimal(200), Decimal(100)),
    crop_box=(Decimal(0), Decimal(0), Decimal(200), Decimal(100)),
)


def unmarked_page() -> _GvMarkGuard:
    """The guard for a page that holds no markup in colour: it never holds an agreement back.

    For the tests of other rules that call `_apply_cross_route_corroboration` directly."""
    return _GvMarkGuard(
        markup=lambda: ColouredMarkup(text=(), paths=(), transform=None),
        render=_never_rendered,
    )


def _never_rendered() -> RenderedPage | None:
    raise AssertionError("a page with no markup in colour is never rendered for the guard")


def _path(
    left: int, top: int, right: int, bottom: int, colour: tuple[int, int, int, int]
) -> VectorPath:
    """A stroked line from one corner to the other, in PDF points (y up)."""
    return VectorPath(
        segments=(
            PathSegment(SegmentKind.MOVE, (Decimal(left), Decimal(top)), False),
            PathSegment(SegmentKind.LINE, (Decimal(right), Decimal(bottom)), False),
        ),
        stroked=True,
        filled=False,
        stroke_colour=colour,
        fill_colour=None,
    )


def _text(*boxes: tuple[int, int, int, int]) -> ColouredMarkup:
    return ColouredMarkup(text=boxes, paths=(), transform=None)


def _paths(*paths: VectorPath, transform: PageTransform | None = TRANSFORM) -> ColouredMarkup:
    return ColouredMarkup(text=(), paths=paths, transform=transform)


# ---------------------------------------------------------------------------
# The test the replay measured: markup in colour in the crop, wholly or in part
# ---------------------------------------------------------------------------


def test_coloured_text_wholly_inside_the_crop_is_a_gv_mark() -> None:
    assert crop_shows_a_gv_mark(CROP, _text((120, 70, 140, 80)))


@pytest.mark.parametrize(
    "box",
    [
        (190, 60, 230, 70),  # over the right edge
        (80, 60, 110, 70),  # over the left edge
        (120, 140, 130, 170),  # over the bottom edge
        (120, 30, 130, 55),  # over the top edge
        (200, 150, 220, 170),  # touching a corner
    ],
    ids=["right", "left", "bottom", "top", "corner"],
)
def test_coloured_text_partly_inside_the_crop_is_a_gv_mark(box: tuple[int, int, int, int]) -> None:
    """**In part counts.** A reader reads whatever it is shown, and GV's number half inside the crop
    is still there to be read as the vendor's."""
    assert crop_shows_a_gv_mark(CROP, _text(box))


@pytest.mark.parametrize(
    "box",
    [(201, 60, 230, 70), (120, 151, 130, 170), (10, 10, 20, 20)],
    ids=["right", "below", "far"],
)
def test_coloured_text_outside_the_crop_is_not(box: tuple[int, int, int, int]) -> None:
    assert not crop_shows_a_gv_mark(CROP, _text(box))


def test_a_path_drawn_in_colour_in_the_crop_is_a_gv_mark_and_the_same_path_in_black_is_not() -> (
    None
):
    """**The colour is the test.** The crop is 50–150 px down a 100 pt page at 72 dpi, so 50 to -50
    pt up in PDF space; a line at 30 pt up, 120–140 across, lies in it."""
    assert crop_shows_a_gv_mark(CROP, _paths(_path(120, 30, 140, 30, RED)))
    assert not crop_shows_a_gv_mark(CROP, _paths(_path(120, 30, 140, 30, BLACK)))


def test_a_path_drawn_in_colour_partly_in_the_crop_is_a_gv_mark() -> None:
    assert crop_shows_a_gv_mark(CROP, _paths(_path(190, 30, 260, 30, RED)))
    assert not crop_shows_a_gv_mark(CROP, _paths(_path(201, 30, 260, 30, RED)))


def test_a_page_with_no_transform_places_no_path() -> None:
    """As the stage's own geometry says nothing there; its coloured text still counts."""
    assert not crop_shows_a_gv_mark(CROP, _paths(_path(120, 30, 140, 30, RED), transform=None))
    assert not _paths(_path(120, 30, 140, 30, RED), transform=None).shown


def _rendered() -> RenderedPage:
    return RenderedPage(
        document_version_id=DOCUMENT,
        page_index=0,
        page_content_hash="0" * 64,
        rotation=0,
        render_failed=False,
        width_px=200,
        height_px=100,
        dpi=72,
        rgb_bytes=bytes(200 * 100 * 3),
    )


def _polygon(left: int, top: int, right: int, bottom: int) -> Polygon:
    """A region's polygon on the 200 × 100 px rendering, as `stored_polygon` makes one."""
    return Polygon(
        points=tuple(
            StoredPoint(x=Decimal(x) / 200, y=Decimal(y) / 100)
            for x, y in ((left, top), (right, top), (right, bottom), (left, bottom))
        ),
        space="stored",
        document_version_id=DOCUMENT,
        page=0,
    )


def test_the_crop_is_the_vision_readers_crop_with_its_margin() -> None:
    """The region is 50–60 px across; the readers' crop adds 9 pt (9 px at 72 dpi) round it, so a
    mark 5 px beyond the region is in the crop, and one 10 px beyond it is not."""
    region = _polygon(50, 40, 60, 50)

    assert gv_mark_in_crop(region, _rendered(), _text((65, 42, 66, 44)))
    assert not gv_mark_in_crop(region, _rendered(), _text((70, 42, 71, 44)))


def test_a_crop_that_cannot_be_cut_counts_as_showing_a_mark_only_where_the_page_holds_one() -> None:
    """Nothing can rule the mark out, and the guard may only hold an agreement back."""
    assert gv_mark_in_crop(None, _rendered(), _text((10, 10, 20, 20)))
    assert not gv_mark_in_crop(None, _rendered(), _text())


# ---------------------------------------------------------------------------
# The page's guard: looked up only when asked, and each refusal counted once with its reason
# ---------------------------------------------------------------------------


def _region(left: int, top: int, right: int, bottom: int) -> ObservationCandidate:
    return ObservationCandidate(
        document_version_id=DOCUMENT,
        polygon=[[left, top], [right, top], [right, bottom], [left, bottom]],
        raw_text='24"',
    )


def _guard(
    markup: ColouredMarkup | None, rendered: Callable[[], RenderedPage | None] = _rendered
) -> _GvMarkGuard:
    return _GvMarkGuard(markup=lambda: markup, render=rendered)


def test_the_guard_counts_one_refusal_per_region_with_its_reason() -> None:
    """Asked twice about one region — the stage asks again once the reading agent has looked — it
    is one refusal; a second marked region is a second."""
    guard = _guard(_text((52, 42, 58, 48), (152, 42, 158, 48)))

    assert guard.holds_back(_region(50, 40, 60, 50))
    assert guard.holds_back(_region(50, 40, 60, 50))
    assert guard.holds_back(_region(150, 40, 160, 50))
    assert not guard.holds_back(_region(100, 40, 110, 50))

    assert len(guard.refused) == 2
    assert guard.reasons(20) == [f"2 × {GV_MARK_REASON}"]


def test_a_page_whose_markup_could_not_be_read_holds_every_agreement_back() -> None:
    guard = _guard(None, _never_rendered)

    assert guard.holds_back(_region(50, 40, 60, 50))
    assert guard.reasons(20) == [f"1 × {GV_MARK_UNCHECKED_REASON}"]


def test_a_page_with_markup_that_cannot_be_rendered_holds_every_agreement_back() -> None:
    guard = _guard(_text((10, 10, 20, 20)), lambda: None)

    assert guard.holds_back(_region(150, 40, 160, 50))
    assert guard.reasons(20) == [f"1 × {GV_MARK_UNCHECKED_REASON}"]


def test_nothing_is_read_or_rendered_until_an_agreement_is_checked() -> None:
    def boom() -> ColouredMarkup | None:
        raise AssertionError("read before any agreement needed it")

    guard = _GvMarkGuard(markup=boom, render=_never_rendered)

    assert guard.refused == {}
    assert guard.reasons(20) == []


# ---------------------------------------------------------------------------
# In the stage: two readers agree on the vendor's `24"`, with GV's `38"` in the crop or not
# ---------------------------------------------------------------------------


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


@pytest.fixture
def store() -> Iterator[LocalStore]:
    with tempfile.TemporaryDirectory() as directory:
        yield LocalStore(root=Path(directory), ticket_secret=b"a secret only this test knows")


#: GV's `38"` in red, written over the vendor's `24"`; and the same mark far from it.
OVER_THE_LABEL = b'1 0 0 rg BT /F1 6 Tf 1 0 0 1 22 72 Tm (38") Tj ET'
FAR_AWAY = b'1 0 0 rg BT /F1 6 Tf 1 0 0 1 150 15 Tm (38") Tj ET'
#: The same `38"` over the label, in the vendor's black.
BLACK_OVER_THE_LABEL = b'0 0 0 rg BT /F1 6 Tf 1 0 0 1 22 72 Tm (38") Tj ET'


def _sheet(mark: bytes) -> bytes:
    """A 200 × 100 pt page whose own text is the vendor's `24"`, with a pasted drawing over the
    whole page holding `mark` — a snapshot's markup, as on the client's sets."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 100]"
            b" /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R /Annots [6 0 R] >>"
        ),
        _stream(b'BT /F1 10 Tf 1 0 0 1 20 70 Tm (24") Tj ET', b""),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Annot /Subtype /Stamp /Rect [0 0 200 100] /T (Vendor) /AP << /N 7 0 R >> >>",
        _stream(
            mark,
            b"/Type /XObject /Subtype /Form /FormType 1 /BBox [0 0 200 100]"
            b" /Matrix [1 0 0 1 0 0] /Resources << /Font << /F1 5 0 R >> >> ",
        ),
    ]
    out = bytearray(b"%PDF-1.7\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += str(number).encode() + b" 0 obj\n" + body + b"\nendobj\n"
    start = len(out)
    out += b"xref\n0 " + str(len(objects) + 1).encode() + b"\n0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        b"trailer\n<< /Size "
        + str(len(objects) + 1).encode()
        + b" /Root 1 0 R >>\nstartxref\n"
        + str(start).encode()
        + b"\n%%EOF\n"
    )
    return bytes(out)


def _stream(content: bytes, dictionary: bytes) -> bytes:
    return (
        b"<< "
        + dictionary
        + b"/Length "
        + str(len(content)).encode()
        + b" >>\nstream\n"
        + content
        + b"\nendstream"
    )


def _revision(session: Session, store: LocalStore, data: bytes) -> PackageRevision:
    digest = hashlib.sha256(data).hexdigest()
    project = Project(name=f"gv mark {uuid4()}")
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
    document = Document(package_id=package.id, kind="shop")
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
    return revision


#: The reader pair as production runs it: two vendors (#775).
PAIR = (
    _reader("bedrock-nova-2-lite", "amazon.nova-2-lite-v1:0"),
    _reader("bedrock-ministral-3-3b", "mistral.ministral-3-3b-instruct"),
)


def _extract(
    session: Session, store: LocalStore, mark: bytes, *, reading: str = '24"'
) -> tuple[list[tuple[ObservationCandidate, str]], dict[str, object]]:
    """The page's `24"` readings with what read each, and the page result."""
    revision = _revision(session, store, _sheet(mark))
    readers = tuple(
        _reader(reader.config.extractor, reader.config.model_id, reading=reading) for reader in PAIR
    )
    (result,) = DatabaseStages(
        store,
        vision_readers=readers,
        missing_space=MISSING_SPACE,
        # The drawing's geometry stated, as in the demo: without it the cut-label guard (#919)
        # cannot rule a cut out, and no agreement is confirmed whatever the crop shows.
        **stated_geometry(PAIR[0].config.extractor),  # type: ignore[arg-type]
    ).extract_pages(session, revision.id)
    rows = [
        (row, extractor)
        for row, extractor in session.execute(
            select(ObservationCandidate, ExtractionRun.extractor).join(
                ExtractionRun, ObservationCandidate.extraction_run_id == ExtractionRun.id
            )
        ).all()
        if row.raw_text != '38"'
    ]
    return rows, dict(result.payload)


def test_two_readers_agreeing_on_a_reading_whose_crop_shows_a_gv_mark_do_not_confirm_it(
    session: Session, store: LocalStore
) -> None:
    """**Done when, first line.** The file's text and both model readers say `24"`, and GV's red
    `38"` is written over it: no reading takes the second-reader lane, so none can be sealed; each
    is a pre-fill a person ticks. The page result counts the refusal and says why."""
    rows, payload = _extract(session, store, OVER_THE_LABEL)

    assert {extractor for _, extractor in rows} == {
        "pdfplumber",
        "bedrock-nova-2-lite",
        "bedrock-ministral-3-3b",
    }
    assert {row.raw_text for row, _ in rows} == {'24"'}
    for row, _ in rows:
        assert (row.corroboration_status, row.corroboration_lane) == (None, None)
    assert payload["agreement_refusals"] == 1
    assert payload["agreement_refusal_reasons"] == [f"1 × {GV_MARK_REASON}"]


@pytest.mark.parametrize("mark", [BLACK_OVER_THE_LABEL, FAR_AWAY], ids=["black", "far-away"])
def test_the_same_pair_with_no_gv_mark_in_the_crop_confirms_it(
    session: Session, store: LocalStore, mark: bytes
) -> None:
    """**Done when, second line.** The same readers on the same `24"`: with the `38"` in the
    vendor's black, or in red but outside the crop, the agreement is confirmed as before."""
    rows, payload = _extract(session, store, mark)

    lanes = {
        extractor: (row.corroboration_status, row.corroboration_lane) for row, extractor in rows
    }
    # The file's own reading was saved with the page's drawings, before the readers ran, and a
    # saved reading keeps its status (#790); the two readings written beside it take the lane.
    assert lanes == {
        "pdfplumber": (None, None),
        "bedrock-nova-2-lite": ("RAW_CANDIDATE", "SECOND_READER"),
        "bedrock-ministral-3-3b": ("RAW_CANDIDATE", "SECOND_READER"),
    }
    assert payload["agreement_refusals"] == 0
    assert payload["agreement_refusal_reasons"] == []


def test_a_disagreement_in_a_marked_crop_is_still_a_conflict(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**Only stricter.** Readers who disagree are a conflict for a reviewer whatever the crop
    shows; the guard is never asked, so it cannot turn a conflict into a pre-fill."""

    def never(*arguments: object) -> ColouredMarkup | None:
        raise AssertionError("the guard is asked only about an agreement")

    monkeypatch.setattr(DatabaseStages, "_coloured_markup", never)
    rows, payload = _extract(session, store, OVER_THE_LABEL, reading='25"')

    lanes = {
        extractor: (row.corroboration_status, row.corroboration_lane) for row, extractor in rows
    }
    assert {row.raw_text for row, _ in rows} == {'24"', '25"'}
    assert lanes == {
        "pdfplumber": (None, None),
        "bedrock-nova-2-lite": ("CONFLICTING", "SECOND_READER"),
        "bedrock-ministral-3-3b": ("CONFLICTING", "SECOND_READER"),
    }
    assert payload["agreement_refusals"] == 0


# ---------------------------------------------------------------------------
# The new pair, Qwen3-VL + Nova 2 Lite (#907)
# ---------------------------------------------------------------------------


def _new_pair(reading: str) -> tuple[_Reader, _Reader]:
    """The two readers #907 makes the pair, as they are configured: Qwen on the crop as cut, Nova 2
    Lite on the label upright and sharper."""
    from extraction.models.nova import (
        NOVA_2_LITE_TAUGHT_EXTRACTOR,
        QWEN3_VL_235B_EXTRACTOR,
        vision_config_for_extractor,
    )

    configs = [
        vision_config_for_extractor(extractor)
        for extractor in (QWEN3_VL_235B_EXTRACTOR, NOVA_2_LITE_TAUGHT_EXTRACTOR)
    ]
    assert all(config is not None for config in configs)
    return _Reader(configs[0], reading=reading), _Reader(configs[1], reading=reading)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("mark", "confirmed"),
    [(OVER_THE_LABEL, False), (BLACK_OVER_THE_LABEL, True), (FAR_AWAY, True)],
    ids=["gv-mark-over-the-label", "black-over-the-label", "gv-mark-far-away"],
)
def test_the_new_pair_agreeing_on_a_gv_mark_s_number_is_held_back(
    session: Session, store: LocalStore, mark: bytes, confirmed: bool
) -> None:
    """**#901 holds for the new pair.** In the trial Qwen3-VL and Nova 2 Lite agreed on GV's own
    red number in three crops whose vendor number was hidden (#728, 2026-10-04). The same agreement
    here — both readers say what GV wrote — confirms nothing where the crop shows GV's mark, and is
    confirmed as before where the mark is the vendor's black or lies outside the crop. Two vendors,
    so without the mark the pair would confirm it (#775)."""
    from workflow.reader_pictures import PictureSettings

    revision = _revision(session, store, _sheet(mark))
    readers = _new_pair('24"')
    stages = DatabaseStages(
        store,
        vision_readers=readers,
        reader_pictures=PictureSettings(sharper_dpi=900),
        missing_space=MISSING_SPACE,
        **stated_geometry(readers[0].config.extractor),  # type: ignore[arg-type]
    )

    (result,) = stages.extract_pages(session, revision.id)

    lanes = {
        extractor: (row.corroboration_status, row.corroboration_lane)
        for row, extractor in session.execute(
            select(ObservationCandidate, ExtractionRun.extractor).join(
                ExtractionRun, ObservationCandidate.extraction_run_id == ExtractionRun.id
            )
        ).all()
        if row.raw_text == '24"' and extractor != "pdfplumber"
    }
    lane = ("RAW_CANDIDATE", "SECOND_READER") if confirmed else (None, None)
    assert lanes == {"bedrock-qwen3-vl-235b": lane, "bedrock-nova-2-lite-taught": lane}
    assert result.payload["agreement_refusals"] == (0 if confirmed else 1)
