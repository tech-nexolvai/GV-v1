"""Localized OCR and the shape reader are held to the stacked fractions a page holds (#846).

Every lane that reads a stacked fraction must record it with `STACKED_FRACTION_FLAG`, so that no
agreement and no millimetre figure confirms it (#726). Localized OCR did not: a stacked `3/4"` it
read as `3 3/4"` was recorded unflagged, with nothing to tell a person it was a fraction. And
neither it nor the shape reader held a reading to the label's layout, which rules that reading out
by its digit count (#834).

**The false-PASS guards come first.** A reading over a stacked fraction is stored flagged, takes no
lane, and two readers agreeing on it confirm nothing. A reading the layout rules out gets no row:
`28"` and `8 3/4"` for the drawn `28 3/4"`, and the dual reading `730 [28 3/4]`, which the
millimetre lane would otherwise have taken on one reader. The shape reader gets the same check.

The drawing is the real one-page stamp of `tests/workflow/test_stacked_fraction_route.py`: the
synthetic `28 3/4"`, found and laid out by the production detector. The OCR engine is a double that
reads each crop it is handed as the text a test gives it; no model is called.

Verification for: `workflow/stages.py` (`stacked_reading_check`, `_read_page_by_localized_ocr`,
`_read_page_by_glyphs`) and `app/evidence/record.py` (`record_ocr_candidates`).
"""

from __future__ import annotations

import tempfile
from collections import Counter
from collections.abc import Iterator
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.evidence.record import open_extraction_run, record_ocr_candidates
from app.models import ObservationCandidate
from app.models.runs import ExtractionRun
from evidence.candidate import STACKED_FRACTION_FLAG
from evidence.coordinates import ImagePoint
from extraction.annotations import read_annotation_layers
from extraction.glyph_reader import GlyphReading
from extraction.ocr import OcrItem
from storage.local import LocalStore
from tests.extraction.models.test_validation import THIRTY_NINE_AND_A_HALF_LAYOUT, THREE_QUARTERS
from tests.workflow.test_association import LOCALIZED, SETTINGS, _revision, _upgrade
from tests.workflow.test_fraction_parts_route import _copy, _second_reader
from tests.workflow.test_glyph_route import _glyph_rows, _settings, _templates
from tests.workflow.test_markup_route import _SilentOcr
from tests.workflow.test_stacked_fraction_route import PLAIN_SHEET, STACKED_SHEET, _fraction
from units.normalise import normalise_to_inches
from units.notation import canonical_notation
from workflow.glyph_route import GlyphRoute
from workflow.stages import DatabaseStages, _vision_pre_call_refusal, stacked_reading_check

pytest_plugins = ("tests.app.postgres_fixture",)

ASSOCIATION = replace(SETTINGS, proximity_limit=Decimal("0.9"))

#: The localized OCR double's extractor name, so its rows can be told from every other route's.
OCR_EXTRACTOR = "localized-846-ocr"


class _ReadsEachCrop:
    """Reads every crop it is handed as `texts`, the first row above the next, filling the crop.

    One text is one reading over the whole crop. Two are a millimetre row over a bracketed inch row,
    the layout `combine_dual_notation` joins into one dual reading.
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


def _extract(
    session: Session,
    store: LocalStore,
    *texts: str,
    sheet: bytes = STACKED_SHEET,
    glyph_route: GlyphRoute | None = None,
) -> dict[str, object]:
    """One extraction of `sheet` by the localized OCR route, its engine reading `texts` from every
    crop — or nothing, where no text is given."""
    revision = _revision(session, store, data=sheet)
    session.commit()
    (result,) = DatabaseStages(
        store,
        dpi=150,
        association=ASSOCIATION,
        ocr_engine=_ReadsEachCrop(*texts) if texts else _SilentOcr(),  # type: ignore[arg-type]
        localized_ocr=LOCALIZED,
        glyph_route=glyph_route,
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


# ---------------------------------------------------------------------------
# Localized OCR on a stacked label: flagged, or refused
# ---------------------------------------------------------------------------


def test_a_stacked_label_read_by_localized_ocr_is_stored_flagged(
    session: Session, store: LocalStore
) -> None:
    """**Done when, first line.** Read as drawn, `28 3/4"` is stored with its value and
    `STACKED_FRACTION_FLAG`, with no lane: a value a person ticks, never evidence (#726)."""
    payload = _extract(session, store, '28 3/4"')

    rows = _ocr_rows(session)
    assert rows, "localized OCR recorded nothing, so this test proves nothing"
    for row in rows:
        assert row.raw_text == '28 3/4"'
        assert (row.value_numerator, row.value_denominator, row.unit) == (115, 4, "in")
        assert STACKED_FRACTION_FLAG in row.ambiguity_flags
        assert (row.corroboration_status, row.corroboration_lane) == (None, None)
    assert payload["localized_ocr_refusals"] == 0


def test_a_reading_of_the_label_without_its_fraction_is_not_flagged(
    session: Session, store: LocalStore
) -> None:
    """The control: the flag comes from the drawing, not from every localized reading. The sheet
    is the same label with its fraction taken away, read as what it then says."""
    _extract(session, store, '28"', sheet=PLAIN_SHEET)

    rows = _ocr_rows(session)
    assert rows, "localized OCR recorded nothing, so this test proves nothing"
    assert all(STACKED_FRACTION_FLAG not in row.ambiguity_flags for row in rows)


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
def test_a_localized_reading_the_layout_rules_out_is_refused(
    session: Session, store: LocalStore, texts: tuple[str, ...]
) -> None:
    """**Done when, third line.** No row, so nothing pre-fills, nothing agrees with it and no
    millimetre figure confirms it; each refusal is counted on the page, naming the reading."""
    payload = _extract(session, store, *texts)

    assert _ocr_rows(session) == []
    refused = payload["localized_ocr_refusals"]
    assert isinstance(refused, int) and refused >= 1
    reasons = payload["localized_ocr_refusal_reasons"]
    assert isinstance(reasons, list) and reasons
    assert all(repr(" ".join(texts)) in reason for reason in reasons)


def test_the_dual_reading_takes_the_millimetre_lane_off_a_stacked_label(
    session: Session, store: LocalStore
) -> None:
    """The control for the dual case above: where no fraction is drawn the same reading is recorded
    and takes the lane on its own, so it is the drawing that shuts it."""
    _extract(session, store, "730", "[28 3/4]", sheet=PLAIN_SHEET)

    rows = _ocr_rows(session)
    assert rows, "localized OCR recorded nothing, so this test proves nothing"
    assert all(row.raw_text == "730 [28 3/4]" for row in rows)
    assert all(row.corroboration_lane == "DUAL_UNIT" for row in rows)


def test_two_readers_agreeing_on_the_flagged_reading_confirm_nothing(
    session: Session, store: LocalStore
) -> None:
    """**Done when, second line: agreement.** The stored reading, with the flags it was stored
    with, and a second reader of the same box from another route agree exactly — and both stay raw
    candidates with no lane. Without the flag the same pair takes the second-reader lane."""
    _extract(session, store, '28 3/4"')
    reading = _ocr_rows(session)[0]
    second = _second_reader(session, reading)

    flagged = _copy(reading, run_id=reading.extraction_run_id, flags=list(reading.ambiguity_flags))
    agreeing = _copy(reading, run_id=second.id, flags=[])
    session.add_all([flagged, agreeing])
    DatabaseStages._apply_cross_route_corroboration(
        session, page_index=0, candidates=(flagged, agreeing)
    )
    for row in (flagged, agreeing):
        assert (row.corroboration_status, row.corroboration_lane) == (None, None)

    unflagged = _copy(reading, run_id=reading.extraction_run_id, flags=[])
    control = _copy(reading, run_id=second.id, flags=[])
    session.add_all([unflagged, control])
    DatabaseStages._apply_cross_route_corroboration(
        session, page_index=0, candidates=(unflagged, control)
    )
    for row in (unflagged, control):
        assert row.corroboration_lane == "SECOND_READER"


def test_a_flagged_ocr_reading_never_takes_the_millimetre_lane(
    session: Session, store: LocalStore
) -> None:
    """**Done when, second line: the millimetre lane.** Over a fraction set in text there is no
    layout to refuse a dual reading by, so the stage records it flagged. Recorded flagged, it never
    takes the lane; the same reading recorded unflagged does."""
    _extract(session, store, '28 3/4"')
    page_row = _ocr_rows(session)[0]
    first = session.get(ExtractionRun, page_row.extraction_run_id)
    assert first is not None
    dual = OcrItem(
        text="730 [28 3/4]", confidence=Decimal("0.9"), image_extent=_corners((0, 0, 9, 9))
    )

    lanes: dict[bool, ObservationCandidate] = {}
    for stacked in (True, False):
        run = open_extraction_run(
            session,
            task_run_id=first.task_run_id,
            extractor=OCR_EXTRACTOR,
            extractor_version="test/1",
            config_hash=f"stacked={stacked}",
            dpi=first.dpi,
        )
        (lanes[stacked],) = record_ocr_candidates(
            session,
            [replace(dual, stacked=stacked)],
            document_version_id=page_row.document_version_id,
            page_id=page_row.page_id,
            extraction_run_id=run.id,
            page_index=0,
        )

    assert STACKED_FRACTION_FLAG in lanes[True].ambiguity_flags
    assert (lanes[True].corroboration_status, lanes[True].corroboration_lane) == (None, None)
    assert STACKED_FRACTION_FLAG not in lanes[False].ambiguity_flags
    assert lanes[False].corroboration_lane == "DUAL_UNIT"


def test_the_localized_run_names_the_detector_it_was_held_to(
    session: Session, store: LocalStore
) -> None:
    """Which readings are flagged and refused depends on the detector's settings, so they are part
    of the run's identity — fingerprinted, because the column holds 200 characters."""
    _extract(session, store, '28 3/4"')

    run = session.get(ExtractionRun, _ocr_rows(session)[0].extraction_run_id)
    assert run is not None
    assert ";stacked=" in run.config_hash
    assert len(run.config_hash) <= 200


# ---------------------------------------------------------------------------
# The rule itself: the vision readers' overlap, then the layout's digit counts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "box",
    [
        (100, 100, 200, 200),  # the fraction half inside
        (195, 155, 225, 185),  # wholly inside the fraction
        (230, 190, 300, 300),  # touching its corner
        (231, 191, 300, 300),  # one pixel clear of it
        (0, 0, 99, 99),  # elsewhere on the page
    ],
)
def test_a_reading_is_over_a_fraction_exactly_where_a_vision_crop_would_be_refused(
    box: tuple[int, int, int, int],
) -> None:
    """**So the two cannot disagree** (#846): for any box, edges included, the reading check finds
    a stacked fraction exactly where the vision readers' pre-call check refuses a crop."""
    fractions = [_fraction(190, 150, 230, 190)]

    stacked, _ = stacked_reading_check(_corners(box), '28 3/4"', fractions)

    assert stacked == (_vision_pre_call_refusal(box, fractions) is not None)


def test_over_a_laid_out_label_a_reading_is_held_to_its_digit_counts() -> None:
    """#834's false-PASS cases, through the lanes' check: `3 3/4"` on a `3/4"`, `9 1/2"` on a
    `39 1/2"`. What the drawing allows is flagged and kept."""
    (quarters,), (half,) = THREE_QUARTERS, THIRTY_NINE_AND_A_HALF_LAYOUT
    on_quarters = [_fraction(190, 150, 230, 190, quarters)]
    on_half = [_fraction(190, 150, 230, 190, half)]
    over = _corners((180, 140, 240, 200))

    stacked, refusal = stacked_reading_check(over, '3 3/4"', on_quarters)
    assert stacked and refusal is not None and "'3 3/4\"'" in refusal
    stacked, refusal = stacked_reading_check(over, '9 1/2"', on_half)
    assert stacked and refusal is not None
    assert stacked_reading_check(over, '3/4"', on_quarters) == (True, None)
    assert stacked_reading_check(over, '39 1/2"', on_half) == (True, None)


def test_a_fraction_set_in_text_flags_and_never_refuses() -> None:
    """Its characters are text, not paths, so there is nothing to count a reading against."""
    in_text = [_fraction(190, 150, 230, 190)]

    assert stacked_reading_check(_corners((180, 140, 240, 200)), '3 3/4"', in_text) == (True, None)


def test_a_reading_clear_of_every_fraction_is_neither_flagged_nor_refused() -> None:
    (quarters,) = THREE_QUARTERS
    fractions = [_fraction(190, 150, 230, 190, quarters)]

    assert stacked_reading_check(_corners((0, 0, 99, 99)), '3 3/4"', fractions) == (False, None)


# ---------------------------------------------------------------------------
# The shape reader: the same check
# ---------------------------------------------------------------------------


def _glyph_reads(monkeypatch: pytest.MonkeyPatch, text: str, *, stacked: bool) -> None:
    """Make the shape reader read the sheet's one label as `text`, over the label's own box."""
    layers = read_annotation_layers(
        STACKED_SHEET,
        0,
        document_version_id=UUID(int=0),
        dpi=150,
        line_minimum_pt=ASSOCIATION.line_minimum_pt,
        glyph_maximum_pt=ASSOCIATION.glyph_maximum_pt,
        glyph_gap_pt=ASSOCIATION.glyph_gap_pt,
        fraction_bar=ASSOCIATION.fraction_bar,
    )
    (fraction,) = layers.stacked_fractions
    assert fraction.layout is not None
    reading = GlyphReading(
        text=text,
        value=normalise_to_inches(canonical_notation(text)[0]),
        rotation_degrees=0,
        box=fraction.layout.box,
        template_set="e" * 64,
        stacked=stacked,
    )

    def read_page_labels(*_args: object) -> tuple[list[GlyphReading], Counter[str]]:
        return [reading], Counter()

    monkeypatch.setattr("workflow.stages.read_page_labels", read_page_labels)


def test_a_glyph_reading_the_layout_rules_out_is_refused(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**Done when, third line, for the shape reader.** Composed across the bar but a digit short,
    `8 3/4"` gets no row and abstains under the sentence saying why."""
    _glyph_reads(monkeypatch, '8 3/4"', stacked=True)

    payload = _extract(session, store, glyph_route=GlyphRoute(_templates(), _settings()))

    assert _glyph_rows(session) == []
    assert payload["glyph_abstentions"] == 1
    (reason,) = payload["glyph_abstention_reasons"]  # type: ignore[misc]
    assert "'8 3/4\"'" in reason


@pytest.mark.parametrize("composed_across_a_bar", [True, False])
def test_a_glyph_reading_over_a_stacked_fraction_is_flagged(
    session: Session,
    store: LocalStore,
    monkeypatch: pytest.MonkeyPatch,
    composed_across_a_bar: bool,
) -> None:
    """What the drawing allows is recorded flagged — whether or not the shape reader itself saw the
    bar, because the page's detector did."""
    _glyph_reads(monkeypatch, '28 3/4"', stacked=composed_across_a_bar)

    payload = _extract(session, store, glyph_route=GlyphRoute(_templates(), _settings()))

    (row,) = _glyph_rows(session)
    assert row.ambiguity_flags == [STACKED_FRACTION_FLAG]
    assert row.corroboration_lane is None
    assert payload["glyph_abstentions"] == 0
