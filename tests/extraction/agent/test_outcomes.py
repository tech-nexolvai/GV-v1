"""Abstention and confidence-independent conflict tests for issue #246."""

from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError, fields
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest

import extraction.agent.outcomes as outcomes_module
from evidence.candidate import ObservationCandidate
from evidence.canonical import EvidenceStatus
from evidence.coordinates import ImagePoint
from evidence.corroborate import CorroborationResult
from extraction.agent.outcomes import (
    AgentAbstention,
    ConflictingReadings,
    abstain,
    assess_readings,
)
from extraction.agent.tools import AbstainArguments
from units.measurement import Measurement, Unit


def _candidate(
    candidate_id: str,
    extractor: str,
    value: int | None,
    *,
    confidence: Decimal,
) -> ObservationCandidate:
    measurement = Measurement(Fraction(value), Unit.MM, str(value)) if value is not None else None
    return ObservationCandidate(
        candidate_id=candidate_id,
        extractor=extractor,
        extractor_version="1",
        raw_text="" if value is None else str(value),
        parsed_value=measurement,
        unit_guess=Unit.MM if value is not None else None,
        semantic_guess=None,
        page=0,
        polygon=(ImagePoint(1, 1), ImagePoint(2, 1), ImagePoint(2, 2)),
        confidence=confidence,
        ambiguity_flags=(),
    )


def test_allow_list_abstain_produces_a_readable_review_outcome() -> None:
    """Input: abstain tool call. Outcome: readable review. Why: decline must never be a gap."""

    result = abstain(AbstainArguments("region-1", "the unit could not be established"))

    assert result == AgentAbstention(
        region_id="region-1",
        reason="the unit could not be established",
        requires_review=True,
    )
    assert result.requires_review is True


def test_abstention_requires_a_non_empty_reviewer_reason() -> None:
    """Input: blank explanation. Outcome: rejection. Why: reviewers need an actionable reason."""

    with pytest.raises(ValueError, match="reason"):
        AgentAbstention("region-1", "")


def test_abstention_is_immutable() -> None:
    """Input: attempted outcome mutation. Outcome: rejection. Why: review routing is auditable."""

    result = AgentAbstention("region-1", "no reliable reading")

    with pytest.raises(FrozenInstanceError):
        result.reason = "ignore it"  # type: ignore[misc]


@pytest.mark.parametrize(
    "candidates",
    [
        (),
        (_candidate("one", "ocr", 984, confidence=Decimal("0.9")),),
    ],
)
def test_fewer_than_two_readings_abstains(
    candidates: tuple[ObservationCandidate, ...],
) -> None:
    """Input: insufficient routes. Outcome: abstention. Why: one reader cannot corroborate itself."""

    result = assess_readings("region-1", candidates)

    assert isinstance(result, AgentAbstention)
    assert result.requires_review is True


def test_repeated_versions_of_one_route_are_not_independent() -> None:
    """Input: two OCR versions. Outcome: abstention. Why: retrying one reader is not corroboration."""

    candidates = (
        _candidate("one", "ocr", 984, confidence=Decimal("0.2")),
        _candidate("two", "ocr", 985, confidence=Decimal("0.9")),
    )

    result = assess_readings("region-1", candidates)

    assert isinstance(result, AgentAbstention)
    assert "not from independent" in result.reason


def test_missing_numeric_reading_abstains_instead_of_becoming_zero() -> None:
    """Input: one unparsed route. Outcome: abstention. Why: missing never defaults to zero."""

    candidates = (
        _candidate("one", "ocr", 984, confidence=Decimal("0.8")),
        _candidate("two", "nova", None, confidence=Decimal("0.9")),
    )

    result = assess_readings("region-1", candidates)

    assert isinstance(result, AgentAbstention)
    assert "did not produce" in result.reason


def test_disagreement_is_conflicting_regardless_of_confidence() -> None:
    """Input: confident 985 vs uncertain 984. Outcome: conflict. Why: confidence has no authority."""

    high = _candidate("high", "nova", 985, confidence=Decimal("0.999999"))
    low = _candidate("low", "ocr", 984, confidence=Decimal("0.000001"))

    result = assess_readings("region-1", (high, low))

    assert isinstance(result, ConflictingReadings)
    assert result.status is EvidenceStatus.CONFLICTING
    assert result.requires_review is True
    assert result.candidate_ids == ("high", "low")
    assert result.readings == (high.parsed_value, low.parsed_value)


def test_reversing_confidence_and_order_never_selects_a_winner() -> None:
    """Input: reversed disagreeing routes. Outcome: same conflict. Why: order cannot express preference."""

    first = _candidate("first", "nova", 985, confidence=Decimal("0.000001"))
    second = _candidate("second", "ocr", 984, confidence=Decimal("0.999999"))

    forward = assess_readings("region-1", (first, second))
    reverse = assess_readings("region-1", (second, first))

    assert isinstance(forward, ConflictingReadings)
    assert isinstance(reverse, ConflictingReadings)
    assert forward.status is reverse.status is EvidenceStatus.CONFLICTING
    assert set(forward.candidate_ids) == set(reverse.candidate_ids) == {"first", "second"}
    prohibited_fields = {"winner", "selected", "preferred", "best_candidate"}
    assert prohibited_fields.isdisjoint(field.name for field in fields(ConflictingReadings))


def test_equal_readings_delegate_to_existing_corroboration_policy() -> None:
    """Input: equal independent readings. Outcome: evidence result. Why: policy has one owner."""

    candidates = (
        _candidate("one", "ocr", 984, confidence=Decimal("0.1")),
        _candidate("two", "nova", 984, confidence=Decimal("0.9")),
    )

    result = assess_readings("region-1", candidates)

    assert isinstance(result, CorroborationResult)
    assert result.status is EvidenceStatus.RAW_CANDIDATE


def test_outcomes_module_does_not_import_verdict_or_rules() -> None:
    """Input: module imports. Outcome: decision layers absent. Why: extraction cannot write verdicts."""

    source_path = Path(outcomes_module.__file__).resolve()
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", maxsplit=1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            roots.add(node.module.split(".", maxsplit=1)[0])

    assert {"verdict", "rules"}.isdisjoint(roots)
