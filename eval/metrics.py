"""The release metrics, in the order `AGENTS.md` §9 requires them to be read.

Critical false-PASS is first because it is the only metric that measures harm. Everything else
measures how useful the system is; this one measures how often it is confidently wrong about
something that could be manufactured.

`eval/release_gates.py` consumes these by key and rejects anything that is not an exact number, so
the interface here is fixed by its consumer rather than chosen. See `docs/DESIGN.md` §3.15.

**The distinction this module is built around:** a metric that cannot be computed returns `None`,
never `0` and never `1`. A critical false-PASS rate of zero over zero critical cases renders as a
perfect score and means nobody measured anything. Those two must not look alike on a report someone
uses to decide whether to ship.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction

from eval.gold_set.schema import ExpectedFinding, GoldMatch, GoldObservation
from verdict.finding import Finding
from verdict.outcomes import Outcome, Severity

#: Outcomes where the system declined to decide. Counted as abstentions, never as failures: the
#: whole safety argument is that abstaining is the correct move under uncertainty, so scoring it
#: as a miss would penalise exactly the behaviour we want.
ABSTENTIONS: frozenset[Outcome] = frozenset(
    {Outcome.NOT_FOUND, Outcome.REVIEW_REQUIRED, Outcome.NO_APPLICABLE_RULE}
)

#: Metrics that describe what human reviewers did, which no gold set can answer. They need
#: production records: the correction ledger (D5.4) and review sessions (C1.10).
REVIEW_DERIVED: frozenset[str] = frozenset({"reviewer_correction_rate", "reviewer_minutes"})


@dataclass(frozen=True, slots=True)
class MetricResult:
    """One measured metric, or an explicit statement that it was not measured.

    `value` is `None` rather than `0` when the denominator is empty. That is the whole point of the
    type: an unmeasured metric and a perfect one must be distinguishable at a glance, because a
    release decision is made from this.
    """

    key: str
    value: Fraction | None
    numerator: int
    denominator: int
    note: str = ""

    @property
    def measured(self) -> bool:
        """True when there was anything to measure at all."""
        return self.value is not None

    def __str__(self) -> str:
        if self.value is None:
            return f"{self.key}: NOT MEASURED — {self.note or 'no cases'}"
        percent = float(self.value) * 100
        return f"{self.key}: {percent:.1f}% ({self.numerator}/{self.denominator})"


def _rate(key: str, numerator: int, denominator: int, *, empty_note: str) -> MetricResult:
    """Build a result, refusing to invent a value when nothing was measured."""
    if denominator == 0:
        return MetricResult(key=key, value=None, numerator=0, denominator=0, note=empty_note)
    return MetricResult(
        key=key,
        value=Fraction(numerator, denominator),
        numerator=numerator,
        denominator=denominator,
    )


def _expected_by_check(gold: Sequence[ExpectedFinding]) -> Mapping[str, Outcome]:
    return {expected.check: expected.outcome for expected in gold}


# ---------------------------------------------------------------------------
# 1. The primary metric
# ---------------------------------------------------------------------------


def critical_false_pass_rate(
    predicted: Sequence[Finding], gold: Sequence[ExpectedFinding]
) -> MetricResult:
    """How often a CRITICAL check said PASS when the reviewed answer was FAIL.

    The denominator is critical checks the gold set says should FAIL — not all checks, and not all
    critical checks. Widening it would dilute the rate with cases that could never have been a false
    PASS, and make the number look better by adding work the system got right for free.

    Until `Q4` (#12) assigns severities, no rule declares CRITICAL and this reports NOT MEASURED.
    That is correct: nothing has been measured.
    """
    expected = _expected_by_check(gold)
    at_risk = 0
    wrong = 0
    for finding in predicted:
        if finding.severity is not Severity.CRITICAL:
            continue
        if expected.get(finding.rule_id) is not Outcome.FAIL:
            continue
        at_risk += 1
        if finding.outcome is Outcome.PASS:
            wrong += 1
    return _rate(
        "critical_false_pass_rate",
        wrong,
        at_risk,
        empty_note=(
            "no CRITICAL check in the gold set expects FAIL. Either no rule declares a severity "
            "yet (Q4 #12 is unanswered) or the gold set contains no critical defect to catch"
        ),
    )


# ---------------------------------------------------------------------------
# 2. Evidence localisation
# ---------------------------------------------------------------------------


def evidence_localisation_rate(
    predicted: Sequence[GoldObservation], gold: Sequence[GoldObservation]
) -> MetricResult:
    """How often a reading was placed on the right page **and** the right region.

    Both must be right. A correct page with the wrong polygon is not a partial success — a reviewer
    sent to the wrong part of the right page still cannot verify the finding at a glance, which is
    the entire purpose of localising it (`AGENTS.md` §9).

    Delegates to `eval/localisation.py` (#172), which reports page and box separately and judges the
    box by intersection-over-union. The original version here required the predicted polygon to
    equal the annotation exactly — unusably strict, since no extraction lane reproduces a hand-drawn
    box to the pixel, so it would have read zero forever and told nobody anything.

    An unannotated prediction makes this NOT MEASURED rather than lowering the score: the gate is
    unmeasurable, not failed, and those are different facts.
    """
    from eval.localisation import DEFAULT_IOU_THRESHOLD, MissingAnnotation, measure

    if not gold:
        return _rate(
            "evidence_localisation_rate",
            0,
            0,
            empty_note="the gold set contains no annotated observations",
        )

    try:
        results = measure(predicted, gold, threshold=DEFAULT_IOU_THRESHOLD)
    except MissingAnnotation as error:
        return MetricResult(
            key="evidence_localisation_rate",
            value=None,
            numerator=0,
            denominator=0,
            note=str(error),
        )

    result = results["all"]
    # The joint count, not min(page, box): those can count different observations, reporting half
    # the set localised when none of it is.
    located = result.both_correct
    return _rate(
        "evidence_localisation_rate",
        located,
        result.compared,
        # Not the same sentence as the empty-gold case above. Reaching here with nothing compared
        # means the annotations exist and nothing was predicted against them — the extraction lane
        # produced no observations at all. Saying "no annotated observations" would describe the
        # wrong failure and send someone to fix the gold set instead of the extractor.
        empty_note=(
            "no observations were predicted, so there was nothing to localise against "
            f"{len(gold)} annotated observation(s)"
        ),
    )


# ---------------------------------------------------------------------------
# 3-4. Reading accuracy
# ---------------------------------------------------------------------------


def numeric_exact_match_accuracy(
    predicted: Sequence[GoldObservation], gold: Sequence[GoldObservation]
) -> MetricResult:
    """How often the number read equals the number annotated, exactly.

    Exactly means exactly. There is no tolerance here — tolerance is a property of a *rule*, applied
    to a value that was already read correctly. Allowing slack in the reading would hide extraction
    error inside rule tolerance, and the two failures need to stay separable.
    """
    truth = {(obs.item_id, obs.semantic_type): obs for obs in gold}
    correct = 0
    for observation in predicted:
        reference = truth.get((observation.item_id, observation.semantic_type))
        if reference is not None and observation.value.exact == reference.value.exact:
            correct += 1
    return _rate(
        "numeric_exact_match_accuracy",
        correct,
        len(truth),
        empty_note="the gold set contains no annotated observations",
    )


def unit_exact_match_accuracy(
    predicted: Sequence[GoldObservation], gold: Sequence[GoldObservation]
) -> MetricResult:
    """How often the unit read matches the unit annotated.

    Tracked separately from the number because the failures are different. A wrong number is usually
    a misread digit; a wrong unit turns 984 mm into 984 inches and produces a verdict that is wrong
    by a factor of twenty-five with no arithmetic error anywhere.
    """
    truth = {(obs.item_id, obs.semantic_type): obs for obs in gold}
    correct = 0
    for observation in predicted:
        reference = truth.get((observation.item_id, observation.semantic_type))
        if reference is not None and observation.value.unit is reference.value.unit:
            correct += 1
    return _rate(
        "unit_exact_match_accuracy",
        correct,
        len(truth),
        empty_note="the gold set contains no annotated observations",
    )


# ---------------------------------------------------------------------------
# 5. Matching
# ---------------------------------------------------------------------------


def identifier_match_precision(
    predicted: Sequence[GoldMatch], gold: Sequence[GoldMatch]
) -> MetricResult:
    """Of the arch-to-shop matches proposed, how many were right.

    Precision, not recall, and deliberately: a wrong match silently compares two different items and
    produces a confident verdict about neither. A missed match produces NOT FOUND, which a reviewer
    sees. Precision measures the failure that hides.
    """
    truth = {(match.arch_item, match.shop_item) for match in gold}
    correct = sum(1 for match in predicted if (match.arch_item, match.shop_item) in truth)
    return _rate(
        "identifier_match_precision",
        correct,
        len(predicted),
        empty_note="no matches were proposed",
    )


# ---------------------------------------------------------------------------
# 6-7. Recall
# ---------------------------------------------------------------------------


def fail_recall(predicted: Sequence[Finding], gold: Sequence[ExpectedFinding]) -> MetricResult:
    """Of the defects the reviewed answer key contains, how many were caught.

    An abstention is not a catch. `REVIEW REQUIRED` on a genuine defect is safe — nothing wrong ships
    unnoticed — but the reviewer did the work, so it does not count as the system finding it.
    """
    expected_fails = {expected.check for expected in gold if expected.outcome is Outcome.FAIL}
    caught = sum(
        1
        for finding in predicted
        if finding.rule_id in expected_fails and finding.outcome is Outcome.FAIL
    )
    return _rate(
        "fail_recall",
        caught,
        len(expected_fails),
        empty_note="the gold set contains no case that should FAIL",
    )


def abstention_recall(
    predicted: Sequence[Finding], gold: Sequence[ExpectedFinding]
) -> MetricResult:
    """Of the cases the answer key says are undecidable, how many the system declined to decide.

    This is the metric that stops the others being gamed. Abstaining less always improves coverage
    and usually improves recall; this is the number that goes down when it does.
    """
    expected_abstentions = {expected.check for expected in gold if expected.outcome in ABSTENTIONS}
    abstained = sum(
        1
        for finding in predicted
        if finding.rule_id in expected_abstentions and finding.outcome in ABSTENTIONS
    )
    return _rate(
        "abstention_recall",
        abstained,
        len(expected_abstentions),
        empty_note="the gold set contains no case that should abstain",
    )


# ---------------------------------------------------------------------------
# 8. Coverage
# ---------------------------------------------------------------------------


def automation_coverage(predicted: Sequence[Finding]) -> MetricResult:
    """The share of checks that reached a decision without a human.

    Listed last in `AGENTS.md` §9 on purpose, and worth restating here because this is the number a
    stakeholder will ask about: it rises whenever the system abstains less, which is also how it gets
    less safe. It is meaningful only alongside `critical_false_pass_rate`, and `F2.3` enforces that
    ordering at the report boundary.
    """
    decided = sum(1 for finding in predicted if finding.outcome not in ABSTENTIONS)
    return _rate(
        "automation_coverage",
        decided,
        len(predicted),
        empty_note="no checks were run",
    )


# ---------------------------------------------------------------------------
# 9-10. Not computable from a gold set
# ---------------------------------------------------------------------------


def review_derived_placeholders() -> Mapping[str, MetricResult]:
    """The two metrics a gold set cannot answer, stated as unmeasured rather than omitted.

    Reviewer correction rate needs the correction ledger (`D5.4`); reviewer minutes needs review
    sessions (`C1.10`). Both describe what humans did in production, which is not a property of a
    drawing.

    They are returned rather than left out so a report shows all nine. A metric that silently
    disappears reads as one that was not needed.
    """
    return {
        "reviewer_correction_rate": MetricResult(
            key="reviewer_correction_rate",
            value=None,
            numerator=0,
            denominator=0,
            note="needs the correction ledger (D5.4) — not derivable from a gold set",
        ),
        "reviewer_minutes": MetricResult(
            key="reviewer_minutes",
            value=None,
            numerator=0,
            denominator=0,
            note="needs review sessions (C1.10) — not derivable from a gold set",
        ),
    }


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------

#: The nine metrics in the order `AGENTS.md` §9 requires. Order is data so `F2.3` can enforce it
#: rather than re-deriving it, and so a reader can see the priority without reading the prose.
METRIC_ORDER: tuple[str, ...] = (
    "critical_false_pass_rate",
    "evidence_localisation_rate",
    "numeric_exact_match_accuracy",
    "unit_exact_match_accuracy",
    "identifier_match_precision",
    "fail_recall",
    "abstention_recall",
    "reviewer_correction_rate",
    "reviewer_minutes",
    "automation_coverage",
)


def compute_all(
    findings: Sequence[Finding],
    expected_findings: Sequence[ExpectedFinding],
    *,
    observations: Sequence[GoldObservation] = (),
    gold_observations: Sequence[GoldObservation] = (),
    matches: Sequence[GoldMatch] = (),
    gold_matches: Sequence[GoldMatch] = (),
) -> Mapping[str, MetricResult]:
    """Every metric, keyed as `eval/release_gates.py` expects to read them.

    Returns all of them including the unmeasured ones. The gate runner treats a missing key and an
    unmeasurable one identically — both leave a gate NOT EVALUATED — but a human reading the report
    needs to know which it was.
    """
    results = {
        "critical_false_pass_rate": critical_false_pass_rate(findings, expected_findings),
        "evidence_localisation_rate": evidence_localisation_rate(observations, gold_observations),
        "numeric_exact_match_accuracy": numeric_exact_match_accuracy(
            observations, gold_observations
        ),
        "unit_exact_match_accuracy": unit_exact_match_accuracy(observations, gold_observations),
        "identifier_match_precision": identifier_match_precision(matches, gold_matches),
        "fail_recall": fail_recall(findings, expected_findings),
        "abstention_recall": abstention_recall(findings, expected_findings),
        "automation_coverage": automation_coverage(findings),
    }
    results.update(review_derived_placeholders())
    return results


def gate_inputs(results: Mapping[str, MetricResult]) -> Mapping[str, Fraction]:
    """Reduce results to the exact values `ReleaseGateInputs.metrics` accepts.

    Unmeasured metrics are **omitted**, not zeroed. `release_gates._exact` returns `None` for a
    missing key and the gate reports NOT EVALUATED — which is the honest outcome. Passing `0` for an
    unmeasured false-PASS rate would sail through a `maximum` threshold and report a green gate for
    something nobody measured.
    """
    return {key: result.value for key, result in results.items() if result.value is not None}


def report(results: Mapping[str, MetricResult]) -> str:
    """A plain-English summary in release-gate priority order.

    Unmeasured metrics are shown, not hidden. A report that lists eight of nine numbers and omits the
    ninth reads as complete.
    """
    lines = ["Release metrics (priority order — critical false-PASS first):", ""]
    for key in METRIC_ORDER:
        result = results.get(key)
        lines.append(f"  {result}" if result else f"  {key}: NOT COMPUTED")
    unmeasured = [k for k in METRIC_ORDER if k in results and not results[k].measured]
    if unmeasured:
        lines.extend(
            [
                "",
                (
                    f"{len(unmeasured)} of {len(METRIC_ORDER)} metrics could not be measured. "
                    "An unmeasured metric is not a passing one."
                ),
            ]
        )
    return "\n".join(lines)
