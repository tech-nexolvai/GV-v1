"""Deterministic permission boundary for the bounded extraction agent.

Fixed extraction always runs first. This module permits escalation only for an
unresolved raw candidate with a usable crop and a closed, code-reviewed ambiguity
reason. Model output is deliberately absent from the interface.

Source: ``docs/DESIGN_AI.md`` section 3.1 and issue #243.
Verification: ``tests/extraction/agent/test_trigger.py``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction

from evidence.canonical import EvidenceStatus


class AmbiguityReason(StrEnum):
    """Fixed-extraction conditions that may justify a bounded agent attempt."""

    UNREADABLE_TEXT = "unreadable_text"
    UNKNOWN_UNIT = "unknown_unit"
    UNCERTAIN_ASSOCIATION = "uncertain_association"


TRIGGERABLE_REASONS: frozenset[AmbiguityReason] = frozenset(AmbiguityReason)


@dataclass(frozen=True, slots=True)
class RegionContext:
    """Immutable facts established before the agent is allowed to run."""

    region_id: str
    fixed_extraction_complete: bool
    crop_available: bool
    ambiguity_reasons: frozenset[AmbiguityReason]

    def __post_init__(self) -> None:
        if not isinstance(self.region_id, str) or not self.region_id.strip():
            raise ValueError("region_id must be a non-empty string")
        if not isinstance(self.fixed_extraction_complete, bool):
            raise TypeError("fixed_extraction_complete must be a bool")
        if not isinstance(self.crop_available, bool):
            raise TypeError("crop_available must be a bool")
        if not isinstance(self.ambiguity_reasons, frozenset):
            raise TypeError("ambiguity_reasons must be a frozenset")
        if any(not isinstance(reason, AmbiguityReason) for reason in self.ambiguity_reasons):
            raise TypeError("ambiguity_reasons must contain only AmbiguityReason values")


@dataclass(frozen=True, slots=True)
class TriggerDecision:
    """One auditable trigger evaluation, separate from metric aggregation."""

    region_id: str
    triggered: bool

    def __post_init__(self) -> None:
        if not isinstance(self.region_id, str) or not self.region_id.strip():
            raise ValueError("region_id must be a non-empty string")
        if not isinstance(self.triggered, bool):
            raise TypeError("triggered must be a bool")


@dataclass(frozen=True, slots=True)
class TriggerRate:
    """Exact count and rate of regions permitted to enter the agent."""

    evaluated: int
    triggered: int

    def __post_init__(self) -> None:
        for name in ("evaluated", "triggered"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if value < 0:
                raise ValueError(f"{name} must be zero or greater")
        if self.triggered > self.evaluated:
            raise ValueError("triggered cannot exceed evaluated")

    @property
    def rate(self) -> Fraction | None:
        """Return the exact trigger rate, or None when no region was evaluated."""

        if self.evaluated == 0:
            return None
        return Fraction(self.triggered, self.evaluated)


def is_ambiguous(status: EvidenceStatus, ctx: RegionContext) -> bool:
    """Return whether fixed evidence permits the bounded agent to run.

    Qualified evidence never reaches the agent. Conflicting evidence also does not:
    a model may not choose between disagreeing readings. A raw candidate triggers only
    after fixed extraction completes, when a bounded crop exists and a closed ambiguity
    reason applies.
    """

    if not isinstance(status, EvidenceStatus):
        raise TypeError("status must be an EvidenceStatus")
    if not isinstance(ctx, RegionContext):
        raise TypeError("ctx must be a RegionContext")
    return (
        status is EvidenceStatus.RAW_CANDIDATE
        and ctx.fixed_extraction_complete
        and ctx.crop_available
        and bool(ctx.ambiguity_reasons & TRIGGERABLE_REASONS)
    )


def evaluate_trigger(status: EvidenceStatus, ctx: RegionContext) -> TriggerDecision:
    """Return an auditable decision without changing trigger policy or state."""

    return TriggerDecision(ctx.region_id, is_ambiguous(status, ctx))


def measure_trigger_rate(decisions: Iterable[TriggerDecision]) -> TriggerRate:
    """Aggregate completed decisions using exact arithmetic and no runtime threshold."""

    materialised = tuple(decisions)
    if any(not isinstance(decision, TriggerDecision) for decision in materialised):
        raise TypeError("decisions must contain only TriggerDecision values")
    return TriggerRate(
        evaluated=len(materialised),
        triggered=sum(decision.triggered for decision in materialised),
    )
