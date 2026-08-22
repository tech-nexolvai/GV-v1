"""Fuse advisory lane rankings while keeping deterministic identifiers pinned.

Only lanes 5–7 participate in Reciprocal Rank Fusion. Exact and alias candidates are copied into a
separate prefix, so no fuzzy score can displace them. RRF scores remain ``Fraction`` values in the
explanation instead of being rounded into ``Decimal`` merely to populate diagnostic candidate data.

Source: ``docs/DESIGN_EXTRACTION.md`` section 8 and issue #177.
Verification: ``tests/retrieval/test_fusion.py``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from types import MappingProxyType
from uuid import UUID

from retrieval.candidate import Lane, MatchCandidate

FUSABLE_LANES = (Lane.TRIGRAM, Lane.LEXICAL, Lane.DENSE)


@dataclass(frozen=True, slots=True)
class LaneRank:
    """The one-indexed position contributed by a particular advisory lane."""

    lane: Lane
    rank: int


@dataclass(frozen=True, slots=True)
class FusedEvaluation:
    """One fused proposal with every source rank and its exact RRF score."""

    candidate: MatchCandidate
    per_lane_ranks: tuple[LaneRank, ...]
    rrf_score: Fraction


@dataclass(frozen=True, slots=True)
class FusionResult:
    """Pinned deterministic candidates followed by the fused advisory ranking."""

    candidates: tuple[MatchCandidate, ...]
    evaluations: tuple[FusedEvaluation, ...]
    k: int


def _validate_pinned(
    candidates: Sequence[MatchCandidate], *, subject_item_id: UUID, lane: Lane
) -> tuple[MatchCandidate, ...]:
    retained: dict[UUID, MatchCandidate] = {}
    for candidate in candidates:
        if not isinstance(candidate, MatchCandidate):
            raise TypeError("pinned rankings must contain only MatchCandidate values")
        if candidate.left_item_id != subject_item_id:
            raise ValueError("every candidate must belong to the requested subject item")
        if candidate.lane is not lane:
            raise ValueError(f"the {lane.value} ranking may contain only {lane.value} candidates")
        if candidate.right_item_id in retained:
            raise ValueError(f"the {lane.value} ranking contains a duplicate candidate")
        retained[candidate.right_item_id] = candidate
    return tuple(retained[item_id] for item_id in sorted(retained, key=lambda value: value.int))


def _validate_rankings(
    rankings: Mapping[Lane, Sequence[MatchCandidate]], *, subject_item_id: UUID
) -> Mapping[Lane, tuple[MatchCandidate, ...]]:
    if not isinstance(rankings, Mapping):
        raise TypeError("rankings must be a mapping")
    unexpected = set(rankings) - set(FUSABLE_LANES)
    if unexpected:
        names = ", ".join(sorted(str(lane) for lane in unexpected))
        raise ValueError(f"only trigram, lexical and dense rankings may be fused; received {names}")
    validated: dict[Lane, tuple[MatchCandidate, ...]] = {}
    for lane in FUSABLE_LANES:
        candidates = rankings.get(lane, ())
        seen: set[UUID] = set()
        accepted: list[MatchCandidate] = []
        for candidate in candidates:
            if not isinstance(candidate, MatchCandidate):
                raise TypeError("lane rankings must contain only MatchCandidate values")
            if candidate.left_item_id != subject_item_id:
                raise ValueError("every candidate must belong to the requested subject item")
            if candidate.lane is not lane:
                raise ValueError(f"the {lane.value} ranking contains a candidate from another lane")
            if candidate.right_item_id in seen:
                raise ValueError(f"the {lane.value} ranking contains a duplicate candidate")
            seen.add(candidate.right_item_id)
            accepted.append(candidate)
        validated[lane] = tuple(accepted)
    return MappingProxyType(validated)


def reciprocal_rank_fusion(
    subject_item_id: UUID,
    *,
    exact: Sequence[MatchCandidate],
    aliases: Sequence[MatchCandidate],
    rankings: Mapping[Lane, Sequence[MatchCandidate]],
    k: int,
) -> FusionResult:
    """Pin exact/alias proposals, then combine lanes 5–7 by their one-indexed ranks.

    ``k`` is mandatory and recorded in the result. Its absence from the design therefore cannot turn
    into a hidden default; the caller must supply the value selected by its published configuration.
    """

    if not isinstance(subject_item_id, UUID):
        raise TypeError("subject_item_id must be a UUID")
    if not isinstance(k, int) or isinstance(k, bool) or k < 0:
        raise ValueError("k must be a non-negative integer")
    exact_candidates = _validate_pinned(exact, subject_item_id=subject_item_id, lane=Lane.EXACT)
    alias_candidates = _validate_pinned(aliases, subject_item_id=subject_item_id, lane=Lane.ALIAS)
    exact_ids = {candidate.right_item_id for candidate in exact_candidates}
    aliases_without_exact = tuple(
        candidate for candidate in alias_candidates if candidate.right_item_id not in exact_ids
    )
    pinned_ids = exact_ids | {candidate.right_item_id for candidate in aliases_without_exact}
    validated = _validate_rankings(rankings, subject_item_id=subject_item_id)

    ranks_by_item: dict[UUID, list[LaneRank]] = {}
    scores: dict[UUID, Fraction] = {}
    for lane in FUSABLE_LANES:
        for rank, candidate in enumerate(validated[lane], start=1):
            if candidate.right_item_id in pinned_ids:
                continue
            ranks_by_item.setdefault(candidate.right_item_id, []).append(LaneRank(lane, rank))
            scores[candidate.right_item_id] = scores.get(
                candidate.right_item_id, Fraction(0)
            ) + Fraction(1, k + rank)

    evaluations = tuple(
        FusedEvaluation(
            candidate=MatchCandidate(subject_item_id, item_id, Lane.FUSION, None),
            per_lane_ranks=tuple(ranks_by_item[item_id]),
            rrf_score=scores[item_id],
        )
        for item_id in sorted(scores, key=lambda value: (-scores[value], value.int))
    )
    return FusionResult(
        candidates=(
            *exact_candidates,
            *aliases_without_exact,
            *(evaluation.candidate for evaluation in evaluations),
        ),
        evaluations=evaluations,
        k=k,
    )
