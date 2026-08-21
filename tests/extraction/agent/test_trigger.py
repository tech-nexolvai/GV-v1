"""Deterministic ambiguity-trigger tests for issue #243."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from fractions import Fraction

import pytest

from evidence.canonical import EvidenceStatus
from extraction.agent.trigger import (
    AmbiguityReason,
    RegionContext,
    TriggerDecision,
    TriggerRate,
    evaluate_trigger,
    is_ambiguous,
    measure_trigger_rate,
)


def _context(
    *,
    complete: bool = True,
    crop: bool = True,
    reasons: frozenset[AmbiguityReason] = frozenset({AmbiguityReason.UNKNOWN_UNIT}),
) -> RegionContext:
    return RegionContext(
        region_id="page-3-region-7",
        fixed_extraction_complete=complete,
        crop_available=crop,
        ambiguity_reasons=reasons,
    )


@pytest.mark.parametrize(
    "status",
    [
        EvidenceStatus.CORROBORATED,
        EvidenceStatus.HUMAN_CONFIRMED,
        EvidenceStatus.CONFLICTING,
        EvidenceStatus.REJECTED,
    ],
)
def test_non_raw_evidence_never_reaches_the_agent(status: EvidenceStatus) -> None:
    """Input: resolved/conflicting/rejected evidence. Outcome: false. Why: AI cannot override it."""

    assert not is_ambiguous(status, _context())


@pytest.mark.parametrize("reason", list(AmbiguityReason))
def test_raw_candidate_with_approved_reason_and_crop_triggers(
    reason: AmbiguityReason,
) -> None:
    """Input: bounded unresolved region. Outcome: true. Why: targeted escalation is allowed."""

    ctx = _context(reasons=frozenset({reason}))

    assert is_ambiguous(EvidenceStatus.RAW_CANDIDATE, ctx)


def test_fixed_extraction_must_finish_before_the_agent_can_run() -> None:
    """Input: raw candidate during fixed extraction. Outcome: false. Why: fixed routing is first."""

    assert not is_ambiguous(EvidenceStatus.RAW_CANDIDATE, _context(complete=False))


def test_region_without_a_crop_cannot_reach_the_agent() -> None:
    """Input: ambiguous region without crop. Outcome: false. Why: full-package context is forbidden."""

    assert not is_ambiguous(EvidenceStatus.RAW_CANDIDATE, _context(crop=False))


def test_raw_candidate_without_approved_reason_does_not_trigger() -> None:
    """Input: raw but unexplained candidate. Outcome: false. Why: raw alone cannot widen routing."""

    assert not is_ambiguous(
        EvidenceStatus.RAW_CANDIDATE,
        _context(reasons=frozenset()),
    )


def test_identical_input_always_produces_the_same_decision() -> None:
    """Input: same immutable state repeatedly. Outcome: same result. Why: routing is deterministic."""

    ctx = _context(reasons=frozenset({AmbiguityReason.UNCERTAIN_ASSOCIATION}))

    results = {is_ambiguous(EvidenceStatus.RAW_CANDIDATE, ctx) for _ in range(20)}

    assert results == {True}


def test_context_rejects_runtime_strings_as_ambiguity_reasons() -> None:
    """Input: arbitrary runtime flag. Outcome: type error. Why: agent cannot invent permission."""

    with pytest.raises(TypeError, match="AmbiguityReason"):
        RegionContext(
            region_id="region",
            fixed_extraction_complete=True,
            crop_available=True,
            ambiguity_reasons=frozenset({"please_retry"}),  # type: ignore[arg-type]
        )


def test_context_and_policy_inputs_are_immutable() -> None:
    """Input: attempted context mutation. Outcome: rejection. Why: agent cannot widen after check."""

    ctx = _context()

    with pytest.raises(FrozenInstanceError):
        ctx.crop_available = False  # type: ignore[misc]


def test_evaluation_records_region_and_boolean_only() -> None:
    """Input: raw ambiguous region. Outcome: auditable decision. Why: metrics need a stable event."""

    decision = evaluate_trigger(EvidenceStatus.RAW_CANDIDATE, _context())

    assert decision == TriggerDecision(region_id="page-3-region-7", triggered=True)


def test_trigger_rate_is_exact_and_counts_every_evaluated_region() -> None:
    """Input: two triggers in three checks. Outcome: 2/3. Why: cost attribution needs denominator."""

    rate = measure_trigger_rate(
        (
            TriggerDecision("one", True),
            TriggerDecision("two", False),
            TriggerDecision("three", True),
        )
    )

    assert rate == TriggerRate(evaluated=3, triggered=2)
    assert rate.rate == Fraction(2, 3)
    assert isinstance(rate.rate, Fraction)


def test_zero_evaluations_is_unmeasured_not_zero() -> None:
    """Input: no evaluated regions. Outcome: None rate. Why: no data must not look cost-free."""

    rate = measure_trigger_rate(())

    assert rate == TriggerRate(evaluated=0, triggered=0)
    assert rate.rate is None


def test_invalid_status_or_context_fails_loudly() -> None:
    """Input: untyped runtime input. Outcome: type error. Why: model output cannot enter directly."""

    with pytest.raises(TypeError, match="EvidenceStatus"):
        is_ambiguous("RAW_CANDIDATE", _context())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="RegionContext"):
        is_ambiguous(EvidenceStatus.RAW_CANDIDATE, object())  # type: ignore[arg-type]
