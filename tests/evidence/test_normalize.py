"""Verification for issue #119: every normalisation input has an explicit outcome and reason."""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction
from uuid import UUID

import pytest

from evidence.candidate import ObservationCandidate
from evidence.canonical import Authority, CanonicalObservation, EvidenceStatus
from evidence.coordinates import ImagePoint, PageTransform
from evidence.normalize import NormalizationReason, NormalizationRefusal, normalize
from rules.semantic_types import DocumentRole, SemanticType
from units.measurement import Measurement, Unit

DOCUMENT_ID = UUID("12345678-1234-5678-1234-567812345678")


def _transform(rotation: int = 0) -> PageTransform:
    return PageTransform(
        dpi=72,
        rotation=rotation,
        media_box=(Decimal(0), Decimal(0), Decimal(612), Decimal(792)),
        crop_box=(Decimal(0), Decimal(0), Decimal(612), Decimal(792)),
    )


def _candidate(**changes: object) -> ObservationCandidate:
    authored_text = '38 3/4"'
    values: dict[str, object] = {
        "candidate_id": "candidate-vector-001",
        "extractor": "pdfplumber",
        "extractor_version": "0.11.7",
        "raw_text": authored_text,
        "parsed_value": Measurement(Fraction(155, 4), Unit.INCH, authored_text),
        "unit_guess": Unit.INCH,
        "semantic_guess": SemanticType.CABINET_WIDTH,
        "page": 2,
        "polygon": (
            ImagePoint(100, 100),
            ImagePoint(200, 100),
            ImagePoint(200, 140),
            ImagePoint(100, 140),
        ),
        "confidence": Decimal("0.98"),
        "ambiguity_flags": (),
    }
    values.update(changes)
    return ObservationCandidate(**values)  # type: ignore[arg-type]


def _normalize(candidate: ObservationCandidate, *, rotation: int = 0) -> object:
    return normalize(
        candidate,
        transform=_transform(rotation),
        document_version_id=DOCUMENT_ID,
        document_role=DocumentRole.SHOP,
    )


def test_valid_inch_candidate_produces_raw_authoritative_observation_without_conversion() -> None:
    """Input: exact inch token. Outcome: RAW observation. Why: all required facts are present."""

    candidate = _candidate()
    result = _normalize(candidate)

    assert isinstance(result, CanonicalObservation)
    assert result.value is candidate.parsed_value
    assert result.value.unit is Unit.INCH
    assert result.value.exact == Fraction(155, 4)
    assert result.value.raw_text == candidate.raw_text
    assert result.status is EvidenceStatus.RAW_CANDIDATE
    assert result.authority is Authority.AUTHORITATIVE
    assert result.supported_by == (candidate.candidate_id,)
    assert result.corroborated_by == ()


@pytest.mark.parametrize(
    ("case_input", "changes", "expected_reason", "why"),
    [
        pytest.param(
            "parsed_value=None",
            {"parsed_value": None},
            NormalizationReason.MISSING_VALUE,
            "there is no value to normalise",
            id="missing-value-refuses-because-no-fact-exists",
        ),
        pytest.param(
            "unit_guess=None",
            {"unit_guess": None},
            NormalizationReason.UNKNOWN_UNIT,
            "will not assume millimetres",
            id="unknown-unit-refuses-instead-of-defaulting-to-mm",
        ),
        pytest.param(
            "unit_guess=MM while parsed value is INCH",
            {"unit_guess": Unit.MM},
            NormalizationReason.AMBIGUOUS_UNIT,
            "unit disagrees",
            id="contradictory-units-refuse-because-authored-unit-is-unclear",
        ),
        pytest.param(
            "semantic_guess=None",
            {"semantic_guess": None},
            NormalizationReason.MISSING_SEMANTIC_TYPE,
            "will not infer one from position",
            id="missing-semantic-type-refuses-instead-of-position-guess",
        ),
        pytest.param(
            "measurement token differs from candidate raw_text",
            {
                "parsed_value": Measurement(Fraction(155, 4), Unit.INCH, '38.75"'),
            },
            NormalizationReason.INCONSISTENT_TOKEN,
            "token differs",
            id="changed-token-refuses-because-reviewer-must-see-authored-text",
        ),
        pytest.param(
            "polygon has fewer than three points",
            {"polygon": (ImagePoint(100, 100), ImagePoint(200, 100))},
            NormalizationReason.INVALID_POLYGON,
            "cannot become valid stored geometry",
            id="invalid-polygon-refuses-because-evidence-cannot-be-located",
        ),
    ],
)
def test_each_invalid_input_returns_its_named_refusal_and_safety_reason(
    case_input: str,
    changes: dict[str, object],
    expected_reason: NormalizationReason,
    why: str,
) -> None:
    """Every row states its input, refusal outcome, and why guessing would be unsafe."""

    result = _normalize(_candidate(**changes))

    assert isinstance(result, NormalizationRefusal), case_input
    assert result.reason is expected_reason, case_input
    assert why in result.detail, case_input


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_polygon_round_trip_returns_each_input_point_within_one_pixel(rotation: int) -> None:
    """Input: each supported rotation. Outcome: stored polygon. Why: localisation stays auditable."""

    candidate = _candidate()
    result = _normalize(candidate, rotation=rotation)

    assert isinstance(result, CanonicalObservation)
    restored = tuple(_transform(rotation).from_stored(point) for point in result.polygon.points)
    for original, round_tripped in zip(candidate.polygon, restored, strict=True):
        assert abs(original.x - round_tripped.x) <= 1
        assert abs(original.y - round_tripped.y) <= 1


def test_same_candidate_and_context_produce_the_same_result_without_io_or_clock() -> None:
    """Input repeated twice. Outcome equal twice. Why: normalisation must be reproducible."""

    candidate = _candidate()

    assert _normalize(candidate) == _normalize(candidate)
