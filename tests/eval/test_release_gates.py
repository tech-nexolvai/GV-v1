"""Verification for issue #70: every release gate is explicit and blocking."""

from decimal import Decimal

from eval.release_gates import (
    ADVISORY_GATE,
    GATE_IDS,
    GOLD_REGRESSION_GATE,
    LOCALISATION_GATE,
    SAFETY_METRICS_GATE,
    GateReport,
    GateResult,
    GateStatus,
    ReleaseGateInputs,
    run_gates,
)


def _by_id(report: GateReport) -> dict[str, GateResult]:
    return {result.gate_id: result for result in report.results}


def test_runner_always_reports_all_seven_gates_in_stable_order() -> None:
    report = run_gates(ReleaseGateInputs())

    assert tuple(result.gate_id for result in report.results) == GATE_IDS
    assert all(result.reason for result in report.results)


def test_invariant_probes_execute_and_expose_the_current_advisory_gap() -> None:
    results = _by_id(run_gates(ReleaseGateInputs()))

    assert [results[gate_id].status for gate_id in GATE_IDS[:3]] == [GateStatus.PASS] * 3
    assert results[ADVISORY_GATE].status is GateStatus.FAIL
    assert "RETRIEVAL" in results[ADVISORY_GATE].reason


def test_missing_metrics_or_thresholds_are_not_evaluated_never_passed() -> None:
    results = _by_id(run_gates(ReleaseGateInputs()))

    assert results[LOCALISATION_GATE].status is GateStatus.NOT_EVALUATED
    assert results[GOLD_REGRESSION_GATE].status is GateStatus.NOT_EVALUATED
    assert results[SAFETY_METRICS_GATE].status is GateStatus.NOT_EVALUATED


def test_localisation_uses_the_declared_threshold_inclusively() -> None:
    at_boundary = run_gates(
        ReleaseGateInputs(
            metrics={"evidence_localisation_rate": Decimal("0.95")},
            thresholds={"evidence_localisation_rate": Decimal("0.95")},
        )
    )
    below = run_gates(
        ReleaseGateInputs(
            metrics={"evidence_localisation_rate": Decimal("0.949")},
            thresholds={"evidence_localisation_rate": Decimal("0.95")},
        )
    )

    assert _by_id(at_boundary)[LOCALISATION_GATE].status is GateStatus.PASS
    assert _by_id(below)[LOCALISATION_GATE].status is GateStatus.FAIL


def test_only_a_complete_unskipped_gold_run_with_snapshot_ids_passes() -> None:
    complete = ReleaseGateInputs(
        gold_set_version="12",
        metrics={
            "gold_manifest_case_ids": ("CASE-1", "CASE-2"),
            "gold_executed_case_ids": ("CASE-2", "CASE-1"),
            "gold_skipped_case_ids": (),
            "rule_snapshot_ids": ("sha256:abc",),
        },
    )
    partial = ReleaseGateInputs(
        gold_set_version="12",
        metrics={
            "gold_manifest_case_ids": ("CASE-1", "CASE-2"),
            "gold_executed_case_ids": ("CASE-1",),
            "gold_skipped_case_ids": ("CASE-2",),
            "rule_snapshot_ids": ("sha256:abc",),
        },
    )

    assert _by_id(run_gates(complete))[GOLD_REGRESSION_GATE].status is GateStatus.PASS
    assert _by_id(run_gates(partial))[GOLD_REGRESSION_GATE].status is GateStatus.NOT_EVALUATED


def test_safety_metrics_apply_minimums_and_false_pass_maximum() -> None:
    thresholds = {
        "critical_false_pass_rate": Decimal(0),
        "numeric_exact_match_accuracy": Decimal("0.98"),
        "unit_exact_match_accuracy": Decimal("0.99"),
        "identifier_match_precision": Decimal("0.97"),
    }
    passing = dict(thresholds)
    failing = dict(thresholds)
    failing["critical_false_pass_rate"] = Decimal("0.01")

    assert (
        _by_id(run_gates(ReleaseGateInputs(metrics=passing, thresholds=thresholds)))[
            SAFETY_METRICS_GATE
        ].status
        is GateStatus.PASS
    )
    assert (
        _by_id(run_gates(ReleaseGateInputs(metrics=failing, thresholds=thresholds)))[
            SAFETY_METRICS_GATE
        ].status
        is GateStatus.FAIL
    )


def test_not_evaluated_and_fail_both_block_shipping_and_optimisation() -> None:
    not_evaluated = GateReport((GateResult("one", GateStatus.NOT_EVALUATED, "missing"),))
    failed = GateReport((GateResult("one", GateStatus.FAIL, "failed"),))

    assert not not_evaluated.ships
    assert not not_evaluated.optimisation_allowed
    assert not failed.ships
    assert not failed.optimisation_allowed


def test_even_seven_passes_must_be_the_complete_named_gate_set() -> None:
    incomplete = GateReport(
        tuple(GateResult(str(index), GateStatus.PASS, "passed") for index in range(7))
    )
    complete = GateReport(
        tuple(GateResult(gate_id, GateStatus.PASS, "passed") for gate_id in GATE_IDS)
    )

    assert not incomplete.ships
    assert complete.ships
    assert complete.optimisation_allowed
