"""Auditable corroboration decisions across independent reading routes.

Confidence is deliberately absent from every decision. Numeric disagreement remains a
conflict, while numeric agreement can promote only when semantic association is also known.

Source: ``docs/DESIGN.md`` section 3.14, plan section F2 and issue #120.
Verification: ``tests/evidence/test_corroborate.py``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from evidence.candidate import ObservationCandidate
from evidence.canonical import CorroborationLane, EvidenceStatus
from units.dual import DualDimension
from units.policy import Consistency, check_dual


@dataclass(frozen=True, slots=True)
class CorroborationResult:
    """One evidence judgment with every contributing candidate identifier."""

    status: EvidenceStatus
    supported_by: tuple[str, ...]
    conflicts_with: tuple[str, ...]
    lane: CorroborationLane | None


def _candidate_ids(candidates: tuple[ObservationCandidate, ...]) -> tuple[str, ...]:
    candidate_ids = tuple(candidate.candidate_id for candidate in candidates)
    if len(set(candidate_ids)) != len(candidate_ids):
        raise ValueError("candidate ids must be unique within one corroboration decision")
    return candidate_ids


def _dual_result(
    candidate: ObservationCandidate, dual_dimension: DualDimension
) -> CorroborationResult:
    if candidate.parsed_value is None:
        raise ValueError("the dual-unit candidate must have a parsed primary measurement")
    if (
        candidate.parsed_value.exact != dual_dimension.primary.exact
        or candidate.parsed_value.unit is not dual_dimension.primary.unit
    ):
        raise ValueError("the dual dimension primary must match its candidate measurement")

    candidate_ids = (candidate.candidate_id,)
    consistency = check_dual(dual_dimension)
    if consistency is Consistency.NOT_CORROBORATED:
        return CorroborationResult(EvidenceStatus.RAW_CANDIDATE, candidate_ids, (), None)
    if consistency is Consistency.INCONSISTENT:
        return CorroborationResult(
            EvidenceStatus.CONFLICTING,
            candidate_ids,
            candidate_ids,
            CorroborationLane.DUAL_UNIT,
        )

    status = (
        EvidenceStatus.CORROBORATED
        if candidate.semantic_guess is not None
        else EvidenceStatus.RAW_CANDIDATE
    )
    return CorroborationResult(status, candidate_ids, (), CorroborationLane.DUAL_UNIT)


def _same_numeric_reading(candidates: tuple[ObservationCandidate, ...]) -> bool:
    values = tuple(candidate.parsed_value for candidate in candidates)
    if any(value is None for value in values):
        return False
    first = values[0]
    assert first is not None
    return all(
        value is not None and value.unit is first.unit and value.exact == first.exact
        for value in values[1:]
    )


def corroborate(
    candidates: Sequence[ObservationCandidate],
    *,
    dual_dimension: DualDimension | None = None,
) -> CorroborationResult:
    """Judge independent readers or one candidate's authored dual-unit token.

    Different extractor names establish reader independence; version changes do not.
    A dual dimension belongs to exactly one candidate and is always delegated to
    :func:`units.policy.check_dual` so rounding policy has one implementation.
    """

    candidate_tuple = tuple(candidates)
    if not candidate_tuple:
        raise ValueError("corroboration requires at least one candidate")
    if any(not isinstance(candidate, ObservationCandidate) for candidate in candidate_tuple):
        raise TypeError("candidates must contain only ObservationCandidate values")
    candidate_ids = _candidate_ids(candidate_tuple)

    if dual_dimension is not None:
        if len(candidate_tuple) != 1:
            raise ValueError("a dual dimension must be attributed to exactly one candidate")
        return _dual_result(candidate_tuple[0], dual_dimension)

    if len(candidate_tuple) == 1:
        return CorroborationResult(EvidenceStatus.RAW_CANDIDATE, candidate_ids, (), None)

    independent = len({candidate.extractor for candidate in candidate_tuple}) >= 2
    if not independent:
        return CorroborationResult(EvidenceStatus.RAW_CANDIDATE, candidate_ids, (), None)

    values_present = all(candidate.parsed_value is not None for candidate in candidate_tuple)
    if not values_present:
        return CorroborationResult(EvidenceStatus.RAW_CANDIDATE, candidate_ids, (), None)
    if not _same_numeric_reading(candidate_tuple):
        return CorroborationResult(
            EvidenceStatus.CONFLICTING,
            candidate_ids,
            candidate_ids,
            CorroborationLane.SECOND_READER,
        )

    semantics = {candidate.semantic_guess for candidate in candidate_tuple}
    status = (
        EvidenceStatus.CORROBORATED
        if None not in semantics and len(semantics) == 1
        else EvidenceStatus.RAW_CANDIDATE
    )
    return CorroborationResult(status, candidate_ids, (), CorroborationLane.SECOND_READER)
