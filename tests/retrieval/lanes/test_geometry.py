"""Geometry-linked candidate examples for issue #182."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest

from retrieval.candidate import Lane, MatchCandidate
from retrieval.lanes.geometry import (
    GeometryPair,
    GeometryPolicy,
    GeometrySignal,
    GeometrySignalKind,
    geometry_match,
)


def _signal(
    kind: GeometrySignalKind,
    *,
    supports: bool = True,
    strength: Decimal = Decimal(1),
) -> GeometrySignal:
    return GeometrySignal(
        kind,
        supports,
        strength if supports else Decimal(0),
        f"{kind.value} was derived from exact drawing geometry",
        (UUID(int=100 + list(GeometrySignalKind).index(kind)),),
    )


def _pair(number: int, *signals: GeometrySignal) -> GeometryPair:
    return GeometryPair(UUID(int=1), UUID(int=number), tuple(signals))


def _policy() -> GeometryPolicy:
    return GeometryPolicy(
        required_kinds=frozenset(
            {GeometrySignalKind.SHARED_VIEW, GeometrySignalKind.SPATIAL_ADJACENCY}
        ),
        weights={
            GeometrySignalKind.SHARED_VIEW: Decimal(2),
            GeometrySignalKind.SPATIAL_ADJACENCY: Decimal(3),
            GeometrySignalKind.DIMENSIONAL_RELATIONSHIP: Decimal(5),
        },
    )


def test_shared_view_adjacency_and_dimensions_produce_an_advisory_candidate() -> None:
    """Input: three supporting structures. Output: geometry candidate. Why: relationships agree."""

    pair = _pair(2, *(_signal(kind) for kind in GeometrySignalKind))
    result = geometry_match([pair], policy=_policy())

    assert result.candidates == (
        MatchCandidate(UUID(int=1), UUID(int=2), Lane.GEOMETRY, Decimal(1)),
    )
    assert result.evaluations[0].pair.signals == pair.signals
    assert not hasattr(result.candidates[0], "approved_by")


def test_missing_required_geometry_is_filtered_instead_of_guessed() -> None:
    """Input: dimensional agreement only. Output: no candidate. Why: shared structure is unknown."""

    result = geometry_match(
        [_pair(2, _signal(GeometrySignalKind.DIMENSIONAL_RELATIONSHIP))], policy=_policy()
    )

    assert result.candidates == ()
    assert result.evaluations[0].filtered
    assert "shared_view" in result.evaluations[0].reason
    assert "spatial_adjacency" in result.evaluations[0].reason


def test_contradictory_required_adjacency_removes_the_pair() -> None:
    """Input: same view but incompatible neighbors. Output: removed. Why: structure contradicts."""

    result = geometry_match(
        [
            _pair(
                2,
                _signal(GeometrySignalKind.SHARED_VIEW),
                _signal(GeometrySignalKind.SPATIAL_ADJACENCY, supports=False),
                _signal(GeometrySignalKind.DIMENSIONAL_RELATIONSHIP),
            )
        ],
        policy=_policy(),
    )

    assert result.candidates == ()
    assert "spatial_adjacency" in result.evaluations[0].reason
    assert result.evaluations[0].pair.signals[1].evidence_ids


def test_exact_structural_strength_ranks_candidates_deterministically() -> None:
    """Input: varying dimension support and a tie. Output: exact score then UUID ordering."""

    required = (
        _signal(GeometrySignalKind.SHARED_VIEW),
        _signal(GeometrySignalKind.SPATIAL_ADJACENCY),
    )
    result = geometry_match(
        [
            _pair(
                4,
                *required,
                _signal(GeometrySignalKind.DIMENSIONAL_RELATIONSHIP, strength=Decimal("0.5")),
            ),
            _pair(2, *required, _signal(GeometrySignalKind.DIMENSIONAL_RELATIONSHIP)),
            _pair(
                3,
                *required,
                _signal(GeometrySignalKind.DIMENSIONAL_RELATIONSHIP, strength=Decimal("0.5")),
            ),
        ],
        policy=_policy(),
    )

    assert [(item.right_item_id, item.score) for item in result.candidates] == [
        (UUID(int=2), Decimal(1)),
        (UUID(int=3), Decimal("0.75")),
        (UUID(int=4), Decimal("0.75")),
    ]


def test_raw_polygon_or_free_text_is_not_a_geometry_lane_input() -> None:
    """Input: module surface. Output: typed relationships only. Why: document planes differ."""

    assert set(GeometryPair.__dataclass_fields__) == {
        "left_item_id",
        "right_item_id",
        "signals",
    }


@pytest.mark.parametrize("strength", [1.0, Decimal("NaN"), Decimal("Infinity"), Decimal("-0.1")])
def test_inexact_or_unsafe_signal_strength_is_refused(strength: object) -> None:
    """Input: float/non-finite/out-of-range support. Output: refusal. Why: ranking stays exact."""

    expected = TypeError if isinstance(strength, float) else ValueError
    with pytest.raises(expected):
        GeometrySignal(
            GeometrySignalKind.SHARED_VIEW,
            True,
            strength,  # type: ignore[arg-type]
            "test signal",
            (UUID(int=10),),
        )


def test_duplicate_signal_kind_is_refused_as_ambiguous_evidence() -> None:
    """Input: two adjacency conclusions. Output: refusal. Why: lane cannot choose one silently."""

    with pytest.raises(ValueError, match="only one signal"):
        _pair(
            2,
            _signal(GeometrySignalKind.SPATIAL_ADJACENCY),
            _signal(GeometrySignalKind.SPATIAL_ADJACENCY),
        )
