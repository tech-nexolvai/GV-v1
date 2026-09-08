"""Score one answer-key package against what the pipeline actually produced.

**The join that was missing.** `eval/gold_set/` loads and verifies answer keys, `eval/metrics.py`
computes the release metrics, `eval/harness.py` runs the synthetic lane — and nothing took a
*reviewed drawing package*, ran the real pipeline on it, and compared the two. `run_gold` refuses for
that reason, and its own docstring says a function returning an empty run would be "the most
dangerous thing in this file". This is the comparison half; `scripts/evaluate_goldset.py` is the
running half.

**It reuses the nine release metrics and does not extend them.** `eval/metrics.py:METRIC_ORDER` is
the governed list from `F2.3` — nine metrics in a required priority order that the release gate reads
— and a tenth added here would quietly change what the gate is. So three of the four headline numbers
come straight from `compute_all`, and the fourth, verdict accuracy, is computed here and reported as
a scorecard number rather than as a release metric.

**Two rules this module exists to keep.**

*An abstention is never scored as a wrong reading.* `REVIEW_REQUIRED`, `NOT_FOUND` and
`NO_APPLICABLE_RULE` mean the system declined to answer, which under `AGENTS.md` §2 is the correct
behaviour when it cannot be sure. Counting those as errors would make the honest path look like the
failing path, and the fastest way to improve such a score would be to guess more.

*Nothing here invents a reading the pipeline did not make.* Where the answer key has an entry and the
system produced nothing, that is recorded as `missing` — never as a wrong value, and never filled in
from the answer key. A harness that supplied the answer it was grading against would report a perfect
score for a pipeline that read nothing at all.

**Offline only.** Nothing in this module writes a finding, an observation or a verdict. It reads what
a run produced and reports on it; the live path cannot be reached from here.

Source: issue #553 · Verification: `tests/eval/test_scorecard.py`
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Protocol, cast

from eval.gold_set.schema import ExpectedFinding, GoldCase, GoldObservation
from eval.metrics import MetricResult, compute_all
from verdict.finding import Finding
from verdict.outcomes import Outcome, Severity

__all__ = ["ReadingComparison", "Scorecard", "ScoredFinding", "score_package"]


class ScoredFinding(Protocol):
    """The three things scoring a finding actually reads.

    **Narrower than `verdict.finding.Finding` on purpose, and the reason is an invariant worth
    keeping.** A `Finding` refuses to exist without a `CalculationTrace` for anything but an
    abstention — *"a decision a reviewer cannot check by hand is not defensible"* — and that is
    right. But a grader reads findings back out of the database, where the trace is stored as JSON
    and there is no inverse for it, so building a `Finding` would mean inventing operands and a
    comparison this code never performed.

    Fabricating a calculation to satisfy a type that exists to prevent fabricated calculations would
    be precisely backwards. So the scorer states the three fields it needs — every metric in
    `eval/metrics.py` reads only these — and a real `Finding` satisfies it unchanged.
    """

    @property
    def rule_id(self) -> str:
        """Which check this is the outcome of. What an answer key pairs its expectations on."""

    @property
    def outcome(self) -> Outcome:
        """What the engine concluded."""

    @property
    def severity(self) -> Severity:
        """How serious the rule is. From the rulebook, never from the answer key."""


#: Outcomes that mean "the system declined to answer", as `AGENTS.md` §2 defines the honest path.
#:
#: Held here as well as in `eval/metrics.py` because the two ask different questions of the same set:
#: the metric asks whether an expected abstention was delivered, and this asks whether a reading was
#: attempted at all. Same members, and if they ever diverge the divergence is a bug in one of them.
ABSTENTIONS: frozenset[Outcome] = frozenset(
    {Outcome.REVIEW_REQUIRED, Outcome.NOT_FOUND, Outcome.NO_APPLICABLE_RULE}
)


@dataclass(frozen=True, slots=True)
class ReadingComparison:
    """One answer-key reading beside what the system read, or the fact that it read nothing.

    `actual` is `None` for a reading the pipeline never produced. That is deliberately not the same
    as a wrong value and is counted separately: a system that read nothing and a system that read
    the wrong number fail differently, and one of them is safe.
    """

    semantic_type: str
    expected: str
    actual: str | None
    matched: bool

    @property
    def missing(self) -> bool:
        """Whether the pipeline produced no reading at all for this answer."""
        return self.actual is None


@dataclass(frozen=True, slots=True)
class Scorecard:
    """What one answer-key package says about one pipeline run.

    Every count is here beside every rate, because a rate over two cases and a rate over two hundred
    read identically and mean different things. `metrics` carries the nine release metrics unchanged,
    so a caller that needs the gate's own numbers has them without this module restating any.
    """

    case_id: str
    readings: tuple[ReadingComparison, ...]
    metrics: Mapping[str, MetricResult]

    verdicts_expected: int
    verdicts_matched: int
    critical_false_passes: tuple[str, ...]
    """Which checks wrongly passed a drawing the reviewer failed. Named, not counted: the primary
    safety metric is not a number somebody should have to go looking for the cases behind."""

    abstained: tuple[str, ...]
    findings_seen: int
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def reading_accuracy(self) -> Fraction | None:
        """Right values over answers the system attempted. `None` when it attempted none.

        **The denominator excludes missing readings on purpose.** This answers "when the system read
        something, was it right", which is the question about reading quality. How often it read
        nothing at all is `coverage`, and mixing the two produces a number that improves when the
        system guesses more.
        """
        attempted = [reading for reading in self.readings if not reading.missing]
        if not attempted:
            return None
        return Fraction(sum(1 for reading in attempted if reading.matched), len(attempted))

    @property
    def coverage(self) -> Fraction | None:
        """Answers the system attempted over answers the key holds. `None` for an empty key."""
        if not self.readings:
            return None
        return Fraction(
            sum(1 for reading in self.readings if not reading.missing), len(self.readings)
        )

    @property
    def verdict_accuracy(self) -> Fraction | None:
        """Checks whose outcome matched the reviewer's, over checks the key states an outcome for."""
        if not self.verdicts_expected:
            return None
        return Fraction(self.verdicts_matched, self.verdicts_expected)

    @property
    def critical_false_pass_rate(self) -> MetricResult:
        """The ship gate, straight from `eval/metrics.py` rather than recomputed here.

        Recomputing it would create a second answer to the one question this project treats as
        primary, and the two would eventually disagree — at which point nobody would know which one
        the release decision had used.
        """
        return self.metrics["critical_false_pass_rate"]

    @property
    def abstention_rate(self) -> Fraction | None:
        """How often the system asked a human instead of answering. `None` when nothing ran.

        Reported as a plain rate and never as a failure. Under V1's exact-match rule this is expected
        to be large, and a harness that scored it as error would be scoring the system for being
        careful.
        """
        if not self.findings_seen:
            return None
        return Fraction(len(self.abstained), self.findings_seen)


def _rendered(value: object) -> str:
    """One measurement as exact text, for a side-by-side a person will read.

    Text rather than a float, for the reason the whole units layer exists: `25.5` and `51/2` are the
    same number and a printed float is neither of them exactly.
    """
    exact = getattr(value, "exact", None)
    unit = getattr(value, "unit", None)
    if exact is None:
        return str(value)
    spelling = getattr(unit, "value", unit)
    return f"{exact} {spelling}"


def _compare_readings(
    actual: Sequence[GoldObservation], expected: Sequence[GoldObservation]
) -> tuple[ReadingComparison, ...]:
    """Pair each answer with the system's reading of the same thing, by semantic type.

    **Paired on the type, which is the reviewer's own label.** It is the only identifier both sides
    share: the answer key's `item_id` is an annotator's name for a drawing item and the pipeline has
    no such thing yet (#165 is unbuilt), and pairing by position would compare the first answer with
    whatever the reader happened to find first.

    A type appearing twice in one key is refused by the caller rather than silently first-wins — see
    `score_package`.
    """
    by_type = {observation.semantic_type: observation for observation in actual}
    compared: list[ReadingComparison] = []
    for answer in expected:
        found = by_type.get(answer.semantic_type)
        compared.append(
            ReadingComparison(
                semantic_type=answer.semantic_type.value,
                expected=_rendered(answer.value),
                actual=None if found is None else _rendered(found.value),
                # **The value and the unit, not the transcription.** `Measurement` equality also
                # compares `raw_text`, which is what was *printed* — so a reading of exactly the
                # right number scored as wrong because the annotator wrote `25.5"` and the reader
                # saw `25.5"` with a different `raw_text` recorded. What is being graded is whether
                # the system got the dimension right, and `(exact, unit)` is the dimension.
                #
                # Still exact: Q2 settled exact match for V1, so a reading that is nearly right is
                # wrong here, and both sides are `Fraction`s.
                matched=found is not None
                and (found.value.exact, found.value.unit)
                == (answer.value.exact, answer.value.unit),
            )
        )
    return tuple(compared)


def score_package(
    case: GoldCase,
    findings: Sequence[ScoredFinding],
    *,
    observations: Sequence[GoldObservation] = (),
) -> Scorecard:
    """Compare one reviewed package's answer key with what a run produced.

    `observations` are the system's own readings, expressed in the answer key's shape so the two are
    comparable. They are *not* taken from the case — `scripts/evaluate_goldset.py` builds them from
    the canonical observations the pipeline wrote, and passing the case's own answers here would
    score the key against itself.

    Raises `ValueError` for an answer key that names one semantic type twice. That is not a scoring
    edge case: it means two answers claim to be about the same quantity, and whichever this happened
    to pair with would decide the score.
    """
    duplicated = sorted(
        {
            answer.semantic_type.value
            for answer in case.ground_truth.observations
            if sum(
                1
                for other in case.ground_truth.observations
                if other.semantic_type is answer.semantic_type
            )
            > 1
        }
    )
    if duplicated:
        raise ValueError(
            f"case {case.id!r} states two answers for {duplicated}. Readings are paired on the "
            "reviewer's semantic type, so a repeated type leaves the score depending on which "
            "answer happened to be compared."
        )

    expected_findings: Sequence[ExpectedFinding] = case.ground_truth.expected_findings
    metrics = compute_all(
        # Every metric reads `rule_id`, `outcome` and `severity` and nothing else — see
        # `ScoredFinding` for why a grader cannot hand over a real `Finding`.
        cast("Sequence[Finding]", findings),
        expected_findings,
        observations=observations,
        gold_observations=case.ground_truth.observations,
        matches=(),
        gold_matches=case.ground_truth.matches,
    )

    outcome_by_check = {finding.rule_id: finding.outcome for finding in findings}
    matched = 0
    false_passes: list[str] = []
    for answer in expected_findings:
        produced = outcome_by_check.get(answer.check)
        if produced is answer.outcome:
            matched += 1
        # A CRITICAL check the reviewer failed and the system passed. The `severity` comes from the
        # finding rather than the answer key: the key states what a reviewer concluded, and how
        # serious the rule is belongs to the rulebook.
        if (
            produced is Outcome.PASS
            and answer.outcome is Outcome.FAIL
            and any(
                finding.rule_id == answer.check and finding.severity is Severity.CRITICAL
                for finding in findings
            )
        ):
            false_passes.append(answer.check)

    notes: list[str] = []
    unanswered = [
        answer.check for answer in expected_findings if answer.check not in outcome_by_check
    ]
    if unanswered:
        notes.append(
            f"{len(unanswered)} check(s) in the answer key produced no finding at all: "
            f"{', '.join(sorted(unanswered))}. Recorded as unmatched, never as a pass."
        )
    unexpected = sorted(set(outcome_by_check) - {answer.check for answer in expected_findings})
    if unexpected:
        notes.append(
            f"{len(unexpected)} check(s) ran that the answer key says nothing about: "
            f"{', '.join(unexpected)}. Not scored — an answer key is silent, not negative."
        )

    return Scorecard(
        case_id=case.id,
        readings=_compare_readings(observations, case.ground_truth.observations),
        metrics=metrics,
        verdicts_expected=len(expected_findings),
        verdicts_matched=matched,
        critical_false_passes=tuple(false_passes),
        abstained=tuple(finding.rule_id for finding in findings if finding.outcome in ABSTENTIONS),
        findings_seen=len(findings),
        notes=tuple(notes),
    )


def render(scorecard: Scorecard) -> str:
    """The scorecard as a person reads it, safety number first.

    Priority order, the way `eval/metrics.py:report` orders the release metrics: the critical
    false-PASS rate leads because it is the one number that can stop a release, and a report that
    buried it among accuracies would invite somebody to read the flattering ones first.

    A rate that could not be measured prints as `not measured` rather than as `0` — the distinction
    `eval/metrics.py` makes for the same reason, since a zero false-PASS rate nobody measured reads
    as the best possible result.
    """

    def rate(value: Fraction | None) -> str:
        return "not measured" if value is None else f"{float(value) * 100:.1f}%"

    critical = scorecard.critical_false_pass_rate
    critical_text = (
        "not measured" if critical.value is None else f"{float(critical.value) * 100:.2f}%"
    )
    if critical.note:
        critical_text += f" ({critical.note})"
    if scorecard.critical_false_passes:
        critical_text += f"   <-- {', '.join(scorecard.critical_false_passes)}"
    attempted = sum(1 for reading in scorecard.readings if not reading.missing)
    lines = [
        f"Answer-key scorecard — case {scorecard.case_id}",
        "",
        f"  CRITICAL false-PASS   {critical_text}",
        (
            f"  verdict accuracy      {rate(scorecard.verdict_accuracy)}"
            f"   ({scorecard.verdicts_matched}/{scorecard.verdicts_expected} checks)"
        ),
        (
            f"  reading accuracy      {rate(scorecard.reading_accuracy)}"
            "   (of the readings the system attempted)"
        ),
        (
            f"  reading coverage      {rate(scorecard.coverage)}"
            f"   ({attempted}/{len(scorecard.readings)} answers attempted)"
        ),
        (
            f"  abstention rate       {rate(scorecard.abstention_rate)}"
            f"   ({len(scorecard.abstained)}/{scorecard.findings_seen} findings)"
            " — the honest path, not an error"
        ),
        "",
        "  readings, side by side:",
    ]
    for reading in scorecard.readings:
        mark = "ok " if reading.matched else ("-- " if reading.missing else "XX ")
        actual = "nothing read" if reading.actual is None else reading.actual
        lines.append(
            f"    {mark} {reading.semantic_type:12} answer {reading.expected:16} system {actual}"
        )
    if scorecard.notes:
        lines.extend(["", "  notes:"])
        lines.extend(f"    - {note}" for note in scorecard.notes)
    return "\n".join(lines)
