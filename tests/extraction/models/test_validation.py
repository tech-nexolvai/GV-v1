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
