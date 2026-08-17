"""The gold-set regression that must pass before a rule may be published.

`AGENTS.md` §9: *"Rule change → human approval + full gold-set regression."* `D6.3` enforced the
approval and took the regression as a required argument with no default, deliberately leaving a hole
that could not be filled by forgetting. This fills it.

**Why the scorer is injected.** Running a rule against a gold case means extracting values from a
real drawing, and that pipeline is blocked on `#274`. Rather than stub it, `gate()` takes a scorer as
a required argument. When the extraction pipeline lands it supplies the real one; today a caller must
pass something explicit and say what it is. A default would be the same hole `D6.3` refused to leave.

**Why an empty gold set is refused rather than passed.** A regression check with nothing to check
against reports a green gate for an unmeasured change — which is worse than no check, because it is
believed. `eval/gold_set/cases/` is empty today, so this refuses every publication until real cases
exist, and that is the correct behaviour rather than a limitation.

**Why this lives in `eval/` and not `rules/governance/`.** #238's plan named
`rules/governance/regression.py`, written before it was known that the gate needs a database session
to read the baseline. Putting it there made `rules/` import SQLAlchemy — and since `verdict/` imports
`rules/`, the verdict package could transitively reach a database, which is the one boundary this
project cares about most. `tests/test_verdict_isolation.py` caught it on the first run. `eval/` is
already permitted to depend on both `rules/` and `app/`, so the gate belongs here.

**Why the baseline is resolved first.** `record_run` flushes rather than commits, and `unit_of_work`
rolls back when its body raises. A run recorded and then refused would vanish with the transaction —
and since a first run has no baseline, it would raise, roll back, and leave nothing behind to become
one. The gate would refuse forever for a reason it had itself created. Checking first also avoids
paying for a scoring pass whose result is discarded.

**Why the baseline must be a stored run.** Scoring twice with the same code and comparing the results
proves only that the code is deterministic. The comparison has to be against a run recorded earlier,
under versions that may differ — which is what makes it a regression test rather than a self-check.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.evaluation import EvaluationRun, GoldSet
from eval.gold_set.schema import GoldManifest
from eval.metrics import MetricResult as ComputedMetric
from eval.regression import IncomparableRuns, NoBaseline, RegressionReport, compare, gate_outcome
from eval.runs import baseline as stored_baseline
from eval.runs import case_results_for, metrics_for, record_run
from rules.governance.proposal import RuleProposal
from rules.governance.publish import RegressionOutcome
from rules.snapshot import RuleSnapshot
from rules.snapshot import publish as build_snapshot
from verdict.outcomes import Outcome

#: Scores a proposed snapshot against the gold set. Supplied by the caller: the real implementation
#: needs the extraction pipeline, which is blocked on #274.
Scorer = Callable[[RuleSnapshot, GoldManifest], Mapping[str, ComputedMetric]]


class NoGoldSet(Exception):
    """There are no gold cases to run against.

    Refusing rather than passing. A regression check that succeeds against an empty set reports that
    nothing broke, when what happened is that nothing was tried — and a release decision made on
    that is made on nothing at all.
    """


class RegressionUnavailable(Exception):
    """The regression could not be run, for a reason that is not the rule's fault.

    Distinct from a failed regression: one means the change is unsafe, the other means nobody
    knows. Publication is refused either way, but the remediation is completely different and
    conflating them sends someone to fix a rule that may be fine.
    """


@dataclass(frozen=True, slots=True)
class RegressionVerdict:
    """The full record of a regression run, beyond the pass/fail `publish()` needs."""

    passed: bool
    summary: str
    report: RegressionReport | None
    run: EvaluationRun | None

    def as_outcome(self) -> RegressionOutcome:
        """The shape `rules/governance/publish.py` consumes."""
        return RegressionOutcome(passed=self.passed, summary=self.summary)


def gate(
    proposal: RuleProposal,
    *,
    session: Session,
    gold_set: GoldSet,
    manifest: GoldManifest,
    scorer: Scorer,
    code_version: str,
    extractor_versions: Mapping[str, str],
    case_outcomes: Mapping[UUID, Mapping[str, tuple[Outcome, Outcome]]] | None = None,
) -> RegressionVerdict:
    """Score the proposal against the gold set and decide whether it may be published.

    There is deliberately **no override parameter**. A rule that regresses a release gate does not
    ship, and an escape hatch would be used exactly once, under deadline, on the change that most
    needed the check.

    `case_outcomes` is optional and, when given, makes the report name the cases that moved rather
    than only the rates (`#315`). Without it the report says `cases_compared=False` — which is not
    "no case changed", and the distinction is the reason that flag exists.
    """
    if not manifest.cases:
        raise NoGoldSet(
            "the gold set contains no cases, so there is nothing to regress against. A check that "
            "passes here would report a green gate for a change nobody measured. Obtain the "
            "drawings (#274) and annotate them (#188) before publishing anything to production."
        )

    if not proposal.approvable:
        raise RegressionUnavailable(
            f"proposal for {proposal.rule_id} did not validate, so scoring it would measure a rule "
            f"that cannot run: {proposal.validation}"
        )

    # Resolved BEFORE anything is scored or recorded. `record_run` flushes rather than commits, and
    # `unit_of_work` rolls back when the body raises — so a run recorded here and then refused would
    # be discarded with the transaction. That is not merely a lost row: with no baseline, every
    # first run would raise, roll back, and leave nothing behind to become one. Bootstrapping would
    # be impossible, and the gate would refuse forever for a reason it had itself created.
    #
    # Failing before the work also avoids paying for a scoring pass whose result is thrown away.
    previous = stored_baseline(session, gold_set_version=gold_set.version)
    if previous is None:
        raise RegressionUnavailable(
            f"no baseline run exists for gold set {gold_set.version}, so there is nothing to "
            "compare against. Record one deliberately with `record_run(..., is_baseline=True)` — a "
            "baseline is a decision about which result is the reference, not a side effect of the "
            "first publication attempt."
        )

    snapshot = build_snapshot(proposal.proposed)
    metrics = scorer(snapshot, manifest)

    candidate = record_run(
        session,
        gold_set=gold_set,
        code_version=code_version,
        rule_snapshot_ids=[snapshot.snapshot_id],
        extractor_versions=extractor_versions,
        results=metrics,
        case_outcomes=case_outcomes,
    )

    # A run cannot be its own baseline here: `previous` was resolved before `candidate` existed.
    try:
        report = compare(
            previous,
            candidate,
            metrics_for(session, previous),
            metrics,
            # Loaded for both runs rather than only the candidate. A baseline recorded before `#315`
            # has no case rows, and `compare_cases` reports that as not-compared instead of letting
            # an absent baseline read as a set of unchanged cases.
            baseline_cases=case_results_for(session, previous),
            candidate_cases=case_results_for(session, candidate),
        )
    except NoBaseline as error:  # pragma: no cover - guarded above
        raise RegressionUnavailable(str(error)) from error
    except IncomparableRuns as error:
        raise RegressionUnavailable(str(error)) from error

    passed, summary = gate_outcome(report)
    return RegressionVerdict(passed=passed, summary=summary, report=report, run=candidate)


def as_regression_check(
    *,
    session: Session,
    gold_set: GoldSet,
    manifest: GoldManifest,
    scorer: Scorer,
    code_version: str,
    extractor_versions: Mapping[str, str],
) -> Callable[[RuleProposal], RegressionOutcome]:
    """Adapt `gate()` into the callable `publish()` expects.

    `publish()` deliberately takes a bare `Callable[[RuleProposal], RegressionOutcome]` so it knows
    nothing about gold sets or sessions. This closes over them rather than widening that signature —
    the publication path should not grow a database dependency to satisfy one of its gates.
    """

    def check(proposal: RuleProposal) -> RegressionOutcome:
        return gate(
            proposal,
            session=session,
            gold_set=gold_set,
            manifest=manifest,
            scorer=scorer,
            code_version=code_version,
            extractor_versions=extractor_versions,
        ).as_outcome()

    return check
