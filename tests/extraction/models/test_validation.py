"""Fail-closed payload validation tests for issues #250, #834 and #865."""

from __future__ import annotations

from typing import Any

import pytest

from evidence.candidate import ObservationCandidate
from evidence.coordinates import ImagePoint
from extraction.glyph_bands import FractionLayout
from extraction.models.validation import (
    DIGITS_NOT_A_NUMBER,
    DIGITS_WRONG_COUNT,
    STACKED_FRACTION_REASON,
    STACKED_LAYOUT_REASON,
    CandidateContext,
    CoordinateMode,
    CropSize,
    ValidationRejection,
    stacked_layout_refusal,
    validate_digits_payload,
    validate_payload,
)
from tests.extraction.test_glyph_bands import FRACTION, THIRTY_NINE_AND_A_HALF, _found, _shifted
from units.measurement import Unit


class RecordingRejections:
    """Collect rejected raw responses without sending drawing data to logs."""

    def __init__(self) -> None:
        self.items: list[ValidationRejection] = []

    def record_rejection(self, rejection: ValidationRejection) -> None:
        self.items.append(rejection)


def _context() -> CandidateContext:
    return CandidateContext(
        candidate_id="candidate-250",
        extractor_version="amazon.nova-2-lite-v1:0",
        page=4,
    )


def _valid_payload() -> dict[str, object]:
    return {
        "reading": "984",
        "unit_guess": "mm",
        "x1": 10,
        "y1": 20,
        "x2": 30,
        "y2": 40,
    }


def _validate(
    payload: object,
    *,
    recorder: RecordingRejections,
    crop_size: CropSize | None = None,
    coordinate_mode: CoordinateMode = CoordinateMode.PIXELS,
    # Defaulted in this helper only, so the tests of everything else stay about everything else.
    # `validate_payload` itself has no default — see the test that pins it.
    stacked_label: bool = False,
    stacked_layouts: tuple[FractionLayout, ...] = (),
) -> ObservationCandidate | ValidationRejection:
    return validate_payload(
        payload,
        context=_context(),
        crop_size=crop_size if crop_size is not None else CropSize(100, 80),
        coordinate_mode=coordinate_mode,
        recorder=recorder,
        stacked_label=stacked_label,
        stacked_layouts=stacked_layouts,
    )


def test_valid_payload_becomes_an_uncorroborated_candidate() -> None:
    """Input: understood payload. Outcome: candidate. Why: validation cannot create evidence."""

    recorder = RecordingRejections()

    outcome = _validate(_valid_payload(), recorder=recorder)

    assert isinstance(outcome, ObservationCandidate)
    assert outcome.raw_text == "984"
    assert outcome.unit_guess is Unit.MM
    assert outcome.parsed_value is None
    assert outcome.confidence is None
    assert outcome.polygon == (
        ImagePoint(10, 20),
        ImagePoint(30, 20),
        ImagePoint(30, 40),
        ImagePoint(10, 40),
    )
    assert "nova_rectangle_polygon_derived" in outcome.ambiguity_flags
    assert recorder.items == []


@pytest.mark.parametrize("unknown_field", ["verdict", "confidence", "helpful_note"])
def test_unknown_field_is_recorded_and_rejected(unknown_field: str) -> None:
    """Input: invented field. Outcome: abstention. Why: unknown output is never silently dropped."""

    payload = _valid_payload()
    payload[unknown_field] = "PASS"
    recorder = RecordingRejections()

    outcome = _validate(payload, recorder=recorder)

    assert isinstance(outcome, ValidationRejection)
    assert outcome.reason == "schema_validation_failed"
    assert unknown_field in outcome.raw_response
    assert any(unknown_field in error for error in outcome.errors)
    assert recorder.items == [outcome]


def test_missing_required_field_never_produces_a_partial_candidate() -> None:
    """Input: no reading. Outcome: abstention only. Why: partial candidates are unsafe."""

    payload = _valid_payload()
    del payload["reading"]
    recorder = RecordingRejections()

    outcome = _validate(payload, recorder=recorder)

    assert isinstance(outcome, ValidationRejection)
    assert outcome.reason == "schema_validation_failed"
    assert len(recorder.items) == 1


@pytest.mark.parametrize("coordinate", [10.5, 10.0])
def test_every_float_is_rejected_before_pydantic_conversion(coordinate: float) -> None:
    """Input: decimal or integral float. Outcome: abstention. Why: coercion cannot erase origin."""

    payload = _valid_payload()
    payload["x1"] = coordinate
    recorder = RecordingRejections()

    outcome = _validate(payload, recorder=recorder)

    assert isinstance(outcome, ValidationRejection)
    assert outcome.reason == "float_not_allowed"
    assert outcome.errors == ("$.x1 contains a float; model numeric values must remain exact",)
    assert recorder.items == [outcome]


def test_float_in_an_unknown_nested_field_is_still_rejected_first() -> None:
    """Input: hidden float. Outcome: float abstention. Why: exactness covers all output."""

    payload = _valid_payload()
    payload["metadata"] = {"score": 0.9}
    recorder = RecordingRejections()

    outcome = _validate(payload, recorder=recorder)

    assert isinstance(outcome, ValidationRejection)
    assert outcome.reason == "float_not_allowed"
    assert "$.metadata.score" in outcome.errors[0]


def test_coordinate_strings_are_rejected_by_the_wire_schema() -> None:
    """Input: string coordinate. Outcome: abstention. Why: arity and type are structural."""

    payload = _valid_payload()
    payload["x1"] = "10"

    outcome = _validate(payload, recorder=RecordingRejections())

    assert isinstance(outcome, ValidationRejection)
    assert outcome.reason == "schema_validation_failed"


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("unit_guess", "cm", "candidate_conversion_failed"),
        ("x2", 10, "candidate_conversion_failed"),
        ("x2", 101, "coordinate_out_of_bounds"),
    ],
)
def test_unsupported_unit_or_polygon_abstains(field: str, value: object, reason: str) -> None:
    """Input: unusable unit/location. Outcome: abstention. Why: evidence cannot be guessed."""

    payload = _valid_payload()
    payload[field] = value
    recorder = RecordingRejections()

    outcome = _validate(payload, recorder=recorder)

    assert isinstance(outcome, ValidationRejection)
    assert outcome.reason == reason
    assert recorder.items == [outcome]


def test_rejection_retains_raw_response_and_trusted_provenance() -> None:
    """Input: malformed output. Outcome: diagnostic record. Why: changes stay auditable."""

    payload: dict[str, Any] = {"reading": "984", "unexpected": "field"}
    recorder = RecordingRejections()

    outcome = _validate(payload, recorder=recorder)

    assert isinstance(outcome, ValidationRejection)
    assert outcome.raw_response == '{"reading":"984","unexpected":"field"}'
    assert outcome.candidate_id == "candidate-250"
    assert outcome.extractor_version == "amazon.nova-2-lite-v1:0"
    assert recorder.items == [outcome]


def test_coordinate_outside_crop_is_refused_with_crop_size_and_value() -> None:
    """Input: pixel coordinate past the crop. Outcome: abstention naming the unsafe value."""

    payload = _valid_payload() | {"x2": 101}
    recorder = RecordingRejections()

    outcome = _validate(payload, recorder=recorder, crop_size=CropSize(100, 80))

    assert isinstance(outcome, ValidationRejection)
    assert outcome.reason == "coordinate_out_of_bounds"
    assert "100x80 crop" in outcome.errors[0]
    assert "101" in outcome.errors[0]
    assert '"x2":101' in outcome.raw_response
    assert recorder.items == [outcome]


def test_nova_grid_coordinates_are_remapped_against_the_crop_size() -> None:
    """Input: 0-1000 grid rectangle on a small crop. Outcome: crop-pixel polygon."""

    payload = _valid_payload() | {"x1": 250, "y1": 500, "x2": 750, "y2": 1000}
    recorder = RecordingRejections()

    outcome = _validate(
        payload,
        recorder=recorder,
        crop_size=CropSize(20, 10),
        coordinate_mode=CoordinateMode.NOVA_GRID,
    )

    assert isinstance(outcome, ObservationCandidate)
    assert outcome.polygon == (
        ImagePoint(5, 5),
        ImagePoint(15, 5),
        ImagePoint(15, 10),
        ImagePoint(5, 10),
    )
    assert recorder.items == []


# ---------------------------------------------------------------------------
# The reading has to be a dimension (#539)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reading",
    [
        '33"',
        '120"',
        "984",
        "3' - 3\"",
        "8'-6''",
        '28 1/2"',
        "984 mm",
        '1 1/4"',
    ],
)
def test_a_dimension_reading_is_accepted(reading: str) -> None:
    """Input: readings a real model returned from real crops. Outcome: candidates.

    **Every one of these was thrown away before #539**, five over a normalised polygon and three
    over a unit spelling, while `1/2` for a 28½-inch label was accepted. They are here as a set
    because the guard has to let the successes through — a validator that refuses everything is not
    safe, it is broken.

    `8'-6''` is the typewriter spelling of the inch double-prime, which is what a model emits when
    it has only ASCII to hand.
    """
    payload = _valid_payload() | {"reading": reading}
    recorder = RecordingRejections()

    outcome = _validate(payload, recorder=recorder)

    assert isinstance(outcome, ObservationCandidate), outcome
    assert outcome.raw_text == reading
    assert recorder.items == []


def test_the_reading_keeps_the_characters_the_model_produced() -> None:
    """Outcome: `8'-6''` is stored as `8'-6''`, not as the spelling the parser preferred.

    The inch-mark substitution exists so the readability probe can read it. Rewriting the candidate
    would show a reviewer something other than what came back, and comparing a reading against its
    crop is the whole point of keeping it.
    """
    payload = _valid_payload() | {"reading": "8'-6''"}

    outcome = _validate(payload, recorder=RecordingRejections())

    assert isinstance(outcome, ObservationCandidate)
    assert outcome.raw_text == "8'-6''"


@pytest.mark.parametrize(
    "reading",
    [
        "189 1 1/4",
        "The image shows a dimension line",
        "abc",
        "12 34",
        "-",
    ],
)
def test_a_reading_that_is_not_a_dimension_is_refused(reading: str) -> None:
    """Input: prose and garbled numbers. Outcome: an abstention, recorded.

    `189 1 1/4` is real: it is what a model returned from a crop where a drawing label and a markup
    label sat on top of each other, and it is neither of them.
    """
    payload = _valid_payload() | {"reading": reading}
    recorder = RecordingRejections()

    outcome = _validate(payload, recorder=recorder)

    assert isinstance(outcome, ValidationRejection)
    assert outcome.reason == "reading_not_a_dimension"
    assert recorder.items == [outcome]


@pytest.mark.parametrize("reading", ["1/2", '1/2"', "3/4", '15/16"'])
def test_a_bare_fraction_abstains_rather_than_being_accepted(reading: str) -> None:
    """**The one this exists for.** Input: a fraction with no whole number. Outcome: abstention.

    A model read a label saying `28 1/2"` as `1/2`, and the validator accepted it because the
    polygon happened to be integral. A dropped whole number is the worst failure this seam can
    produce: `1/2"` is a dimension a fabricator could plausibly be given, so nothing downstream has
    any reason to question it.

    These readings are probably *right* when a drawing really does say `3/4"` — a 3/4" side panel is
    in the rulebook — and they still abstain. That is the trade: a false abstention costs a reviewer
    one look at a crop, and the alternative cost is a wrong dimension nobody sees.
    """
    payload = _valid_payload() | {"reading": reading}
    recorder = RecordingRejections()

    outcome = _validate(payload, recorder=recorder)

    assert isinstance(outcome, ValidationRejection)
    assert outcome.reason == "reading_not_a_dimension"
    assert "no whole number" in outcome.errors[0]
    assert recorder.items == [outcome]


def test_a_refused_reading_keeps_the_payload_that_produced_it() -> None:
    """Outcome: the raw response is retained, so the refusal can be understood.

    A rejection whose payload was discarded says only that something was wrong. The reading is the
    thing a person needs in order to tell a misread from a prompt that needs changing.
    """
    payload = _valid_payload() | {"reading": "1/2"}

    outcome = _validate(payload, recorder=RecordingRejections())

    assert isinstance(outcome, ValidationRejection)
    assert '"1/2"' in outcome.raw_response


def test_the_dimension_check_produces_no_value() -> None:
    """Outcome: `parsed_value` is still `None` on an accepted reading.

    The check parses in order to decide readability and throws the result away. This seam reads; it
    does not convert, and `evidence/normalize.py` is where a unit becomes authoritative — under a
    caller who knows what the sheet is drawn in, rather than a model that guessed.
    """
    outcome = _validate(_valid_payload() | {"reading": '33"'}, recorder=RecordingRejections())

    assert isinstance(outcome, ObservationCandidate)
    assert outcome.parsed_value is None


@pytest.mark.parametrize("reading", ["0", "00", '0"', "0 mm", "0.0"])
def test_a_reading_that_measures_zero_is_refused(reading: str) -> None:
    """Input: zero, in the spellings a model produces. Outcome: abstention.

    `00` came back from a real crop and was accepted, because zero parses as a dimension. A
    dimension line of no length is not a dimension and nobody draws one — and a zero is the
    quietest possible way to satisfy a sum, so accepting it would put a reading that failed into
    arithmetic that then succeeds.
    """
    payload = _valid_payload() | {"reading": reading}
    recorder = RecordingRejections()

    outcome = _validate(payload, recorder=recorder)

    assert isinstance(outcome, ValidationRejection)
    assert outcome.reason == "reading_not_a_dimension"
    assert "measures zero" in outcome.errors[0]
    assert recorder.items == [outcome]


def test_a_small_reading_is_not_mistaken_for_zero() -> None:
    """Input: a sixteenth of an inch. Outcome: accepted.

    The zero refusal is about zero, not about smallness. `1/16"` is a real dimension and the check
    is exact — a float comparison here could have made a small value round into a refusal.
    """
    outcome = _validate(_valid_payload() | {"reading": '1 1/16"'}, recorder=RecordingRejections())

    assert isinstance(outcome, ObservationCandidate)


@pytest.mark.parametrize(
    "reading",
    [
        "284",  # `28 3/4"` with the fraction absorbed into the digits — the case #541 was written for
        '3 3/4"',  # a stacked `3/4"` with its numerator promoted — two vendors agreed on this (#726)
        '3/4"',  # the correct reading of that label, which the bare-fraction guard would also refuse
        '28 3/4"',  # the correct reading of the first
    ],
)
def test_every_reading_of_a_stacked_crop_goes_to_a_reviewer(reading: str) -> None:
    """**The admin's rule on #726, and why it is not a rule about the string.**

    Right and wrong readings of a stacked fraction are indistinguishable once read: `3 3/4"` has its
    `/`, parses, and two readers from different vendors returned it for a `3/4"`. The guard #541 built
    asked only for a `/`, so it passed that exactly as it passed the right answer. So a crop that
    shows a stacked fraction abstains on every reading, and says why.
    """
    recorder = RecordingRejections()

    outcome = _validate(
        {**_valid_payload(), "reading": reading, "unit_guess": "in"},
        recorder=recorder,
        stacked_label=True,
    )

    assert isinstance(outcome, ValidationRejection)
    assert outcome.reason == STACKED_FRACTION_REASON == "stacked_fraction_requires_review"
    assert "stacked fraction" in outcome.errors[0]
    assert recorder.items == [outcome], "an abstention nobody recorded did not happen"


def test_a_reading_that_is_not_a_dimension_keeps_its_own_reason_on_a_stacked_crop() -> None:
    """The truer reason wins. Most of the detector's false alarms are hatching beside a label, and a
    crop of hatching that reads as `GFI` is not a dimension — which is what a reviewer needs told.
    """
    outcome = _validate(
        {**_valid_payload(), "reading": "GFI", "unit_guess": None},
        recorder=RecordingRejections(),
        stacked_label=True,
    )

    assert isinstance(outcome, ValidationRejection)
    assert outcome.reason == "reading_not_a_dimension"


def test_a_crop_with_no_stacked_fraction_is_not_refused_for_one() -> None:
    outcome = _validate(
        {**_valid_payload(), "reading": '28 3/4"', "unit_guess": "in"},
        recorder=RecordingRejections(),
        stacked_label=False,
    )

    assert isinstance(outcome, ObservationCandidate)


def test_every_caller_must_say_whether_its_crop_is_stacked() -> None:
    """**No default, because the default is how #541's guard never ran.** Both production callers
    left it out, it defaulted to `False`, and the guard was tested, worked, and was never engaged.
    """
    with pytest.raises(TypeError, match="stacked_label"):
        validate_payload(  # type: ignore[call-arg]
            _valid_payload(),
            context=_context(),
            crop_size=CropSize(100, 80),
            coordinate_mode=CoordinateMode.PIXELS,
            recorder=RecordingRejections(),
        )


# ---------------------------------------------------------------------------
# A reading the drawing's layout contradicts (#834)
# ---------------------------------------------------------------------------

#: The layouts the bar detector gives the synthetic `3/4"` and `39 1/2"` of the detector's own tests:
#: no whole number and one over one, and two whole-number characters and one over one.
THREE_QUARTERS = _found(FRACTION)
THIRTY_NINE_AND_A_HALF_LAYOUT = _found(THIRTY_NINE_AND_A_HALF)


def _stacked_reading(reading: str, layouts: tuple[FractionLayout, ...]) -> object:
    recorder = RecordingRejections()
    outcome = _validate(
        {**_valid_payload(), "reading": reading, "unit_guess": "in"},
        recorder=recorder,
        stacked_label=True,
        stacked_layouts=layouts,
    )
    assert recorder.items == ([outcome] if isinstance(outcome, ValidationRejection) else [])
    return outcome


@pytest.mark.parametrize(
    ("reading", "layouts"),
    [
        # **The false PASS.** Two readers of different vendors agreed on this for a `3/4"` (#726).
        ('3 3/4"', THREE_QUARTERS),
        # The other way round: a digit of the whole number dropped.
        ('9 1/2"', THIRTY_NINE_AND_A_HALF_LAYOUT),
        ('3/4"', THIRTY_NINE_AND_A_HALF_LAYOUT),
        # The numerator run into the whole number, and the fraction absorbed into it (#541).
        ('391/2"', THIRTY_NINE_AND_A_HALF_LAYOUT),
        ("392", THIRTY_NINE_AND_A_HALF_LAYOUT),
        # A millimetre number the label does not have.
        ("991 [39 1/2]", THIRTY_NINE_AND_A_HALF_LAYOUT),
    ],
)
def test_a_reading_whose_digit_counts_the_layout_contradicts_is_refused(
    reading: str, layouts: tuple[FractionLayout, ...]
) -> None:
    """**The false-PASS guard (#834).** The string cannot show that `3 3/4"` is wrong; the drawing
    can — the label has no whole number. Refused under its own reason, before any lane sees it, and
    recorded."""
    outcome = _stacked_reading(reading, layouts)

    assert isinstance(outcome, ValidationRejection)
    assert outcome.reason == STACKED_LAYOUT_REASON == "reading_contradicts_stacked_layout"
    assert stacked_layout_refusal(reading, layouts) == outcome.errors[0]


@pytest.mark.parametrize(
    ("reading", "layouts"),
    [
        ('39 1/2"', THIRTY_NINE_AND_A_HALF_LAYOUT),
        ('39-1/2"', THIRTY_NINE_AND_A_HALF_LAYOUT),  # the trade's hyphen, read as the space
        ("39 1/2", THIRTY_NINE_AND_A_HALF_LAYOUT),  # the inch mark is not a digit
        ('3/4"', THREE_QUARTERS),
    ],
)
def test_a_reading_that_matches_its_layout_still_goes_to_a_reviewer(
    reading: str, layouts: tuple[FractionLayout, ...]
) -> None:
    """Matching the layout clears this check only. A stacked fraction always goes to a reviewer
    (#726), so the reading is refused as it was before, under the stacked-fraction reason."""
    assert stacked_layout_refusal(reading, layouts) is None

    outcome = _stacked_reading(reading, layouts)

    assert isinstance(outcome, ValidationRejection)
    assert outcome.reason == STACKED_FRACTION_REASON


def test_the_check_counts_and_does_not_read() -> None:
    """What it cannot do, pinned so nobody claims otherwise: the right count with a wrong digit
    passes. `3/8"` for a `3/4"` is still refused — by the stacked-fraction rule, not this one."""
    assert stacked_layout_refusal('3/8"', THREE_QUARTERS) is None


def test_with_two_stacked_labels_in_the_crop_a_reading_may_match_either() -> None:
    both = THREE_QUARTERS + THIRTY_NINE_AND_A_HALF_LAYOUT

    assert stacked_layout_refusal('3/4"', both) is None
    assert stacked_layout_refusal('39 1/2"', both) is None
    refusal = stacked_layout_refusal('3 3/4"', both)
    assert refusal is not None and "2 stacked labels" in refusal


def test_with_no_layout_there_is_nothing_to_check_against() -> None:
    """A crop with no stacked label, or only one set in text, which has no paths to count."""
    assert stacked_layout_refusal('3 3/4"', ()) is None
    outcome = _stacked_reading('3 3/4"', ())
    assert isinstance(outcome, ValidationRejection)
    assert outcome.reason == STACKED_FRACTION_REASON


def test_a_reading_that_is_not_a_dimension_keeps_its_reason_beside_a_layout() -> None:
    outcome = _validate(
        {**_valid_payload(), "reading": "GFI", "unit_guess": None},
        recorder=RecordingRejections(),
        stacked_label=True,
        stacked_layouts=THREE_QUARTERS,
    )

    assert isinstance(outcome, ValidationRejection)
    assert outcome.reason == "reading_not_a_dimension"


def test_layouts_on_a_crop_said_to_show_no_stacked_label_are_a_contradiction() -> None:
    with pytest.raises(ValueError, match="stacked_label must be True"):
        _validate(
            _valid_payload(),
            recorder=RecordingRejections(),
            stacked_label=False,
            stacked_layouts=THREE_QUARTERS,
        )


def test_every_caller_must_state_the_layouts() -> None:
    """**No default**, for the reason `stacked_label` has none: a guard nobody hands its input to
    never runs."""
    with pytest.raises(TypeError, match="stacked_layouts"):
        validate_payload(  # type: ignore[call-arg]
            _valid_payload(),
            context=_context(),
            crop_size=CropSize(100, 80),
            coordinate_mode=CoordinateMode.PIXELS,
            recorder=RecordingRejections(),
            stacked_label=True,
        )


def test_the_layouts_used_here_are_the_ones_the_detector_draws() -> None:
    """The guard is only as good as the counts, so the fixtures are pinned: `3/4"` has no whole
    number, `39 1/2"` two digits of one, and a layout moved along the sheet is laid out the same."""
    (quarters,), (half,) = THREE_QUARTERS, THIRTY_NINE_AND_A_HALF_LAYOUT
    (moved,) = _found(_shifted(THIRTY_NINE_AND_A_HALF, "200"))

    assert (len(quarters.whole), len(quarters.numerator), len(quarters.denominator)) == (0, 1, 1)
    assert (len(half.whole), len(half.numerator), len(half.denominator)) == (2, 1, 1)
    assert len(moved.whole) == len(half.whole)


def test_every_tool_schema_property_tells_the_model_the_contract() -> None:
    """**Input: the generated Bedrock tool schema. Outcome: every property is described.**

    The schema is the only thing a reader is told about what an acceptable answer looks like. With
    bare field names, `mistral.ministral-3-3b-instruct` answered `unit_guess` as `"inch"` and had
    every reply rejected while reading the crop correctly — 851 of one run's 1,743 rejections (#718).
    """
    from extraction.models.nova import _bedrock_tool_schema

    schema = _bedrock_tool_schema()
    undescribed = [
        name
        for name, spec in schema["properties"].items()  # type: ignore[union-attr,index]
        if not str(spec.get("description", "")).strip()  # type: ignore[union-attr]
    ]

    assert undescribed == [], f"a reader is told nothing about: {undescribed}"


def test_the_unit_field_is_described_rather_than_enumerated() -> None:
    """An `enum` on `unit_guess` was measured as worse than describing it (#718).

    Constraining the field did not remove the malformation, it moved it into `reading` — 2 of 3
    accepted against 4 of 4 for the description alone. This asserts the measured choice, so that
    "just make it an enum" is not re-tried silently.
    """
    from extraction.models.nova import _bedrock_tool_schema

    unit = _bedrock_tool_schema()["properties"]["unit_guess"]  # type: ignore[index]

    assert "enum" not in unit
    assert '"in"' in unit["description"] and '"mm"' in unit["description"]


# --- #733: the drawing's own notation reaches the reader's shape check -------------------------


@pytest.mark.parametrize("reading", ['10-1/4"', "300 [12]", '2" (VIF)', "8'-6''", '100 1/4" (4EQ)'])
def test_a_reading_in_the_drawings_notation_is_accepted(reading: str) -> None:
    """Every one of seven readers read the hyphenated fraction right in the bake-off, and every one was
    refused here as "not a dimension" (#733). The candidate keeps exactly what the model returned.
    """
    recorder = RecordingRejections()

    outcome = _validate(
        {**_valid_payload(), "reading": reading, "unit_guess": "in"}, recorder=recorder
    )

    assert isinstance(outcome, ObservationCandidate), [r.reason for r in recorder.items]
    assert outcome.raw_text == reading


def test_a_compound_reading_is_refused_under_its_own_reason() -> None:
    """Two dimensions and an operator is a different next action from "not a dimension"."""
    recorder = RecordingRejections()

    outcome = _validate(
        {**_valid_payload(), "reading": '10 1/4"+6"', "unit_guess": "in"}, recorder=recorder
    )

    assert isinstance(outcome, ValidationRejection)
    assert [r.reason for r in recorder.items] == ["not_a_single_value"]


def test_a_dual_token_cannot_carry_a_bare_fraction_past_the_guard() -> None:
    """`19 [3/4]` does not look like a bare fraction as written; its value is one.

    The bare-fraction guard exists to stop a dropped whole number becoming a plausible small dimension.
    Judging the raw text would let a dual token slip one straight past it, so the guard reads the
    canonical form.
    """
    recorder = RecordingRejections()

    outcome = _validate(
        {**_valid_payload(), "reading": "19 [3/4]", "unit_guess": "in"}, recorder=recorder
    )

    assert isinstance(outcome, ValidationRejection)
    assert [r.reason for r in recorder.items] == ["reading_not_a_dimension"]


# ---------------------------------------------------------------------------
# The digits request kind (#865): one piece of a stacked label, read as its digits
# ---------------------------------------------------------------------------


def _digits(payload: object, *, count: int, recorder: RecordingRejections) -> object:
    return validate_digits_payload(
        payload, context=_context(), digit_count=count, recorder=recorder
    )


@pytest.mark.parametrize(("digits", "count"), [("28", 2), ("3", 1), ("16", 2), ("101", 3)])
def test_digits_as_many_as_the_drawing_has_are_the_answer(digits: str, count: int) -> None:
    """The answer is the digits as written — a string, never turned into a number here."""
    recorder = RecordingRejections()

    assert _digits({"digits": digits}, count=count, recorder=recorder) == digits
    assert recorder.items == []


@pytest.mark.parametrize(
    ("digits", "count"),
    [
        # A numerator answered with a digit the drawing does not have, and a whole number short of one.
        ("33", 1),
        ("8", 2),
        ("283", 2),
    ],
)
def test_digits_of_another_count_than_the_drawing_has_are_refused(digits: str, count: int) -> None:
    """**The count is the drawing's.** A reading that is right in every other way but has a digit
    more or fewer than the piece has characters is refused, and recorded with its reason."""
    recorder = RecordingRejections()

    outcome = _digits({"digits": digits}, count=count, recorder=recorder)

    assert isinstance(outcome, ValidationRejection)
    assert outcome.reason == DIGITS_WRONG_COUNT
    assert recorder.items == [outcome]


@pytest.mark.parametrize("digits", ["２８", "2 8", '28"', "3/4", "²", "1028", "-3", "", "twenty"])
def test_anything_but_one_to_three_ascii_digits_is_refused(digits: str) -> None:
    """Not `str.isdigit`: full-width digits and a superscript two are digits to Python, and not a
    number this drawing wrote. An empty answer is refused by the schema before that."""
    recorder = RecordingRejections()

    outcome = _digits({"digits": digits}, count=2, recorder=recorder)

    assert isinstance(outcome, ValidationRejection)
    assert outcome.reason in {DIGITS_NOT_A_NUMBER, "schema_validation_failed"}
    assert recorder.items == [outcome]


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({"digits": 28}, "schema_validation_failed"),
        ({"digits": 28.0}, "float_not_allowed"),
        ({"digits": "28", "unit_guess": "in"}, "schema_validation_failed"),
        ({}, "schema_validation_failed"),
        ("28", "schema_validation_failed"),
    ],
)
def test_a_digits_answer_of_any_other_shape_is_refused(payload: object, reason: str) -> None:
    """A number where the string belongs, a float, a field not asked for, or no answer at all: each
    is a recorded refusal, never coerced into the digits the model did not write."""
    recorder = RecordingRejections()

    outcome = _digits(payload, count=2, recorder=recorder)

    assert isinstance(outcome, ValidationRejection)
    assert outcome.reason == reason


@pytest.mark.parametrize("count", [0, 4, True, "2"])
def test_a_digit_count_no_piece_can_have_is_the_callers_mistake(count: object) -> None:
    with pytest.raises(ValueError, match="digit_count"):
        _digits({"digits": "28"}, count=count, recorder=RecordingRejections())  # type: ignore[arg-type]
