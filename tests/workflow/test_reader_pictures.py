"""What each vision reader is shown: the crop as cut, or the label upright and sharper (#907).

Verification for: `workflow/reader_pictures.py`, and `workflow/stages.py` where the vision route shows
each reader its picture (`_read_page_by_vision`, `DatabaseStages(reader_pictures=...)`).

**The admin's rules, held here.** Sharper means the vector page rendered again at a stated higher
dpi, never an upscaled crop. The upright turn comes from the drawing's own rotation facts — the
direction the file prints a label's text in, else the direction its glyph paths run — never from an
answer key's mark. Qwen3-VL keeps the crop as cut. The drawings are made up; no model is called.
"""

from __future__ import annotations

import hashlib
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.db.session import session_factory
from app.models import ExtractionRun, ObservationCandidate
from evidence.candidate import ObservationCandidate as DomainCandidate
from evidence.coordinates import ImagePoint, StoredPoint
from evidence.crop import decode_rgb_png, encode_png
from evidence.polygon import Polygon
from extraction.models.context import AssembledContext
from extraction.models.nova import (
    NOVA_2_LITE_TAUGHT_EXTRACTOR,
    QWEN3_VL_235B_EXTRACTOR,
    NovaConfig,
    NovaInvocation,
    NovaInvocationOutcome,
    NovaRequest,
    ReaderPicture,
    vision_config_for_extractor,
)
from extraction.rasterise import render_page, render_region
from extraction.reader import TextItem, read_page_contents, read_pages
from extraction.vector_first import upright_png
from storage.local import LocalStore
from tests.extraction.test_reader import MISSING_SPACE, _pdf
from tests.workflow.test_gv_mark_guard import _revision, _upgrade
from units.measurement import Unit
from workflow.reader_pictures import (
    SHARPER_PICTURE_DPI_ENV,
    PictureSettings,
    PrintedRun,
    label_turn,
    picture_settings_from_environment,
    printed_runs,
    reader_picture,
    sharper_box,
)
from workflow.stages import (
    MAXIMUM_RENDER_PIXELS,
    VISION_CROP_CONTEXT_MARGIN_PT,
    DatabaseStages,
    _VisionRegion,
    crop_box_px,
    stored_polygon,
)

pytest_plugins = ("tests.app.postgres_fixture",)

# ---------------------------------------------------------------------------
# Which way a label runs: the drawing's own facts
# ---------------------------------------------------------------------------

REGION = (100, 100, 140, 200)


def _run(box: tuple[int, int, int, int], degrees: int) -> PrintedRun:
    return PrintedRun(box=box, rotation_degrees=degrees)


@pytest.mark.parametrize("degrees", [90, 270])
def test_a_label_printed_sideways_is_turned_by_its_own_direction(degrees: int) -> None:
    assert label_turn(REGION, [_run((110, 120, 130, 180), degrees)], geometry_degrees=0) == degrees


def test_the_printed_direction_speaks_before_the_glyph_paths() -> None:
    """The file's own text says how it is printed; where it says upright, the label is upright."""
    assert label_turn(REGION, [_run((110, 120, 130, 180), 0)], geometry_degrees=90) == 0


def test_runs_that_disagree_settle_nothing() -> None:
    """An upright label touching a sideways one: which is this region's label is not settled, so it
    is read as it stands, as the geometry rule reads a label with runs both ways (#783)."""
    printed = [_run((110, 120, 130, 180), 90), _run((95, 95, 105, 105), 0)]
    assert label_turn(REGION, printed, geometry_degrees=90) == 0


def test_with_no_printed_text_the_glyph_paths_say() -> None:
    assert label_turn(REGION, [], geometry_degrees=90) == 90
    assert label_turn(REGION, [_run((300, 300, 310, 310), 0)], geometry_degrees=90) == 90
    assert label_turn(REGION, [], geometry_degrees=0) == 0


def test_upside_down_text_is_read_as_it_stands() -> None:
    """Only the two quarter turns are undone (`upright_png`)."""
    assert label_turn(REGION, [_run((110, 120, 130, 180), 180)], geometry_degrees=0) == 0
    assert label_turn(REGION, [], geometry_degrees=180) == 0


def test_text_touching_the_region_edge_counts() -> None:
    assert label_turn(REGION, [_run((140, 150, 150, 160), 90)], geometry_degrees=0) == 90
    assert label_turn(REGION, [_run((141, 150, 150, 160), 90)], geometry_degrees=0) == 0


def _item(text: str, degrees: int, corners: tuple[tuple[int, int], ...]) -> TextItem:
    return TextItem(
        text=text,
        extent=Polygon(
            points=(
                StoredPoint(Decimal("0.1"), Decimal("0.1")),
                StoredPoint(Decimal("0.2"), Decimal("0.1")),
                StoredPoint(Decimal("0.2"), Decimal("0.2")),
            ),
            space="stored",
            document_version_id=uuid4(),
            page=0,
        ),
        image_extent=tuple(ImagePoint(x, y) for x, y in corners),
        rotation_degrees=degrees,
        upright=degrees == 0,
    )


def test_only_runs_holding_a_numeral_speak_for_a_label() -> None:
    """A word beside a label says nothing about which way the label runs."""
    runs = printed_runs(
        [
            _item("SINK", 0, ((1, 2), (9, 2), (9, 7), (1, 7))),
            _item("102", 90, ((10, 20), (14, 20), (14, 40), (10, 40))),
            _item("½", 270, ((50, 60), (54, 60), (54, 64), (50, 64))),
        ]
    )
    assert runs == (
        PrintedRun(box=(10, 20, 14, 40), rotation_degrees=90),
        PrintedRun(box=(50, 60, 54, 64), rotation_degrees=270),
    )


# ---------------------------------------------------------------------------
# Sharper: the same page area, rendered at the stated dpi
# ---------------------------------------------------------------------------


def test_the_sharper_box_covers_the_crop_exactly_at_a_multiple_of_the_stage_dpi() -> None:
    assert sharper_box((10, 21, 40, 55), base_dpi=300, dpi=900) == (30, 63, 120, 165)


def test_the_sharper_box_is_scaled_out_never_in() -> None:
    """At 1.5 times, an odd edge is rounded outward, so the picture shows all the crop showed."""
    assert sharper_box((11, 21, 41, 55), base_dpi=300, dpi=450) == (16, 31, 62, 83)


def _render(box: tuple[int, int, int, int], dpi: int) -> bytes:
    width, height = box[2] - box[0], box[3] - box[1]
    shade = dpi % 251
    pixels = bytearray()
    for y in range(height):
        for x in range(width):
            pixels += bytes([shade, (x * 7) % 256, (y * 13) % 256])
    return encode_png(width, height, bytes(pixels))


def test_a_reader_shown_the_crop_as_cut_gets_exactly_its_bytes() -> None:
    def never(box: tuple[int, int, int, int], dpi: int) -> bytes:
        raise AssertionError("nothing is rendered for a reader shown the crop as cut")

    as_cut = encode_png(2, 3, bytes(18))
    shown = reader_picture(
        ReaderPicture.AS_CUT,
        as_cut=as_cut,
        crop_box=(0, 0, 2, 3),
        base_dpi=300,
        settings=None,
        turn=90,
        render=never,
    )
    assert shown is as_cut


def test_a_sharper_picture_is_rendered_at_the_stated_dpi_and_turned_by_the_drawing() -> None:
    asked: list[tuple[tuple[int, int, int, int], int]] = []

    def render(box: tuple[int, int, int, int], dpi: int) -> bytes:
        asked.append((box, dpi))
        return _render(box, dpi)

    common = {
        "as_cut": b"unused",
        "crop_box": (10, 20, 30, 70),
        "base_dpi": 300,
        "settings": PictureSettings(sharper_dpi=900),
        "render": render,
    }
    upright = reader_picture(ReaderPicture.UPRIGHT_SHARPER, turn=0, **common)  # type: ignore[arg-type]
    turned = reader_picture(ReaderPicture.UPRIGHT_SHARPER, turn=90, **common)  # type: ignore[arg-type]

    assert asked == [((30, 60, 90, 210), 900)] * 2
    assert upright == _render((30, 60, 90, 210), 900)
    assert decode_rgb_png(turned)[:2] == (150, 60)
    assert turned == upright_png(upright, label_rotation_degrees=90)


def test_a_sharper_picture_needs_a_stated_dpi_above_the_stage_s() -> None:
    common = {"as_cut": b"", "crop_box": (0, 0, 2, 2), "base_dpi": 300, "turn": 0}
    with pytest.raises(ValueError, match=SHARPER_PICTURE_DPI_ENV):
        reader_picture(ReaderPicture.UPRIGHT_SHARPER, settings=None, render=_render, **common)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="above the stage"):
        reader_picture(
            ReaderPicture.UPRIGHT_SHARPER,
            settings=PictureSettings(sharper_dpi=300),
            render=_render,
            **common,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("stated", "expected"),
    [("", None), ("   ", None), ("900", PictureSettings(sharper_dpi=900))],
)
def test_the_sharper_dpi_is_stated_or_absent_never_defaulted(
    stated: str, expected: PictureSettings | None
) -> None:
    assert picture_settings_from_environment({SHARPER_PICTURE_DPI_ENV: stated}) == expected
    assert picture_settings_from_environment({}) is None


@pytest.mark.parametrize("stated", ["900.5", "9e2", "-900", "0", "nine hundred", "９００"])
def test_a_sharper_dpi_that_is_not_a_whole_number_is_refused(stated: str) -> None:
    with pytest.raises(ValueError):
        picture_settings_from_environment({SHARPER_PICTURE_DPI_ENV: stated})


# ---------------------------------------------------------------------------
# Through the stage
# ---------------------------------------------------------------------------

#: A 200 × 100 pt page: `24 1/2"` printed reading up the page, and `36"` printed across it.
SIDEWAYS = b'BT /F1 10 Tf 0 1 -1 0 40 20 Tm (24 1/2") Tj ET\n'
ACROSS = b'BT /F1 10 Tf 1 0 0 1 120 60 Tm (36") Tj ET\n'
SHEET = _pdf(SIDEWAYS + ACROSS)
SHARPER = PictureSettings(sharper_dpi=900)


@dataclass
class _Recorder:
    """Stands in for a model: records each picture it is shown, by request."""

    config: NovaConfig
    shown: dict[str, bytes] = field(default_factory=dict)

    def extract(self, request: NovaRequest, recorder: object) -> DomainCandidate:
        recorder.record(  # type: ignore[attr-defined]
            NovaInvocation(
                model_id=self.config.model_id,
                prompt_id=self.config.prompt_id,
                template_id=self.config.template_id,
                attempt=1,
                latency_ms=1,
                input_tokens=10,
                output_tokens=2,
                outcome=NovaInvocationOutcome.OK,
                request_id="r",
                context=AssembledContext(nearby_text=(), nearby_geometry=()),
                bound_pt=Decimal(9),
                injection_attempts=(),
            )
        )
        self.shown[request.candidate_id] = request.crop
        return DomainCandidate(
            candidate_id=request.candidate_id,
            extractor=self.config.extractor,
            extractor_version=self.config.model_id,
            raw_text='24"',
            parsed_value=None,
            unit_guess=Unit.INCH,
            semantic_guess=None,
            page=request.page,
            polygon=(ImagePoint(0, 0), ImagePoint(1, 0), ImagePoint(1, 1)),
            confidence=None,
            ambiguity_flags=(),
        )


def _pair() -> tuple[_Recorder, _Recorder]:
    qwen = vision_config_for_extractor(QWEN3_VL_235B_EXTRACTOR)
    nova = vision_config_for_extractor(NOVA_2_LITE_TAUGHT_EXTRACTOR)
    assert qwen is not None and nova is not None
    return _Recorder(qwen), _Recorder(nova)


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


def _crop_boxes(dpi: int) -> dict[str, tuple[int, int, int, int]]:
    """Each printed label's crop box at `dpi`, from the stage's own reading of the page."""
    version = uuid4()
    rendered = render_page(
        SHEET,
        0,
        document_version_id=version,
        page_content_hash=hashlib.sha256(read_pages(SHEET)[0].content).hexdigest(),
        dpi=dpi,
        maximum_pixels=MAXIMUM_RENDER_PIXELS,
        vendor_only=True,
    )
    boxes: dict[str, tuple[int, int, int, int]] = {}
    for item in read_page_contents(
        SHEET, 0, document_version_id=version, dpi=dpi, missing_space=MISSING_SPACE
    ).texts:
        region = _VisionRegion(
            id=uuid4(),
            polygon=[[point.x, point.y] for point in item.image_extent],
            document_version_id=version,
        )
        polygon = stored_polygon(region, rendered)
        assert polygon is not None
        boxes[item.text] = crop_box_px(rendered, polygon, VISION_CROP_CONTEXT_MARGIN_PT)
    return boxes


def test_each_reader_is_shown_its_own_measured_picture(session: Session, store: LocalStore) -> None:
    """**Done when: Nova sees the label upright and sharper; Qwen sees the crop as cut.** The label
    printed up the page reaches Nova 2 Lite turned by the file's own text direction and rendered at
    the stated 900 dpi from the vector page — undo the turn and it is exactly that page area at
    900 dpi. The label printed across reaches it sharper and unturned. Qwen3-VL is shown the stage's
    crops, at the stage's dpi."""
    qwen, nova = _pair()
    revision = _revision(session, store, SHEET)
    stages = DatabaseStages(
        store, vision_readers=(qwen, nova), reader_pictures=SHARPER, missing_space=MISSING_SPACE
    )

    stages.extract_pages(session, revision.id)

    boxes = _crop_boxes(300)
    cut = {"sideways": boxes['24 1/2"'], "across": boxes['36"']}
    for picture in qwen.shown.values():
        width, height, _ = decode_rgb_png(picture)
        assert (width, height) in {(b[2] - b[0], b[3] - b[1]) for b in cut.values()}
    assert len(nova.shown) == 2
    sideways = sharper_box(cut["sideways"], base_dpi=300, dpi=900)
    across = sharper_box(cut["across"], base_dpi=300, dpi=900)

    def rendered(box: tuple[int, int, int, int]) -> bytes:
        return render_region(
            SHEET, 0, box_px=box, dpi=900, maximum_pixels=MAXIMUM_RENDER_PIXELS, vendor_only=True
        )

    # The label printed up the page: the page area at 900 dpi, tall, turned to lie across.
    turned = upright_png(rendered(sideways), label_rotation_degrees=90)
    assert decode_rgb_png(turned)[:2] == (sideways[3] - sideways[1], sideways[2] - sideways[0])
    assert turned in nova.shown.values()
    assert upright_png(turned, label_rotation_degrees=270) == rendered(sideways)
    # The label printed across: the page area at 900 dpi, as it stands.
    assert rendered(across) in nova.shown.values()


def test_the_picture_is_part_of_the_reading_run_s_identity(
    session: Session, store: LocalStore
) -> None:
    """A reader shown another picture read another thing: its run says which, at which dpi."""
    qwen, nova = _pair()
    revision = _revision(session, store, SHEET)

    DatabaseStages(
        store, vision_readers=(qwen, nova), reader_pictures=SHARPER, missing_space=MISSING_SPACE
    ).extract_pages(session, revision.id)

    runs = {
        run.extractor: run.config_hash
        for run in session.execute(select(ExtractionRun)).scalars()
        if run.extractor in {QWEN3_VL_235B_EXTRACTOR, NOVA_2_LITE_TAUGHT_EXTRACTOR}
    }
    assert "picture=" not in runs[QWEN3_VL_235B_EXTRACTOR]
    assert (
        "picture=upright_sharper;sharper_picture_dpi=900;run=" in runs[NOVA_2_LITE_TAUGHT_EXTRACTOR]
    )
    rows = session.execute(select(ObservationCandidate)).scalars().all()
    assert {row.raw_text for row in rows} >= {'24 1/2"', '36"'}


def test_a_stage_with_a_reader_shown_a_sharper_picture_states_its_dpi() -> None:
    """**No default.** Unstated, the reader could only be shown a picture nobody measured it on."""
    qwen, nova = _pair()
    with pytest.raises(ValueError, match="GV_VISION_SHARPER_PICTURE_DPI"):
        DatabaseStages(None, vision_readers=(qwen, nova))
    with pytest.raises(ValueError, match="above the stage"):
        DatabaseStages(
            None, vision_readers=(qwen, nova), reader_pictures=PictureSettings(sharper_dpi=300)
        )
    DatabaseStages(None, vision_readers=(qwen,))
