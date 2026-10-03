"""Stacked fractions read piece by piece, as a pipeline route (#848, #865).

Verification for: `_read_page_by_fraction_parts`, `_PieceReads` and their wiring in
`workflow/stages.py`, and `fraction_parts_from_environment`.

**The false-PASS guards come first.** A reading put together from the pieces of a stacked label is
written only where the second reader agrees on every piece (#865): a piece the two read differently
gives no row, a second reading with the wrong number of digits is refused, and a spent budget stops
the calls and gives no row. A row is stored with `STACKED_FRACTION_FLAG`; two readers agreeing on it
leave it a raw candidate with no lane, where the same row without its flag would take the
second-reader lane; automatic typing refuses it. It pre-fills the form for a person to tick and is
never evidence on its own (#726).

The drawing is a real one-page PDF whose stamp draws the synthetic `28 3/4"` of
`tests/extraction/test_annotations.py`. Its digits are plotter-shaped strokes, not numerals, so the
OCR engine is a double: it answers each piece the route draws in turn, and reads every other
picture as the whole crop it was handed. **The second reader is the real Bedrock adapter and its
validator, against a scripted client**: what it answers is written in each test, every call is
recorded in `model_invocations`, and no model is called.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Iterator
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.evidence.automatic_typing import AutomaticTypingSettings, qualify_exact_tag_pair
from app.evidence.record import open_extraction_run
from app.models import ObservationCandidate
from app.models.runs import ExtractionRun, ModelInvocation
from app.runs.rates import MODEL_RATES_ENV
from evidence.candidate import STACKED_FRACTION_FLAG
from evidence.candidate import ObservationCandidate as DomainCandidate
from evidence.coordinates import ImagePoint
from evidence.semantic_typing import SemanticTypingDecision, TypingDisposition
from extraction import fraction_parts
from extraction.fraction_parts import FRACTION_PARTS_EXTRACTOR, PieceDrawing
from extraction.models.nova import (
    DIGITS_TOOL_NAME,
    InferenceProfileRoutes,
    NovaAdapter,
    NovaConfig,
    NovaDigitsRequest,
    NovaRequest,
    NovaServiceError,
)
from extraction.models.sanitisation import DIGITS_PROMPT_ID, DIGITS_TEMPLATE_ID
from extraction.ocr import OcrItem
from storage.local import LocalStore
from tests.extraction.models.test_nova import FakeBedrock
from tests.extraction.test_annotations import STACKED_APPEARANCE, _appearance, _pdf, _stamp
from tests.workflow.test_association import LOCALIZED, SETTINGS, _revision, _upgrade
from tests.workflow.test_stacked_fraction_route import DIMENSION_LINE, STACKED_SHEET
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

#: The second reader's identity: a Mistral model, so another vendor than the OCR engine's route.
SECOND_EXTRACTOR = "bedrock-route-second"
SECOND_MODEL = "mistral.route-second-v1"


def _digits_answer(digits: object) -> dict[str, Any]:
    """One Bedrock answer to a digits request, at 1,000 tokens each way."""
    return {
        "stopReason": "tool_use",
        "output": {
            "message": {
                "content": [
                    {
                        "toolUse": {
                            "name": DIGITS_TOOL_NAME,
                            "toolUseId": "call-1",
                            "input": {"digits": digits},
                        }
                    }
                ]
            }
        },
        "usage": {"inputTokens": 1000, "outputTokens": 1000},
        "ResponseMetadata": {"RequestId": "aws-request-1"},
    }


class _Gate:
    """The gate reader, and so the route's second reader: the real adapter, a scripted Bedrock.

    `read_digits` goes through `NovaAdapter.read_digits` and its validator, exactly as production's
    `BedrockVisionReader` does; only the client is scripted. `extract` keeps any whole-crop request
    and declines it, so a test can see that none was made.
    """

    def __init__(self, *answers: object) -> None:
        self.config = NovaConfig(
            model_id=SECOND_MODEL,
            prompt_id="dimension-reader-v1",
            template_id="bounded-crop-v1",
            connect_timeout_seconds=1,
            read_timeout_seconds=1,
            max_attempts=1,
            extractor=SECOND_EXTRACTOR,
        )
        self.client = FakeBedrock(*(_digits_answer(answer) for answer in answers))
        self.requests: list[NovaRequest] = []

    def extract(self, request: NovaRequest, recorder: object) -> DomainCandidate:
        self.requests.append(request)
        raise NovaServiceError("this double reads drawn pieces only")

    def read_digits(self, request: NovaDigitsRequest, recorder: object) -> str:
        adapter = NovaAdapter(
            self.config, self.client, recorder, InferenceProfileRoutes()  # type: ignore[arg-type]
        )
        return adapter.read_digits(request)


class _Engine:
    """Answers each piece the route draws in turn; reads any other picture as one `28 3/4"` filling
    it — what the label says, so localized OCR records it, flagged, rather than refusing it (#846).

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
            texts = ('28 3/4"',)
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
    gate: _Gate | None = None,
    sheet: bytes = STACKED_SHEET,
    ai_budget_usd: Decimal | None = None,
) -> dict[str, object]:
    """One extraction of `sheet`, with `gate` as the vision gate reader and so the second reader —
    by default one that agrees with every piece of the synthetic label."""
    gate = _Gate("28", "3", "4") if gate is None else gate
    revision = _revision(session, store, data=sheet)
    session.commit()
    (result,) = DatabaseStages(
        store,
        dpi=150,
        association=ASSOCIATION,
        ocr_engine=engine,
        localized_ocr=LOCALIZED,
        vision_readers=(gate,),  # type: ignore[arg-type]
        vision_gate=SECOND_EXTRACTOR,
        ai_budget_usd=ai_budget_usd,
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


def _route_invocations(session: Session) -> list[ModelInvocation]:
    """The second reader's calls: recorded under the route's own run."""
    return list(
        session.execute(
            select(ModelInvocation)
            .join(ExtractionRun, ExtractionRun.id == ModelInvocation.extraction_run_id)
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
    """**One candidate, flagged, with no lane**, where both readers agree on all three pieces. The
    value is the one put together from them; the flag is what keeps it a pre-filled value a person
    ticks."""
    engine = _Engine(PIECES)
    gate = _Gate("28", "3", "4")
    payload = _extract(session, store, engine, gate=gate)

    (row,) = _route_rows(session)
    assert engine.pieces_shown == 3
    assert len(gate.client.requests) == 3
    assert row.raw_text == '28 3/4"'
    assert (row.value_numerator, row.value_denominator, row.unit) == (115, 4, "in")
    assert row.ambiguity_flags == [STACKED_FRACTION_FLAG]
    assert (row.corroboration_status, row.corroboration_lane) == (None, None)
    assert row.confidence is None
    assert payload["fraction_parts_readings"] == 1
    assert payload["fraction_parts_refusals"] == 0
    assert payload["fraction_parts_invocations"] == 3


def test_every_second_reading_is_recorded_under_the_routes_run(
    session: Session, store: LocalStore
) -> None:
    """**Every call is in `model_invocations`**, under the digits request's own prompt and template
    and the route's run, with no context sent beside the picture. No call names the row: the column
    holds one call per row, and this row was read from three."""
    _extract(session, store, _Engine(PIECES))

    (row,) = _route_rows(session)
    invocations = _route_invocations(session)
    assert len(invocations) == 3
    for invocation in invocations:
        assert invocation.extraction_run_id == row.extraction_run_id
        assert (invocation.model_id, invocation.outcome) == (SECOND_MODEL, "ok")
        assert (invocation.prompt_id, invocation.template_id) == (
            DIGITS_PROMPT_ID,
            DIGITS_TEMPLATE_ID,
        )
        assert invocation.candidate_id is None
        assert invocation.assembled_context == {"nearby_text": [], "nearby_geometry": []}
        assert invocation.bound_pt == Decimal(0)
    run = session.get(ExtractionRun, row.extraction_run_id)
    assert run is not None
    assert f";second={SECOND_MODEL};readers=" in run.config_hash


@pytest.mark.parametrize(
    ("seconded", "asked"),
    [
        (("29", "3", "4"), 1),
        (("28", "5", "4"), 2),
        (("28", "3", "8"), 3),
    ],
)
def test_a_piece_the_two_readers_disagree_on_gives_no_row(
    session: Session, store: LocalStore, seconded: tuple[str, ...], asked: int
) -> None:
    """**The false PASS #865 exists for.** Wherever the second reader reads a piece differently, no
    row is written: the page counts why, and a person types the value. The first disagreement ends
    the calls."""
    gate = _Gate(*seconded)
    payload = _extract(session, store, _Engine(PIECES), gate=gate)

    assert _route_rows(session) == []
    assert len(gate.client.requests) == asked
    assert payload["fraction_parts_readings"] == 0
    assert payload["fraction_parts_refusal_reasons"] == [f"1 × {fraction_parts.DISAGREE}"]
    assert payload["fraction_parts_invocations"] == asked


@pytest.mark.parametrize("answer", ["8", "283", "2 8", 28, "２８"])
def test_a_second_reading_with_the_wrong_digit_count_is_refused(
    session: Session, store: LocalStore, answer: object
) -> None:
    """**The second reader is held to the drawing's count too.** A `28` drawn with two characters,
    answered as one digit, three, or not as digits at all, is refused by the validator — recorded
    with its reason — and no row is written."""
    gate = _Gate(answer)
    payload = _extract(session, store, _Engine(PIECES), gate=gate)

    assert _route_rows(session) == []
    (invocation,) = _route_invocations(session)
    assert invocation.outcome == "rejected"
    assert invocation.rejection_reason in {
        "digits_wrong_count",
        "digits_not_a_number",
        "schema_validation_failed",
    }
    assert payload["fraction_parts_refusal_reasons"] == [
        f"1 × the second reader's answer was refused: {invocation.rejection_reason}"
    ]


def test_a_spent_budget_stops_the_calls_and_gives_no_row(
    session: Session, store: LocalStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**The cap holds here too.** The first piece's call costs $4 by a price file this test writes,
    over a $3 cap by itself: the second piece is never asked about, the label gets no row, and the
    page says why — the meter's own sentence."""
    prices = tmp_path / "prices.json"
    prices.write_text(
        json.dumps(
            {
                "source": "made up for this test",
                "retrieved": "2026-10-03",
                "currency": "USD",
                "rates": {SECOND_MODEL: {"input_per_1k_tokens": "2", "output_per_1k_tokens": "2"}},
            }
        )
    )
    monkeypatch.setenv(MODEL_RATES_ENV, str(prices))
    gate = _Gate("28", "3", "4")

    payload = _extract(session, store, _Engine(PIECES), gate=gate, ai_budget_usd=Decimal(3))

    assert len(gate.client.requests) == 1
    assert _route_rows(session) == []
    assert payload["ai_budget"] == {
        "cap_usd": "3",
        "spent_usd": "4",
        "calls_without_a_price": 0,
        "reached": True,
    }
    reasons = payload["fraction_parts_refusal_reasons"]
    assert isinstance(reasons, list) and len(reasons) == 1
    assert "spent its AI budget of $3.00" in reasons[0]


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
    """**#762's skip stays.** With the route on, the label is read in parts, and no crop showing it
    whole reaches the vision reader — the very reader that seconded its pieces."""
    gate = _Gate("28", "3", "4")
    engine = _Engine(PIECES)
    payload = _extract(session, store, engine, gate=gate)

    assert len(_route_rows(session)) == 1
    assert gate.requests == []
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


def test_the_route_cannot_be_on_without_its_second_reader(tmp_path: Path) -> None:
    """**One reader is one witness (#865).** Without a gate reader there is no second reader, so a
    route turned on could never pre-fill anything; it is refused rather than left on and silent."""
    store = LocalStore(root=tmp_path, ticket_secret=b"a secret only this test knows")
    with pytest.raises(ValueError, match="GV_VISION_GATE_READER"):
        DatabaseStages(
            store,
            association=ASSOCIATION,
            fraction_parts=DRAWING,
            vision_readers=(_Gate(),),  # type: ignore[arg-type]
        )


def test_the_run_identity_names_every_reader_and_fits_its_column(tmp_path: Path) -> None:
    """A reading made by another second reader, another engine or other drawing settings is another
    run; and with production's own names the identity still fits the column's 200 characters."""
    store = LocalStore(root=tmp_path, ticket_secret=b"a secret only this test knows")
    gate = _Gate()
    stages = DatabaseStages(
        store,
        association=ASSOCIATION,
        fraction_parts=DRAWING,
        vision_readers=(gate,),  # type: ignore[arg-type]
        vision_gate=SECOND_EXTRACTOR,
    )
    other_gate = _Gate()
    other_gate.config = replace(gate.config, model_id="amazon.another-second-v1")
    production_engine = _Engine([])
    production_engine.name = "rapidocr"
    production_engine.version = "extraction.ocr.rapidocr/2"
    production_gate = _Gate()
    production_gate.config = replace(
        gate.config,
        extractor="bedrock-ministral-3-3b",
        model_id="mistral.ministral-3-3b-instruct",
    )

    hashes = {
        stages._fraction_parts_config_hash(DRAWING, _Engine([]), gate),  # type: ignore[arg-type]
        stages._fraction_parts_config_hash(DRAWING, _Engine([]), other_gate),  # type: ignore[arg-type]
        stages._fraction_parts_config_hash(DRAWING, production_engine, gate),  # type: ignore[arg-type]
        stages._fraction_parts_config_hash(
            replace(DRAWING, stroke_px=3), _Engine([]), gate  # type: ignore[arg-type]
        ),
    }
    assert len(hashes) == 4
    assert (
        len(stages._fraction_parts_config_hash(DRAWING, production_engine, production_gate))  # type: ignore[arg-type]
        <= 200
    )


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
