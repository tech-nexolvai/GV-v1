"""Verification for issue #50: identifier-keyed pairwise comparison."""

from __future__ import annotations

from fractions import Fraction

import pytest

from units.measurement import Measurement, MixedUnitError, Unit
from verdict.operations.pairwise import (
    PAIRWISE_SPECS,
    CountComparison,
    PairComparison,
    pairwise_within_tolerance,
)
from verdict.outcomes import Outcome
from verdict.registry import Arity, RuleAuthoringError


def mm(value: int | Fraction) -> Measurement:
    return Measurement(Fraction(value), Unit.MM, str(value))


def inch(value: int | Fraction) -> Measurement:
    return Measurement(Fraction(value), Unit.INCH, str(value))


def pair_results(result: object) -> list[PairComparison]:
    intermediates = result.intermediates  # type: ignore[attr-defined]
    return [value for name, value in intermediates if name.startswith("pair[")]


def test_pairs_by_identifier_not_mapping_position() -> None:
    left = {"CAB-1": mm(600), "CAB-2": mm(800)}
    right = {"CAB-2": mm(800), "CAB-1": mm(600)}

    result = pairwise_within_tolerance(left=left, right=right, tolerance=mm(0))

    assert result.outcome is Outcome.PASS
    assert [pair.identifier for pair in pair_results(result)] == ["CAB-1", "CAB-2"]
    assert all(
        pair.delta == Measurement(Fraction(0), Unit.MM, None) for pair in pair_results(result)
    )


@pytest.mark.parametrize(
    ("right_value", "outcome"),
    [(mm(598), Outcome.PASS), (mm(597), Outcome.FAIL)],
)
def test_pair_tolerance_boundary_is_inclusive(right_value: Measurement, outcome: Outcome) -> None:
    result = pairwise_within_tolerance(
        left={"CAB-1": mm(600)},
        right={"CAB-1": right_value},
        tolerance=mm(2),
    )
    pair = pair_results(result)[0]
    assert pair.outcome is outcome
    assert result.outcome is outcome


def test_count_mismatch_and_unmatched_identifier_are_individually_visible() -> None:
    result = pairwise_within_tolerance(
        left={"CAB-1": mm(600), "CAB-2": mm(800)},
        right={"CAB-1": mm(600)},
        tolerance=mm(0),
    )

    count_result = result.intermediates[0][1]
    assert count_result == CountComparison(
        left_count=2,
        right_count=1,
        outcome=Outcome.NOT_FOUND,
        comparison="left count 2 != right count 1",
    )
    assert result.outcome is Outcome.NOT_FOUND
    missing = pair_results(result)[1]
    assert missing.identifier == "CAB-2"
    assert missing.left == mm(800)
    assert missing.right is None
    assert missing.delta is None
    assert missing.outcome is Outcome.NOT_FOUND


def test_equal_counts_with_different_keys_still_report_each_unmatched_item() -> None:
    result = pairwise_within_tolerance(
        left={"CAB-1": mm(600)},
        right={"CAB-2": mm(600)},
        tolerance=mm(0),
    )

    count_result = result.intermediates[0][1]
    assert isinstance(count_result, CountComparison)
    assert count_result.outcome is Outcome.PASS
    assert result.outcome is Outcome.NOT_FOUND
    pairs = pair_results(result)
    assert [(pair.identifier, pair.outcome) for pair in pairs] == [
        ("CAB-1", Outcome.NOT_FOUND),
        ("CAB-2", Outcome.NOT_FOUND),
    ]


def test_verified_failure_dominates_an_unmatched_pair_without_hiding_it() -> None:
    result = pairwise_within_tolerance(
        left={"CAB-1": mm(600), "CAB-2": mm(800)},
        right={"CAB-1": mm(590)},
        tolerance=mm(2),
    )

    assert result.outcome is Outcome.FAIL
    assert [(pair.identifier, pair.outcome) for pair in pair_results(result)] == [
        ("CAB-1", Outcome.FAIL),
        ("CAB-2", Outcome.NOT_FOUND),
    ]
    assert "1 fail" in result.comparison
    assert "1 not found" in result.comparison


def test_right_only_identifier_reports_the_missing_left_counterpart() -> None:
    result = pairwise_within_tolerance(
        left={},
        right={"CAB-1": mm(600)},
        tolerance=mm(0),
    )
    pair = pair_results(result)[0]
    assert pair.left is None
    assert pair.right == mm(600)
    assert "left counterpart not found" in pair.comparison


def test_trace_facts_list_every_pair_delta_and_outcome_in_sorted_order() -> None:
    result = pairwise_within_tolerance(
        left={"CAB-2": mm(800), "CAB-1": mm(600)},
        right={"CAB-2": mm(801), "CAB-1": mm(600)},
        tolerance=mm(1),
    )

    pairs = pair_results(result)
    assert [pair.identifier for pair in pairs] == ["CAB-1", "CAB-2"]
    assert [pair.delta for pair in pairs] == [
        Measurement(Fraction(0), Unit.MM, None),
        Measurement(Fraction(1), Unit.MM, None),
    ]
    assert [pair.outcome for pair in pairs] == [Outcome.PASS, Outcome.PASS]
    assert result.delta == Measurement(Fraction(1), Unit.MM, None)


def test_rejects_empty_comparison_instead_of_silently_passing() -> None:
    with pytest.raises(ValueError, match="at least one identifier"):
        pairwise_within_tolerance(left={}, right={}, tolerance=mm(1))


def test_rejects_mixed_units_including_an_unmatched_value() -> None:
    with pytest.raises(MixedUnitError):
        pairwise_within_tolerance(
            left={"CAB-1": mm(25)},
            right={"CAB-2": inch(1)},
            tolerance=mm(1),
        )


def test_rejects_negative_tolerance_and_wrong_mapping_shapes() -> None:
    with pytest.raises(RuleAuthoringError, match="negative"):
        pairwise_within_tolerance(left={"CAB-1": mm(1)}, right={"CAB-1": mm(1)}, tolerance=mm(-1))
    with pytest.raises(RuleAuthoringError, match="mapping"):
        pairwise_within_tolerance(
            left=[mm(1)], right={"CAB-1": mm(1)}, tolerance=mm(1)  # type: ignore[arg-type]
        )
    with pytest.raises(RuleAuthoringError, match="invalid identifier"):
        pairwise_within_tolerance(left={"": mm(1)}, right={}, tolerance=mm(1))


def test_registry_spec_requires_identifier_keyed_list_arity() -> None:
    assert len(PAIRWISE_SPECS) == 1
    spec = PAIRWISE_SPECS[0]
    assert spec.name == "pairwise_within_tolerance"
    assert spec.version == "1.0.0"
    assert spec.operands == {
        "left": Arity.LIST,
        "right": Arity.LIST,
        "tolerance": Arity.SCALAR,
    }
