"""The real arch-versus-shop cabinet rule compares widths for exact equality.

Q2 is answered — exact match, no tolerance band — so the rule's tolerance is a
confirmed ``0 mm`` and the two widths must agree to the millimetre. This file
guards that the authored tolerance is exact (not unset), that the rule is now
production-ready, and that the comparison still abstains honestly on a missing
counterpart.

Source: issue #619, Q2 (``docs/CLIENT_FACTS.md``), ``docs/V1_TO_WORKING_PLAN.md`` §3 Phase B.
Verification: ``rules/rulebook/cab_arch_vs_shop_001.yaml``.
"""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import yaml

from rules.publication import is_production_ready, unconfirmed_tolerance_count
from rules.schema import Cardinality, CheckType, Rule
from rules.semantic_types import OperandSource, SemanticType
from units.measurement import Measurement, Unit
from verdict.operations.pairwise import CountComparison, PairComparison, pairwise_within_tolerance
from verdict.outcomes import Outcome

RULE_PATH = Path(__file__).resolve().parents[2] / "rules" / "rulebook" / "cab_arch_vs_shop_001.yaml"


def _load_rule() -> Rule:
    authored = yaml.safe_load(RULE_PATH.read_text(encoding="utf-8"))
    return Rule.model_validate(authored)


def _mm(value: int) -> Measurement:
    return Measurement(Fraction(value), Unit.MM, str(value))


def _pairs(result: object) -> dict[str, PairComparison]:
    intermediates = result.intermediates  # type: ignore[attr-defined]
    return {
        pair.identifier: pair
        for name, pair in intermediates
        if name.startswith("pair[") and isinstance(pair, PairComparison)
    }


def test_rule_selects_identifier_keyed_cabinets_from_both_documents() -> None:
    rule = _load_rule()

    assert rule.check_type is CheckType.ARCH_VS_SHOP
    assert rule.operation.type == "pairwise_within_tolerance"
    assert rule.operation.operands == {
        "left": "architectural_cabinets",
        "right": "shop_cabinets",
    }

    arch = rule.inputs["architectural_cabinets"]
    shop = rule.inputs["shop_cabinets"]
    assert arch.source is OperandSource.ARCH
    assert shop.source is OperandSource.SHOP
    assert arch.semantic_type is SemanticType.CABINET_WIDTH
    assert shop.semantic_type is SemanticType.CABINET_WIDTH
    assert arch.cardinality is Cardinality.MANY
    assert shop.cardinality is Cardinality.MANY
    assert arch.scope == shop.scope


def test_rule_is_exact_equality_now_that_q2_is_answered() -> None:
    rule = _load_rule()

    assert rule.version == "1.1.0"
    assert rule.operation.tolerance is not None
    # Zero is a *confirmed* tolerance — exact match, not an unset one.
    assert rule.operation.tolerance.value == Fraction(0)
    assert rule.operation.tolerance.unit is Unit.MM
    assert rule.operation.tolerance.is_confirmed
    assert unconfirmed_tolerance_count(rule) == 0
    assert is_production_ready(rule)


def test_exact_tolerance_passes_equal_widths_and_fails_any_difference() -> None:
    # The verdict is driven by the rule's *own* authored tolerance, so this proves
    # exactness flows from the YAML, not from a tolerance a test happened to pass in.
    rule = _load_rule()
    assert rule.operation.tolerance is not None
    tolerance = rule.operation.tolerance.as_measurement()

    equal = pairwise_within_tolerance(
        left={"CAB-1": _mm(600)},
        right={"CAB-1": _mm(600)},
        tolerance=tolerance,
    )
    assert equal.outcome is Outcome.PASS

    # One millimetre apart is the smallest whole-mm difference and must fail under an
    # exact (zero) tolerance — the boundary just past equality.
    off_by_one = pairwise_within_tolerance(
        left={"CAB-1": _mm(600)},
        right={"CAB-1": _mm(601)},
        tolerance=tolerance,
    )
    assert off_by_one.outcome is Outcome.FAIL
    assert _pairs(off_by_one)["CAB-1"].delta == Measurement(Fraction(1), Unit.MM, None)


def test_cabinets_pair_by_identifier_and_not_mapping_position() -> None:
    result = pairwise_within_tolerance(
        left={"CAB-1": _mm(600), "CAB-2": _mm(800)},
        right={"CAB-2": _mm(800), "CAB-1": _mm(600)},
        tolerance=_mm(0),
    )

    assert result.outcome is Outcome.PASS
    assert list(_pairs(result)) == ["CAB-1", "CAB-2"]


def test_count_mismatch_remains_separate_from_missing_cabinet_result() -> None:
    result = pairwise_within_tolerance(
        left={"CAB-1": _mm(600), "CAB-2": _mm(800)},
        right={"CAB-1": _mm(600)},
        tolerance=_mm(0),
    )

    count = result.intermediates[0]
    assert count[0] == "count_comparison"
    assert count[1] == CountComparison(
        left_count=2,
        right_count=1,
        outcome=Outcome.NOT_FOUND,
        comparison="left count 2 != right count 1",
    )
    assert _pairs(result)["CAB-2"].outcome is Outcome.NOT_FOUND


def test_each_mismatched_cabinet_has_an_identifier_and_exact_delta() -> None:
    result = pairwise_within_tolerance(
        left={"CAB-1": _mm(600), "CAB-2": _mm(800)},
        right={"CAB-1": _mm(590), "CAB-2": _mm(780)},
        tolerance=_mm(2),
    )

    pairs = _pairs(result)
    assert pairs["CAB-1"] == PairComparison(
        identifier="CAB-1",
        left=_mm(600),
        right=_mm(590),
        delta=Measurement(Fraction(10), Unit.MM, None),
        outcome=Outcome.FAIL,
        comparison="CAB-1: |600 - 590| = 10 > 2 mm",
    )
    assert pairs["CAB-2"].identifier == "CAB-2"
    assert pairs["CAB-2"].delta == Measurement(Fraction(20), Unit.MM, None)
    assert pairs["CAB-2"].outcome is Outcome.FAIL
