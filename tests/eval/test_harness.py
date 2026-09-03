"""What the harness must refuse, and the dependency that only shows outside pytest.

The interesting cases here are not "does it compute a number" — `tests/eval/test_metrics.py` owns
that. They are the two ways a run can look like a run without being one: scoring zero cases, and
offering the gold lane before it exists.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from eval.harness import (
    GOLD_LANE,
    SYNTHETIC_LANE,
    CaseOutcome,
    GoldLaneUnavailable,
    HarnessRun,
    NoCasesToRun,
    gold_lane_blockers,
    run_gold,
    run_synthetic,
    summary,
)
from eval.synthetic import build_off_by_tolerance_case, build_passing_case

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def test_the_synthetic_lane_runs_every_case_and_scores_it() -> None:
    """The whole point: findings produced *and* put through `compute_all`.

    Before this module the engine could be exercised and nothing scored it — `compute_all` had no
    caller outside its own unit tests.
    """
    run = run_synthetic()

    assert run.lane == SYNTHETIC_LANE
    assert len(run.outcomes) >= 4, "the generator should cover every primary outcome"
    assert run.metrics["critical_false_pass_rate"].measured, "the primary metric was not computed"
    assert run.rule_snapshot_ids, "a run that cannot name its rule snapshots cannot be attributed"


def test_every_synthetic_case_agrees_with_its_authored_expectation() -> None:
    """A regression signal on the engine, which is what the synthetic lane is actually for."""
    run = run_synthetic()

    assert run.disagreed == (), summary(run)


def test_the_operations_registry_is_filled_by_the_harness_not_by_luck() -> None:
    """**The bug this test exists for cannot be reproduced inside pytest.**

    The registry is global and empty until something fills it, and under pytest some other import
    always has. Run standalone, every operation lookup failed and the engine returned
    REVIEW_REQUIRED for every case — which in a scored run reads as "the engine abstained", not "the
    registry was empty". Found by running the module outside the suite; asserted here in a
    subprocess, because that is the only place the failure is visible.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from eval.harness import run_synthetic, summary;"
                "run = run_synthetic();"
                "assert run.disagreed == (), summary(run);"
                "print('ok')"
            ),
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )

    assert result.returncode == 0, (
        "the harness does not fill the operation registry itself, so it only works when something "
        f"else happens to have imported it first:\n{result.stdout}\n{result.stderr}"
    )
    assert "ok" in result.stdout


def test_scoring_zero_cases_is_refused_rather_than_reported() -> None:
    """A run over no cases is a page of unmeasured metrics that reads like a clean run.

    `eval/metrics.py` already stops the *number* lying by returning `None` rather than a perfect
    score. This stops the *run* existing, because a recorded one becomes a baseline that later
    comparisons treat as real.
    """
    with pytest.raises(NoCasesToRun) as raised:
        run_synthetic(cases=())

    assert "zero cases" in str(raised.value)


def test_the_gold_lane_refuses_and_says_what_is_missing() -> None:
    """**The most dangerous thing this module could do is return an empty gold run.**

    Every metric would read unmeasured, the run would record, and the release gate would report NOT
    EVALUATED — indistinguishable from a gold set that exists and was not run. So it raises, and the
    message names both blockers because they have different owners.
    """
    with pytest.raises(GoldLaneUnavailable) as raised:
        run_gold()

    message = str(raised.value)
    assert "#274" in message, "the client dependency is not named, so nobody knows who is blocked"
    assert "matching" in message or "evidence gate" in message, "our own half is not named"


def test_the_blockers_are_reportable_without_raising() -> None:
    """A caller printing the situation should not have to catch an exception to do it."""
    blockers = gold_lane_blockers()

    assert blockers, "the gold lane is not runnable, so there is at least one blocker to report"
    assert any("#274" in blocker for blocker in blockers)


def test_synthetic_metrics_never_claim_evidence_localisation() -> None:
    """A synthetic operand has no polygon, so localisation must come back unmeasured.

    ADR-0014 keeps synthetic cases out of this metric, and the enforcement is that no observations
    are passed rather than a filter somewhere downstream. A number here would assert the system found
    something on a drawing that does not exist.
    """
    run = run_synthetic()

    for key in (
        "evidence_localisation_rate",
        "numeric_exact_match_accuracy",
        "unit_exact_match_accuracy",
        "identifier_match_precision",
    ):
        assert not run.metrics[key].measured, f"{key} was measured from cases with no drawing"


def test_a_case_that_disagrees_is_reported_with_both_outcomes() -> None:
    """The summary has to say what was expected and what happened, or it sends a reader to the code."""
    passing = build_passing_case()
    mislabelled = CaseOutcome(
        case_id="SYNTH-DELIBERATE",
        lane=SYNTHETIC_LANE,
        findings=(),
        expected=(build_off_by_tolerance_case().expected,),
    )
    run = HarnessRun(
        lane=SYNTHETIC_LANE,
        outcomes=(mislabelled,),
        metrics={},
        rule_snapshot_ids=(passing.rule_snapshot.snapshot_id,),
    )

    text = summary(run)

    assert "SYNTH-DELIBERATE" in text
    assert "no finding" in text, "a case that produced nothing must say so, not go unmentioned"
    assert run.disagreed == (mislabelled,)


def test_the_synthetic_summary_says_these_numbers_are_not_the_safety_number() -> None:
    """Someone reading a green synthetic run must not take it for evidence about real drawings."""
    text = summary(run_synthetic())

    assert "no drawing" in text
    assert GOLD_LANE in text


def test_the_cli_runs_the_synthetic_lane_and_exits_zero() -> None:
    """A subprocess, not a direct call to `main`.

    The import path is the thing under test as much as the exit code: run as a script, `sys.path[0]`
    is `scripts/`, not the repository root — which is how the missing `vocabulary` package was found.
    Calling `main()` from inside pytest would exercise neither.
    """
    result = subprocess.run(
        [sys.executable, "scripts/run_gold_set.py"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "critical_false_pass_rate" in result.stdout
    assert "no drawing" in result.stdout, "the run does not say these are not the safety number"


def test_the_cli_exits_two_when_the_lane_cannot_run() -> None:
    """Two, not one. A caller has to tell "the run found a problem" from "there was no run".

    A CI step that treated them alike would report a passing gold set on a repository that has never
    had one — which is the whole failure this harness exists to make impossible.
    """
    result = subprocess.run(
        [sys.executable, "scripts/run_gold_set.py", "--gold"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )

    assert result.returncode == 2, f"{result.stdout}\n{result.stderr}"
    assert "#274" in result.stderr
