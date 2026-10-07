"""Evaluation-only replay for form-first CT-WIDTH-001 proposals.

This module deliberately does not read database rows or write review data. Callers provide an
immutable projection of the private confirmed key and saved, pre-review proposals. Both sides are
evaluated by the currently published CT-WIDTH-001 rule; missing reviewer inputs remain
unaccounted, rather than being filled from a default or treated as a safe result.

The production rule is a V1 ``FLAG`` rule, so the metric name is explicit: this is the CT-WIDTH-001
false-PASS safety count requested for the form-first experiment, not the repository's
``critical_false_pass_rate`` severity metric.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import Final, Literal

import yaml  # type: ignore[import-untyped]  # PyYAML does not publish inline type information.

from eval.release_gates import ReleaseGateInputs
from rules.parameters import ParameterLayer, ParameterValue, Provenance, ResolvedParameter
from rules.schema import Quantity, Rule
from rules.semantic_types import SemanticType
from rules.snapshot import RuleSnapshot, publish
from units.measurement import Measurement, Unit
from verdict.engine import execute
from verdict.finding import Finding
from verdict.operands import EvidenceStatus, VerdictOperand
from verdict.operations import register_all
from verdict.outcomes import Outcome

CT_WIDTH_RULE: Final = Path(__file__).resolve().parents[1] / "rules/rulebook/ct_width_001.yaml"
_WALL_LAYOUTS: Final = frozenset({"back_left_right", "back_only", "island"})
ReaderPath = Literal["whole_page", "slot_crop"]


@dataclass(frozen=True, slots=True)
class WidthInputs:
    """Exact inputs for one countertop, sourced from either key truth or saved proposals."""

    overall: Fraction | None
    cabinets: tuple[Fraction, ...] | None
    fillers: tuple[Fraction, ...] | None
    wall_layout: str | None
    field_cut: Fraction | None
    pieces: tuple[Fraction, ...] | None = None

    @property
    def complete(self) -> bool:
        return (
            self.overall is not None
            and (bool(self.pieces) or (bool(self.cabinets) and bool(self.fillers)))
            and self.wall_layout in _WALL_LAYOUTS
            and self.field_cut is not None
        )


@dataclass(frozen=True, slots=True)
class SafetyCase:
    """One aligned truth/proposal pair. Missing records are explicit, never interpreted as zero."""

    case_id: str
    truth: WidthInputs | None
    proposed: WidthInputs | None
    raw_attempts_complete: bool
    unaccounted_reason: str | None = None


@dataclass(frozen=True, slots=True)
class CaseScore:
    case_id: str
    truth_outcome: Outcome | None
    proposed_outcome: Outcome | None
    false_pass: bool
    false_negative: bool
    measured: bool
    reason: str | None


@dataclass(frozen=True, slots=True)
class SafetyReport:
    reader_path: ReaderPath
    scores: tuple[CaseScore, ...]
    measured: int
    unaccounted: int
    false_passes: int
    false_negatives: int
    snapshot_id: str

    @property
    def zero_false_pass(self) -> bool:
        """Only a complete measured set with no false-PASS can claim zero."""
        return self.unaccounted == 0 and self.false_passes == 0


def published_ct_width_snapshot(path: Path = CT_WIDTH_RULE) -> RuleSnapshot:
    """Load the checked-in published rule text and pin it with the production snapshot hash."""
    rule = Rule.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    if rule.id != "CT-WIDTH-001":
        raise ValueError("form-first safety evaluation requires CT-WIDTH-001")
    return publish(rule)


def _measure(value: Fraction | None, name: str) -> VerdictOperand:
    quantity = None if value is None else Measurement(value, Unit.INCH, str(value))
    return VerdictOperand(
        name=name,
        value=quantity,
        status=EvidenceStatus.CORROBORATED,
        source="SHOP",
        evidence_ref=f"form-first-eval:{name}",
    )


def _parameter(value: Fraction | None) -> dict[str, ResolvedParameter]:
    if value is None:
        return {}
    return {
        "field_cut": ResolvedParameter(
            name="field_cut",
            value=ParameterValue(
                value=Quantity(value=value, unit=Unit.INCH),
                provenance=Provenance.MEASURED,
                set_by="form-first evaluation input",
                set_at=datetime(2000, 1, 1, tzinfo=UTC),
            ),
            layer=ParameterLayer.RUN,
        )
    }


def run_width_check(inputs: WidthInputs, snapshot: RuleSnapshot) -> Finding:
    """Replay a single side through the unchanged deterministic verdict engine."""
    register_all()
    if inputs.wall_layout is not None and inputs.wall_layout not in _WALL_LAYOUTS:
        raise ValueError("wall layout must be one of the published CT-WIDTH-001 choices")
    operands = {"countertop_width": _measure(inputs.overall, "countertop_width")}
    if inputs.pieces is not None:
        operands["piece_widths"] = VerdictOperand(
            name="piece_widths",
            value=tuple(Measurement(v, Unit.INCH, str(v)) for v in inputs.pieces),
            status=EvidenceStatus.CORROBORATED,
            source="SHOP",
            evidence_ref="form-first-eval:piece_widths",
        )
    if inputs.cabinets is not None:
        operands["cabinet_widths"] = VerdictOperand(
            name="cabinet_widths",
            value=tuple(Measurement(v, Unit.INCH, str(v)) for v in inputs.cabinets),
            status=EvidenceStatus.CORROBORATED,
            source="SHOP",
            evidence_ref="form-first-eval:cabinet_widths",
        )
    if inputs.fillers is not None:
        operands["filler_widths"] = VerdictOperand(
            name="filler_widths",
            value=tuple(Measurement(v, Unit.INCH, str(v)) for v in inputs.fillers),
            status=EvidenceStatus.CORROBORATED,
            source="SHOP",
            evidence_ref="form-first-eval:filler_widths",
        )
    return execute(
        snapshot,
        operands,
        _parameter(inputs.field_cut),
        discriminators=(
            {}
            if inputs.wall_layout is None
            else {SemanticType.WALL_CONFIG.value: inputs.wall_layout}
        ),
    )


def evaluate(
    cases: tuple[SafetyCase, ...],
    snapshot: RuleSnapshot | None = None,
    *,
    reader_path: ReaderPath = "whole_page",
) -> SafetyReport:
    """Compare exact key truth with no-manual-review proposals; incomplete cases stay unaccounted."""
    if not cases:
        raise ValueError("at least one form-first safety case is required")
    if len({case.case_id for case in cases}) != len(cases):
        raise ValueError("form-first safety case ids must be unique")
    pinned = snapshot or published_ct_width_snapshot()
    scores: list[CaseScore] = []
    for case in cases:
        reason = case.unaccounted_reason
        if reason is None and not case.raw_attempts_complete:
            reason = "per-attempt raw reader answers are unavailable"
        truth_inputs = case.truth
        proposed_inputs = case.proposed
        if reason is None and (truth_inputs is None or proposed_inputs is None):
            reason = "truth or saved no-review proposal is missing"
        if (
            reason is None
            and truth_inputs is not None
            and proposed_inputs is not None
            and (not truth_inputs.complete or not proposed_inputs.complete)
        ):
            reason = "same-countertop layout, field cut, or complete width chain is missing"
        if reason is not None:
            scores.append(CaseScore(case.case_id, None, None, False, False, False, reason))
            continue
        assert truth_inputs is not None and proposed_inputs is not None
        truth = run_width_check(truth_inputs, pinned).outcome
        proposed = run_width_check(proposed_inputs, pinned).outcome
        decisive = truth in (Outcome.PASS, Outcome.FAIL) and proposed in (
            Outcome.PASS,
            Outcome.FAIL,
        )
        if not decisive:
            scores.append(
                CaseScore(
                    case.case_id,
                    truth,
                    proposed,
                    False,
                    False,
                    False,
                    "the production rule abstained on truth or proposed inputs",
                )
            )
            continue
        scores.append(
            CaseScore(
                case_id=case.case_id,
                truth_outcome=truth,
                proposed_outcome=proposed,
                false_pass=truth is Outcome.FAIL and proposed is Outcome.PASS,
                false_negative=truth is Outcome.PASS and proposed is Outcome.FAIL,
                measured=True,
                reason=None,
            )
        )
    measured = sum(score.measured for score in scores)
    unaccounted = len(scores) - measured
    return SafetyReport(
        reader_path=reader_path,
        scores=tuple(scores),
        measured=measured,
        unaccounted=unaccounted,
        false_passes=sum(score.false_pass for score in scores),
        false_negatives=sum(score.false_negative for score in scores),
        snapshot_id=pinned.snapshot_id,
    )


def adversarial_guards() -> tuple[tuple[str, str], ...]:
    """The synthetic threat inventory and the existing guard expected to block each case."""
    return (
        ("missing filler", "complete input gate / CT-WIDTH-001 on_missing"),
        ("stacked fraction misread", "form-reader stacked-fraction review guard"),
        ("wall-scope overall", "form-reader overall_scope=run agreement guard"),
        ("two countertops on one page", "single-countertop mapping guard"),
        ("garbled reader answer", "strict answer schema and exact-text parser"),
        ("GV red corrections add up perfectly", "GV-ink guard at the evidence boundary"),
        ("vendor label covered by a GV box", "covered-by-reviewer-markup guard"),
        ("label cut off at drawing edge", "unknown/partial location refuses corroboration"),
    )


def release_gate_record(report: SafetyReport, *, gold_set_version: str) -> ReleaseGateInputs:
    """Supply honest manifest accounting; never invent other release metrics or thresholds.

    The full gate still needs localisation, numeric/unit precision, and approved thresholds.
    This adapter only lets its gold-regression gate tell complete from partial form-first replay.
    """
    if not gold_set_version.strip():
        raise ValueError("a confirmed gold-set version is required")
    return ReleaseGateInputs(
        gold_set_version=gold_set_version,
        metrics={
            "gold_manifest_case_ids": tuple(score.case_id for score in report.scores),
            "gold_executed_case_ids": tuple(
                score.case_id for score in report.scores if score.measured
            ),
            "gold_skipped_case_ids": tuple(
                score.case_id for score in report.scores if not score.measured
            ),
            "rule_snapshot_ids": (report.snapshot_id,),
        },
    )


__all__ = [
    "CaseScore",
    "ReaderPath",
    "SafetyCase",
    "SafetyReport",
    "WidthInputs",
    "adversarial_guards",
    "evaluate",
    "published_ct_width_snapshot",
    "release_gate_record",
    "run_width_check",
]
