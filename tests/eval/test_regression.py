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

from dataclasses import dataclass
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
    compare_cases,
    gate_outcome,
)
from eval.release_metrics import Direction

#: One identity shared by comparable runs. Identity is the id, not the version string — two
#: unrelated sets can both be at "1.0".
GOLD_SET_ID = uuid4()


def _run(
    *,
    code: str = "abc123",
    snapshots: list[str] | None = None,
    extractors: dict[str, str] | None = None,
    gold_version: str = "1.0",
    gold_set_id: object = None,
) -> EvaluationRun:
    run = EvaluationRun(
        gold_set_id=gold_set_id or GOLD_SET_ID,
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
    """Identity is the id. Different sets hold different cases, so a difference between them says
    nothing about the code."""
    with pytest.raises(IncomparableRuns, match="different cases"):
        compare(_run(), _run(gold_set_id=uuid4()), {}, {})


def test_two_unrelated_sets_sharing_a_version_string_are_still_refused() -> None:
    """The bug this replaces: comparing on `gold_set_version` accepted two unrelated sets that both
    happened to be at "1.0"."""
    with pytest.raises(IncomparableRuns):
        compare(_run(gold_version="1.0"), _run(gold_set_id=uuid4(), gold_version="1.0"), {}, {})


def test_the_same_set_at_a_new_version_is_comparable_and_attributed() -> None:
    """`attribute()` has a branch for a changed gold-set version, and comparing on the version
    string made that branch unreachable — compare() refused the runs before attribution ran."""
    report = compare(
        _run(gold_version="1.0"),
        _run(gold_version="1.1"),
        {PRIMARY_METRIC: _metric(PRIMARY_METRIC, "1/100")},
        {PRIMARY_METRIC: _metric(PRIMARY_METRIC, "1/100")},
    )
    assert report.attributed_to == "gold set version"


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


# ---------------------------------------------------------------------------
# A metric with no declared direction is not judged by value
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("before", "after"), [("1/10", "9/10"), ("9/10", "1/10")])
def test_an_unconfigured_metric_is_never_called_better_or_worse(before: str, after: str) -> None:
    """Both directions, because assigning a default classified one of them as a regression on an
    invented rule — and the comment above that line claimed the metric could not be judged."""
    report = compare(
        _run(),
        _run(code="x"),
        {"experimental_thing": _metric("experimental_thing", before)},
        {"experimental_thing": _metric("experimental_thing", after)},
    )
    assert not report.worse
    assert not report.better
    assert [d.metric for d in report.unchanged] == ["experimental_thing"]


def test_an_unconfigured_metric_still_regresses_when_the_measurement_is_lost() -> None:
    """Direction-independent: losing the measurement is a regression whichever way it was meant to
    move."""
    report = compare(
        _run(),
        _run(code="x"),
        {"experimental_thing": _metric("experimental_thing", "1/2")},
        {"experimental_thing": _metric("experimental_thing", None)},
    )
    assert [d.metric for d in report.worse] == ["experimental_thing"]
    assert report.worse[0].direction is None


# ---------------------------------------------------------------------------
# Per-case deltas (#315) — a rate says something broke, the case says what
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Row:
    """Stands in for `app.models.CaseResult`, so this file needs no database."""

    gold_case_id: str
    check: str
    outcome: str
    expected: str


def test_a_case_that_started_failing_is_named() -> None:
    """The whole point. 1% to 4% on fifty cases is three cases, and which three is the difference
    between a morning's debugging and an afternoon's."""
    before = [_Row("case-7", "CT-1", "PASS", "PASS"), _Row("case-8", "CT-1", "PASS", "PASS")]
    after = [_Row("case-7", "CT-1", "FAIL", "PASS"), _Row("case-8", "CT-1", "PASS", "PASS")]

    worse, better, compared = compare_cases(before, after)
    assert compared
    assert not better
    assert [d.gold_case_id for d in worse] == ["case-7"]


def test_a_case_that_started_passing_is_named_too() -> None:
    before = [_Row("case-7", "CT-1", "FAIL", "PASS")]
    after = [_Row("case-7", "CT-1", "PASS", "PASS")]
    _, better, _ = compare_cases(before, after)
    assert [d.gold_case_id for d in better] == ["case-7"]


def test_a_change_between_two_wrong_answers_is_neither() -> None:
    """`NOT_FOUND` becoming `REVIEW_REQUIRED` is movement, and it is not a regression or a fix — the
    case was wrong before and is wrong now. Counting it either way would put noise in the one report
    somebody reads when a release is blocked."""
    before = [_Row("case-9", "CT-1", "NOT_FOUND", "PASS")]
    after = [_Row("case-9", "CT-1", "REVIEW_REQUIRED", "PASS")]
    worse, better, _ = compare_cases(before, after)
    assert not worse and not better


def test_no_case_rows_is_reported_as_not_compared_rather_than_as_no_change() -> None:
    """The distinction this turns on. Two empty tuples read exactly like "nothing changed", and a
    run recorded before #315 has no case rows at all — so absence has to be reported as absence, the
    same way `MetricResult.value` keeps `None` apart from zero."""
    worse, better, compared = compare_cases(None, [_Row("case-1", "CT-1", "PASS", "PASS")])
    assert not compared and not worse and not better


def test_an_empty_case_list_is_also_not_a_comparison() -> None:
    *_, compared = compare_cases([], [_Row("case-1", "CT-1", "PASS", "PASS")])
    assert not compared


def test_a_case_only_one_run_scored_is_not_a_delta() -> None:
    """Adding a gold case is not a regression, and removing one is a change to the answer key rather
    than to the system."""
    before = [_Row("case-1", "CT-1", "PASS", "PASS")]
    after = [_Row("case-1", "CT-1", "PASS", "PASS"), _Row("case-2", "CT-1", "FAIL", "PASS")]
    worse, better, compared = compare_cases(before, after)
    assert compared
    assert not worse and not better


def test_the_same_case_is_tracked_per_check() -> None:
    """A case carries several checks and a regression is usually one of them moving."""
    before = [_Row("case-1", "CT-1", "PASS", "PASS"), _Row("case-1", "CT-2", "PASS", "PASS")]
    after = [_Row("case-1", "CT-1", "FAIL", "PASS"), _Row("case-1", "CT-2", "PASS", "PASS")]
    worse, _, _ = compare_cases(before, after)
    assert [d.check for d in worse] == ["CT-1"]


def test_a_metric_change_and_its_per_case_deltas_agree() -> None:
    """#315's acceptance criterion: a rise in the false-PASS rate with no newly-failing case means
    one of the two is wrong. Here two of four cases regress, and the rate moves by two."""
    before = [_Row(f"case-{n}", "CT-1", "PASS", "PASS") for n in range(4)]
    after = [
        _Row("case-0", "CT-1", "FAIL", "PASS"),
        _Row("case-1", "CT-1", "FAIL", "PASS"),
        _Row("case-2", "CT-1", "PASS", "PASS"),
        _Row("case-3", "CT-1", "PASS", "PASS"),
    ]
    worse, _, _ = compare_cases(before, after)

    report = compare(
        _run(),
        _run(code="def456"),
        {"fail_recall": _metric("fail_recall", "4/4")},
        {"fail_recall": _metric("fail_recall", "2/4")},
        baseline_cases=before,
        candidate_cases=after,
    )
    moved = 4 - 2  # denominators match, so the numerator delta is a count of cases
    assert len(worse) == moved
    assert len(report.cases_worse) == moved
    assert report.worse[0].metric == "fail_recall"


def test_a_report_without_case_rows_says_it_did_not_compare_them() -> None:
    report = compare(
        _run(),
        _run(code="x"),
        {PRIMARY_METRIC: _metric(PRIMARY_METRIC, "1/100")},
        {PRIMARY_METRIC: _metric(PRIMARY_METRIC, "2/100")},
    )
    assert report.cases_compared is False
    assert report.cases_worse == ()
