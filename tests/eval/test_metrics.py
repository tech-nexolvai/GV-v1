"""Release metrics — every expected value hand-computed, per #69's acceptance criteria.

The tests that matter most here are not the arithmetic ones. They are the ones asserting that an
unmeasured metric stays `None`: a critical false-PASS rate of `0` over zero critical cases renders
as a perfect score, and a release decision gets made from that number.
"""

from __future__ import annotations

from fractions import Fraction

import pytest

from eval.gold_set.schema import ExpectedFinding, GoldMatch, GoldObservation
from eval.metrics import (
    METRIC_ORDER,
    MetricResult,
    abstention_recall,
    automation_coverage,
    compute_all,
    critical_false_pass_rate,
    evidence_localisation_rate,
    fail_recall,
    gate_inputs,
    identifier_match_precision,
    numeric_exact_match_accuracy,
    report,
    unit_exact_match_accuracy,
)
from rules.semantic_types import OperandSource, SemanticType
from units.measurement import Measurement, Unit
from verdict.finding import Finding
from verdict.outcomes import Outcome, Severity, is_decision
from verdict.trace import CalculationTrace, TracedOperand


def _trace(outcome: Outcome) -> CalculationTrace:
    """A decision must carry a trace — `Finding` refuses PASS or FAIL without one, because a
    verdict a reviewer cannot check by hand is not defensible. Abstentions may omit it."""
    return CalculationTrace(
        operation="equals",
        operands=(TracedOperand(name="x", value=Fraction(1), source="SHOP", evidence_ref=None),),
        intermediates=(),
        comparison="1 == 1",
        tolerance=None,
        arithmetic_unit=Unit.MM,
        outcome=outcome,
        engine_version="test",
        operation_version="1.0.0",
    )


def _finding(rule_id: str, outcome: Outcome, severity: Severity = Severity.CRITICAL) -> Finding:
    return Finding(
        rule_id=rule_id,
        outcome=outcome,
        severity=severity,
        reason="test",
        snapshot_id="snap-1",
        engine_version="test",
        trace=_trace(outcome) if is_decision(outcome) else None,
    )


def _expected(check: str, outcome: Outcome) -> ExpectedFinding:
    return ExpectedFinding(check=check, outcome=outcome, reason=f"Expected {outcome.value} in test")


def _obs(
    item: str,
    value: str = "984",
    unit: Unit = Unit.MM,
    page: int = 1,
    polygon: tuple[int, int, int, int] = (0, 0, 10, 10),
) -> GoldObservation:
    return GoldObservation(
        semantic_type=SemanticType.CABINET_WIDTH,
        source=OperandSource.SHOP,
        value=Measurement(Fraction(value), unit, value),
        page=page,
        polygon=polygon,
        item_id=item,
    )


# ---------------------------------------------------------------------------
# The primary metric
# ---------------------------------------------------------------------------


def test_critical_false_pass_counts_only_critical_checks_that_should_fail() -> None:
    """Hand-computed: 3 critical checks expect FAIL, 1 wrongly passed -> 1/3."""
    gold = [
        _expected("R1", Outcome.FAIL),
        _expected("R2", Outcome.FAIL),
        _expected("R3", Outcome.FAIL),
        _expected("R4", Outcome.PASS),
    ]
    predicted = [
        _finding("R1", Outcome.PASS),  # false PASS
        _finding("R2", Outcome.FAIL),
        _finding("R3", Outcome.REVIEW_REQUIRED),  # safe, not a false PASS
        _finding("R4", Outcome.PASS),  # correct, and not in the denominator
    ]
    result = critical_false_pass_rate(predicted, gold)
    assert result.value == Fraction(1, 3)
    assert (result.numerator, result.denominator) == (1, 3)


def test_a_non_critical_false_pass_is_not_counted_here() -> None:
    """It is still a defect, measured elsewhere. This metric is about what could be manufactured."""
    gold = [_expected("R1", Outcome.FAIL)]
    predicted = [_finding("R1", Outcome.PASS, severity=Severity.ADVISORY)]
    assert critical_false_pass_rate(predicted, gold).value is None


def test_an_abstention_on_a_defect_is_not_a_false_pass() -> None:
    gold = [_expected("R1", Outcome.FAIL)]
    for outcome in (Outcome.REVIEW_REQUIRED, Outcome.NOT_FOUND, Outcome.NO_APPLICABLE_RULE):
        assert critical_false_pass_rate([_finding("R1", outcome)], gold).value == Fraction(0, 1)


def test_no_critical_cases_is_not_measured_rather_than_perfect() -> None:
    """The most important assertion in this file.

    Zero over zero is not a perfect score. Before Q4 (#12) assigns severities this is the state the
    real gold set will be in, and a `0` here would sail through a `maximum` threshold.
    """
    result = critical_false_pass_rate([], [])
    assert result.value is None
    assert not result.measured
    assert "Q4" in result.note


# ---------------------------------------------------------------------------
# Localisation, reading accuracy
# ---------------------------------------------------------------------------


def test_right_page_wrong_polygon_is_not_localised() -> None:
    """A reviewer sent to the wrong part of the right page still cannot verify at a glance."""
    gold = [_obs("cab-1", page=2, polygon=(0, 0, 10, 10))]
    predicted = [_obs("cab-1", page=2, polygon=(50, 50, 60, 60))]
    assert evidence_localisation_rate(predicted, gold).value == Fraction(0, 1)


def test_localisation_requires_both_page_and_polygon() -> None:
    gold = [_obs("a", page=1), _obs("b", page=2)]
    predicted = [_obs("a", page=1), _obs("b", page=9)]  # b on the wrong page
    assert evidence_localisation_rate(predicted, gold).value == Fraction(1, 2)


def test_numeric_accuracy_is_exact_with_no_tolerance() -> None:
    """Tolerance belongs to a rule, applied to a value already read correctly. Slack here would
    hide extraction error inside rule tolerance."""
    gold = [_obs("a", value="984")]
    predicted = [_obs("a", value="985")]
    assert numeric_exact_match_accuracy(predicted, gold).value == Fraction(0, 1)


def test_numeric_accuracy_counts_an_exact_match() -> None:
    gold = [_obs("a", value="984"), _obs("b", value="600")]
    predicted = [_obs("a", value="984"), _obs("b", value="601")]
    assert numeric_exact_match_accuracy(predicted, gold).value == Fraction(1, 2)


def test_unit_accuracy_is_separate_from_numeric() -> None:
    """Same number, wrong unit: 984 mm read as 984 inches. The arithmetic is flawless and the
    answer is wrong by a factor of twenty-five."""
    gold = [_obs("a", value="984", unit=Unit.MM)]
    predicted = [_obs("a", value="984", unit=Unit.INCH)]
    assert numeric_exact_match_accuracy(predicted, gold).value == Fraction(1, 1)
    assert unit_exact_match_accuracy(predicted, gold).value == Fraction(0, 1)


# ---------------------------------------------------------------------------
# Matching, recall, coverage
# ---------------------------------------------------------------------------


def test_match_precision_is_over_what_was_proposed() -> None:
    """Hand-computed: 3 proposed, 2 correct -> 2/3."""
    gold = [GoldMatch(arch_item="A1", shop_item="S1"), GoldMatch(arch_item="A2", shop_item="S2")]
    predicted = [
        GoldMatch(arch_item="A1", shop_item="S1"),
        GoldMatch(arch_item="A2", shop_item="S2"),
        GoldMatch(arch_item="A3", shop_item="S9"),
    ]
    assert identifier_match_precision(predicted, gold).value == Fraction(2, 3)


def test_fail_recall_does_not_credit_an_abstention() -> None:
    """REVIEW REQUIRED on a defect is safe but it is not the system finding it."""
    gold = [_expected("R1", Outcome.FAIL), _expected("R2", Outcome.FAIL)]
    predicted = [_finding("R1", Outcome.FAIL), _finding("R2", Outcome.REVIEW_REQUIRED)]
    assert fail_recall(predicted, gold).value == Fraction(1, 2)


def test_abstention_recall_measures_declining_to_decide() -> None:
    gold = [_expected("R1", Outcome.REVIEW_REQUIRED), _expected("R2", Outcome.NOT_FOUND)]
    predicted = [_finding("R1", Outcome.REVIEW_REQUIRED), _finding("R2", Outcome.PASS)]
    assert abstention_recall(predicted, gold).value == Fraction(1, 2)


def test_coverage_rises_when_the_system_abstains_less() -> None:
    """Demonstrates why coverage is ranked last: this is an improvement in the number and a
    regression in the behaviour."""
    cautious = [_finding("R1", Outcome.PASS), _finding("R2", Outcome.REVIEW_REQUIRED)]
    reckless = [_finding("R1", Outcome.PASS), _finding("R2", Outcome.PASS)]
    assert automation_coverage(cautious).value == Fraction(1, 2)
    assert automation_coverage(reckless).value == Fraction(1, 1)


# ---------------------------------------------------------------------------
# Exactness
# ---------------------------------------------------------------------------


def test_every_value_is_an_exact_fraction() -> None:
    """`release_gates._exact` rejects floats outright, so a float metric would be silently dropped
    and its gate would report NOT EVALUATED."""
    gold = [
        _expected("R1", Outcome.FAIL),
        _expected("R2", Outcome.FAIL),
        _expected("R3", Outcome.FAIL),
    ]
    predicted = [
        _finding("R1", Outcome.PASS),
        _finding("R2", Outcome.FAIL),
        _finding("R3", Outcome.FAIL),
    ]
    value = critical_false_pass_rate(predicted, gold).value
    assert isinstance(value, Fraction)
    assert not isinstance(value, float)
    assert value == Fraction(1, 3)  # exact; 0.333... would not compare equal


# ---------------------------------------------------------------------------
# The report and the gate handoff
# ---------------------------------------------------------------------------


def test_compute_all_returns_every_metric_including_unmeasurable_ones() -> None:
    results = compute_all([], [])
    assert set(results) == set(METRIC_ORDER)


@pytest.mark.parametrize("key", ["reviewer_correction_rate", "reviewer_minutes"])
def test_review_derived_metrics_say_what_they_need(key: str) -> None:
    """Returned as unmeasured rather than omitted. A metric that silently disappears from a report
    reads as one that was not needed."""
    result = compute_all([], [])[key]
    assert result.value is None
    assert "D5.4" in result.note or "C1.10" in result.note


def test_gate_inputs_omits_unmeasured_metrics_rather_than_zeroing_them() -> None:
    """The trap this avoids: `0` for an unmeasured false-PASS rate passes a `maximum` threshold and
    reports a green gate for something nobody measured."""
    results = compute_all([], [])
    assert "critical_false_pass_rate" not in gate_inputs(results)


def test_gate_inputs_passes_exact_values_through() -> None:
    findings = [_finding("R1", Outcome.PASS), _finding("R2", Outcome.FAIL)]
    values = gate_inputs(compute_all(findings, [_expected("R1", Outcome.FAIL)]))
    assert values["critical_false_pass_rate"] == Fraction(1, 1)
    assert all(isinstance(v, Fraction) for v in values.values())


def test_the_report_lists_all_nine_in_priority_order() -> None:
    text = report(compute_all([], []))
    positions = [text.index(key) for key in METRIC_ORDER]
    assert positions == sorted(positions)
    assert text.index("critical_false_pass_rate") < text.index("automation_coverage")


def test_the_report_says_when_a_metric_was_not_measured() -> None:
    text = report(compute_all([], []))
    assert "NOT MEASURED" in text
    assert "An unmeasured metric is not a passing one" in text


def test_a_metric_result_renders_readably() -> None:
    assert "50.0%" in str(MetricResult("k", Fraction(1, 2), 1, 2))
    assert "NOT MEASURED" in str(MetricResult("k", None, 0, 0, note="nothing to measure"))
