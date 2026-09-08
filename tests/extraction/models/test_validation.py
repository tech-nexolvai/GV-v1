"""Fail-closed payload validation tests for issue #250."""

from __future__ import annotations

from typing import Any

import pytest

from evidence.candidate import ObservationCandidate
from evidence.coordinates import ImagePoint
from extraction.models.validation import (
    CandidateContext,
    ValidationRejection,
    validate_payload,
)
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
        "polygon": [[10, 20], [30, 20], [30, 40]],
    }


def test_valid_payload_becomes_an_uncorroborated_candidate() -> None:
    """Input: understood payload. Outcome: candidate. Why: validation cannot create evidence."""

    recorder = RecordingRejections()

    outcome = validate_payload(_valid_payload(), context=_context(), recorder=recorder)

    assert isinstance(outcome, ObservationCandidate)
    assert outcome.raw_text == "984"
    assert outcome.unit_guess is Unit.MM
    assert outcome.parsed_value is None
    assert outcome.confidence is None
    assert outcome.polygon == (ImagePoint(10, 20), ImagePoint(30, 20), ImagePoint(30, 40))
    assert recorder.items == []


@pytest.mark.parametrize("unknown_field", ["verdict", "confidence", "helpful_note"])
def test_unknown_field_is_recorded_and_rejected(unknown_field: str) -> None:
    """Input: invented field. Outcome: abstention. Why: unknown output is never silently dropped."""

    payload = _valid_payload()
    payload[unknown_field] = "PASS"
    recorder = RecordingRejections()

    outcome = validate_payload(payload, context=_context(), recorder=recorder)

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

    outcome = validate_payload(payload, context=_context(), recorder=recorder)

    assert isinstance(outcome, ValidationRejection)
    assert outcome.reason == "schema_validation_failed"
    assert len(recorder.items) == 1


@pytest.mark.parametrize("coordinate", [10.5, 10.0])
def test_every_float_is_rejected_before_pydantic_conversion(coordinate: float) -> None:
    """Input: decimal or integral float. Outcome: abstention. Why: coercion cannot erase origin."""

    payload = _valid_payload()
    payload["polygon"] = [[coordinate, 20], [30, 20], [30, 40]]
    recorder = RecordingRejections()

    outcome = validate_payload(payload, context=_context(), recorder=recorder)

    assert isinstance(outcome, ValidationRejection)
    assert outcome.reason == "float_not_allowed"
    assert outcome.errors == (
        "$.polygon[0][0] contains a float; model numeric values must remain exact",
    )
    assert recorder.items == [outcome]


def test_float_in_an_unknown_nested_field_is_still_rejected_first() -> None:
    """Input: hidden float. Outcome: float abstention. Why: exactness covers all output."""

    payload = _valid_payload()
    payload["metadata"] = {"score": 0.9}
    recorder = RecordingRejections()

    outcome = validate_payload(payload, context=_context(), recorder=recorder)

    assert isinstance(outcome, ValidationRejection)
    assert outcome.reason == "float_not_allowed"
    assert "$.metadata.score" in outcome.errors[0]


def test_exact_integral_coordinate_strings_are_accepted() -> None:
    """Input: exact numeric strings. Outcome: candidate. Why: no binary float path is involved."""

    payload = _valid_payload()
    payload["polygon"] = [["10", "20"], ["30", "20"], ["30", "40"]]

    outcome = validate_payload(
        payload,
        context=_context(),
        recorder=RecordingRejections(),
    )

    assert isinstance(outcome, ObservationCandidate)
    assert outcome.polygon[0] == ImagePoint(10, 20)


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("unit_guess", "cm", "candidate_conversion_failed"),
        ("polygon", [[10, 20], [30, 20]], "schema_validation_failed"),
        (
            "polygon",
            [["10.25", "20"], ["30", "20"], ["30", "40"]],
            "candidate_conversion_failed",
        ),
    ],
)
def test_unsupported_unit_or_polygon_abstains(field: str, value: object, reason: str) -> None:
    """Input: unusable unit/location. Outcome: abstention. Why: evidence cannot be guessed."""

    payload = _valid_payload()
    payload[field] = value
    recorder = RecordingRejections()

    outcome = validate_payload(payload, context=_context(), recorder=recorder)

    assert isinstance(outcome, ValidationRejection)
    assert outcome.reason == reason
    assert recorder.items == [outcome]


def test_rejection_retains_raw_response_and_trusted_provenance() -> None:
    """Input: malformed output. Outcome: diagnostic record. Why: changes stay auditable."""

    payload: dict[str, Any] = {"reading": "984", "unexpected": "field"}
    recorder = RecordingRejections()

    outcome = validate_payload(payload, context=_context(), recorder=recorder)

    assert isinstance(outcome, ValidationRejection)
    assert outcome.raw_response == '{"reading":"984","unexpected":"field"}'
    assert outcome.candidate_id == "candidate-250"
    assert outcome.extractor_version == "amazon.nova-2-lite-v1:0"
    assert recorder.items == [outcome]


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

    outcome = validate_payload(payload, context=_context(), recorder=recorder)

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

    outcome = validate_payload(payload, context=_context(), recorder=RecordingRejections())

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

    outcome = validate_payload(payload, context=_context(), recorder=recorder)

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

    outcome = validate_payload(payload, context=_context(), recorder=recorder)

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

    outcome = validate_payload(payload, context=_context(), recorder=RecordingRejections())

    assert isinstance(outcome, ValidationRejection)
    assert '"1/2"' in outcome.raw_response


def test_the_dimension_check_produces_no_value() -> None:
    """Outcome: `parsed_value` is still `None` on an accepted reading.

    The check parses in order to decide readability and throws the result away. This seam reads; it
    does not convert, and `evidence/normalize.py` is where a unit becomes authoritative — under a
    caller who knows what the sheet is drawn in, rather than a model that guessed.
    """
    outcome = validate_payload(
        _valid_payload() | {"reading": '33"'},
        context=_context(),
        recorder=RecordingRejections(),
    )

    assert isinstance(outcome, ObservationCandidate)
    assert outcome.parsed_value is None
