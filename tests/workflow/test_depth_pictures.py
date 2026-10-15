"""Every depth reading has its own picture, cut the same way as every other reading (#1389).

Verification for `workflow/depth_pictures.py`, its call at the end of
`DatabaseStages.read_depths_again`, and the one-time backfill `scripts/backfill_depth_pictures.py`.

The drawing is the invented one-page sheet of `tests/workflow/test_depth_evidence._Set`, rendered
for real; the depth readings are made up and no model is asked. Every value here is made up.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.db.session import session_factory
from app.models import EvidenceArtifact, ObservationCandidate
from app.models.runs import ExtractionRun, TaskRun
from evidence.crop import crop_png
from extraction.depth.read import MEASURED_DEPTH_READER_SETTINGS, DepthReading
from storage.local import LocalStore
from tests.evidence.test_bridge import _upgrade
from tests.workflow.test_depth_evidence import _Set
from tests.workflow.test_read_depths_again import _again_runs, _Answering, _read_set, _stages
from workflow.config import READER_RASTER_DPI
from workflow.depth_evidence import (
    CONFIRM_ONLY,
    DEPTH_EXTRACTOR,
    DEPTH_READER_FLAG,
    DEPTH_READER_VERSION,
    HELD,
)
from workflow.depth_pictures import cut_depth_pictures
from workflow.stages import DatabaseStages, evidence_crop_spec

pytest_plugins = ("tests.app.postgres_fixture",)

CT010 = "CT010"


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    _upgrade(postgres_engine)
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
def store(tmp_path: Any) -> LocalStore:
    return LocalStore(root=tmp_path, ticket_secret=b"synthetic-test-only")


def _crops(session: Session, rows: list[ObservationCandidate]) -> dict[UUID, EvidenceArtifact]:
    return {
        artifact.candidate_id: artifact
        for artifact in session.scalars(
            select(EvidenceArtifact).where(
                EvidenceArtifact.candidate_id.in_([row.id for row in rows]),
                EvidenceArtifact.kind == "crop",
            )
        )
        if artifact.candidate_id is not None
    }


def _all_crops(session: Session) -> int:
    return int(session.scalar(select(func.count()).select_from(EvidenceArtifact)) or 0)


def _readings(sheet: _Set) -> list[ObservationCandidate]:
    """Two depth readings with no picture: one a person may confirm, one held."""
    return [
        sheet.reading(CT010, 25, CONFIRM_ONLY, answers=False),
        sheet.reading(CT010, None, HELD, reason="the readers disagree", answers=False),
    ]


def _another_run(session: Session, sheet: _Set, *, dpi: int | None) -> ExtractionRun:
    task = session.get_one(TaskRun, sheet.run.task_run_id)
    run = ExtractionRun(
        task_run_id=task.id,
        extractor=DEPTH_EXTRACTOR,
        extractor_version=DEPTH_READER_VERSION,
        config_hash=f"synthetic-{uuid4()}",
        dpi=dpi,
    )
    session.add(run)
    session.flush()
    return run


# -- the dpi a depth reading's region is in is the dpi pictures are cut at --------------------------


def test_the_depth_reader_reads_at_the_dpi_pictures_are_cut_at(store: LocalStore) -> None:
    # A depth reading's region is pixels at the reader's dpi; the crop stage cuts the region out of
    # a page rendered at its own. Were they to differ, every depth picture would show other pixels.
    assert MEASURED_DEPTH_READER_SETTINGS.dpi == READER_RASTER_DPI
    assert DatabaseStages(store).evidence_dpi == READER_RASTER_DPI


# -- the backfill ------------------------------------------------------------------------------------


def test_the_dry_run_counts_and_stores_nothing_then_the_run_cuts_each_once(
    session: Session, store: LocalStore
) -> None:
    sheet = _Set(session, store)
    rows = _readings(sheet)
    stages = DatabaseStages(store)
    before = _all_crops(session)

    dry = cut_depth_pictures(session, stages, dry_run=True)

    assert (dry.readings, dry.cut, dry.not_cut, dry.reasons) == (2, 2, 0, [])
    assert _all_crops(session) == before and _crops(session, rows) == {}

    done = cut_depth_pictures(session, stages, dry_run=False)

    assert (done.readings, done.cut, done.not_cut) == (2, 2, 0)
    crops = _crops(session, rows)
    assert set(crops) == {row.id for row in rows}
    for row in rows:
        crop = crops[row.id]
        assert crop.page_id == sheet.page.id and crop.coordinate_space == "image"
        with store.get(crop.storage_key) as stored:
            assert hashlib.sha256(stored.read()).hexdigest() == crop.sha256

    again = cut_depth_pictures(session, stages, dry_run=False)
    assert (again.readings, again.cut) == (0, 0)
    assert len(_crops(session, rows)) == 2


def test_a_picture_cut_again_is_byte_for_byte_the_crop_stage_s(
    session: Session, store: LocalStore
) -> None:
    sheet = _Set(session, store)
    first = sheet.reading(CT010, 25, CONFIRM_ONLY, answers=False)
    stages = DatabaseStages(store)
    stages.validate_evidence(session, sheet.revision.id)
    stored = _crops(session, [first])[first.id]

    # The very row, cut again with the crop stage's own steps.
    with store.get(f"{stored.storage_key}") as picture:
        stored_bytes = picture.read()
    data = _document(session, store, sheet)
    rendered = stages.evidence_render(data, sheet.page)
    spec = evidence_crop_spec(first, rendered)
    assert spec is not None
    assert crop_png(rendered, spec) == stored_bytes

    # A later reading of the same region, given its picture by this job: the same bytes.
    later = sheet.reading(
        CT010, 25, CONFIRM_ONLY, run=_another_run(session, sheet, dpi=300), answers=False
    )
    cut_depth_pictures(session, stages)
    recut = _crops(session, [later])[later.id]
    assert recut.sha256 == stored.sha256 and recut.storage_key == stored.storage_key


def _document(session: Session, store: LocalStore, sheet: _Set) -> bytes:
    from app.api.documents import storage_key
    from app.models import DocumentVersion

    version = session.get_one(DocumentVersion, sheet.page.document_version_id)
    with store.get(storage_key(version.document_id, version.sha256)) as stored:
        return stored.read()


def test_a_page_that_will_not_render_gets_no_picture_and_says_why(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    sheet = _Set(session, store)
    rows = _readings(sheet)
    _too_large_to_render(monkeypatch)

    outcome = cut_depth_pictures(session, DatabaseStages(store))

    assert (outcome.cut, outcome.not_cut) == (0, 2)
    (reason,) = outcome.reasons
    assert reason.startswith("page 1: ") and "pixels" in reason, reason
    assert _crops(session, rows) == {}


def _too_large_to_render(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every page is over the render limit, so `render_page` refuses it (`PageTooLarge`)."""
    import workflow.stages as stages_module

    monkeypatch.setattr(stages_module, "MAXIMUM_RENDER_PIXELS", 10)


def test_a_reading_made_at_another_dpi_gets_no_picture_and_says_why(
    session: Session, store: LocalStore
) -> None:
    sheet = _Set(session, store)
    row = sheet.reading(
        CT010, 25, CONFIRM_ONLY, run=_another_run(session, sheet, dpi=150), answers=False
    )

    outcome = cut_depth_pictures(session, DatabaseStages(store))

    assert (outcome.cut, outcome.not_cut) == (0, 1)
    assert outcome.reasons == ["page 1: it was read at 150 dpi and pictures are cut at 300 dpi"]
    assert _crops(session, [row]) == {}


def test_a_stored_file_that_is_not_its_digest_gets_no_picture_and_says_why(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    sheet = _Set(session, store)
    rows = _readings(sheet)
    real_get = store.get

    class _Altered:
        def __init__(self, key: str) -> None:
            self._inner = real_get(key)

        def __enter__(self) -> Any:
            inner = self._inner.__enter__()
            return SimpleNamespace(read=lambda: inner.read() + b"%")

        def __exit__(self, *exc: object) -> None:
            self._inner.__exit__(*exc)

    monkeypatch.setattr(store, "get", _Altered)

    outcome = cut_depth_pictures(session, DatabaseStages(store))

    assert (outcome.cut, outcome.not_cut) == (0, 2)
    assert outcome.reasons == ["the drawing's stored file does not match its digest"]
    assert _crops(session, rows) == {}


def test_the_backfill_keeps_to_the_revision_it_is_given(
    session: Session, store: LocalStore
) -> None:
    sheet = _Set(session, store)
    mine = sheet.reading(CT010, 25, CONFIRM_ONLY, answers=False)
    theirs = _reading_on_another_set(session, store, sheet)

    outcome = cut_depth_pictures(
        session, DatabaseStages(store), package_revision_id=sheet.revision.id
    )

    assert (outcome.readings, outcome.cut) == (1, 1)
    assert set(_crops(session, [mine, theirs])) == {mine.id}


def _reading_on_another_set(
    session: Session, store: LocalStore, sheet: _Set
) -> ObservationCandidate:
    """A depth reading on the page of another drawing set."""
    from app.models import PackageRevisionDocument, Page
    from tests.workflow.test_one_drawing_per_check import _drawing, _revision

    revision = _revision(session, store, _drawing("another"))
    version = session.scalars(
        select(PackageRevisionDocument.document_version_id).where(
            PackageRevisionDocument.package_revision_id == revision.id
        )
    ).one()
    page = Page(
        document_version_id=version,
        index=0,
        content_hash="e" * 64,
        width_pt=400,
        height_pt=100,
        rotation=0,
        has_vector_text=True,
        render_failed=False,
        media_box=["0", "0", "400", "100"],
        crop_box=["0", "0", "400", "100"],
    )
    session.add(page)
    session.flush()
    row = ObservationCandidate(
        document_version_id=version,
        page_id=page.id,
        extraction_run_id=sheet.run.id,
        raw_text='9"',
        polygon=[[700, 150], [800, 150], [800, 200], [700, 200]],
        coordinate_space="image",
        ambiguity_flags=[DEPTH_READER_FLAG, CONFIRM_ONLY, f"field:{CT010}"],
    )
    session.add(row)
    session.flush()
    return row


def test_only_depth_readings_are_cut(session: Session, store: LocalStore) -> None:
    sheet = _Set(session, store)
    run = ExtractionRun(
        task_run_id=sheet.run.task_run_id,
        extractor="extraction.slot_reader",
        extractor_version="synthetic",
        config_hash="synthetic",
        dpi=300,
    )
    session.add(run)
    session.flush()
    other = sheet.reading(CT010, 25, CONFIRM_ONLY, run=run, answers=False)

    outcome = cut_depth_pictures(session, DatabaseStages(store))

    assert outcome.readings == 0 and _crops(session, [other]) == {}


# -- at the end of "Read depths again" ---------------------------------------------------------------


def _reading_that_writes_one_row(monkeypatch: pytest.MonkeyPatch, sheet: _Set) -> list[UUID]:
    """The depth reader 'reads' one value on the sheet: a row in the request's own run."""
    import extraction.depth.read as read_module
    from workflow import depth_reader

    written: list[UUID] = []
    real_persist = depth_reader.persist_depth_reading

    def prepare(*_: object, **__: object) -> SimpleNamespace:
        return SimpleNamespace(views=[])

    def read(*_: object, **__: object) -> DepthReading:
        return DepthReading(views=(), questions_asked=0)

    def persist(
        session: Session, revision_id: UUID, run: ExtractionRun, *args: Any, **kw: Any
    ) -> Any:
        report = real_persist(session, revision_id, run, *args, **kw)
        row = ObservationCandidate(
            document_version_id=sheet.page.document_version_id,
            page_id=sheet.page.id,
            extraction_run_id=run.id,
            raw_text='25"',
            polygon=[[700, 150], [800, 150], [800, 200], [700, 200]],
            coordinate_space="image",
            ambiguity_flags=[DEPTH_READER_FLAG, CONFIRM_ONLY, f"field:{CT010}"],
        )
        session.add(row)
        session.flush()
        written.append(row.id)
        return report

    monkeypatch.setattr(read_module, "prepare_pdf_page", prepare)
    monkeypatch.setattr(read_module, "read_depth", read)
    monkeypatch.setattr(depth_reader, "persist_depth_reading", persist)
    return written


def test_read_depths_again_leaves_every_depth_reading_with_a_picture(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    sheet = _read_set(session, store)
    earlier = _readings(sheet)  # from a run that never reached the crop stage
    written = _reading_that_writes_one_row(monkeypatch, sheet)

    result = _stages(store, _Answering()).read_depths_again(session, sheet.revision.id, "r1")

    assert result["ran"] is True and len(written) == 1
    rows = [*earlier, session.get_one(ObservationCandidate, written[0])]
    assert set(_crops(session, rows)) == {row.id for row in rows}
    assert result["pictures"]["cut"] == 3 and result["pictures"]["not_cut"] == 0  # type: ignore[index]
    assert result["reason"] == "Depths read again."


def test_read_depths_again_names_a_reading_left_without_a_picture(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    sheet = _read_set(session, store)
    _readings(sheet)
    _reading_that_writes_one_row(monkeypatch, sheet)
    _too_large_to_render(monkeypatch)

    result = _stages(store, _Answering()).read_depths_again(session, sheet.revision.id, "r2")

    # The reading stands; its pictures are missing, and the reason says so plainly.
    assert result["ran"] is True
    reason = str(result["reason"])
    assert reason.startswith(
        "Depths read again. 3 depth readings have no picture: page 1: "
    ), reason
    assert len(_again_runs(session, sheet)) == 1
