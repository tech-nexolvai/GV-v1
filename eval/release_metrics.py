"""The release report — the one a release decision is actually made from.

`eval/metrics.py` computes the numbers. This decides what may be *shown*, and refuses to present a
flattering metric above a failing safety gate.

`AGENTS.md` §9, verbatim:

> *"Optimise reviewer minutes and automation coverage **only after** false-PASS, evidence
> localisation, numeric/unit accuracy and match precision meet their release gates."*

Automation coverage is the metric a stakeholder asks about first and the one most easily improved by
abstaining less — that is, by making the system decide more often, which is also how it gets less
safe. A report showing coverage climbing while the false-PASS gate is unmet would be technically
accurate and actively misleading.

So the ordering is not a formatting convention. `render()` raises rather than producing that report,
because the alternative is trusting whoever assembles the slide to remember.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction

from eval.metrics import METRIC_ORDER, MetricResult


class Direction(StrEnum):
    """Which way a metric has to move to be good."""

    MAXIMUM = "maximum"
    """Lower is better. Only false-PASS: it counts harm, so the threshold is a ceiling."""

    MINIMUM = "minimum"
    """Higher is better — accuracy, precision, recall, coverage."""


class GateOutcome(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    NOT_MEASURED = "NOT_MEASURED"
    """Distinct from FAILED, and it must never be treated as PASSED. See `blocking_failures`."""


#: Which way each metric moves. Exhaustive over `METRIC_ORDER` — a new metric without a direction
#: raises at import rather than silently defaulting to "higher is better", which would be wrong for
#: exactly the metric that matters most.
DIRECTIONS: Mapping[str, Direction] = {
    "critical_false_pass_rate": Direction.MAXIMUM,
    "evidence_localisation_rate": Direction.MINIMUM,
    "numeric_exact_match_accuracy": Direction.MINIMUM,
    "unit_exact_match_accuracy": Direction.MINIMUM,
    "identifier_match_precision": Direction.MINIMUM,
    "fail_recall": Direction.MINIMUM,
    "abstention_recall": Direction.MINIMUM,
    "reviewer_correction_rate": Direction.MAXIMUM,
    "reviewer_minutes": Direction.MAXIMUM,
    "automation_coverage": Direction.MINIMUM,
}

#: The five that gate a release, per `AGENTS.md` §9. Derived from `METRIC_ORDER` rather than
#: re-listed, so the priority order and the blocking set cannot drift apart.
GATE_BLOCKING: tuple[str, ...] = METRIC_ORDER[:5]

#: The two that may only be optimised once every blocking gate has passed.
OPTIMISATION: tuple[str, ...] = ("reviewer_minutes", "automation_coverage")

_MISSING_DIRECTIONS = set(METRIC_ORDER) - set(DIRECTIONS)
if _MISSING_DIRECTIONS:  # pragma: no cover - a wiring error, caught at import
    raise RuntimeError(
        f"metrics without a declared direction: {sorted(_MISSING_DIRECTIONS)}. "
        "Defaulting to 'higher is better' would invert the false-PASS gate."
    )


class MetricsOutOfOrder(Exception):
    """Raised when a report would show an optimisation metric above a failing safety gate.

    An exception rather than a filtered-out section, because silently dropping the metric someone
    asked for looks like a bug and invites them to go and find the number elsewhere. Refusing, with
    the failing gate named, tells them why they cannot have it yet.
    """


@dataclass(frozen=True, slots=True)
class GatedMetric:
    """One metric, its threshold, and whether it cleared it."""

    key: str
    result: MetricResult
    threshold: Fraction | None
    direction: Direction
    outcome: GateOutcome

    @property
    def blocking(self) -> bool:
        return self.key in GATE_BLOCKING

    def __str__(self) -> str:
        if self.outcome is GateOutcome.NOT_MEASURED:
            return f"  [ ? ] {self.result}"
        mark = "PASS" if self.outcome is GateOutcome.PASSED else "FAIL"
        limit = f"{self.direction.value} {float(self.threshold or 0) * 100:.1f}%"
        return f"  [{mark}] {self.result}  (gate: {limit})"


def _outcome(result: MetricResult, threshold: Fraction | None, direction: Direction) -> GateOutcome:
    if result.value is None or threshold is None:
        return GateOutcome.NOT_MEASURED
    held = (
        result.value <= threshold if direction is Direction.MAXIMUM else result.value >= threshold
    )
    return GateOutcome.PASSED if held else GateOutcome.FAILED


def evaluate(
    results: Mapping[str, MetricResult], thresholds: Mapping[str, Fraction]
) -> tuple[GatedMetric, ...]:
    """Grade every metric against its threshold, in priority order.

    A metric with no threshold is `NOT_MEASURED`, not passed. Declining to declare a limit is not
    the same as clearing one, and a release gate with no declared threshold is not a gate.
    """
    gated: list[GatedMetric] = []
    for key in METRIC_ORDER:
        result = results.get(key)
        if result is None:
            result = MetricResult(
                key=key, value=None, numerator=0, denominator=0, note="not computed"
            )
        threshold = thresholds.get(key)
        direction = DIRECTIONS[key]
        gated.append(
            GatedMetric(
                key=key,
                result=result,
                threshold=threshold,
                direction=direction,
                outcome=_outcome(result, threshold, direction),
            )
        )
    return tuple(gated)


def blocking_failures(gated: Sequence[GatedMetric]) -> tuple[GatedMetric, ...]:
    """Every blocking gate that has not affirmatively passed.

    `NOT_MEASURED` counts as a failure here, and that is the most important line in this module. An
    unmeasured gate is not a cleared one — treating the two alike is how a release ships on the
    strength of a metric nobody computed. Today every metric is unmeasured because there is no gold
    set, so this correctly reports that nothing may be optimised yet.
    """
    return tuple(g for g in gated if g.blocking and g.outcome is not GateOutcome.PASSED)


def ships(gated: Sequence[GatedMetric]) -> bool:
    """True only when every blocking gate affirmatively passed."""
    return not blocking_failures(gated)


def render(gated: Sequence[GatedMetric], *, include_optimisation: bool = False) -> str:
    """The release report, in priority order.

    `include_optimisation` must stay False while any blocking gate is unmet. Asking for it anyway
    raises `MetricsOutOfOrder` — this is the whole point of the module, and the reason it is a
    separate call rather than a flag someone might not notice.
    """
    failures = blocking_failures(gated)
    if include_optimisation and failures:
        raise MetricsOutOfOrder(
            "Refusing to report reviewer minutes or automation coverage while "
            f"{len(failures)} safety gate(s) have not passed: "
            f"{', '.join(g.key for g in failures)}.\n"
            "Coverage rises whenever the system abstains less, which is also how it becomes less "
            "safe. Presenting it above an unmet safety gate would read as progress. Clear the gates "
            "first — see AGENTS.md §9."
        )

    lines = ["Release report — safety gates first (AGENTS.md §9)", ""]
    section: str | None = None
    for metric in gated:
        if metric.key in OPTIMISATION and not include_optimisation:
            continue
        # Label each group. Listing a diagnostic metric under "Blocking gates" would overstate
        # what actually holds a release, which is the same class of misleading report this module
        # exists to refuse.
        heading = (
            "Blocking gates — these decide whether we ship:"
            if metric.blocking
            else (
                "Optimisation — only meaningful once the gates above pass:"
                if metric.key in OPTIMISATION
                else "Diagnostic — informative, not blocking:"
            )
        )
        if heading != section:
            if section is not None:
                lines.append("")
            lines.append(heading)
            section = heading
        lines.append(str(metric))

    lines.append("")
    if failures:
        lines.append(
            f"NOT RELEASABLE — {len(failures)} blocking gate(s) unmet: "
            f"{', '.join(g.key for g in failures)}."
        )
        unmeasured = [g.key for g in failures if g.outcome is GateOutcome.NOT_MEASURED]
        if unmeasured:
            lines.append(
                f"{len(unmeasured)} of those were never measured. An unmeasured gate is not a "
                "passed one; it means nobody has checked."
            )
        lines.append(
            "Reviewer minutes and automation coverage are withheld until the gates above pass."
        )
    else:
        lines.append("All blocking gates passed. Optimisation metrics may now be reported.")
    return "\n".join(lines)
