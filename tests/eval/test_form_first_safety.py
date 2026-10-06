"""Synthetic verification for the form-first CT-WIDTH-001 safety measurement."""

from fractions import Fraction

from eval.form_first_safety import (
    SafetyCase,
    WidthInputs,
    adversarial_guards,
    evaluate,
    published_ct_width_snapshot,
    run_width_check,
)
from eval.release_gates import GOLD_REGRESSION_GATE, GateStatus, ReleaseGateInputs, run_gates
from verdict.outcomes import Outcome


def _inputs(
    *,
    overall: Fraction = Fraction(6),
    cabinets: tuple[Fraction, ...] | None = (Fraction(4),),
    fillers: tuple[Fraction, ...] | None = (Fraction(2),),
    field_cut: Fraction | None = Fraction(1),
):
    return WidthInputs(
        overall=overall,
        cabinets=cabinets,
        fillers=fillers,
        wall_layout="back_only",
        field_cut=field_cut,
    )


def test_truth_fail_and_proposed_pass_are_counted_as_a_false_pass() -> None:
    report = evaluate(
        (
            SafetyCase(
                "synthetic-countertop",
                truth=_inputs(overall=Fraction(7)),
                proposed=_inputs(),
                raw_attempts_complete=True,
            ),
        )
    )

    assert report.measured == 1
    assert report.unaccounted == 0
    assert report.false_passes == 1
    assert report.false_negatives == 0
    assert report.zero_false_pass is False
    assert report.scores[0].truth_outcome is Outcome.FAIL
    assert report.scores[0].proposed_outcome is Outcome.PASS


def test_truth_pass_and_proposed_fail_are_reported_separately() -> None:
    report = evaluate(
        (
            SafetyCase(
                "synthetic-countertop",
                truth=_inputs(),
                proposed=_inputs(overall=Fraction(7)),
                raw_attempts_complete=True,
            ),
        )
    )

    assert report.false_passes == 0
    assert report.false_negatives == 1
    assert report.scores[0].truth_outcome is Outcome.PASS
    assert report.scores[0].proposed_outcome is Outcome.FAIL


def test_missing_filler_runs_the_production_rule_and_cannot_pass() -> None:
    finding = run_width_check(_inputs(fillers=None), published_ct_width_snapshot())

    assert finding.outcome is Outcome.NOT_FOUND
    assert finding.trace is None


def test_missing_wall_layout_abstains_in_the_production_rule() -> None:
    finding = run_width_check(
        WidthInputs(
            overall=Fraction(6),
            cabinets=(Fraction(4),),
            fillers=(Fraction(2),),
            wall_layout=None,
            field_cut=Fraction(1),
        ),
        published_ct_width_snapshot(),
    )

    assert finding.outcome is Outcome.REVIEW_REQUIRED
    assert finding.trace is None


def test_missing_field_cut_stays_unaccounted_not_zero_error() -> None:
    report = evaluate(
        (
            SafetyCase(
                "missing-project-input",
                truth=_inputs(),
                proposed=_inputs(field_cut=None),
                raw_attempts_complete=True,
            ),
        )
    )

    assert report.measured == 0
    assert report.unaccounted == 1
    assert report.false_passes == 0
    assert report.zero_false_pass is False
    assert report.scores[0].reason is not None


def test_missing_per_attempt_answers_are_unaccounted_not_zero_wrong() -> None:
    report = evaluate(
        (
            SafetyCase(
                "raw-attempts-not-persisted",
                truth=_inputs(overall=Fraction(7)),
                proposed=_inputs(),
                raw_attempts_complete=False,
            ),
        )
    )

    assert report.measured == 0
    assert report.unaccounted == 1
    assert report.false_passes == 0
    assert "raw reader answers" in (report.scores[0].reason or "")
    assert report.zero_false_pass is False


def test_safety_report_carries_the_exact_production_rule_snapshot() -> None:
    snapshot = published_ct_width_snapshot()
    report = evaluate(
        (
            SafetyCase(
                "complete",
                truth=_inputs(),
                proposed=_inputs(),
                raw_attempts_complete=True,
            ),
        ),
        snapshot,
    )

    assert report.snapshot_id == snapshot.snapshot_id
    assert report.reader_path == "whole_page"
    assert report.zero_false_pass


def test_slot_crop_run_is_labelled_as_a_separate_measurement_path() -> None:
    report = evaluate(
        (
            SafetyCase(
                "slot-crop-case",
                truth=_inputs(),
                proposed=_inputs(),
                raw_attempts_complete=True,
            ),
        ),
        reader_path="slot_crop",
    )

    assert report.reader_path == "slot_crop"


def test_current_release_gate_does_not_pass_without_complete_manifest_and_thresholds() -> None:
    report = {result.gate_id: result for result in run_gates(ReleaseGateInputs()).results}

    assert report[GOLD_REGRESSION_GATE].status is GateStatus.NOT_EVALUATED


def test_adversarial_inventory_names_all_eight_existing_guards() -> None:
    entries = adversarial_guards()

    assert len(entries) == 8
    assert len({case for case, _guard in entries}) == 8
    assert all(guard for _case, guard in entries)
    assert "GV red corrections add up perfectly" in dict(entries)
    assert "vendor label covered by a GV box" in dict(entries)
