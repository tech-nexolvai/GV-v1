"""The real arch-versus-shop cabinet rule applies Q2's exact-match answer.

Source: issue #62 and the client vocabulary in plan section 3.
Verification: ``rules/rulebook/cab_arch_vs_shop_001.yaml``.
"""

from __future__ import annotations

from copy import deepcopy
from fractions import Fraction
from pathlib import Path

import yaml

from rules.publication import awaiting_tolerance, is_production_ready, unconfirmed_tolerance_count
from rules.schema import Cardinality, CheckType, Rule
from rules.semantic_types import OperandSource, SemanticType
from rules.snapshot import SnapshotStore, publish
from units.measurement import Measurement, Unit
from verdict.engine import execute
from verdict.operands import EvidenceStatus, VerdictOperand
from verdict.operations.pairwise import CountComparison, PairComparison, pairwise_within_tolerance
from verdict.outcomes import Outcome

RULE_PATH = Path(__file__).resolve().parents[2] / "rules" / "rulebook" / "cab_arch_vs_shop_001.yaml"


def _load_rule() -> Rule:
    authored = yaml.safe_load(RULE_PATH.read_text(encoding="utf-8"))
    return Rule.model_validate(authored)


def _load_authored() -> dict[str, object]:
    authored = yaml.safe_load(RULE_PATH.read_text(encoding="utf-8"))
    assert isinstance(authored, dict)
    return authored


def _old_unconfirmed_rule() -> Rule:
    authored = deepcopy(_load_authored())
    authored["version"] = "1.0.1"
    operation = authored["operation"]
    assert isinstance(operation, dict)
    operation["tolerance"] = {"value": "UNCONFIRMED"}
    return Rule.model_validate(authored)


def _mm(value: int) -> Measurement:
    return Measurement(Fraction(value), Unit.MM, str(value))


def _many_operand(name: str, *values: int, status: EvidenceStatus) -> VerdictOperand:
    return VerdictOperand(
        name=name,
        value=tuple(_mm(value) for value in values),
        status=status,
        source="USER_INPUT",
        evidence_ref=f"reviewer:{name}",
    )


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


def test_rule_authors_q2_as_exact_zero_tolerance() -> None:
    """The zero is stated in inches, the unit the rule's arithmetic is in (#1054).

    Version 1.1.0 said `0 mm` while its arithmetic was inches. Zero is zero in any unit, so no
    outcome changes, but a rule must state its tolerance in the unit it decides in: a reader of
    `0 mm` could think millimetres decide, and Raj's word is inches only.
    """
    rule = _load_rule()

    assert rule.version == "1.1.1"
    assert rule.arithmetic_unit is Unit.INCH
    assert rule.operation.tolerance is not None
    assert rule.operation.tolerance.value == Fraction(0)
    assert rule.operation.tolerance.unit is Unit.INCH
    assert unconfirmed_tolerance_count(rule) == 0
    assert is_production_ready(rule)


def test_publication_report_has_no_unconfirmed_cabinet_tolerances() -> None:
    store = SnapshotStore()
    store.add(publish(_old_unconfirmed_rule()))
    store.add(publish(_load_rule()))

    latest = store.latest("CAB-ARCH-VS-SHOP-001")
    assert latest is not None
    assert latest.version == "1.1.1"
    assert unconfirmed_tolerance_count(latest.rule) == 0
    assert "CAB-ARCH-VS-SHOP-001" not in {waiting.rule_id for waiting in awaiting_tolerance(store)}


def test_prior_snapshot_text_stays_available_for_old_findings() -> None:
    store = SnapshotStore()
    old_snapshot = store.add(publish(_old_unconfirmed_rule()))
    new_snapshot = store.add(publish(_load_rule()))

    assert old_snapshot.snapshot_id != new_snapshot.snapshot_id
    assert store.get(old_snapshot.snapshot_id).version == "1.0.1"
    assert '"version":"1.0.1"' in old_snapshot.canonical_json
    assert '"UNCONFIRMED"' in old_snapshot.canonical_json
    assert store.latest("CAB-ARCH-VS-SHOP-001") == new_snapshot

    finding = execute(
        old_snapshot,
        {
            "architectural_cabinets": _many_operand(
                "architectural_cabinets", 600, status=EvidenceStatus.HUMAN_CONFIRMED
            ),
            "shop_cabinets": _many_operand(
                "shop_cabinets", 600, status=EvidenceStatus.HUMAN_CONFIRMED
            ),
        },
    )
    assert finding.snapshot_id == old_snapshot.snapshot_id
    assert finding.outcome is Outcome.REVIEW_REQUIRED


def test_ambiguous_cabinet_reading_requires_review_before_arithmetic() -> None:
    finding = execute(
        publish(_load_rule()),
        {
            "architectural_cabinets": _many_operand(
                "architectural_cabinets", 600, status=EvidenceStatus.HUMAN_CONFIRMED
            ),
            "shop_cabinets": _many_operand("shop_cabinets", 600, status=EvidenceStatus.CONFLICTING),
        },
    )

    assert finding.outcome is Outcome.REVIEW_REQUIRED
    assert finding.trace is None
    assert "evidence is not qualified" in finding.reason


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


def test_any_mismatched_cabinet_fails_with_exact_zero_tolerance() -> None:
    result = pairwise_within_tolerance(
        left={"CAB-1": _mm(600), "CAB-2": _mm(800)},
        right={"CAB-1": _mm(599), "CAB-2": _mm(800)},
        tolerance=_mm(0),
    )

    pairs = _pairs(result)
    assert pairs["CAB-1"] == PairComparison(
        identifier="CAB-1",
        left=_mm(600),
        right=_mm(599),
        delta=Measurement(Fraction(1), Unit.MM, None),
        outcome=Outcome.FAIL,
        comparison="CAB-1: |600 - 599| = 1 > 0 mm",
    )
    assert pairs["CAB-2"].identifier == "CAB-2"
    assert pairs["CAB-2"].delta == Measurement(Fraction(0), Unit.MM, None)
    assert pairs["CAB-2"].outcome is Outcome.PASS
    assert result.outcome is Outcome.FAIL


def _previous_millimetre_rule() -> Rule:
    authored = deepcopy(_load_authored())
    authored["version"] = "1.1.0"
    operation = authored["operation"]
    assert isinstance(operation, dict)
    operation["tolerance"] = {"value": 0, "unit": "mm"}
    return Rule.model_validate(authored)


def test_the_inch_tolerance_is_a_new_version_beside_the_published_one() -> None:
    """Publishing the fix next to 1.1.0 is a new snapshot, not a conflict, and the newest wins."""
    store = SnapshotStore()
    previous = store.add(publish(_previous_millimetre_rule()))
    current = store.add(publish(_load_rule()))

    assert previous.snapshot_id != current.snapshot_id
    assert store.latest("CAB-ARCH-VS-SHOP-001") == current
    assert '"unit":"mm"' in previous.canonical_json


def test_the_unit_fix_changes_no_outcome() -> None:
    """Same inputs, same outcome, under 1.1.0 (0 mm) and 1.1.1 (0 in)."""

    def run(rule: Rule, shop: int) -> Outcome:
        return execute(
            publish(rule),
            {
                "architectural_cabinets": VerdictOperand(
                    name="architectural_cabinets",
                    value=(Measurement(Fraction(24), Unit.INCH, None),),
                    status=EvidenceStatus.HUMAN_CONFIRMED,
                    source="USER_INPUT",
                ),
                "shop_cabinets": VerdictOperand(
                    name="shop_cabinets",
                    value=(Measurement(Fraction(shop), Unit.INCH, None),),
                    status=EvidenceStatus.HUMAN_CONFIRMED,
                    source="USER_INPUT",
                ),
            },
        ).outcome

    from verdict.operations import register_all

    register_all()
    for shop in (24, 25):
        assert run(_previous_millimetre_rule(), shop) is run(_load_rule(), shop)
    assert run(_load_rule(), 24) is Outcome.PASS
    assert run(_load_rule(), 25) is Outcome.FAIL
