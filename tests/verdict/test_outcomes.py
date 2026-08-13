"""Outcome and Severity are part of the persisted data contract.

These values are stored inside findings, so a rename is not a refactor — it invalidates
every finding already on record. The tests below pin the exact strings deliberately.
"""

from __future__ import annotations

import pytest

from verdict.outcomes import (
    ABSTAINING_OUTCOMES,
    DECISIVE_OUTCOMES,
    Outcome,
    Severity,
    is_abstention,
    is_decision,
)


def test_outcome_has_exactly_the_five_specified_values() -> None:
    assert {o.value for o in Outcome} == {
        "PASS",
        "FAIL",
        "NOT_FOUND",
        "REVIEW_REQUIRED",
        "NO_APPLICABLE_RULE",
    }


def test_severity_has_exactly_the_four_specified_values() -> None:
    assert {s.value for s in Severity} == {"CRITICAL", "MAJOR", "MINOR", "ADVISORY"}


def test_no_applicable_rule_is_distinct_from_pass_and_not_found() -> None:
    """ADR-0004. If this ever aliases PASS, an unchecked package renders as clean — the
    most dangerous false PASS available, because it leaves nothing to audit."""
    assert Outcome.NO_APPLICABLE_RULE is not Outcome.PASS
    assert Outcome.NO_APPLICABLE_RULE is not Outcome.NOT_FOUND
    assert Outcome.NO_APPLICABLE_RULE.value != Outcome.PASS.value


def test_decision_and_abstention_partition_every_outcome() -> None:
    """Every outcome must fall in exactly one bucket. A new member that lands in neither
    would be silently excluded from both the false-PASS metric and coverage."""
    assert DECISIVE_OUTCOMES | ABSTAINING_OUTCOMES == set(Outcome)
    assert DECISIVE_OUTCOMES & ABSTAINING_OUTCOMES == set()


@pytest.mark.parametrize("outcome", [Outcome.PASS, Outcome.FAIL])
def test_pass_and_fail_are_decisions(outcome: Outcome) -> None:
    assert is_decision(outcome)
    assert not is_abstention(outcome)


@pytest.mark.parametrize(
    "outcome",
    [Outcome.NOT_FOUND, Outcome.REVIEW_REQUIRED, Outcome.NO_APPLICABLE_RULE],
)
def test_the_three_abstentions_are_not_decisions(outcome: Outcome) -> None:
    """A missing value, a conflict and an unwritten rule are all reasons not to answer.
    None of them may be counted as the engine having decided anything."""
    assert is_abstention(outcome)
    assert not is_decision(outcome)


def test_values_are_stable_strings_suitable_for_persistence() -> None:
    """StrEnum members compare equal to their stored string, so a finding read back from
    the database round-trips without a lookup table."""
    assert Outcome.PASS == "PASS"
    assert Severity.CRITICAL == "CRITICAL"
    assert Outcome("NO_APPLICABLE_RULE") is Outcome.NO_APPLICABLE_RULE
    assert Severity("ADVISORY") is Severity.ADVISORY


def test_unknown_value_is_rejected() -> None:
    """A typo in stored data must raise rather than resolve to something plausible."""
    with pytest.raises(ValueError):
        Outcome("PASSED")
    with pytest.raises(ValueError):
        Severity("BLOCKER")
