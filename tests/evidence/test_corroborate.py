"""Verification for issue #120: each corroboration case states input, outcome, and reason."""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

import pytest

from evidence.candidate import ObservationCandidate
from evidence.canonical import CorroborationLane, EvidenceStatus
from evidence.coordinates import ImagePoint
from evidence.corroborate import CorroborationResult, corroborate
from rules.semantic_types import SemanticType
from units.dual import parse_dual
from units.measurement import Measurement, Unit


def _candidate(
    candidate_id: str,
    extractor: str,
    *,
    exact: Fraction = Fraction(984),
    unit: Unit = Unit.MM,
    raw_text: str = "984",
    semantic_guess: SemanticType | None = SemanticType.CABINET_WIDTH,
    confidence: Decimal = Decimal("0.5"),
    extractor_version: str = "1.0",
) -> ObservationCandidate:
    return ObservationCandidate(
        candidate_id=candidate_id,
        extractor=extractor,
        extractor_version=extractor_version,
        raw_text=raw_text,
        parsed_value=Measurement(exact, unit, raw_text),
        unit_guess=unit,
        semantic_guess=semantic_guess,
        page=2,
        polygon=(ImagePoint(10, 10), ImagePoint(20, 10), ImagePoint(20, 20)),
        confidence=confidence,
        ambiguity_flags=(),
    )


def test_two_independent_readers_agreeing_numerically_and_semantically_are_corroborated() -> None:
    """Input: equal PDF/OCR readings. Outcome: CORROBORATED. Why: routes and meaning agree."""

    vector = _candidate("vector-1", "pdfplumber")
    ocr = _candidate("ocr-1", "paddleocr")

    result = corroborate((vector, ocr))

    assert result == CorroborationResult(
        EvidenceStatus.CORROBORATED,
        ("vector-1", "ocr-1"),
        (),
        CorroborationLane.SECOND_READER,
    )


def test_numeric_agreement_with_different_semantics_stays_raw() -> None:
    """Input: equal numbers, different meanings. Outcome: RAW. Why: reading is not association."""

    cabinet = _candidate("vector-1", "pdfplumber")
    filler = _candidate(
        "ocr-1",
        "paddleocr",
        semantic_guess=SemanticType.FILLER_WIDTH,
    )

    result = corroborate((cabinet, filler))

    assert result.status is EvidenceStatus.RAW_CANDIDATE
    assert result.lane is CorroborationLane.SECOND_READER
    assert result.conflicts_with == ()


def test_numeric_agreement_with_an_unknown_semantic_type_stays_raw() -> None:
    """Input: one unknown association. Outcome: RAW. Why: position never supplies a meaning."""

    known = _candidate("vector-1", "pdfplumber")
    unknown = _candidate("ocr-1", "paddleocr", semantic_guess=None)

    assert corroborate((known, unknown)).status is EvidenceStatus.RAW_CANDIDATE


def test_same_extractor_at_different_versions_is_not_independent() -> None:
    """Input: two versions of one reader. Outcome: RAW. Why: systematic errors can repeat."""

    first = _candidate("ocr-1", "paddleocr", extractor_version="1.0")
    second = _candidate("ocr-2", "paddleocr", extractor_version="2.0")

    result = corroborate((first, second))

    assert result.status is EvidenceStatus.RAW_CANDIDATE
    assert result.lane is None


def test_disagreeing_readers_are_conflicting_regardless_of_confidence() -> None:
    """Input: high-confidence 985 vs low-confidence 984. Outcome: CONFLICTING. Why: no winner."""

    high_confidence = _candidate(
        "ocr-1",
        "paddleocr",
        exact=Fraction(985),
        raw_text="985",
        confidence=Decimal("0.999"),
    )
    low_confidence = _candidate(
        "vector-1",
        "pdfplumber",
        confidence=Decimal("0.1"),
    )

    result = corroborate((high_confidence, low_confidence))

    assert result.status is EvidenceStatus.CONFLICTING
    assert result.supported_by == ("ocr-1", "vector-1")
    assert result.conflicts_with == ("ocr-1", "vector-1")
    assert result.lane is CorroborationLane.SECOND_READER


def test_single_candidate_without_an_independent_lane_stays_raw() -> None:
    """Input: one reading. Outcome: RAW. Why: one route cannot corroborate itself."""

    result = corroborate((_candidate("vector-1", "pdfplumber"),))

    assert result == CorroborationResult(
        EvidenceStatus.RAW_CANDIDATE,
        ("vector-1",),
        (),
        None,
    )


def test_consistent_dual_unit_token_corroborates_a_known_semantic_reading() -> None:
    """Input: 984 [38 3/4]. Outcome: CORROBORATED. Why: authored readings agree in-band."""

    candidate = _candidate("vector-1", "pdfplumber")

    result = corroborate((candidate,), dual_dimension=parse_dual("984 [38 3/4]"))

    assert result.status is EvidenceStatus.CORROBORATED
    assert result.supported_by == ("vector-1",)
    assert result.lane is CorroborationLane.DUAL_UNIT


def test_consistent_dual_unit_token_cannot_promote_an_unknown_semantic_association() -> None:
    """Input: agreeing token, unknown meaning. Outcome: RAW. Why: the lane checks reading only."""

    candidate = _candidate("vector-1", "pdfplumber", semantic_guess=None)

    result = corroborate((candidate,), dual_dimension=parse_dual("984 [38 3/4]"))

    assert result.status is EvidenceStatus.RAW_CANDIDATE
    assert result.lane is CorroborationLane.DUAL_UNIT


def test_single_unit_token_is_not_corroboration_or_conflict() -> None:
    """Input: 984 with no alternate. Outcome: RAW. Why: absence is not agreement or conflict."""

    candidate = _candidate("vector-1", "pdfplumber")

    result = corroborate((candidate,), dual_dimension=parse_dual("984"))

    assert result.status is EvidenceStatus.RAW_CANDIDATE
    assert result.conflicts_with == ()
    assert result.lane is None


def test_inconsistent_dual_unit_token_becomes_conflicting_without_a_preferred_side() -> None:
    """Input: 984 [39 3/4]. Outcome: CONFLICTING. Why: authored readings disagree."""

    candidate = _candidate("vector-1", "pdfplumber")

    result = corroborate((candidate,), dual_dimension=parse_dual("984 [39 3/4]"))

    assert result.status is EvidenceStatus.CONFLICTING
    assert result.supported_by == ("vector-1",)
    assert result.conflicts_with == ("vector-1",)
    assert result.lane is CorroborationLane.DUAL_UNIT


def test_empty_input_raises_instead_of_inventing_an_evidence_state() -> None:
    """Input: no candidates. Outcome: ValueError. Why: there is no observation to judge."""

    with pytest.raises(ValueError, match="at least one"):
        corroborate(())


def test_dual_dimension_must_be_attributed_to_exactly_one_matching_candidate() -> None:
    """Input: ambiguous or mismatched owner. Outcome: ValueError. Why: provenance must be exact."""

    first = _candidate("vector-1", "pdfplumber")
    second = _candidate("ocr-1", "paddleocr")
    with pytest.raises(ValueError, match="exactly one"):
        corroborate((first, second), dual_dimension=parse_dual("984 [38 3/4]"))

    mismatched = _candidate("vector-2", "pdfplumber", exact=Fraction(985), raw_text="985")
    with pytest.raises(ValueError, match="primary must match"):
        corroborate((mismatched,), dual_dimension=parse_dual("984 [38 3/4]"))
