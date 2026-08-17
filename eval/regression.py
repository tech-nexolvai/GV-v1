"""Compare a run against a baseline, and say what moved and why.

`AGENTS.md` §9 phrases the release gate as *"does not regress critical false-PASS"*. That is a
comparison, so it needs something to compare against — and until this module existed, `D6.2` (#238)
had nothing to consume and `publish()` took its regression check as a stub.

**Direction is not obvious and getting it backwards inverts the gate.** For `critical_false_pass_rate`
a rise is worse; for every accuracy and recall metric a fall is worse. That mapping already exists in
`eval/release_metrics.DIRECTIONS` and is imported rather than restated — two copies would eventually
disagree, and the copy that disagreed quietly would be the one deciding releases.

**Losing the ability to measure is not an improvement.** A metric that had a value in the baseline and
has none now reads, naively, as "no longer failing". It means nobody measured it. That is treated as a
regression here, because the alternative is a gate that gets greener as coverage disappears.

**Per-case deltas are not available.** `metric_results` stores one row per metric and check type; no
table records how an individual gold case fared. The `worse`/`better` lists are therefore per metric.
Per-case attribution needs a schema change and is tracked separately — inventing it here would mean
reporting cases we never stored.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Protocol

from app.models.evaluation import EvaluationRun
from eval.metrics import MetricResult as ComputedMetric
from eval.release_metrics import DIRECTIONS, GATE_BLOCKING, Direction

#: The metric the gate is named after. Its regression blocks publication outright (`D6.2`).
PRIMARY_METRIC = "critical_false_pass_rate"


class NoBaseline(Exception):
    """There is nothing to compare against.

    Raised rather than returning an empty report. *"Nothing regressed"* and *"nobody checked"* are
    different statements, and a publication gate that cannot tell them apart passes every change
    made before the first baseline exists — which is every change, for as long as nobody notices.
    """


class IncomparableRuns(Exception):
    """The two runs scored different gold sets.

    Identity is `gold_set_id`, not `gold_set_version`. Two unrelated sets can both be at version
    "1.0", and comparing those says nothing at all — whereas one set at 1.0 against the same set at
    1.1 is a meaningful comparison whose difference may well be the added cases, which is exactly
    what `attribute()` exists to point out.
    """


@dataclass(frozen=True, slots=True)
class MetricDelta:
    """One metric, before and after, and which way that counts."""

    metric: str
    check_type: str
    before: Fraction | None
    after: Fraction | None
    direction: Direction | None
    """None when the metric declares no direction in `release_metrics.DIRECTIONS`.

    A value change cannot then be called better or worse without inventing which way is good, and
    an invented direction is how a metric silently regresses in the flattering direction.
    """

    @property
    def measurement_lost(self) -> bool:
        """The baseline measured this and the new run did not."""
        return self.before is not None and self.after is None

    @property
    def measurement_gained(self) -> bool:
        return self.before is None and self.after is not None

    @property
    def regressed(self) -> bool:
        """Worse than the baseline, or no longer measurable.

        Losing a measurement counts. A gate that treats "we stopped measuring" as neutral gets
        greener as coverage is removed, which is the opposite of what it is for.
        """
        if self.measurement_lost:
            # Direction-independent: losing the measurement is a regression whichever way the
            # metric was supposed to move.
            return True
        if self.before is None or self.after is None or self.direction is None:
            return False
        return (
            self.after > self.before
            if self.direction is Direction.MAXIMUM
            else self.after < self.before
        )

    @property
    def improved(self) -> bool:
        """Better than the baseline. Gaining a measurement is not an improvement in the value —
        there was no value before — so it is reported separately rather than counted here."""
        if self.before is None or self.after is None or self.direction is None:
            return False
        return (
            self.after < self.before
            if self.direction is Direction.MAXIMUM
            else self.after > self.before
        )

    @property
    def blocking(self) -> bool:
        return self.metric in GATE_BLOCKING

    def __str__(self) -> str:
        def render(value: Fraction | None) -> str:
            return "not measured" if value is None else f"{float(value) * 100:.1f}%"

        arrow = "→"
        note = ""
        if self.measurement_lost:
            note = "  (measurement lost — not an improvement)"
        elif self.measurement_gained:
            note = "  (newly measurable)"
        return f"{self.metric} [{self.check_type}]: {render(self.before)} {arrow} {render(self.after)}{note}"


@dataclass(frozen=True, slots=True)
class CaseDelta:
    """One gold case whose outcome on one check changed between two runs.

    `expected` comes from the run that recorded it, not from the gold set as it stands now. The
    answer key is versioned, and a comparison has to mean what it meant when it was made.
    """

    gold_case_id: str
    check: str
    before: str
    after: str
    expected_before: str
    expected_after: str
    """The answer key **as each run recorded it**, kept separately.

    `compare()` accepts the same gold set at a new version, so the expectation can differ between the
    two runs. Judging both outcomes against the candidate's key would then misclassify: a case whose
    outcome never moved can go from correct to wrong purely because the annotation was corrected, and
    calling that a system regression sends somebody to debug code that did not change.
    """

    @property
    def was_correct(self) -> bool:
        return self.before == self.expected_before

    @property
    def is_correct(self) -> bool:
        return self.after == self.expected_after

    @property
    def newly_correct(self) -> bool:
        return self.is_correct and not self.was_correct

    @property
    def newly_wrong(self) -> bool:
        return self.was_correct and not self.is_correct

    @property
    def expectation_changed(self) -> bool:
        """The answer key moved under this case. Worth surfacing: it is a change to what "right"
        means, not to the system, and the two get confused."""
        return self.expected_before != self.expected_after

    def __str__(self) -> str:
        key = (
            f"expected {self.expected_after}"
            if not self.expectation_changed
            else f"expected {self.expected_before} -> {self.expected_after}"
        )
        return f"{self.gold_case_id[:8]}/{self.check}: {self.before} -> {self.after} ({key})"


@dataclass(frozen=True, slots=True)
class RegressionReport:
    """What moved between two runs, and what it means for publication."""

    worse: tuple[MetricDelta, ...]
    better: tuple[MetricDelta, ...]
    unchanged: tuple[MetricDelta, ...]
    attributed_to: str | None
    """The single version that differed, or None when several moved at once.

    None is the honest answer for a multi-version change: naming one of four differences as *the*
    cause would be a guess wearing the clothes of an attribution.
    """

    baseline_run_id: str
    candidate_run_id: str

    cases_worse: tuple[CaseDelta, ...] = ()
    cases_better: tuple[CaseDelta, ...] = ()
    cases_compared: bool = False
    """Whether per-case results existed on **both** runs.

    False is not "no cases changed" — it is "nobody looked", and the two must not read alike. A run
    recorded before `#315`, or by a caller scoring only aggregate metrics, has no case rows, and a
    report that showed an empty `cases_worse` for it would claim a clean comparison it never made.
    """

    @property
    def critical_false_pass_regressed(self) -> bool:
        return any(d.metric == PRIMARY_METRIC for d in self.worse)

    @property
    def blocks_publication(self) -> bool:
        """`D6.2` consumes this. Any blocking-gate metric getting worse stops a publish."""
        return any(d.blocking for d in self.worse)

    def summary(self) -> str:
        lines = [
            f"Regression report — {self.candidate_run_id[:8]} against {self.baseline_run_id[:8]}"
        ]
        lines.append(
            f"attributed to: {self.attributed_to or 'several versions changed at once — not attributable'}"
        )
        lines.append("")
        for label, deltas in (("worse", self.worse), ("better", self.better)):
            if deltas:
                lines.append(f"{label}:")
                lines.extend(f"  {d}" for d in deltas)
                lines.append("")
        if self.blocks_publication:
            blocking = ", ".join(d.metric for d in self.worse if d.blocking)
            lines.append(f"BLOCKS PUBLICATION — a release gate regressed: {blocking}.")
        elif not self.worse:
            lines.append("Nothing regressed.")
        else:
            lines.append("Regressions are outside the blocking gates; publication is not blocked.")
        return "\n".join(lines)


def attribute(baseline: EvaluationRun, candidate: EvaluationRun) -> str | None:
    """Name the single version that differs, or None when more than one does.

    Attribution is only meaningful when one thing changed. With four differences, calling any of
    them the cause is a guess — and a confident wrong attribution sends someone to investigate the
    wrong component, which costs more than saying nothing.
    """
    differences: list[str] = []
    if baseline.code_version != candidate.code_version:
        differences.append(f"code {baseline.code_version} → {candidate.code_version}")
    if sorted(baseline.rule_snapshot_ids) != sorted(candidate.rule_snapshot_ids):
        differences.append("rule snapshots")
    if baseline.extractor_versions != candidate.extractor_versions:
        changed = sorted(
            name
            for name in set(baseline.extractor_versions) | set(candidate.extractor_versions)
            if baseline.extractor_versions.get(name) != candidate.extractor_versions.get(name)
        )
        differences.append(f"extractor(s) {', '.join(changed)}")
    if baseline.gold_set_version != candidate.gold_set_version:
        differences.append("gold set version")

    return differences[0] if len(differences) == 1 else None


def compare(
    baseline: EvaluationRun | None,
    candidate: EvaluationRun,
    baseline_metrics: Mapping[str, ComputedMetric],
    candidate_metrics: Mapping[str, ComputedMetric],
    *,
    baseline_cases: Sequence[CaseResultRow] | None = None,
    candidate_cases: Sequence[CaseResultRow] | None = None,
    check_type: str = "all",
) -> RegressionReport:
    """Compare a candidate run against its baseline.

    `baseline` is typed optional so a caller can pass the result of `runs.baseline()` straight in and
    get the refusal rather than having to remember to check for None first — the check that gets
    forgotten is the one that has to be unforgettable.
    """
    if baseline is None:
        raise NoBaseline(
            "no baseline run exists for this gold-set version, so nothing can be compared. "
            "'Nothing regressed' and 'nobody checked' are different statements, and a publication "
            "gate that cannot tell them apart approves every change made before the first baseline."
        )

    if baseline.gold_set_id != candidate.gold_set_id:
        raise IncomparableRuns(
            f"baseline scored gold set {baseline.gold_set_id} and this run scored "
            f"{candidate.gold_set_id}. Different sets hold different cases, so a difference between "
            "them says nothing about the code. Note that two unrelated sets can share a version "
            "string, which is why identity is the id."
        )

    worse: list[MetricDelta] = []
    better: list[MetricDelta] = []
    unchanged: list[MetricDelta] = []

    for metric in sorted(set(baseline_metrics) | set(candidate_metrics)):
        # No default. Assigning one would classify an unconfigured metric by an invented rule, and
        # the comment claiming it "cannot be judged" would be contradicted by the line below it.
        # It still appears in the report, with its values visible, under `unchanged`.
        delta = MetricDelta(
            metric=metric,
            check_type=check_type,
            before=_value(baseline_metrics.get(metric)),
            after=_value(candidate_metrics.get(metric)),
            direction=DIRECTIONS.get(metric),
        )
        if delta.regressed:
            worse.append(delta)
        elif delta.improved:
            better.append(delta)
        else:
            unchanged.append(delta)

    cases_worse, cases_better, cases_compared = compare_cases(baseline_cases, candidate_cases)

    return RegressionReport(
        worse=tuple(worse),
        better=tuple(better),
        unchanged=tuple(unchanged),
        attributed_to=attribute(baseline, candidate),
        baseline_run_id=str(baseline.id),
        candidate_run_id=str(candidate.id),
        cases_worse=cases_worse,
        cases_better=cases_better,
        cases_compared=cases_compared,
    )


def _value(metric: ComputedMetric | None) -> Fraction | None:
    return metric.value if metric is not None else None


def gate_outcome(report: RegressionReport) -> tuple[bool, str]:
    """The (passed, summary) pair `D6.2` needs to allow or block a publish.

    Returned rather than raised: publication is `D6.3`'s decision, and this module's job is to
    report accurately, not to decide.
    """
    if report.blocks_publication:
        blocking = ", ".join(d.metric for d in report.worse if d.blocking)
        return False, f"release gate regressed: {blocking}"
    return True, (
        f"{len(report.better)} metric(s) improved, {len(report.worse)} regressed outside the "
        f"blocking gates, {len(report.unchanged)} unchanged"
    )


class CaseResultRow(Protocol):
    """The shape `compare_cases` needs, so it can be given `app.models.CaseResult` rows or plain
    records in a test without `eval/` importing the ORM.

    Read-only properties rather than plain attributes. A mutable attribute in a Protocol matches
    invariantly, so `Mapped[UUID]` on the ORM model would not satisfy a bare `gold_case_id: object`
    — and nothing here writes to these, so the weaker requirement is also the honest one.
    """

    @property
    def gold_case_id(self) -> object: ...

    @property
    def check(self) -> str: ...

    @property
    def outcome(self) -> str: ...

    @property
    def expected(self) -> str: ...


def compare_cases(
    baseline: Sequence[CaseResultRow] | None,
    candidate: Sequence[CaseResultRow] | None,
) -> tuple[tuple[CaseDelta, ...], tuple[CaseDelta, ...], bool]:
    """Which cases got better, which got worse, and whether the question was asked at all.

    The third value is the one that matters most. `#263` compares rates, and a rate that moved tells
    you something broke without telling you what; this names the cases. But a run with no case rows
    would otherwise produce two empty tuples that read exactly like "nothing changed" — so absence is
    reported as absence rather than as a clean result, the same distinction `MetricResult.value`
    draws between `None` and zero.

    A case present in one run and not the other is not a delta. Adding a gold case is not a
    regression, and removing one is a change to the answer key rather than to the system.
    """
    if baseline is None or candidate is None:
        return (), (), False

    def keyed(rows: Sequence[CaseResultRow]) -> dict[tuple[str, str], CaseResultRow]:
        return {(str(row.gold_case_id), row.check): row for row in rows}

    before, after = keyed(baseline), keyed(candidate)
    shared = before.keys() & after.keys()
    if not shared:
        # Not "nothing changed". Two runs with rows but no case in common have not been compared at
        # all — the same thing an empty input means, and reporting it as a clean result would be the
        # exact failure this function's third return value exists to prevent.
        return (), (), False

    worse: list[CaseDelta] = []
    better: list[CaseDelta] = []
    for key in sorted(shared):
        was, now = before[key], after[key]
        delta = CaseDelta(
            gold_case_id=key[0],
            check=key[1],
            before=was.outcome,
            after=now.outcome,
            expected_before=was.expected,
            expected_after=now.expected,
        )
        # Deliberately not `if was.outcome == now.outcome: continue`. An unchanged outcome can still
        # change from correct to wrong when the answer key moved beneath it, and skipping on the
        # outcome alone hides exactly those cases.
        if delta.newly_wrong:
            worse.append(delta)
        elif delta.newly_correct:
            better.append(delta)

    return tuple(worse), tuple(better), True
