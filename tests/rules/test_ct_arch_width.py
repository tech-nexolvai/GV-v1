"""CT-ARCH-WIDTH-001: the vendor's countertop widths against the architect's, exact, in inches.

The client lead, 24 Jul: "overall dimensions has to match". Settled with the client lead: exact match, inches only, every
difference a flag. Which architect dimension measures the same thing as which vendor piece is the
pairing's job (#1053); this rule only compares the pairs it is given, position by position.

Source: issue #1054 · Verification: ``rules/rulebook/ct_arch_width_001.yaml``. Synthetic values only.
"""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import yaml

from rules.publication import is_production_ready, unconfirmed_tolerance_count
from rules.required_inputs import required_inputs
from rules.schema import Cardinality, CheckType, GlobalApplicability, Rule
from rules.semantic_types import OperandSource, ProductType, SemanticType
from rules.snapshot import SnapshotStore, publish
from units.measurement import Measurement, Unit
from verdict.engine import execute
from verdict.operands import EvidenceStatus, VerdictOperand
from verdict.operations import register_all
from verdict.outcomes import Outcome, Severity

RULEBOOK = Path(__file__).resolve().parents[2] / "rules" / "rulebook"
RULE_PATH = RULEBOOK / "ct_arch_width_001.yaml"


def _rule() -> Rule:
    return Rule.model_validate(yaml.safe_load(RULE_PATH.read_text(encoding="utf-8")))


def _widths(name: str, source: str, *values: Fraction, unit: Unit = Unit.INCH) -> VerdictOperand:
    return VerdictOperand(
        name=name,
        value=tuple(Measurement(value, unit, None) for value in values),
        status=EvidenceStatus.CORROBORATED,
        source=source,
    )


def _check(
    architect: tuple[Fraction, ...],
    vendor: tuple[Fraction, ...],
    *,
    architect_unit: Unit = Unit.INCH,
) -> Outcome:
    register_all()
    finding = execute(
        publish(_rule()),
        {
            "architect_widths": _widths(
                "architect_widths", "ARCH", *architect, unit=architect_unit
            ),
            "vendor_widths": _widths("vendor_widths", "SHOP", *vendor),
        },
    )
    return finding.outcome


def test_the_rule_compares_architect_and_vendor_widths_exactly_in_inches() -> None:
    rule = _rule()

    assert rule.id == "CT-ARCH-WIDTH-001"
    assert rule.version == "1.0.1"
    assert rule.product_type is ProductType.COUNTERTOP
    assert rule.check_type is CheckType.ARCH_VS_SHOP
    assert rule.severity is Severity.FLAG
    assert rule.arithmetic_unit is Unit.INCH
    assert isinstance(rule.applicability, GlobalApplicability)
    assert rule.operation.type == "pairwise_within_tolerance"
    assert rule.operation.operands == {"left": "architect_widths", "right": "vendor_widths"}
    assert rule.operation.tolerance is not None
    assert rule.operation.tolerance.value == Fraction(0)
    assert rule.operation.tolerance.unit is Unit.INCH
    assert rule.on_missing is Outcome.NOT_FOUND
    assert rule.on_ambiguous is Outcome.REVIEW_REQUIRED
    assert unconfirmed_tolerance_count(rule) == 0
    assert is_production_ready(rule)

    architect = rule.inputs["architect_widths"]
    vendor = rule.inputs["vendor_widths"]
    assert architect.source is OperandSource.ARCH
    assert vendor.source is OperandSource.SHOP
    assert architect.semantic_type is vendor.semantic_type is SemanticType.COUNTERTOP_PIECE_WIDTH
    assert architect.cardinality is vendor.cardinality is Cardinality.MANY


def test_the_rule_says_what_it_does_in_plain_english() -> None:
    rule = _rule()

    assert "architect" in rule.name.lower()
    assert "overall dimensions has to match" in rule.description
    assert "exact" in rule.description.lower()
    assert "inch" in rule.description.lower()


def test_equal_widths_pass() -> None:
    assert _check((Fraction(42), Fraction(18)), (Fraction(42), Fraction(18))) is Outcome.PASS


def test_a_sixteenth_apart_fails() -> None:
    assert _check((Fraction(42),), (Fraction(42) - Fraction(1, 16),)) is Outcome.FAIL


def test_one_failing_pair_fails_the_row() -> None:
    assert _check((Fraction(42), Fraction(18)), (Fraction(42), Fraction(37, 2))) is Outcome.FAIL


def test_a_width_with_no_counterpart_is_not_found_never_a_pass() -> None:
    assert _check((Fraction(42), Fraction(18)), (Fraction(42),)) is Outcome.NOT_FOUND


def test_millimetres_never_decide() -> None:
    outcome = _check((Fraction(1067),), (Fraction(42),), architect_unit=Unit.MM)

    assert outcome is Outcome.REVIEW_REQUIRED


def test_an_unqualified_operand_never_reaches_arithmetic() -> None:
    register_all()
    finding = execute(
        publish(_rule()),
        {
            "architect_widths": VerdictOperand(
                name="architect_widths",
                value=(Measurement(Fraction(42), Unit.INCH, None),),
                status=EvidenceStatus.RAW_CANDIDATE,
                source="ARCH",
            ),
            "vendor_widths": _widths("vendor_widths", "SHOP", Fraction(42)),
        },
    )

    assert finding.outcome is Outcome.REVIEW_REQUIRED
    assert finding.trace is None


def test_the_rulebook_still_builds_one_reviewer_form() -> None:
    rules = [
        Rule.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        for path in sorted(RULEBOOK.glob("*.yaml"))
    ]

    needs = required_inputs(rules)
    vendor = next(q for q in needs.quantities if q.key == "SHOP:countertop_piece_width")
    assert {(c.rule_id, c.input_name) for c in vendor.consumers} >= {
        ("CT-WIDTH-001", "piece_widths"),
        ("CT-ARCH-WIDTH-001", "vendor_widths"),
    }


def test_publishing_twice_gives_one_snapshot() -> None:
    store = SnapshotStore()
    first = store.add(publish(_rule()))
    second = store.add(publish(_rule()))

    assert first == second
    assert store.latest("CT-ARCH-WIDTH-001") == first
