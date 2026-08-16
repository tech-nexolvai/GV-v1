"""Comparing a run against a baseline (#263).

Three properties matter more than the arithmetic.

**Direction.** `critical_false_pass_rate` rising is worse; every accuracy metric falling is worse.
Getting that backwards inverts the release gate — a system producing more false PASSes would score
as improved.

**A refusal, not an empty report.** With no baseline there is nothing to compare, and "nothing
regressed" is not the same statement as "nobody checked".

**Losing a measurement is a regression.** A metric that had a value and now has none reads naively
as no-longer-failing. It means coverage disappeared, and a gate that rewards that gets greener as it
gets weaker.
"""

from __future__ import annotations

from fractions import Fraction
from uuid import uuid4

import pytest

from app.models.evaluation import EvaluationRun
from eval.metrics import MetricResult as ComputedMetric
from eval.regression import (
    PRIMARY_METRIC,
    IncomparableRuns,
    MetricDelta,
    NoBaseline,
    attribute,
    compare,
    gate_outcome,
)
from eval.release_metrics import Direction


def _run(
    *,
    code: str = "abc123",
    snapshots: list[str] | None = None,
    extractors: dict[str, str] | None = None,
    gold_version: str = "1.0",
) -> EvaluationRun:
    run = EvaluationRun(
        gold_set_id=uuid4(),
        gold_set_version=gold_version,
        code_version=code,
        rule_snapshot_ids=snapshots if snapshots is not None else ["snap-1"],
        extractor_versions=extractors if extractors is not None else {"pdfplumber": "0.11.0"},
        is_baseline=False,
    )
    return run


def _metric(key: str, value: str | None) -> ComputedMetric:
    if value is None:
        return ComputedMetric(key=key, value=None, numerator=0, denominator=0, note="no cases")
    frac = Fraction(value)
    return ComputedMetric(key, frac, frac.numerator, frac.denominator)


# ---------------------------------------------------------------------------
# Direction — inverting this inverts the gate
# ---------------------------------------------------------------------------


def test_a_rising_false_pass_rate_is_a_regression() -> None:
    report = compare(
        _run(),
        _run(code="def456"),
        {PRIMARY_METRIC: _metric(PRIMARY_METRIC, "1/100")},
        {PRIMARY_METRIC: _metric(PRIMARY_METRIC, "5/100")},
    )
    assert report.critical_false_pass_regressed
    assert report.blocks_publication


def test_a_falling_false_pass_rate_is_an_improvement() -> None:
    report = compare(
        _run(),
        _run(code="def456"),
        {PRIMARY_METRIC: _metric(PRIMARY_METRIC, "5/100")},
        {PRIMARY_METRIC: _metric(PRIMARY_METRIC, "1/100")},
    )
    assert not report.critical_false_pass_regressed
    assert [d.metric for d in report.better] == [PRIMARY_METRIC]


def test_a_falling_accuracy_is_a_regression() -> None:
    """The opposite direction from false-PASS, and the reason DIRECTIONS is imported rather than
    re-derived here."""
    report = compare(
        _run(),
        _run(code="def456"),
        {"numeric_exact_match_accuracy": _metric("numeric_exact_match_accuracy", "99/100")},
        {"numeric_exact_match_accuracy": _metric("numeric_exact_match_accuracy", "80/100")},
    )
    assert [d.metric for d in report.worse] == ["numeric_exact_match_accuracy"]


def test_improvements_are_reported_not_only_regressions() -> None:
    """A silent improvement is information too — it is how you learn a change helped."""
    report = compare(
        _run(),
        _run(code="def456"),
        {"fail_recall": _metric("fail_recall", "1/2")},
        {"fail_recall": _metric("fail_recall", "9/10")},
    )
    assert report.better and not report.worse


# ---------------------------------------------------------------------------
# Losing a measurement
# ---------------------------------------------------------------------------


def test_losing_a_measurement_counts_as_a_regression() -> None:
    """The subtle one. Naively this reads as "no longer failing"; it means nobody measured it, and a
    gate that rewards that gets greener as coverage is removed."""
    report = compare(
        _run(),
        _run(code="def456"),
        {PRIMARY_METRIC: _metric(PRIMARY_METRIC, "1/100")},
        {PRIMARY_METRIC: _metric(PRIMARY_METRIC, None)},
    )
    assert report.critical_false_pass_regressed
    assert report.worse[0].measurement_lost


def test_gaining_a_measurement_is_not_counted_as_an_improvement() -> None:
    """There was no value to improve on. It is reported, but not as a win — otherwise adding a gold
    case would look like the code got better."""
    report = compare(
        _run(),
        _run(code="def456"),
        {PRIMARY_METRIC: _metric(PRIMARY_METRIC, None)},
        {PRIMARY_METRIC: _metric(PRIMARY_METRIC, "1/100")},
    )
    assert not report.better
    assert not report.worse
    assert report.unchanged[0].measurement_gained


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_no_baseline_is_refused_rather_than_scored_as_a_pass() -> None:
    """ "Nothing regressed" and "nobody checked" are different statements, and a gate that cannot
    tell them apart approves every change made before the first baseline exists."""
    with pytest.raises(NoBaseline, match="nobody checked"):
        compare(None, _run(), {}, {})


def test_runs_against_different_gold_sets_are_refused() -> None:
    """The cases changed, so a difference says nothing about the code — and attributing a gold-set
    edit to whoever pushed next is worse than refusing."""
    with pytest.raises(IncomparableRuns, match="cases changed"):
        compare(_run(gold_version="1.0"), _run(gold_version="2.0"), {}, {})


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------


def test_a_single_changed_version_is_named() -> None:
    assert attribute(_run(code="aaa"), _run(code="bbb")) == "code aaa → bbb"


def test_a_changed_extractor_names_which_one() -> None:
    result = attribute(
        _run(extractors={"pdfplumber": "0.11.0", "paddleocr": "2.7"}),
        _run(extractors={"pdfplumber": "0.12.0", "paddleocr": "2.7"}),
    )
    assert result is not None and "pdfplumber" in result


def test_several_changes_at_once_attribute_to_nothing() -> None:
    """Naming one of four differences as the cause would be a guess wearing the clothes of an
    attribution, and it sends someone to investigate the wrong component."""
    assert attribute(_run(code="aaa"), _run(code="bbb", snapshots=["snap-2"])) is None


def test_identical_runs_attribute_to_nothing() -> None:
    assert attribute(_run(), _run()) is None


# ---------------------------------------------------------------------------
# What D6.2 consumes
# ---------------------------------------------------------------------------


def test_a_blocking_gate_regression_blocks_publication() -> None:
    report = compare(
        _run(),
        _run(code="x"),
        {"identifier_match_precision": _metric("identifier_match_precision", "99/100")},
        {"identifier_match_precision": _metric("identifier_match_precision", "50/100")},
    )
    passed, reason = gate_outcome(report)
    assert not passed
    assert "identifier_match_precision" in reason


def test_a_non_blocking_regression_does_not_block_publication() -> None:
    """`reviewer_minutes` getting worse is worth knowing and is not a safety gate."""
    report = compare(
        _run(),
        _run(code="x"),
        {"reviewer_minutes": _metric("reviewer_minutes", "1/10")},
        {"reviewer_minutes": _metric("reviewer_minutes", "5/10")},
    )
    assert report.worse
    assert not report.blocks_publication
    assert gate_outcome(report)[0]


def test_the_summary_says_what_it_could_not_attribute() -> None:
    report = compare(
        _run(),
        _run(code="x", snapshots=["s2"]),
        {PRIMARY_METRIC: _metric(PRIMARY_METRIC, "1/100")},
        {PRIMARY_METRIC: _metric(PRIMARY_METRIC, "2/100")},
    )
    assert "not attributable" in report.summary()
    assert "BLOCKS PUBLICATION" in report.summary()


def test_exact_fractions_survive_the_comparison() -> None:
    """Comparisons are on exact rationals. A rounded 1/3 would make two identical runs differ."""
    delta = MetricDelta(
        metric=PRIMARY_METRIC,
        check_type="all",
        before=Fraction(1, 3),
        after=Fraction(1, 3),
        direction=Direction.MAXIMUM,
    )
    assert not delta.regressed and not delta.improved
