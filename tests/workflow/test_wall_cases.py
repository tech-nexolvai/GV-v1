"""The countertop width checked for every wall case when the walls are not established (#1202).

Verification for `verdict/every_variant.py` and `workflow/wall_cases.py`, on the published
CT-WIDTH-001 rule. Every number is invented: pieces of 40" and 54" add up to 94", so the wall cases
need 94" (no end walls), 95" (one wall end) or 96" (walls at both ends) with a 1" field cut.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path

import pytest
import yaml

from app.verdicts.trace import calculation_trace
from rules.parameters import ParameterLayer, ParameterValue, Provenance, ResolvedParameter
from rules.schema import Quantity, Rule
from rules.snapshot import RuleSnapshot, publish
from units.measurement import Measurement, Unit
from verdict.every_variant import (
    combined_finding,
    execute_every_variant,
    trace_result,
    unanimous_outcome,
)
from verdict.operands import EvidenceStatus, VerdictOperand
from verdict.operations import register_all
from verdict.outcomes import Outcome
from verdict.registry import REGISTRY
from vocabulary.wall_layouts import NOT_NEEDED_FAIL_LINE
from workflow.wall_cases import is_every_wall_case, stored_wall_cases, wall_free_finding

RULE_PATH = Path(__file__).resolve().parents[2] / "rules" / "rulebook" / "ct_width_001.yaml"
WAITING = "Choose the wall layout for this countertop row."


@pytest.fixture(autouse=True)
def _registered_operations() -> Iterator[None]:
    previous = dict(REGISTRY)
    REGISTRY.clear()
    register_all()
    yield
    REGISTRY.clear()
    REGISTRY.update(previous)


def _snapshot() -> RuleSnapshot:
    rule = Rule.model_validate(yaml.safe_load(RULE_PATH.read_text(encoding="utf-8")))
    return publish(rule)


def _inch(value: int | Fraction) -> Measurement:
    return Measurement(Fraction(value), Unit.INCH, str(value))


def _operand(name: str, value: object) -> VerdictOperand:
    return VerdictOperand(
        name=name,
        value=value,  # type: ignore[arg-type]
        status=EvidenceStatus.CORROBORATED,
        source="SHOP",
        evidence_ref=f"shop:p1:{name}",
    )


def _operands(
    overall: int | Fraction, pieces: tuple[int, ...] = (40, 54)
) -> dict[str, VerdictOperand]:
    return {
        "countertop_width": _operand("countertop_width", _inch(overall)),
        "piece_widths": _operand("piece_widths", tuple(_inch(piece) for piece in pieces)),
    }


def _field_cut(value: int | Fraction = 1) -> dict[str, ResolvedParameter]:
    return {
        "field_cut": ResolvedParameter(
            name="field_cut",
            value=ParameterValue(
                value=Quantity(value=value, unit=Unit.INCH),
                provenance=Provenance.COMPANY_STANDARD,
                set_by="GV",
                set_at=datetime(2026, 10, 13, tzinfo=UTC),
            ),
            layer=ParameterLayer.PROJECT,
        )
    }


def test_every_published_wall_case_is_evaluated_by_the_engine_in_the_rules_order() -> None:
    outcomes = execute_every_variant(_snapshot(), _operands(95), _field_cut())
    assert [item.variant for item in outcomes] == [
        "back_left_right",
        "back_and_left",
        "back_and_right",
        "back_only",
        "island",
    ]
    expected = [trace_result(item.finding, "expected_width") for item in outcomes]
    assert [value.exact for value in expected if isinstance(value, Measurement)] == [
        96,
        95,
        95,
        94,
        94,
    ]
    assert [item.finding.outcome for item in outcomes] == [
        Outcome.FAIL,
        Outcome.PASS,
        Outcome.PASS,
        Outcome.FAIL,
        Outcome.FAIL,
    ]
    assert unanimous_outcome(outcomes) is Outcome.REVIEW_REQUIRED


def test_wrong_for_every_wall_case_is_a_fail_that_says_so_in_plain_words() -> None:
    finding = wall_free_finding(
        _snapshot(), _operands(99), _field_cut(), discriminators={}, waiting=WAITING
    )
    assert finding.outcome is Outcome.FAIL
    assert finding.reason == (
        'Wrong whatever the walls: it should be 94", 95" or 96" depending on the walls; '
        'the vendor wrote 99".'
    )
    assert f"Wall layout: {NOT_NEEDED_FAIL_LINE}." in finding.notes
    assert finding.variant is None
    assert finding.trace is not None and finding.trace.outcome is Outcome.FAIL


def test_a_vendor_overall_under_the_pieces_also_fails_whatever_the_walls() -> None:
    finding = wall_free_finding(
        _snapshot(), _operands(90), _field_cut(), discriminators={}, waiting=WAITING
    )
    assert finding.outcome is Outcome.FAIL
    assert 'the vendor wrote 90"' in finding.reason


def test_right_for_every_wall_case_is_a_pass() -> None:
    """Only possible with no field cut at all: every case needs the pieces' own total."""
    finding = wall_free_finding(
        _snapshot(), _operands(94), _field_cut(0), discriminators={}, waiting=WAITING
    )
    assert finding.outcome is Outcome.PASS
    assert finding.reason.startswith('Right whatever the walls: it should be 94" in every')


def test_right_for_some_cases_still_needs_the_walls_and_keeps_every_case() -> None:
    finding = wall_free_finding(
        _snapshot(), _operands(95), _field_cut(), discriminators={}, waiting=WAITING
    )
    assert finding.outcome is Outcome.REVIEW_REQUIRED
    assert finding.reason.startswith(WAITING)
    assert 'right with a wall at one end (95")' in finding.reason
    assert 'wrong with no end walls (94") or walls at both ends (96")' in finding.reason
    # Not decided, yet the record says what each case would have given.
    assert finding.trace is not None and finding.trace.outcome is Outcome.REVIEW_REQUIRED


def test_a_case_the_engine_cannot_work_out_leaves_the_walls_needed() -> None:
    """No field cut set anywhere: every case abstains, so nothing is decided."""
    finding = wall_free_finding(_snapshot(), _operands(99), {}, discriminators={}, waiting=WAITING)
    assert finding.outcome is Outcome.REVIEW_REQUIRED
    assert finding.reason == WAITING
    assert finding.trace is None


def test_the_stored_trace_gives_every_case_back_exactly() -> None:
    finding = wall_free_finding(
        _snapshot(),
        _operands(Fraction(191, 2)),
        _field_cut(Fraction(3, 4)),
        discriminators={},
        waiting=WAITING,
    )
    assert finding.trace is not None
    stored = calculation_trace(finding.trace, outcome=finding.outcome)
    assert is_every_wall_case(stored)
    cases = {case.layout: case for case in stored_wall_cases(stored)}
    assert list(cases) == [
        "back_left_right",
        "back_and_left",
        "back_and_right",
        "back_only",
        "island",
    ]
    both_ends = cases["back_left_right"]
    assert both_ends.expected == Fraction(191, 2)
    assert both_ends.outcome is Outcome.PASS
    assert both_ends.field_cut_count == 2
    assert both_ends.field_cut_per_end == Fraction(3, 4)
    assert both_ends.field_cut_total == Fraction(3, 2)
    assert both_ends.row_width == 94
    assert cases["back_and_left"].expected == Fraction(379, 4)
    assert cases["island"].expected == 94 and cases["island"].field_cut_count == 0


def test_an_every_case_trace_that_cannot_be_read_back_says_so() -> None:
    """#1202 review: never a silently empty or partial list of cases."""
    from workflow.wall_cases import UNREADABLE_CASES, read_wall_cases

    finding = wall_free_finding(
        _snapshot(), _operands(99), _field_cut(), discriminators={}, waiting=WAITING
    )
    assert finding.trace is not None
    stored = calculation_trace(finding.trace, outcome=finding.outcome)
    assert read_wall_cases(stored)[1] is None
    broken_outcome = {
        **stored,
        "intermediates": [
            [name, "MAYBE" if name == "island.outcome" else value]
            for name, value in stored["intermediates"]
        ],
    }
    assert read_wall_cases(broken_outcome) == ((), UNREADABLE_CASES)
    no_expected = {
        **stored,
        "intermediates": [
            pair for pair in stored["intermediates"] if pair[0] != "back_only.expected_width"
        ],
    }
    assert read_wall_cases(no_expected) == ((), UNREADABLE_CASES)
    assert read_wall_cases({**stored, "intermediates": []}) == ((), UNREADABLE_CASES)
    assert stored_wall_cases(broken_outcome) == ()


def test_an_ordinary_trace_is_not_read_as_wall_cases() -> None:
    assert stored_wall_cases({"operation": "equals", "intermediates": []}) == ()
    assert stored_wall_cases(None) == ()
    assert not is_every_wall_case({"cause": "missing", "reason": "x"})


def test_combining_needs_at_least_one_case() -> None:
    from verdict.engine import EngineError

    with pytest.raises(EngineError):
        combined_finding(_snapshot(), (), reason="nothing")
