"""Metadata filter and ranker examples for issue #189."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest

from retrieval.candidate import Lane, MatchCandidate
from retrieval.lanes.metadata import (
    ComparisonStatus,
    MetadataField,
    MetadataItem,
    MetadataPolicy,
    metadata_match,
)


def _item(
    number: int,
    *,
    sheet: str | None = "A-102",
    view: str | None = "Kitchen North",
    item_type: str | None = "cabinet",
    material: str | None = "oak",
) -> MetadataItem:
    return MetadataItem(UUID(int=number), sheet, view, item_type, material)


def _policy() -> MetadataPolicy:
    return MetadataPolicy(
        hard_fields=frozenset({MetadataField.VIEW, MetadataField.ITEM_TYPE}),
        weights={
            MetadataField.SHEET: Decimal(1),
            MetadataField.VIEW: Decimal(3),
            MetadataField.ITEM_TYPE: Decimal(4),
            MetadataField.MATERIAL: Decimal(2),
        },
    )


def test_known_hard_mismatch_is_filtered_and_explained() -> None:
    """Input: kitchen cabinet vs bathroom cabinet. Output: removed. Why: view is hard."""

    result = metadata_match(_item(1), [_item(2, view="Bathroom")], policy=_policy())

    assert result.candidates == ()
    evaluation = result.evaluations[0]
    assert evaluation.filtered
    mismatch = next(item for item in evaluation.comparisons if item.field is MetadataField.VIEW)
    assert mismatch.status is ComparisonStatus.MISMATCH
    assert mismatch.left_value == "Kitchen North"
    assert mismatch.right_value == "Bathroom"
    assert mismatch.hard_filter


def test_soft_material_mismatch_remains_a_ranked_near_miss() -> None:
    """Input: matching location/type but walnut. Output: candidate. Why: material is ranking-only."""

    result = metadata_match(_item(1), [_item(2, material="walnut")], policy=_policy())

    assert result.candidates == (
        MatchCandidate(UUID(int=1), UUID(int=2), Lane.METADATA, Decimal("0.8")),
    )
    evaluation = result.evaluations[0]
    mismatch = next(item for item in evaluation.comparisons if item.field is MetadataField.MATERIAL)
    assert mismatch.status is ComparisonStatus.MISMATCH
    assert not mismatch.hard_filter
    assert evaluation.candidate is result.candidates[0]


def test_missing_metadata_is_unknown_and_does_not_erase_a_possible_match() -> None:
    """Input: missing hard-field view. Output: lower candidate. Why: absence is not disagreement."""

    result = metadata_match(_item(1), [_item(2, view=None)], policy=_policy())

    assert result.candidates[0].score == Decimal("0.7")
    view = next(
        item for item in result.evaluations[0].comparisons if item.field is MetadataField.VIEW
    )
    assert view.status is ComparisonStatus.UNKNOWN


def test_exact_weighted_scores_rank_candidates_with_a_reproducible_tie_break() -> None:
    """Input: exact, two equal near-misses. Output: score then UUID. Why: runs reproduce exactly."""

    result = metadata_match(
        _item(1),
        [_item(4, material="walnut"), _item(2), _item(3, material="maple")],
        policy=_policy(),
    )

    assert [(item.right_item_id, item.score) for item in result.candidates] == [
        (UUID(int=2), Decimal(1)),
        (UUID(int=3), Decimal("0.8")),
        (UUID(int=4), Decimal("0.8")),
    ]


def test_metadata_lane_can_only_produce_advisory_match_candidates() -> None:
    """Input: perfect metadata. Output: MatchCandidate only. Why: retrieval cannot approve."""

    candidate = metadata_match(_item(1), [_item(2)], policy=_policy()).candidates[0]

    assert type(candidate) is MatchCandidate
    assert candidate.lane is Lane.METADATA
    assert not hasattr(candidate, "approved_by")


@pytest.mark.parametrize("weight", [1.0, Decimal("NaN"), Decimal("Infinity"), Decimal(0)])
def test_inexact_or_unsafe_weights_are_refused(weight: object) -> None:
    """Input: float/non-finite/non-positive weight. Output: refusal. Why: ranking stays exact."""

    weights: dict[MetadataField, object] = {name: Decimal(1) for name in MetadataField}
    weights[MetadataField.SHEET] = weight
    expected = TypeError if isinstance(weight, float) else ValueError

    with pytest.raises(expected):
        MetadataPolicy(frozenset(), weights)  # type: ignore[arg-type]


def test_policy_requires_an_explicit_weight_for_every_field() -> None:
    """Input: partial policy. Output: refusal. Why: missing policy must not become a hidden default."""

    with pytest.raises(ValueError, match="every metadata field"):
        MetadataPolicy(frozenset(), {MetadataField.SHEET: Decimal(1)})


def test_metadata_comparison_is_literal_and_does_not_normalise_silently() -> None:
    """Input: Oak vs oak. Output: visible soft mismatch. Why: normalisation is an upstream decision."""

    result = metadata_match(_item(1), [_item(2, material="Oak")], policy=_policy())
    material = next(
        item for item in result.evaluations[0].comparisons if item.field is MetadataField.MATERIAL
    )

    assert material.status is ComparisonStatus.MISMATCH
