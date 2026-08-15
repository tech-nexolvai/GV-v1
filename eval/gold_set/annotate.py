"""Validated construction helper for one reviewed gold case.

The helper only packages human-authored ground truth. It never creates observations,
chooses outcomes, resolves disagreements, or writes proprietary case data into the
repository.

Source: issue #129 and ``AGENTS.md`` section 8, Phase 0.
Verification: ``tests/eval/test_annotate.py``.
"""

from __future__ import annotations

from pathlib import Path

from eval.gold_set.schema import Disagreement, GoldCase, GroundTruth, Provenance
from rules.semantic_types import ProductType


def build_case(
    *,
    case_id: str,
    product_type: ProductType,
    arch: str | Path,
    shop: str | Path,
    ground_truth: GroundTruth,
    provenance: Provenance,
    disagreements: tuple[Disagreement, ...] = (),
) -> GoldCase:
    """Return one frozen, validated case from explicitly reviewed inputs."""

    return GoldCase(
        id=case_id,
        product_type=product_type,
        arch=Path(arch),
        shop=Path(shop),
        ground_truth=ground_truth,
        provenance=provenance,
        disagreements=disagreements,
    )
