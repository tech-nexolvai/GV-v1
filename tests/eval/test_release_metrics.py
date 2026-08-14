"""The report must refuse to flatter.

`AGENTS.md` §9 orders the metrics and says coverage may only be optimised once the safety gates
pass. The refusal test below is the story — everything else here supports it.

The reason it has to be structural: automation coverage improves whenever the system abstains less,
which is also how it becomes less safe. A report showing coverage climbing beside an unmet false-PASS
gate would be accurate and would mislead the person reading it.
"""

from __future__ import annotations

from fractions import Fraction

import pytest

from eval.metrics import METRIC_ORDER, MetricResult
from eval.release_metrics import (
    DIRECTIONS,
    GATE_BLOCKING,
    OPTIMISATION,
    Direction,
    GateOutcome,
    MetricsOutOfOrder,
    blocking_failures,
    evaluate,
    render,
    ships,
)


def _result(key: str, value: str | None) -> MetricResult:
    if value is None:
        return MetricResult(key=key, value=None, numerator=0, denominator=0, note="no cases")
    frac = Fraction(value)
    return MetricResult(key=key, value=frac, numerator=frac.numerator, denominator=frac.denominator)


def _all_good() -> tuple[dict[str, MetricResult], dict[str, Fraction]]:
    """Every gate passing: a perfect false-PASS rate of zero, everything else at 1."""
    results = {
        key: _result(key, "0" if DIRECTIONS[key] is Direction.MAXIMUM else "1")
        for key in METRIC_ORDER
    }
    thresholds = {
        key: Fraction(0) if DIRECTIONS[key] is Direction.MAXIMUM else Fraction(1)
        for key in METRIC_ORDER
    }
    return results, thresholds


# ---------------------------------------------------------------------------
# The refusal — this is the story
# ---------------------------------------------------------------------------


def test_coverage_is_refused_while_a_safety_gate_fails() -> None:
    """The one that matters. A failing false-PASS gate makes the coverage number unreportable."""
    results, thresholds = _all_good()
    results["critical_false_pass_rate"] = _result("critical_false_pass_rate", "1/10")
    gated = evaluate(results, thresholds)

    with pytest.raises(MetricsOutOfOrder, match="critical_false_pass_rate"):
        render(gated, include_optimisation=True)


def test_the_refusal_explains_why_rather_than_just_refusing() -> None:
    results, thresholds = _all_good()
    results["critical_false_pass_rate"] = _result("critical_false_pass_rate", "1/10")
    with pytest.raises(MetricsOutOfOrder) as err:
        render(evaluate(results, thresholds), include_optimisation=True)
    assert "abstains less" in str(err.value)
    assert "AGENTS.md §9" in str(err.value)


def test_coverage_is_permitted_once_every_gate_passes() -> None:
    results, thresholds = _all_good()
    text = render(evaluate(results, thresholds), include_optimisation=True)
    assert "automation_coverage" in text
    assert "All blocking gates passed" in text


def test_the_ungated_report_omits_optimisation_metrics_entirely() -> None:
    """Not merely unhighlighted — absent. A number on the page is a number that gets quoted."""
    results, thresholds = _all_good()
    results["critical_false_pass_rate"] = _result("critical_false_pass_rate", "1/10")
    text = render(evaluate(results, thresholds))
    for key in OPTIMISATION:
        assert key not in text


# ---------------------------------------------------------------------------
# Unmeasured is not passed
# ---------------------------------------------------------------------------


def test_an_unmeasured_gate_blocks_exactly_like_a_failing_one() -> None:
    """The most consequential line in the module. Today every metric is unmeasured because there
    is no gold set; treating that as a pass would ship on numbers nobody computed."""
    results, thresholds = _all_good()
    results["critical_false_pass_rate"] = _result("critical_false_pass_rate", None)
    gated = evaluate(results, thresholds)

    assert not ships(gated)
    assert "critical_false_pass_rate" in [g.key for g in blocking_failures(gated)]


def test_a_metric_with_no_declared_threshold_is_not_measured() -> None:
    """Declining to declare a limit is not the same as clearing one. A gate with no threshold is
    not a gate."""
    results, thresholds = _all_good()
    del thresholds["identifier_match_precision"]
    gated = {g.key: g for g in evaluate(results, thresholds)}
    assert gated["identifier_match_precision"].outcome is GateOutcome.NOT_MEASURED
    assert not ships(evaluate(results, thresholds))


def test_the_report_says_how_many_gates_were_never_measured() -> None:
    results, thresholds = _all_good()
    results["critical_false_pass_rate"] = _result("critical_false_pass_rate", None)
    text = render(evaluate(results, thresholds))
    assert "An unmeasured gate is not a passed one" in text


def test_everything_unmeasured_is_the_projects_state_today() -> None:
    """With no gold set, nothing is measurable and nothing may be optimised. The report should say
    so plainly rather than rendering ten blanks."""
    results = {key: _result(key, None) for key in METRIC_ORDER}
    gated = evaluate(results, {})
    assert not ships(gated)
    assert len(blocking_failures(gated)) == len(GATE_BLOCKING)
    assert "NOT RELEASABLE" in render(gated)


# ---------------------------------------------------------------------------
# Direction — false-PASS is the only one where lower is better
# ---------------------------------------------------------------------------


def test_false_pass_is_a_ceiling_not_a_floor() -> None:
    """Inverting this one metric would turn the primary safety gate into its opposite: a system
    producing more false PASSes would score better."""
    assert DIRECTIONS["critical_false_pass_rate"] is Direction.MAXIMUM

    results, thresholds = _all_good()
    thresholds["critical_false_pass_rate"] = Fraction(1, 100)
    results["critical_false_pass_rate"] = _result("critical_false_pass_rate", "1/2")
    gated = {g.key: g for g in evaluate(results, thresholds)}
    assert gated["critical_false_pass_rate"].outcome is GateOutcome.FAILED


def test_accuracy_metrics_are_floors() -> None:
    results, thresholds = _all_good()
    thresholds["numeric_exact_match_accuracy"] = Fraction(99, 100)
    results["numeric_exact_match_accuracy"] = _result("numeric_exact_match_accuracy", "1/2")
    gated = {g.key: g for g in evaluate(results, thresholds)}
    assert gated["numeric_exact_match_accuracy"].outcome is GateOutcome.FAILED


def test_every_metric_declares_a_direction() -> None:
    """Enforced at import too. A new metric defaulting to 'higher is better' would be wrong for
    exactly the metric that matters most."""
    assert set(DIRECTIONS) >= set(METRIC_ORDER)


# ---------------------------------------------------------------------------
# Ordering and completeness
# ---------------------------------------------------------------------------


def test_critical_false_pass_is_reported_first_and_cannot_be_dropped() -> None:
    results, thresholds = _all_good()
    text = render(evaluate(results, thresholds))
    positions = [text.index(g) for g in GATE_BLOCKING]
    assert positions == sorted(positions)
    assert text.index("critical_false_pass_rate") == min(positions)


def test_the_blocking_set_is_derived_from_the_order_not_re_listed() -> None:
    """If the two were maintained separately they would drift, and the drift would be silent."""
    assert GATE_BLOCKING == METRIC_ORDER[:5]


def test_a_metric_missing_from_the_results_is_reported_not_skipped() -> None:
    """A metric that silently disappears from a report reads as one that was not needed."""
    gated = evaluate({}, {})
    assert tuple(g.key for g in gated) == METRIC_ORDER
    assert all(g.outcome is GateOutcome.NOT_MEASURED for g in gated)


def test_only_the_five_blocking_gates_appear_under_the_blocking_heading() -> None:
    """A report listing eight metrics under "Blocking gates" overstates what holds a release — the
    same class of misleading presentation this module refuses elsewhere."""
    results, thresholds = _all_good()
    text = render(evaluate(results, thresholds))
    blocking_section = text.split("Blocking gates")[1].split("Diagnostic")[0]
    for key in GATE_BLOCKING:
        assert key in blocking_section
    assert "fail_recall" not in blocking_section
    assert "abstention_recall" not in blocking_section


def test_diagnostic_metrics_are_labelled_as_not_blocking() -> None:
    results, thresholds = _all_good()
    text = render(evaluate(results, thresholds))
    assert "Diagnostic — informative, not blocking:" in text
    assert "fail_recall" in text.split("Diagnostic")[1]
