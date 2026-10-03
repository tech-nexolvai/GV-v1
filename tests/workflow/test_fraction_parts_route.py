"""Stacked fractions read piece by piece, as a pipeline route (#848).

Verification for: `_read_page_by_fraction_parts` and its wiring in `workflow/stages.py`, and
`fraction_parts_from_environment`.

**The false-PASS guards come first.** A reading put together from the pieces of a stacked label is
stored with `STACKED_FRACTION_FLAG`; two readers agreeing on it leave it a raw candidate with no
lane, where the same row without its flag would take the second-reader lane; automatic typing
refuses it. It pre-fills the form for a person to tick and is never evidence on its own (#726).

The drawing is a real one-page PDF whose stamp draws the synthetic `28 3/4"` of
`tests/extraction/test_annotations.py`. Its digits are plotter-shaped strokes, not numerals, so the
OCR engine is a double: it answers each piece the route draws in turn, and reads every other
picture as the whole crop it was handed. No client drawing is read and no model is called.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.evidence.automatic_typing import AutomaticTypingSettings, qualify_exact_tag_pair
from app.evidence.record import open_extraction_run
from app.models import ObservationCandidate
from app.models.runs import ExtractionRun
from evidence.candidate import STACKED_FRACTION_FLAG
from evidence.coordinates import ImagePoint
from evidence.semantic_typing import SemanticTypingDecision, TypingDisposition
from extraction import fraction_parts
from extraction.fraction_parts import FRACTION_PARTS_EXTRACTOR, PieceDrawing
from extraction.ocr import OcrItem
from storage.local import LocalStore
from tests.extraction.test_annotations import STACKED_APPEARANCE, _appearance, _pdf, _stamp
from tests.workflow.test_association import LOCALIZED, SETTINGS, _revision, _upgrade
from tests.workflow.test_stacked_fraction_route import DIMENSION_LINE, STACKED_SHEET, _reader
from vocabulary.semantic_types import SemanticType
from workflow.stages import (
    FRACTION_PARTS_ENV,
    FRACTION_PARTS_SETTINGS,
    DatabaseStages,
    fraction_parts_from_environment,
)

pytest_plugins = ("tests.app.postgres_fixture",)

ASSOCIATION = replace(SETTINGS, proximity_limit=Decimal("0.9"))
DRAWING = PieceDrawing(height_px=40, stroke_px=4, bezier_steps=8, margin_px=32)

#: What a person reads on the synthetic label, piece by piece: `28`, `3`, `4`.
PIECES: list[tuple[str, ...]] = [("28",), ("3",), ("4",)]


class _Engine:
    """Answers each piece the route draws in turn; reads any other picture as one `28"` filling it.

    **A piece is told apart by how it was made.** The route draws a piece: pure black on white. The
    localized OCR route shares this engine and hands it crops of a page render, whose thin lines are
    smoothed into grey. The tests count the pieces shown, so a picture taken for the wrong kind
    fails one rather than passing quietly.
    """

    name = "fraction-route-ocr"
    version = "test/1"

    def __init__(self, pieces: list[tuple[str, ...]]) -> None:
        self.pieces = pieces
        self.pieces_shown = 0

    def read(self, rgb: bytes, *, width: int, height: int) -> tuple[OcrItem, ...]:
        if set(np.unique(np.frombuffer(rgb, dtype=np.uint8))) <= {0, 255}:
            texts = self.pieces[self.pieces_shown]
            self.pieces_shown += 1
            corners = (ImagePoint(0, 0), ImagePoint(1, 0), ImagePoint(1, 1), ImagePoint(0, 1))
        else:
            texts = ('28"',)
            corners = (
                ImagePoint(0, 0),
                ImagePoint(width - 1, 0),
                ImagePoint(width - 1, height - 1),
                ImagePoint(0, height - 1),
            )
        return tuple(
            OcrItem(text=text, confidence=Decimal("0.9"), image_extent=corners) for text in texts
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
    engine: _Engine,
    *,
    fraction_parts: PieceDrawing | None = DRAWING,
    vision_readers: tuple[object, ...] = (),
    sheet: bytes = STACKED_SHEET,
) -> dict[str, object]:
    revision = _revision(session, store, data=sheet)
    session.commit()
    (result,) = DatabaseStages(
        store,
        dpi=150,
        association=ASSOCIATION,
        ocr_engine=engine,
        localized_ocr=LOCALIZED,
        vision_readers=vision_readers,  # type: ignore[arg-type]
        fraction_parts=fraction_parts,
    ).extract_pages(session, revision.id)
    session.commit()
    return dict(result.payload)


def _route_rows(session: Session) -> list[ObservationCandidate]:
    return list(
        session.execute(
            select(ObservationCandidate)
            .join(ExtractionRun, ExtractionRun.id == ObservationCandidate.extraction_run_id)
            .where(ExtractionRun.extractor == FRACTION_PARTS_EXTRACTOR)
        ).scalars()
    )


def _the_reading(session: Session, store: LocalStore) -> ObservationCandidate:
    engine = _Engine(PIECES)
    _extract(session, store, engine)
    (row,) = _route_rows(session)
    return row


def _copy(row: ObservationCandidate, *, run_id: object, flags: list[str]) -> ObservationCandidate:
    """The same reading, recorded again under `run_id` with `flags`: a second reader of one box."""
    return ObservationCandidate(
        document_version_id=row.document_version_id,
        page_id=row.page_id,
        extraction_run_id=run_id,
        raw_text=row.raw_text,
        value_numerator=row.value_numerator,
        value_denominator=row.value_denominator,
        unit=row.unit,
        unit_guess=row.unit_guess,
        semantic_guess=None,
        polygon=row.polygon,
        coordinate_space="image",
        confidence=None,
        ambiguity_flags=flags,
    )


def _second_reader(session: Session, row: ObservationCandidate) -> ExtractionRun:
    first = session.get(ExtractionRun, row.extraction_run_id)
    assert first is not None
    return open_extraction_run(
        session,
        task_run_id=first.task_run_id,
        extractor="a-second-reader",
        extractor_version="test/1",
        config_hash="test",
        dpi=first.dpi,
    )


# ---------------------------------------------------------------------------
# The false-PASS guards
# ---------------------------------------------------------------------------


def test_a_stacked_label_is_read_in_parts_and_stored_flagged_for_a_person(
    session: Session, store: LocalStore
) -> None:
    """**One candidate, flagged, with no lane.** The value is the one put together from the three
    pieces; the flag is what keeps it a pre-filled value a person ticks."""
    engine = _Engine(PIECES)
    payload = _extract(session, store, engine)

    (row,) = _route_rows(session)
    assert engine.pieces_shown == 3
    assert row.raw_text == '28 3/4"'
    assert (row.value_numerator, row.value_denominator, row.unit) == (115, 4, "in")
    assert row.ambiguity_flags == [STACKED_FRACTION_FLAG]
    assert (row.corroboration_status, row.corroboration_lane) == (None, None)
    assert row.confidence is None
    assert payload["fraction_parts_readings"] == 1
    assert payload["fraction_parts_refusals"] == 0


def test_two_agreeing_readers_leave_it_unconfirmed_with_no_lane(
    session: Session, store: LocalStore
) -> None:
    """**The agreement gate cannot seal it** (#726). The stored reading and a second reader of the
    same box, from another route, agree exactly — and both stay raw candidates with no lane."""
    reading = _the_reading(session, store)
    second = _second_reader(session, reading)
    flagged = _copy(reading, run_id=reading.extraction_run_id, flags=list(reading.ambiguity_flags))
    agreeing = _copy(reading, run_id=second.id, flags=[])
    session.add_all([flagged, agreeing])

    DatabaseStages._apply_cross_route_corroboration(
        session, page_index=0, candidates=(flagged, agreeing)
    )

    for row in (flagged, agreeing):
        assert (row.corroboration_status, row.corroboration_lane) == (None, None)


def test_the_same_agreement_without_the_flag_does_corroborate(
    session: Session, store: LocalStore
) -> None:
    """The control: it is the flag that holds the reading back, not the readers or the box."""
    reading = _the_reading(session, store)
    second = _second_reader(session, reading)
    unflagged = _copy(reading, run_id=reading.extraction_run_id, flags=[])
    agreeing = _copy(reading, run_id=second.id, flags=[])
    session.add_all([unflagged, agreeing])

    DatabaseStages._apply_cross_route_corroboration(
        session, page_index=0, candidates=(unflagged, agreeing)
    )

    for row in (unflagged, agreeing):
        assert row.corroboration_lane == "SECOND_READER"


def test_automatic_typing_refuses_it(session: Session, store: LocalStore) -> None:
    """**The second guard behind `corroborate`'s.** Asked to type the reading by a tag on its page,
    the automatic lane sends it to a reviewer for being a stacked fraction, before it looks at the
    tag, the line or any agreement; the same reading without its flag is not refused for that."""
    reading = _the_reading(session, store)
    tag = (
        session.execute(
            select(ObservationCandidate).where(
                ObservationCandidate.page_id == reading.page_id,
                ObservationCandidate.id != reading.id,
            )
        )
        .scalars()
        .first()
    )
    assert tag is not None, "the localized OCR route reads the page too"
    settings = AutomaticTypingSettings(frozenset({SemanticType.CT010}))

    decision = qualify_exact_tag_pair(
        session, candidate_id=reading.id, tag_candidate_id=tag.id, settings=settings
    )

    assert isinstance(decision, SemanticTypingDecision)
    assert decision.disposition is TypingDisposition.REVIEW_REQUIRED
    assert "#726" in decision.reason
    unflagged = _copy(reading, run_id=reading.extraction_run_id, flags=[])
    session.add(unflagged)
    session.flush()
    control = qualify_exact_tag_pair(
        session, candidate_id=unflagged.id, tag_candidate_id=tag.id, settings=settings
    )
    assert isinstance(control, SemanticTypingDecision)
    assert "#726" not in control.reason


def test_the_whole_stacked_crop_is_still_never_sent_to_a_vision_reader(
    session: Session, store: LocalStore
) -> None:
    """**#762's skip stays.** With the route on and a vision reader configured, the label is read in
    parts, and no crop showing it whole reaches the reader."""
    reader = _reader()
    engine = _Engine(PIECES)
    payload = _extract(session, store, engine, vision_readers=(reader,))

    assert len(_route_rows(session)) == 1
    assert reader.requests == []
    assert payload["vision_invocations"] == 0
    refusals = payload["vision_refusals"]
    assert isinstance(refusals, list)
    assert any("stacked_fraction_requires_review" in str(refusal) for refusal in refusals)


def test_the_reading_funnel_counts_it_on_its_own_line(
    session: Session, store: LocalStore, postgres_engine: Engine
) -> None:
    """`scripts/reading_funnel.py` counts the route's rows by the name they are recorded under."""
    from scripts.reading_funnel import collect

    _the_reading(session, store)

    assert collect(postgres_engine)["funnel"]["read_in_parts"] == 1


# ---------------------------------------------------------------------------
# No row where a piece does not bear it out
# ---------------------------------------------------------------------------


def _sheet(appearance: bytes) -> bytes:
    """A one-page sheet whose stamp draws `appearance` beside the dimension line."""
    return _pdf(
        annotations=[_stamp(appearance_object=6)],
        extra_objects=[_appearance(DIMENSION_LINE + appearance)],
    )


#: The label with no inch mark after it: the last two lines of the synthetic stamp are its ticks.
UNMARKED_SHEET = _sheet(b"".join(STACKED_APPEARANCE.splitlines(True)[:-2]))

#: The label with a `+` just after its inch mark, centred on the bar: more label than it counts.
NEIGHBOURED_SHEET = _sheet(STACKED_APPEARANCE + b"129 525 m 131 525 l S\n130 523 m 130 527 l S\n")


@pytest.mark.parametrize(
    ("sheet", "pieces", "reason"),
    [
        (STACKED_SHEET, [()], fraction_parts.NOT_A_NUMBER),
        (STACKED_SHEET, [("2",)], fraction_parts.WRONG_COUNT),
        (STACKED_SHEET, [("28",), ("4",), ("4",)], fraction_parts.NOT_BELOW),
        (STACKED_SHEET, [("28",), ("3",), ("5",)], fraction_parts.NOT_AN_INCH_FRACTION),
        (UNMARKED_SHEET, [], fraction_parts.NO_INCH_MARK),
        (NEIGHBOURED_SHEET, [], fraction_parts.NEIGHBOUR),
    ],
)
def test_a_label_its_pieces_do_not_bear_out_writes_no_row(
    session: Session,
    store: LocalStore,
    sheet: bytes,
    pieces: list[tuple[str, ...]],
    reason: str,
) -> None:
    """**No row** for an unreadable piece, a wrong count, a numerator at or above the denominator, an
    odd denominator, a missing mark, or a neighbouring character within the gap. Each is counted on
    the page result under the route's own reason, and only the pieces that were read were drawn."""
    engine = _Engine(pieces)
    payload = _extract(session, store, engine, sheet=sheet)

    assert engine.pieces_shown == len(pieces)

    assert _route_rows(session) == []
    assert payload["fraction_parts_readings"] == 0
    assert payload["fraction_parts_refusals"] == 1
    assert payload["fraction_parts_refusal_reasons"] == [f"1 × {reason}"]


# ---------------------------------------------------------------------------
# Off unless a deployment turns it on
# ---------------------------------------------------------------------------


def test_the_route_is_off_unless_a_deployment_turns_it_on(
    session: Session, store: LocalStore
) -> None:
    """`None`, not zero: the route did not run, which is not the same fact as its reading nothing."""
    engine = _Engine(PIECES)
    payload = _extract(session, store, engine, fraction_parts=None)

    assert engine.pieces_shown == 0
    assert payload["fraction_parts_readings"] is None
    assert payload["fraction_parts_refusals"] is None
    assert (
        session.execute(
            select(ExtractionRun).where(ExtractionRun.extractor == FRACTION_PARTS_EXTRACTOR)
        ).first()
        is None
    )


def test_the_route_cannot_be_on_without_the_detector(tmp_path: Path) -> None:
    """Without association settings no stacked fraction is ever laid out, so a route turned on
    there could never read anything; it is refused rather than left on and silent."""
    store = LocalStore(root=tmp_path, ticket_secret=b"a secret only this test knows")
    with pytest.raises(ValueError, match="association settings"):
        DatabaseStages(store, fraction_parts=DRAWING, vision_readers=())


STATED = {
    FRACTION_PARTS_ENV: "1",
    "GV_FRACTION_PARTS_HEIGHT_PX": "40",
    "GV_FRACTION_PARTS_STROKE_PX": "4",
    "GV_FRACTION_PARTS_BEZIER_STEPS": "8",
    "GV_FRACTION_PARTS_MARGIN_PX": "32",
}


@pytest.mark.parametrize("switch", [None, "", "0", "no", "off"])
def test_the_switch_is_off_by_default(switch: str | None) -> None:
    environ = {name: value for name, value in STATED.items() if name != FRACTION_PARTS_ENV}
    if switch is not None:
        environ[FRACTION_PARTS_ENV] = switch

    assert fraction_parts_from_environment(environ) is None


def test_switched_on_it_draws_with_exactly_what_was_stated() -> None:
    assert fraction_parts_from_environment(STATED) == DRAWING


@pytest.mark.parametrize("missing", list(FRACTION_PARTS_SETTINGS))
def test_switched_on_every_drawing_setting_is_required(missing: str) -> None:
    environ = {name: value for name, value in STATED.items() if name != missing}

    with pytest.raises(ValueError, match=missing):
        fraction_parts_from_environment(environ)


@pytest.mark.parametrize("stated", ["4.5", "-4", "four", "0"])
def test_a_drawing_setting_that_is_not_a_positive_whole_number_is_refused(stated: str) -> None:
    with pytest.raises(ValueError):
        fraction_parts_from_environment({**STATED, "GV_FRACTION_PARTS_STROKE_PX": stated})
