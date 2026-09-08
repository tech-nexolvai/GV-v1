"""CT-2 proves exact countertop depth and the derived back-clearance guard.

Source: issue #59; client facts Q2, Q5, Q6, Q12 and Q13.
Verification: ``rules/rulebook/ct_depth_001.yaml`` and
``rules/rulebook/ct_back_offset_min_001.yaml``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import cast

import pytest
import yaml

from rules.parameters import ParameterLayer, ParameterValue, Provenance, ResolvedParameter
from rules.publication import is_production_ready, tolerances_of
from rules.schema import Quantity, Rule
from rules.semantic_types import SemanticType
from rules.snapshot import publish
from units.measurement import Measurement, Unit
from verdict.engine import execute
from verdict.operands import EvidenceStatus, VerdictOperand
from verdict.operations import register_all
from verdict.outcomes import Outcome, Severity
from verdict.registry import REGISTRY

RULEBOOK = Path(__file__).resolve().parents[2] / "rules" / "rulebook"
DEPTH_RULE_PATH = RULEBOOK / "ct_depth_001.yaml"
BACK_OFFSET_RULE_PATH = RULEBOOK / "ct_back_offset_min_001.yaml"
WHEN = datetime(2026, 8, 22, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _registered_operations() -> object:
    previous = dict(REGISTRY)
    REGISTRY.clear()
    register_all()
    yield
    REGISTRY.clear()
    REGISTRY.update(previous)


def _load(path: Path) -> Rule:
    return Rule.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def _inch(value: int | Fraction, raw_text: str | None = None) -> Measurement:
    return Measurement(Fraction(value), Unit.INCH, raw_text)


def _operand(name: str, value: int | Fraction) -> VerdictOperand:
    return VerdictOperand(
        name=name,
        value=_inch(value, str(value)),
        status=EvidenceStatus.CORROBORATED,
        source="SHOP",
        evidence_ref=f"shop:p1:{name}",
    )


def _parameter(name: str, value: int | Fraction) -> ResolvedParameter:
    return ResolvedParameter(
        name=name,
        value=ParameterValue(
            value=Quantity(value=value, unit=Unit.INCH),
            provenance=Provenance.GC_CLIENT,
            set_by="project reviewer",
            set_at=WHEN,
        ),
        layer=ParameterLayer.PROJECT,
    )


def _trace_derivation(finding: object, name: str) -> dict[str, object]:
    trace = finding.trace  # type: ignore[attr-defined]
    assert trace is not None
    for intermediate_name, fields in trace.intermediates:
        if intermediate_name == name:
            return dict(cast(tuple[tuple[str, object], ...], fields))
    raise AssertionError(f"trace did not contain derivation {name!r}")


def test_depth_rule_is_an_exact_v1_flag_inch_check() -> None:
    """Input: depth-rule YAML. Outcome: one FLAG equality check with no tolerance."""
    rule = _load(DEPTH_RULE_PATH)

    assert rule.id == "CT-DEPTH-001"
    assert rule.severity is Severity.FLAG
    assert rule.arithmetic_unit is Unit.INCH
    assert rule.operation.type == "equals"
    assert rule.inputs["countertop_depth"].semantic_type is SemanticType.CT010
    assert tolerances_of(rule) == ()
    assert is_production_ready(rule)
    assert "field_cut" not in rule.parameters


def test_depth_passes_only_when_cabinet_and_overhang_sum_exactly() -> None:
    """Input: 24-inch cabinet + 1-inch overhang and CT010=25. Outcome: exact PASS."""
    finding = execute(
        publish(_load(DEPTH_RULE_PATH)),
        {"countertop_depth": _operand("countertop_depth", 25)},
        {
            "cabinet_depth": _parameter("cabinet_depth", 24),
            "countertop_overhang": _parameter("countertop_overhang", 1),
        },
    )

    assert finding.outcome is Outcome.PASS
    assert finding.trace is not None
    assert finding.trace.tolerance is None
    assert _trace_derivation(finding, "expected_depth")["result"] == _inch(25)


def test_any_depth_difference_fails_without_a_hidden_band() -> None:
    """Input: CT010 is 1/16 inch deeper than the exact sum. Outcome: FAIL, not tolerance."""
    finding = execute(
        publish(_load(DEPTH_RULE_PATH)),
        {"countertop_depth": _operand("countertop_depth", Fraction(401, 16))},
        {
            "cabinet_depth": _parameter("cabinet_depth", 24),
            "countertop_overhang": _parameter("countertop_overhang", 1),
        },
    )

    assert finding.outcome is Outcome.FAIL
    assert finding.trace is not None
    assert finding.trace.tolerance is None


def test_back_offset_is_derived_as_a_remainder_and_passes_at_the_minimum() -> None:
    """Input: 26 3/4 - 3/4 - 4 - 18 - 1 = 3 and minimum=3. Outcome: inclusive boundary PASS.

    **Five terms, not three.** `C_Tops_Checks_New.pptx` slide 8 decomposes the depth front-to-back
    as `CT010 = C.T_OH + CT007 + CT008 + CT009 + B.S_THK`, so the remainder has the overhang and the
    backsplash taken off it as well. This test used to supply a 25-inch depth against a three-term
    remainder; the same sink in the same countertop now needs a 26 3/4-inch depth to leave the same
    3 inches behind it, because the overhang and the backsplash were always occupying that inch and
    three quarters and the rule was crediting it to the back offset.
    """
    finding = execute(
        publish(_load(BACK_OFFSET_RULE_PATH)),
        {
            "countertop_depth": _operand("countertop_depth", Fraction(107, 4)),
            "front_offset": _operand("front_offset", 4),
            "sink_depth": _operand("sink_depth", 18),
        },
        {
            "back_offset_minimum": _parameter("back_offset_minimum", 3),
            "countertop_overhang": _parameter("countertop_overhang", Fraction(3, 4)),
            "backsplash_thickness": _parameter("backsplash_thickness", 1),
        },
    )

    assert finding.outcome is Outcome.PASS
    back_offset = _trace_derivation(finding, "back_offset")
    assert back_offset["result"] == _inch(3)
    # The four segments come off in one subtraction, so the trace shows what was taken and what was
    # left rather than three chained differences a reviewer would have to re-add.
    subtracted = _trace_derivation(finding, "segments_before_the_back_offset")
    assert subtracted["result"] == _inch(Fraction(95, 4))
    # The operand bindings, not just the arithmetic: four addends in the deck's own front-to-back
    # order, and the remainder taken off the depth rather than the depth off the remainder. A
    # result-only assertion would still hold if a segment were dropped and another double-counted,
    # or if `a` and `b` were swapped into a negative clearance that no minimum could catch.
    assert subtracted["inputs"] == (
        (
            "values",
            (
                _inch(Fraction(3, 4)),
                _inch(4, "4"),
                _inch(18, "18"),
                _inch(1),
            ),
        ),
    )
    assert back_offset["inputs"] == (
        ("a", _inch(Fraction(107, 4), "107/4")),
        ("b", _inch(Fraction(95, 4))),
    )


def test_back_offset_below_the_25_inch_default_flags() -> None:
    """A 2 7/16-inch remainder is below the default 2.5-inch vendor standard."""
    finding = execute(
        publish(_load(BACK_OFFSET_RULE_PATH)),
        {
            "countertop_depth": _operand("countertop_depth", Fraction(419, 16)),
            "front_offset": _operand("front_offset", 4),
            "sink_depth": _operand("sink_depth", 18),
        },
        {
            "back_offset_minimum": _parameter("back_offset_minimum", Fraction(5, 2)),
            "countertop_overhang": _parameter("countertop_overhang", Fraction(3, 4)),
            "backsplash_thickness": _parameter("backsplash_thickness", 1),
        },
    )

    assert finding.outcome is Outcome.FAIL


def test_the_backsplash_and_overhang_are_not_credited_to_the_back_offset() -> None:
    """**The bug the deck exposed, as a test.**

    Before the five-term decomposition this rule subtracted only the front offset and the sink, so
    the overhang and the backsplash stayed inside the remainder and the back offset came out larger
    than it is. Here that difference is exactly the margin: the same countertop passes with those
    two segments at zero and fails at their real sizes, so a rule that ignored them would report a
    sink set too far back as correctly placed — a wrong PASS on a critical check.
    """
    inputs = {
        "countertop_depth": _operand("countertop_depth", 25),
        "front_offset": _operand("front_offset", 4),
        "sink_depth": _operand("sink_depth", 18),
    }
    rule = publish(_load(BACK_OFFSET_RULE_PATH))

    as_if_ignored = execute(
        rule,
        inputs,
        {
            "back_offset_minimum": _parameter("back_offset_minimum", 3),
            "countertop_overhang": _parameter("countertop_overhang", 0),
            "backsplash_thickness": _parameter("backsplash_thickness", 0),
        },
    )
    as_measured = execute(
        rule,
        inputs,
        {
            "back_offset_minimum": _parameter("back_offset_minimum", 3),
            "countertop_overhang": _parameter("countertop_overhang", Fraction(3, 4)),
            "backsplash_thickness": _parameter("backsplash_thickness", 1),
        },
    )

    assert as_if_ignored.outcome is Outcome.PASS
    assert as_measured.outcome is Outcome.FAIL


def test_back_offset_default_is_25_inches_and_is_releasable() -> None:
    """Raj supplied a range; 2.5 inches is the false-PASS-safe V1 default."""
    rule = _load(BACK_OFFSET_RULE_PATH)

    default = rule.parameters["back_offset_minimum"].default
    assert default is not None
    assert default.exact_value == Fraction(5, 2)
    assert default.unit is Unit.INCH
    assert is_production_ready(rule)


def test_back_offset_may_be_overridden_to_the_vendor_floor_per_project() -> None:
    """The reviewer may set the supplied lower end, 2.375 inches, for a project."""
    finding = execute(
        publish(_load(BACK_OFFSET_RULE_PATH)),
        {
            "countertop_depth": _operand("countertop_depth", Fraction(419, 16)),
            "front_offset": _operand("front_offset", 4),
            "sink_depth": _operand("sink_depth", 18),
        },
        {
            "back_offset_minimum": _parameter("back_offset_minimum", Fraction(19, 8)),
            "countertop_overhang": _parameter("countertop_overhang", Fraction(3, 4)),
            "backsplash_thickness": _parameter("backsplash_thickness", 1),
        },
    )

    assert finding.outcome is Outcome.PASS


def test_offset_sum_is_not_authored_as_a_tautological_check() -> None:
    """Input: both CT-2 YAMLs. Outcome: no reconstructed offset-sum terminal comparison."""
    depth_rule = _load(DEPTH_RULE_PATH)
    back_rule = _load(BACK_OFFSET_RULE_PATH)

    assert depth_rule.operation.operands == {
        "actual": "countertop_depth",
        "expected": "expected_depth",
    }
    assert back_rule.operation.operands == {
        "x": "back_offset",
        "bound": "back_offset_minimum",
    }
    assert all(derivation.name != "offset_sum" for derivation in back_rule.derivations)


def test_only_the_confirmed_back_offset_standard_has_a_default() -> None:
    """The vendor supplied only the back-offset range; the two specified dimensions stay required.

    `B.S_THK` and `C.T_OH` are `Specified` in the deck's own acquisition column, and the deck gives
    no figure for those dimensions. Their absence must remain NOT_FOUND rather than quietly
    substituting a plausible joinery value.

    `CAB_SIDE_THK`, the other new `Specified` parameter, belongs to CT-4 and is covered in
    ``test_ct4_sink_cabinet_width.py``.
    """
    back_rule = _load(BACK_OFFSET_RULE_PATH)

    assert back_rule.parameters["backsplash_thickness"].default is None
    assert back_rule.parameters["countertop_overhang"].default is None
    assert back_rule.parameters["back_offset_minimum"].default == Quantity(
        value=Fraction(5, 2), unit=Unit.INCH
    )
