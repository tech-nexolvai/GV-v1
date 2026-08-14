"""A placeholder tolerance must stay visible until somebody replaces it with a real number.

The failure this guards against already happened once, outside the code: a `±1/8″` placeholder
in a sample file — labelled *"PLACEHOLDER — please confirm your acceptable deviation"* — reached
`docs/RULE_ENGINE_SPEC.md` §4 and started being quoted as though the client had supplied it.
Nothing counted it, so nothing contradicted it.
"""

from __future__ import annotations

import pytest

from rules.publication import (
    NotProductionReadyError,
    assert_production_ready,
    awaiting_tolerance,
    is_production_ready,
    tolerance_report,
    unconfirmed_tolerance_count,
)
from rules.schema import (
    TOLERANCE_UNCONFIRMED,
    Applicability,
    ApplicabilityVariant,
    CheckType,
    GlobalApplicability,
    InputSelector,
    OperationRef,
    Rule,
    Tolerance,
)
from rules.semantic_types import OperandSource, ProductType, SemanticType
from rules.snapshot import SnapshotStore, publish
from units.measurement import Unit
from verdict.outcomes import Severity


def _rule(rule_id: str = "CT-WIDTH-001", **overrides: object) -> Rule:
    base: dict[str, object] = {
        "id": rule_id,
        "version": "1.0.0",
        "product_type": ProductType.COUNTERTOP,
        "check_type": CheckType.INTERNAL,
        "severity": Severity.CRITICAL,
        "arithmetic_unit": Unit.MM,
        "inputs": {
            "width": InputSelector(
                source=OperandSource.SHOP,
                semantic_type=SemanticType.COUNTERTOP_OVERALL_WIDTH,
            )
        },
        "applicability": GlobalApplicability(scope="global"),
        "operation": OperationRef(type="exists", operands={"x": "width"}),
    }
    base.update(overrides)
    return Rule(**base)  # type: ignore[arg-type]


def _with_tolerance(value: str, rule_id: str = "CT-WIDTH-001") -> Rule:
    return _rule(
        rule_id,
        operation=OperationRef(
            type="within_tolerance",
            operands={"actual": "width"},
            tolerance=(
                Tolerance(value=TOLERANCE_UNCONFIRMED)
                if value == TOLERANCE_UNCONFIRMED
                else Tolerance(value=value, unit=Unit.INCH)
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Production readiness
# ---------------------------------------------------------------------------


def test_a_confirmed_tolerance_is_production_ready() -> None:
    assert is_production_ready(_with_tolerance("1/8"))


def test_an_unconfirmed_tolerance_is_not_production_ready() -> None:
    assert not is_production_ready(_with_tolerance(TOLERANCE_UNCONFIRMED))


def test_a_rule_with_no_tolerance_at_all_is_ready() -> None:
    """`exists` and `equals` need none. Only a declared-but-unconfirmed tolerance holds a rule
    back — otherwise every tolerance-free check would be permanently unreleasable."""
    assert is_production_ready(_rule())


def test_releasing_an_unconfirmed_rule_raises() -> None:
    with pytest.raises(NotProductionReadyError, match="cannot decide anything"):
        assert_production_ready(_with_tolerance(TOLERANCE_UNCONFIRMED))


def test_the_error_explains_why_review_required_is_not_good_enough() -> None:
    """The distinction that makes this an error rather than a warning: 'a reviewer should look
    at this' and 'nobody has told us the limit' read identically in a report."""
    with pytest.raises(NotProductionReadyError) as err:
        assert_production_ready(_with_tolerance(TOLERANCE_UNCONFIRMED))
    assert "nobody has told us the limit" in str(err.value)


def test_releasing_a_confirmed_rule_does_not_raise() -> None:
    assert_production_ready(_with_tolerance("1/8"))


# ---------------------------------------------------------------------------
# Counting across variants
# ---------------------------------------------------------------------------


def test_every_variant_is_counted_not_just_the_first() -> None:
    """A rule half-confirmed is not confirmed. Counting only the first variant would release a
    rule whose island tolerance was still a guess."""
    rule = _rule(
        applicability=Applicability(
            discriminator="wall_config",
            variants=(
                ApplicabilityVariant(
                    when="back_left_right", tolerance=Tolerance(value="1/8", unit=Unit.INCH)
                ),
                ApplicabilityVariant(
                    when="island", tolerance=Tolerance(value=TOLERANCE_UNCONFIRMED)
                ),
                ApplicabilityVariant(
                    when="back_only", tolerance=Tolerance(value=TOLERANCE_UNCONFIRMED)
                ),
            ),
        )
    )
    assert unconfirmed_tolerance_count(rule) == 2
    assert not is_production_ready(rule)


def test_all_variants_confirmed_is_ready() -> None:
    rule = _rule(
        applicability=Applicability(
            discriminator="wall_config",
            variants=(
                ApplicabilityVariant(
                    when="back_left_right", tolerance=Tolerance(value="1/8", unit=Unit.INCH)
                ),
                ApplicabilityVariant(
                    when="island", tolerance=Tolerance(value="1/16", unit=Unit.INCH)
                ),
            ),
        )
    )
    assert is_production_ready(rule)


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


def _store(*rules: Rule) -> SnapshotStore:
    store = SnapshotStore()
    for r in rules:
        store.add(publish(r))
    return store


def test_the_report_names_every_rule_still_waiting() -> None:
    store = _store(
        _with_tolerance(TOLERANCE_UNCONFIRMED, "CT-WIDTH-001"),
        _with_tolerance(TOLERANCE_UNCONFIRMED, "CT-DEPTH-002"),
        _with_tolerance("1/8", "CT-SINK-003"),
    )
    waiting = awaiting_tolerance(store)
    assert [a.rule_id for a in waiting] == ["CT-DEPTH-002", "CT-WIDTH-001"]


def test_the_report_is_readable_by_someone_outside_the_codebase() -> None:
    """The point of the report is that a status update or a client email can quote it."""
    report = tolerance_report(_store(_with_tolerance(TOLERANCE_UNCONFIRMED), _rule("CT-EXISTS-9")))
    assert "1 of 2 published rule(s) cannot be released" in report
    assert "CT-WIDTH-001" in report
    assert "None of them can produce a PASS or a FAIL until the value arrives" in report


def test_the_report_says_so_when_nothing_is_waiting() -> None:
    assert "All 1 published rule(s) have client-confirmed" in tolerance_report(
        _store(_with_tolerance("1/8"))
    )


def test_an_empty_rulebook_does_not_claim_a_problem() -> None:
    assert "All 0 published rule(s)" in tolerance_report(SnapshotStore())


def test_the_report_uses_the_effective_version_not_every_historical_one() -> None:
    """The question is "what would block a release today?" — a superseded snapshot does not."""
    store = SnapshotStore()
    store.add(publish(_with_tolerance(TOLERANCE_UNCONFIRMED)))
    confirmed = _rule(
        "CT-WIDTH-001",
        version="1.0.1",
        operation=OperationRef(
            type="within_tolerance",
            operands={"actual": "width"},
            tolerance=Tolerance(value="1/8", unit=Unit.INCH),
        ),
    )
    store.add(publish(confirmed))

    assert awaiting_tolerance(store) == ()
    assert "All 1 published rule(s) have client-confirmed" in tolerance_report(store)


def test_the_count_survives_onto_the_report_line() -> None:
    rule = _rule(
        applicability=Applicability(
            discriminator="wall_config",
            variants=(
                ApplicabilityVariant(
                    when="island", tolerance=Tolerance(value=TOLERANCE_UNCONFIRMED)
                ),
                ApplicabilityVariant(
                    when="back_only", tolerance=Tolerance(value=TOLERANCE_UNCONFIRMED)
                ),
            ),
        )
    )
    line = str(awaiting_tolerance(_store(rule))[0])
    assert "2 tolerances awaiting a client value" in line
