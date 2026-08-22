"""Examples for exact-pinned Reciprocal Rank Fusion in issue #177."""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction
from uuid import UUID

import pytest

from retrieval.candidate import Lane, MatchCandidate
from retrieval.fusion import LaneRank, reciprocal_rank_fusion

SUBJECT = UUID(int=1)


def candidate(item: int, lane: Lane, score: str | None = None) -> MatchCandidate:
    return MatchCandidate(
        SUBJECT,
        UUID(int=item),
        lane,
        None if score is None else Decimal(score),
    )


def test_exact_candidate_can_never_be_displaced_by_fusion() -> None:
    """Input: exact last in fuzzy lanes. Output: exact first. Why: scores cannot overrule identity."""

    exact = candidate(9, Lane.EXACT)
    result = reciprocal_rank_fusion(
        SUBJECT,
        exact=[exact],
        aliases=[],
        rankings={
            Lane.TRIGRAM: [candidate(2, Lane.TRIGRAM), candidate(9, Lane.TRIGRAM)],
            Lane.LEXICAL: [candidate(2, Lane.LEXICAL), candidate(9, Lane.LEXICAL)],
            Lane.DENSE: [candidate(2, Lane.DENSE), candidate(9, Lane.DENSE)],
        },
        k=60,
    )

    assert result.candidates[0] is exact
    assert result.candidates[0].lane is Lane.EXACT
    assert [value.right_item_id for value in result.candidates] == [UUID(int=9), UUID(int=2)]
    assert all(value.candidate.right_item_id != UUID(int=9) for value in result.evaluations)


def test_aliases_are_pinned_below_exact_and_above_fusion() -> None:
    """Input: exact, alias and fuzzy. Output: priority order. Why: lanes 1–2 precede lanes 5–7."""

    result = reciprocal_rank_fusion(
        SUBJECT,
        exact=[candidate(3, Lane.EXACT)],
        aliases=[candidate(2, Lane.ALIAS)],
        rankings={Lane.DENSE: [candidate(4, Lane.DENSE)]},
        k=10,
    )

    assert [value.lane for value in result.candidates] == [Lane.EXACT, Lane.ALIAS, Lane.FUSION]


def test_fused_candidate_retains_every_contributing_lane_rank() -> None:
    """Input: one item in three rankings. Output: three ranks. Why: reviewers can reconstruct RRF."""

    result = reciprocal_rank_fusion(
        SUBJECT,
        exact=[],
        aliases=[],
        rankings={
            Lane.TRIGRAM: [candidate(3, Lane.TRIGRAM), candidate(2, Lane.TRIGRAM)],
            Lane.LEXICAL: [candidate(2, Lane.LEXICAL)],
            Lane.DENSE: [
                candidate(4, Lane.DENSE),
                candidate(5, Lane.DENSE),
                candidate(2, Lane.DENSE),
            ],
        },
        k=4,
    )

    evaluation = next(
        value for value in result.evaluations if value.candidate.right_item_id == UUID(int=2)
    )
    assert evaluation.per_lane_ranks == (
        LaneRank(Lane.TRIGRAM, 2),
        LaneRank(Lane.LEXICAL, 1),
        LaneRank(Lane.DENSE, 3),
    )
    assert evaluation.rrf_score == Fraction(1, 6) + Fraction(1, 5) + Fraction(1, 7)
    assert evaluation.candidate.score is None
    assert result.k == 4


def test_fusion_uses_rank_not_incomparable_lane_scores() -> None:
    """Input: conflicting score scales. Output: rank-based order. Why: lane scores are incomparable."""

    result = reciprocal_rank_fusion(
        SUBJECT,
        exact=[],
        aliases=[],
        rankings={
            Lane.TRIGRAM: [candidate(2, Lane.TRIGRAM, "0.1"), candidate(3, Lane.TRIGRAM, "0.99")],
            Lane.LEXICAL: [candidate(2, Lane.LEXICAL, "0.01")],
        },
        k=1,
    )

    assert result.candidates[0].right_item_id == UUID(int=2)
    assert result.evaluations[0].rrf_score == Fraction(1, 2) + Fraction(1, 2)


def test_equal_rrf_scores_use_uuid_as_a_deterministic_tie_breaker() -> None:
    """Input: equal scores in reverse input order. Output: UUID order. Why: reruns reproduce."""

    result = reciprocal_rank_fusion(
        SUBJECT,
        exact=[],
        aliases=[],
        rankings={
            Lane.TRIGRAM: [candidate(4, Lane.TRIGRAM)],
            Lane.LEXICAL: [candidate(2, Lane.LEXICAL)],
        },
        k=5,
    )

    assert [value.right_item_id for value in result.candidates] == [UUID(int=2), UUID(int=4)]


@pytest.mark.parametrize("unsafe", [-1, True, 1.5])
def test_k_must_be_explicitly_safe(unsafe: object) -> None:
    """Input: unsafe k. Output: refusal. Why: the fusion constant cannot be a hidden guess."""

    with pytest.raises(ValueError, match="non-negative integer"):
        reciprocal_rank_fusion(
            SUBJECT,
            exact=[],
            aliases=[],
            rankings={},
            k=unsafe,  # type: ignore[arg-type]
        )


def test_only_lanes_five_to_seven_can_enter_fusion() -> None:
    """Input: geometry ranking. Output: refusal. Why: scope is exactly trigram/lexical/dense."""

    with pytest.raises(ValueError, match="only trigram"):
        reciprocal_rank_fusion(
            SUBJECT,
            exact=[],
            aliases=[],
            rankings={Lane.GEOMETRY: [candidate(2, Lane.GEOMETRY)]},
            k=60,
        )


def test_duplicate_in_one_lane_is_refused() -> None:
    """Input: same item twice in one ranking. Output: refusal. Why: one item cannot have two ranks."""

    repeated = candidate(2, Lane.DENSE)
    with pytest.raises(ValueError, match="duplicate"):
        reciprocal_rank_fusion(
            SUBJECT,
            exact=[],
            aliases=[],
            rankings={Lane.DENSE: [repeated, repeated]},
            k=60,
        )
