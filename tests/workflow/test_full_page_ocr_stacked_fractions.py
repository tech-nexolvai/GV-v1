"""Full-page OCR is held to the stacked fractions a page holds, as localized OCR is (#896).

#846 held every localized OCR reading to the page's stacked fractions. Full-page OCR, the route for
scanned pages and the fallback where a page has no recorded transform, was not held to them: a
stacked `3/4"` it read as `3 3/4"` was stored with no flag.

**Both ways into full-page OCR are tested**: the direct route, taken where a deployment has no
localized OCR settings, and the fallback inside the localized route, taken where the page manifest
recorded no transform. On each, a reading over the fraction is stored flagged, and one the drawn
layout rules out gets no row and is counted on the page.

**Where the page's fractions are not known, nothing is held**, and one test pins that as the
known limit it is: without the detector, the same wrong reading is stored unflagged, exactly as
the localized route and the vision readers treat a page whose fractions are not known.

The drawing is the real one-page stamp of `tests/workflow/test_stacked_fraction_route.py`, its
synthetic `28 3/4"` found and laid out by the production detector. The OCR engine is a double that
reads the page it is handed as the text a test gives it; no model is called.

Verification for: `workflow/stages.py` (`_read_page_by_ocr`, `_held_to_stacked_fractions`,
`_stacked_config`).
"""

from __future__ import annotations

import re
import tempfile
from collections.abc import Iterator
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

import workflow.stages as stages_module
from app.models import ObservationCandidate
from app.models.runs import ExtractionRun
from evidence.candidate import STACKED_FRACTION_FLAG
from evidence.coordinates import ImagePoint
from extraction.annotations import read_annotation_layers
from extraction.manifest import PageManifest
from extraction.ocr import OcrItem
from storage.local import LocalStore
from tests.extraction.test_reader import MISSING_SPACE
from tests.workflow.test_association import LOCALIZED, _revision, _upgrade
from tests.workflow.test_localized_ocr_stacked_fractions import ASSOCIATION
from tests.workflow.test_stacked_fraction_route import PLAIN_SHEET, STACKED_SHEET
from workflow.stages import DatabaseStages, _held_to_stacked_fractions

pytest_plugins = ("tests.app.postgres_fixture",)

#: The full-page OCR double's extractor name, so its rows can be told from every other route's.
OCR_EXTRACTOR = "full-page-896-ocr"

#: The stage's dpi in every test here, and the detector's when a test asks where the fraction is.
DPI = 150

#: The two ways a page reaches full-page OCR.
ROUTES = ("direct", "no transform")


class _ReadsThePage:
    """Reads the page it is handed as `texts`, the first row above the next, filling the page.

    One text is one reading over the whole page, so over the fraction wherever the sheet draws it.
    Two are a millimetre row over a bracketed inch row, the layout `combine_dual_notation` joins
    into one dual reading.
    """

    name = OCR_EXTRACTOR
    version = "test/1"

    def __init__(self, *texts: str) -> None:
        self.texts = texts

    def read(self, rgb: bytes, *, width: int, height: int) -> tuple[OcrItem, ...]:
        assert rgb
        rows = len(self.texts)
        return tuple(
            OcrItem(
                text=text,
                confidence=Decimal("0.9"),
                image_extent=(
                    ImagePoint(0, (height - 1) * index // rows),
                    ImagePoint(width - 1, (height - 1) * index // rows),
                    ImagePoint(width - 1, (height - 1) * (index + 1) // rows),
                    ImagePoint(0, (height - 1) * (index + 1) // rows),
                ),
            )
            for index, text in enumerate(self.texts)
        )


class _ReadsOneBox:
    """Reads `text` at one box of page pixels, wherever on the page the test puts it."""

    name = OCR_EXTRACTOR
    version = "test/1"

    def __init__(self, text: str, box: tuple[int, int, int, int]) -> None:
        self.text = text
        self.box = box

    def read(self, rgb: bytes, *, width: int, height: int) -> tuple[OcrItem, ...]:
        left, top, right, bottom = self.box
        assert 0 <= left < right < width and 0 <= top < bottom < height
        return (
            OcrItem(text=self.text, confidence=Decimal("0.9"), image_extent=_corners(self.box)),
        )


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    from app.db.session import session_factory

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


def _without_transforms(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the manifest record no transform for any page, as a reader that could not report one
    does: the localized route then falls back to full-page OCR."""
    build = stages_module.build_manifest

    def build_without(*args: Any, **kwargs: Any) -> PageManifest:
        manifest = build(*args, **kwargs)
        return replace(
            manifest,
            pages=tuple(
                replace(record, media_box=None, crop_box=None) for record in manifest.pages
            ),
        )

    monkeypatch.setattr(stages_module, "build_manifest", build_without)


def _extract(
    session: Session,
    store: LocalStore,
    engine: object,
    *,
    route: str = "direct",
    sheet: bytes = STACKED_SHEET,
    detector: bool = True,
) -> dict[str, object]:
    """One extraction of `sheet` that reaches full-page OCR by `route`, read by `engine`.

    `direct` is a deployment with the detector's settings and no localized OCR settings; `no
    transform` has both, on a page whose manifest recorded no transform (the caller patches that).
    `detector=False` gives the stage no association settings, so no stacked fraction is found.
    """
    revision = _revision(session, store, data=sheet)
    session.commit()
    (result,) = DatabaseStages(
        store,
        dpi=DPI,
        association=ASSOCIATION if detector else None,
        ocr_engine=engine,  # type: ignore[arg-type]
        localized_ocr=LOCALIZED if route == "no transform" else None,
        missing_space=MISSING_SPACE,
    ).extract_pages(session, revision.id)
    session.commit()
    return dict(result.payload)


def _ocr_rows(session: Session) -> list[ObservationCandidate]:
    return list(
        session.execute(
            select(ObservationCandidate)
            .join(ExtractionRun, ExtractionRun.id == ObservationCandidate.extraction_run_id)
            .where(ExtractionRun.extractor == OCR_EXTRACTOR)
        ).scalars()
    )


def _corners(box: tuple[int, int, int, int]) -> tuple[ImagePoint, ...]:
    left, top, right, bottom = box
    return (
        ImagePoint(left, top),
        ImagePoint(right, top),
        ImagePoint(right, bottom),
        ImagePoint(left, bottom),
    )


def _fraction_box() -> tuple[int, int, int, int]:
    """Where the production detector finds the sheet's stacked fraction, in page pixels at `DPI`."""
    layers = read_annotation_layers(
        STACKED_SHEET,
        0,
        document_version_id=UUID(int=0),
        dpi=DPI,
        line_minimum_pt=ASSOCIATION.line_minimum_pt,
        glyph_maximum_pt=ASSOCIATION.glyph_maximum_pt,
        glyph_gap_pt=ASSOCIATION.glyph_gap_pt,
        fraction_bar=ASSOCIATION.fraction_bar,
    )
    (fraction,) = layers.stacked_fractions
    xs = [corner.x for corner in fraction.image_extent]
    ys = [corner.y for corner in fraction.image_extent]
    return min(xs), min(ys), max(xs), max(ys)


@pytest.fixture(params=ROUTES)
def route(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    if request.param == "no transform":
        _without_transforms(monkeypatch)
    return str(request.param)


# ---------------------------------------------------------------------------
# Full-page OCR on a stacked label: flagged, or refused
# ---------------------------------------------------------------------------


def test_a_stacked_label_read_by_full_page_ocr_is_stored_flagged(
    session: Session, store: LocalStore, route: str
) -> None:
    """**Done when, first line.** Read as drawn, `28 3/4"` is stored with its value and
    `STACKED_FRACTION_FLAG`, with no lane: a value a person ticks, never evidence (#726)."""
    payload = _extract(session, store, _ReadsThePage('28 3/4"'), route=route)

    rows = _ocr_rows(session)
    assert len(rows) == 1, "full-page OCR recorded nothing, so this test proves nothing"
    (row,) = rows
    assert row.raw_text == '28 3/4"'
    assert (row.value_numerator, row.value_denominator, row.unit) == (115, 4, "in")
    assert STACKED_FRACTION_FLAG in row.ambiguity_flags
    assert (row.corroboration_status, row.corroboration_lane) == (None, None)
    assert payload["ocr_refusals"] == 0


@pytest.mark.parametrize(
    "texts",
    [
        # The fraction dropped (#541): a whole number where the drawing has a whole and a fraction.
        ('28"',),
        # A whole-number digit dropped, as `9 1/2"` for a `39 1/2"`: right shape, wrong count.
        ('8 3/4"',),
        # The dual reading the millimetre lane takes on one reader: 28 3/4" is 730.25 mm.
        ("730", "[28 3/4]"),
    ],
)
def test_a_full_page_reading_the_layout_rules_out_is_refused(
    session: Session, store: LocalStore, route: str, texts: tuple[str, ...]
) -> None:
    """**Done when, first line.** No row, so nothing pre-fills, nothing agrees with it and no
    millimetre figure confirms it; each refusal is counted on the page, naming the reading."""
    payload = _extract(session, store, _ReadsThePage(*texts), route=route)

    assert _ocr_rows(session) == []
    assert payload["ocr_refusals"] == 1
    reasons = payload["ocr_refusal_reasons"]
    assert isinstance(reasons, list) and len(reasons) == 1
    assert repr(" ".join(texts)) in reasons[0]


def test_a_reading_of_the_label_without_its_fraction_is_recorded_unflagged(
    session: Session, store: LocalStore, route: str
) -> None:
    """The control: the flag and the refusal come from the drawing, not from every full-page
    reading. The sheet is the same label with its fraction taken away, read as what it then says,
    and the dual reading takes the millimetre lane on its own there."""
    payload = _extract(
        session, store, _ReadsThePage("730", "[28 3/4]"), route=route, sheet=PLAIN_SHEET
    )

    (row,) = _ocr_rows(session)
    assert row.raw_text == "730 [28 3/4]"
    assert STACKED_FRACTION_FLAG not in row.ambiguity_flags
    assert row.corroboration_lane == "DUAL_UNIT"
    assert payload["ocr_refusals"] == 0


def test_the_no_transform_cases_reach_the_fallback(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**So the `no transform` cases above test the fallback**, not the localized route: the page
    takes the localized route, and its reading is recorded under the full-page run, not a crop run.
    """
    _without_transforms(monkeypatch)
    payload = _extract(session, store, _ReadsThePage('28 3/4"'), route="no transform")

    assert payload["route"] == "localized_ocr"
    (row,) = _ocr_rows(session)
    run = session.get(ExtractionRun, row.extraction_run_id)
    assert run is not None
    assert "layers=vendor" in run.config_hash
    assert "route=localized_vendor_regions" not in run.config_hash


# ---------------------------------------------------------------------------
# The engine's boxes and the detector's are the same pixels
# ---------------------------------------------------------------------------


def test_a_reading_on_the_fraction_is_flagged_and_one_pixel_clear_is_not(
    session: Session, store: LocalStore
) -> None:
    """**The frame the docstring claims.** The engine's box is compared with the fraction's
    `image_extent` as it comes, no conversion: a reading over exactly the detector's box is held,
    and the same reading one pixel to its right, touching nothing, is recorded as read."""
    left, top, right, bottom = _fraction_box()

    _extract(session, store, _ReadsOneBox('28 3/4"', (left, top, right, bottom)))
    (over,) = _ocr_rows(session)
    assert STACKED_FRACTION_FLAG in over.ambiguity_flags

    clear = (right + 1, top, right + 1 + (right - left), bottom)
    _extract(session, store, _ReadsOneBox('28 3/4"', clear))
    (beside,) = [row for row in _ocr_rows(session) if row.id != over.id]
    assert STACKED_FRACTION_FLAG not in beside.ambiguity_flags


# ---------------------------------------------------------------------------
# Where the page's fractions are not known
# ---------------------------------------------------------------------------


def test_without_the_detector_a_full_page_reading_is_recorded_as_read(
    session: Session, store: LocalStore
) -> None:
    """**The known limit, pinned so it is not mistaken for a guard.** With no association settings
    the detector does not run, as on a scan there are no paths for it to find a fraction in. The
    same wrong reading the drawn layout refuses above is then recorded, unflagged: nothing on the
    page says it is a fraction. The vision readers and the localized route do the same."""
    payload = _extract(session, store, _ReadsThePage('28"'), detector=False)

    (row,) = _ocr_rows(session)
    assert row.raw_text == '28"'
    assert STACKED_FRACTION_FLAG not in row.ambiguity_flags
    assert payload["ocr_refusals"] == 0


def test_with_no_fractions_every_reading_comes_back_as_it_was() -> None:
    """The helper's own promise: no fractions, nothing flagged, nothing left out, order kept."""
    readings = tuple(
        OcrItem(text=text, confidence=Decimal("0.9"), image_extent=_corners(box))
        for text, box in (('28"', (0, 0, 9, 9)), ('3 3/4"', (20, 0, 29, 9)))
    )
    held, refusals = _held_to_stacked_fractions(readings, ())
    assert held == readings
    assert not refusals


# ---------------------------------------------------------------------------
# The run's identity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("detector", [True, False])
def test_the_full_page_run_names_what_its_readings_were_held_to(
    session: Session, store: LocalStore, detector: bool
) -> None:
    """A run from before #896 holds readings that were never held to a fraction, so it must not be
    reused as one that was: the run's identity names the detector's settings, fingerprinted to fit
    the column's 200 characters, or `text` where only fractions set in text can be known."""
    _extract(session, store, _ReadsThePage('28 3/4"'), detector=detector)

    run = session.get(ExtractionRun, _ocr_rows(session)[0].extraction_run_id)
    assert run is not None
    if detector:
        assert re.search(r";stacked=[0-9a-f]{16}$", run.config_hash)
    else:
        assert run.config_hash.endswith(";stacked=text")
    assert len(run.config_hash) <= 200
