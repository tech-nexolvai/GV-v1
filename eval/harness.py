"""Run the cases, score them, and say plainly what was not measured.

**The gap this closes.** Every part of the evaluation harness existed and nothing joined them up.
`eval/gold_set/` loads and verifies cases, `eval/metrics.py` computes the numbers, `eval/runs.py`
stores a run, `eval/regression.py` compares two, `eval/release_gates.py` reads the result — and
`compute_all()` had no caller outside its own unit tests. Nothing took a case, produced findings from
it, and scored them.

That made `CLAUDE.md`'s merge gate unperformable: *"No change to `verdict/`/`rules/`/`evidence/`
merges without unit tests **and a gold-set run** that does not regress critical false-PASS."* There was
no way to perform a gold-set run, so every change to those directories merged without one.

**Two lanes, and they are not interchangeable.** The synthetic lane runs authored engine inputs
through `verdict.engine.execute` and scores the outcomes; it works today and it is what makes this
module testable at all. The real lane needs annotated packages, and `data/drawings/` is empty while
`#274` is open. This module implements the lane that exists and **refuses** the one that does not,
rather than offering a function that returns nothing and looks like a run.

**Synthetic numbers are not the safety number.** A synthetic case has no drawing, page or polygon, so
it cannot speak to whether the system found the right dimension on a real sheet — ADR-0014 keeps it
out of evidence localisation for exactly that reason. What it *can* speak to is whether the
deterministic engine turns known operands into the right verdict, which is worth measuring on its own
and worth never confusing with the other thing. `HarnessRun.lane` is on every result, and
`scripts/run_gold_set.py` will not record a synthetic run as a baseline for the real gate.

The 0-over-0 trap is already handled where it belongs: `eval/metrics.py` returns `None` for a metric
nobody could compute rather than a perfect score. This module's job is not to re-guard that but to
avoid manufacturing the empty input in the first place.

Source: `AGENTS.md` §8 Phase 0 and §9 · Verification: `tests/eval/test_harness.py`
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final

from eval.gold_set.schema import DEFAULT_CASES_DIRECTORY, ExpectedFinding
from eval.gold_set.store import load_cases
from eval.metrics import MetricResult, compute_all
from eval.synthetic import SyntheticCase, generate_synthetic_cases, run_synthetic_case
from verdict.finding import Finding
from verdict.operations import register_all

__all__ = [
    "GOLD_LANE",
    "SYNTHETIC_LANE",
    "CaseOutcome",
    "GoldLaneUnavailable",
    "HarnessRun",
    "NoCasesToRun",
    "gold_lane_blockers",
    "run_gold",
    "run_synthetic",
    "summary",
]

#: The lane whose numbers are the project's safety metric — real drawings, real answers.
GOLD_LANE: Final = "gold"

#: The lane that proves the engine, with no drawing behind any operand.
SYNTHETIC_LANE: Final = "synthetic"


class NoCasesToRun(Exception):
    """Raised rather than scoring nothing.

    A run over zero cases produces a report full of `None`s that reads like a run. `eval/metrics.py`
    refuses to render 0-over-0 as a perfect score, which stops the *number* lying; this stops the
    *run* existing. Recording it would put a row in `evaluation_runs` that a later comparison would
    treat as a baseline.
    """


class GoldLaneUnavailable(Exception):
    """Raised when the real lane is asked for and cannot be honoured.

    Carries what is missing, because "the gold lane is unavailable" is not actionable and the two
    blockers have different owners: drawings come from the client (`#274`), the stages between a PDF
    and a finding are ours.
    """


@dataclass(frozen=True, slots=True)
class CaseOutcome:
    """One case, what it produced, and what it was supposed to produce."""

    case_id: str
    lane: str
    findings: tuple[Finding, ...]
    expected: tuple[ExpectedFinding, ...]

    @property
    def agreed(self) -> bool:
        """Whether every expected check came back with the outcome the case says it should.

        Not a metric — the metrics module owns those, and this is deliberately not one of them. It is
        what a person reading the run wants per case: did this one do what it was supposed to.
        """
        observed = {finding.rule_id: finding.outcome for finding in self.findings}
        return all(
            observed.get(expectation.check) == expectation.outcome for expectation in self.expected
        )


@dataclass(frozen=True, slots=True)
class HarnessRun:
    """Everything one execution of the harness produced, including what it could not measure."""

    lane: str
    outcomes: tuple[CaseOutcome, ...]
    metrics: Mapping[str, MetricResult]
    rule_snapshot_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "metrics", MappingProxyType(dict(self.metrics)))

    @property
    def disagreed(self) -> tuple[CaseOutcome, ...]:
        """The cases that did not do what they were supposed to, which is what a reader looks for."""
        return tuple(outcome for outcome in self.outcomes if not outcome.agreed)


def run_synthetic(cases: Sequence[SyntheticCase] | None = None) -> HarnessRun:
    """Execute the synthetic cases through the real verdict engine and score them.

    `run_synthetic_case` already runs one through `verdict.engine.execute`; what was missing was
    collecting the findings and putting them through `compute_all` — which is the whole difference
    between "the engine was exercised" and "the run was scored".

    No observations or matches are passed, so evidence localisation, numeric and unit accuracy and
    identifier precision all come back unmeasured. That is correct rather than a shortfall: a
    synthetic operand has no polygon, and a number invented here would say the system located
    something on a drawing that does not exist.
    """
    # **`register_all()` first, for the reason `workflow/stages.py:run_checks` gives.** The operation
    # registry is global and empty until something fills it, and the only thing that does is importing
    # `app/api/operations.py` — which nothing on this path touches. Without it every operation lookup
    # fails and the engine converts the failure to REVIEW_REQUIRED, so a scored run would report the
    # engine abstaining on every case rather than the registry being empty. Found by running this
    # module outside pytest, where the ambient import that hides it is absent. Idempotent.
    register_all()

    selected = tuple(cases) if cases is not None else generate_synthetic_cases()
    if not selected:
        raise NoCasesToRun(
            "no synthetic cases to run. Scoring zero cases produces a report of unmeasured metrics "
            "that reads exactly like a run that measured nothing wrong."
        )

    outcomes: list[CaseOutcome] = []
    findings: list[Finding] = []
    expected: list[ExpectedFinding] = []
    snapshots: list[str] = []
    for case in selected:
        finding = run_synthetic_case(case)
        findings.append(finding)
        expected.append(case.expected)
        snapshots.append(case.rule_snapshot.snapshot_id)
        outcomes.append(
            CaseOutcome(
                case_id=case.case_id,
                lane=SYNTHETIC_LANE,
                findings=(finding,),
                expected=(case.expected,),
            )
        )

    return HarnessRun(
        lane=SYNTHETIC_LANE,
        outcomes=tuple(outcomes),
        metrics=compute_all(findings, expected),
        rule_snapshot_ids=tuple(sorted(set(snapshots))),
    )


def gold_lane_blockers(*, root: Path = Path(DEFAULT_CASES_DIRECTORY)) -> tuple[str, ...]:
    """What stands between here and a real gold-set run, named so each has an owner.

    Reported rather than raised, so a caller can print the list. `#274` is the client's; the pipeline
    from a PDF to a finding is ours and is `AGENTS.md` §8 phases 1 through 3.
    """
    blockers: list[str] = []
    try:
        cases = load_cases(cases_directory=root)
    except FileNotFoundError:
        cases = ()
    if not cases:
        blockers.append(
            f"no annotated cases under {root} — the client has not sent the drawing set (#274), "
            "and a case invented here would make the safety metric measure a guess (AGENTS.md §9)"
        )
    blockers.append(
        "no path from a drawing to a finding: matching and the evidence gate are phases 5 and 3, "
        "so nothing can turn an annotated package into the findings this would score"
    )
    return tuple(blockers)


def run_gold(*, root: Path = Path(DEFAULT_CASES_DIRECTORY)) -> HarnessRun:
    """The real lane. Refuses, with the reasons, until it can be honoured.

    A function that returned an empty `HarnessRun` here would be the most dangerous thing in this
    file: the metrics would all read unmeasured, the run would record, and the release gate would say
    NOT EVALUATED — which is indistinguishable from a gold set that exists and was not run.
    """
    raise GoldLaneUnavailable(
        "the gold lane cannot run yet:\n  - " + "\n  - ".join(gold_lane_blockers(root=root))
    )


def summary(run: HarnessRun) -> str:
    """The run in plain English, leading with what disagreed.

    Deliberately not just the metric table. A rate tells somebody whether to ship; the list of cases
    that did the wrong thing tells them what to go and look at.
    """
    lines = [
        f"Lane: {run.lane}",
        f"Cases: {len(run.outcomes)}",
    ]
    if run.lane == SYNTHETIC_LANE:
        lines.append(
            "These cases have no drawing behind them. They measure the engine, not whether the "
            "system reads a real sheet correctly — that is the gold lane, and it is blocked."
        )
    disagreed = run.disagreed
    if disagreed:
        lines.append("")
        lines.append(f"{len(disagreed)} case(s) did not match their authored expectation:")
        for outcome in disagreed:
            observed = {finding.rule_id: str(finding.outcome) for finding in outcome.findings}
            for expectation in outcome.expected:
                got = observed.get(expectation.check, "no finding")
                if got != str(expectation.outcome):
                    lines.append(
                        f"  {outcome.case_id} · {expectation.check}: "
                        f"expected {expectation.outcome}, got {got}"
                    )
    else:
        lines.append("Every case matched its authored expectation.")
    return "\n".join(lines)
