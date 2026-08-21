"""Fail-closed terminal outcomes for the bounded extraction agent.

The agent may produce an explicit abstention or preserve a disagreement as
``CONFLICTING``. Confidence is not an input to numeric comparison, and no result type
contains a winner or preferred reading.

Source: ``docs/DESIGN_AI.md`` section 3.2 and issue #246.
Verification: ``tests/extraction/agent/test_outcomes.py``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from evidence.candidate import ObservationCandidate
from evidence.canonical import EvidenceStatus
from evidence.corroborate import CorroborationResult, corroborate
from extraction.agent.tools import AbstainArguments
from units.measurement import Measurement


def _require_text(value: object, *, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")


@dataclass(frozen=True, slots=True)
class AgentAbstention:
    """A reviewer-readable reason the agent stopped without a reliable result."""

    region_id: str
    reason: str
    requires_review: Literal[True] = True

    def __post_init__(self) -> None:
        _require_text(self.region_id, field="region_id")
        _require_text(self.reason, field="reason")
        if self.requires_review is not True:
            raise ValueError("an agent abstention must require review")


@dataclass(frozen=True, slots=True)
class ConflictingReadings:
    """Every exact reading in a disagreement, with no field that can select a winner."""

    region_id: str
    candidate_ids: tuple[str, ...]
    readings: tuple[Measurement, ...]
    reason: str
    status: Literal[EvidenceStatus.CONFLICTING] = EvidenceStatus.CONFLICTING
    requires_review: Literal[True] = True

    def __post_init__(self) -> None:
        _require_text(self.region_id, field="region_id")
        _require_text(self.reason, field="reason")
        if not isinstance(self.candidate_ids, tuple) or len(self.candidate_ids) < 2:
            raise ValueError("a conflict requires at least two candidate ids")
        if any(
            not isinstance(candidate_id, str) or not candidate_id.strip()
            for candidate_id in self.candidate_ids
        ):
            raise ValueError("candidate_ids must contain only non-empty strings")
        if len(set(self.candidate_ids)) != len(self.candidate_ids):
            raise ValueError("candidate_ids must be unique")
        if not isinstance(self.readings, tuple) or len(self.readings) != len(self.candidate_ids):
            raise ValueError("readings must correspond one-to-one with candidate_ids")
        if any(not isinstance(reading, Measurement) for reading in self.readings):
            raise TypeError("readings must contain only exact Measurement values")
        if self.status is not EvidenceStatus.CONFLICTING:
            raise ValueError("numeric disagreement must remain CONFLICTING")
        if self.requires_review is not True:
            raise ValueError("conflicting readings must require review")


type AgentReadingOutcome = CorroborationResult | AgentAbstention | ConflictingReadings


def abstain(arguments: AbstainArguments) -> AgentAbstention:
    """Turn the allow-listed abstain tool into an explicit review-required outcome."""

    if not isinstance(arguments, AbstainArguments):
        raise TypeError("arguments must be AbstainArguments")
    return AgentAbstention(region_id=arguments.region_id, reason=arguments.reason)


def assess_readings(
    region_id: str,
    candidates: Sequence[ObservationCandidate],
) -> AgentReadingOutcome:
    """Compare independent route readings without considering confidence.

    The evidence layer remains the single owner of agreement policy. This function
    makes insufficient input an explicit abstention and expands a corroboration conflict
    into a traceable agent outcome containing every exact reading.
    """

    _require_text(region_id, field="region_id")
    candidate_tuple = tuple(candidates)
    if any(not isinstance(candidate, ObservationCandidate) for candidate in candidate_tuple):
        raise TypeError("candidates must contain only ObservationCandidate values")
    if len(candidate_tuple) < 2:
        return AgentAbstention(
            region_id,
            "at least two independent readings are required for comparison",
        )
    if len({candidate.extractor for candidate in candidate_tuple}) < 2:
        return AgentAbstention(
            region_id,
            "the available readings are not from independent extraction routes",
        )
    if any(candidate.parsed_value is None for candidate in candidate_tuple):
        return AgentAbstention(
            region_id,
            "at least one extraction route did not produce a numeric reading",
        )

    result = corroborate(candidate_tuple)
    if result.status is not EvidenceStatus.CONFLICTING:
        return result

    readings = tuple(candidate.parsed_value for candidate in candidate_tuple)
    if any(reading is None for reading in readings):
        raise AssertionError("corroboration reported conflict without numeric readings")
    exact_readings = tuple(reading for reading in readings if reading is not None)
    return ConflictingReadings(
        region_id=region_id,
        candidate_ids=tuple(candidate.candidate_id for candidate in candidate_tuple),
        readings=exact_readings,
        reason="independent extraction routes produced different numeric readings",
    )
