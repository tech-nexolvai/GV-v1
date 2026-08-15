"""Verification for issue #117: extractors create candidates, not facts."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields
from decimal import Decimal
from fractions import Fraction

import pytest

from evidence.candidate import ObservationCandidate
from evidence.coordinates import ImagePoint, StoredPoint
from rules.semantic_types import SemanticType
from units.measurement import Measurement, Unit


def candidate(**overrides: object) -> ObservationCandidate:
    values: dict[str, object] = {
        "candidate_id": "candidate-paddleocr-001",
        "extractor": "paddleocr",
        "extractor_version": "3.1.0",
        "raw_text": "  984 [38 3/4]\n",
        "parsed_value": Measurement(Fraction(984), Unit.MM, "984"),
        "unit_guess": Unit.MM,
        "semantic_guess": SemanticType.CABINET_WIDTH,
        "page": 2,
        "polygon": (
            ImagePoint(120, 80),
            ImagePoint(240, 80),
            ImagePoint(240, 110),
            ImagePoint(120, 110),
        ),
        "confidence": Decimal("0.999"),
        "ambiguity_flags": (),
    }
    values.update(overrides)
    return ObservationCandidate(**values)  # type: ignore[arg-type]


def test_candidate_preserves_raw_text_and_extractor_identity_exactly() -> None:
    observation = candidate()

    assert observation.candidate_id == "candidate-paddleocr-001"
    assert observation.extractor == "paddleocr"
    assert observation.extractor_version == "3.1.0"
    assert observation.raw_text == "  984 [38 3/4]\n"


def test_candidate_is_frozen_and_hashable() -> None:
    observation = candidate()

    assert hash(observation)
    with pytest.raises(FrozenInstanceError):
        observation.raw_text = "984"  # type: ignore[misc]


def test_candidate_has_guesses_but_cannot_claim_evidence_status() -> None:
    observation = candidate(
        unit_guess=None,
        semantic_guess=None,
        ambiguity_flags=("unit unclear", "association uncertain"),
    )

    field_names = {field.name for field in fields(ObservationCandidate)}
    assert observation.unit_guess is None
    assert observation.semantic_guess is None
    assert observation.ambiguity_flags == ("unit unclear", "association uncertain")
    assert "status" not in field_names
    assert not hasattr(observation, "status")


def test_confidence_is_retained_without_promoting_the_candidate() -> None:
    observation = candidate(confidence=Decimal(1))

    assert observation.confidence == Decimal(1)
    assert not hasattr(observation, "status")


def test_missing_parse_and_diagnostic_values_are_honest_candidate_states() -> None:
    observation = candidate(
        parsed_value=None,
        unit_guess=None,
        semantic_guess=None,
        confidence=None,
        polygon=(),
        ambiguity_flags=("could not parse",),
    )

    assert observation.parsed_value is None
    assert observation.confidence is None
    assert observation.polygon == ()


@pytest.mark.parametrize("bad_value", [1.0, Fraction(984), Decimal(984), 984])
def test_parsed_value_must_be_an_exact_measurement_or_none(bad_value: object) -> None:
    with pytest.raises(TypeError, match="Measurement or None"):
        candidate(parsed_value=bad_value)


def test_float_confidence_is_rejected_instead_of_being_silently_converted() -> None:
    with pytest.raises(TypeError, match="Decimal or None"):
        candidate(confidence=0.99)


def test_polygon_preserves_extractor_image_space() -> None:
    image_polygon = (ImagePoint(1, 2), ImagePoint(3, 4))

    assert candidate(polygon=image_polygon).polygon == image_polygon
    with pytest.raises(TypeError, match="ImagePoint"):
        candidate(polygon=(StoredPoint(Decimal("0.1"), Decimal("0.2")),))


@pytest.mark.parametrize(
    ("overrides", "error", "message"),
    [
        ({"candidate_id": ""}, ValueError, "non-empty"),
        ({"page": -1}, ValueError, "zero or greater"),
        ({"page": False}, TypeError, "must be an integer"),
        ({"unit_guess": "mm"}, TypeError, "Unit or None"),
        ({"semantic_guess": "cabinet_width"}, TypeError, "SemanticType or None"),
        ({"ambiguity_flags": ["unclear"]}, TypeError, "tuple"),
        ({"ambiguity_flags": (1,)}, TypeError, "only strings"),
    ],
)
def test_invalid_candidate_boundary_values_are_rejected(
    overrides: dict[str, object], error: type[Exception], message: str
) -> None:
    with pytest.raises(error, match=message):
        candidate(**overrides)


def test_extractor_version_is_a_required_constructor_argument() -> None:
    values = {
        "candidate_id": "candidate-paddleocr-001",
        "extractor": "paddleocr",
        "raw_text": "984",
        "parsed_value": None,
        "unit_guess": None,
        "semantic_guess": None,
        "page": 0,
        "polygon": (),
        "confidence": None,
        "ambiguity_flags": (),
    }

    with pytest.raises(TypeError, match="extractor_version"):
        ObservationCandidate(**values)  # type: ignore[call-arg]
