"""A placeholder tolerance must stay visible until somebody replaces it with a real number.

The failure this guards against already happened once, outside the code: a `±1/8″` placeholder
in a sample file — labelled *"PLACEHOLDER — please confirm your acceptable deviation"* — reached
`docs/RULE_ENGINE_SPEC.md` §4 and started being quoted as though the client had supplied it.
Nothing counted it, so nothing contradicted it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from rules.publication import (
    NotProductionReadyError,
    assert_production_ready,
    awaiting_tolerance,
    is_production_ready,
    tolerance_report,
    unconfirmed_tolerance_count,
    unresolved_client_parameters,
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

RULEBOOK = Path(__file__).resolve().parents[2] / "rules" / "rulebook"


def _load_rulebook_rule(filename: str) -> Rule:
    """A real authored rule, so these assertions track the rulebook rather than a fixture."""
    return Rule.model_validate(yaml.safe_load((RULEBOOK / filename).read_text(encoding="utf-8")))


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


# ---------------------------------------------------------------------------
# The other way a rule cannot decide: a client-owed parameter with no value (#427)
# ---------------------------------------------------------------------------


def _rule_needing(scope: str, *, with_default: bool) -> Rule:
    """A minimal `exists` rule whose one parameter has the given scope.

    `exists` carries no tolerance, so anything these tests catch is the parameter check rather
    than the tolerance count leaking into it.
    """
    parameter: dict[str, object] = {"scope": scope}
    if with_default:
        parameter["default"] = {"value": "4", "unit": "in"}
    return Rule.model_validate(
        {
            "id": f"TEST-{scope.upper()}-{'DEFAULT' if with_default else 'BARE'}",
            "version": "1.0.0",
            "product_type": "countertop",
            "check_type": "internal",
            "severity": "CRITICAL",
            "arithmetic_unit": "in",
            "applicability": {"scope": "global"},
            "inputs": {
                "drawn": {
                    "source": "SHOP",
                    "semantic_type": "CT010",
                    "scope": "same_assembly",
                    "cardinality": "one",
                }
            },
            "parameters": {"the_value": parameter},
            "operation": {"type": "exists", "operands": {"x": "drawn"}},
        }
    )


def test_a_global_parameter_with_no_value_holds_a_rule_back() -> None:
    """Input: a rule needing a company standard nobody has supplied. Outcome: not releasable.

    It would return NOT FOUND for every drawing. Before #427 the gate counted only tolerances, so
    a rule like this reported ready — the failure this module's docstring describes, one field over.
    """
    rule = _rule_needing("global", with_default=False)

    assert unresolved_client_parameters(rule) == ("the_value",)
    assert not is_production_ready(rule)


@pytest.mark.parametrize("scope", ["project", "run"])
def test_routine_configuration_does_not_hold_a_rule_back(scope: str) -> None:
    """A cabinet depth or a sink dimension is supplied per project or per drawing set.

    Absent at publish says nothing about absent at run time. Flagging these would hold back almost
    the whole rulebook for values that are not due yet — the opposite mistake, and just as good at
    stopping work.
    """
    rule = _rule_needing(scope, with_default=False)

    assert unresolved_client_parameters(rule) == ()
    assert is_production_ready(rule)


def test_a_global_parameter_with_a_default_is_supplied_and_does_not_block() -> None:
    """The default *is* the value, confirmed by a human — `Parameter`'s own contract."""
    assert is_production_ready(_rule_needing("global", with_default=True))


def test_the_refusal_names_the_parameter_and_who_owes_it() -> None:
    """ "Not ready" is useless on its own: somebody has to know what to go and get."""
    with pytest.raises(NotProductionReadyError) as refusal:
        assert_production_ready(_rule_needing("global", with_default=False))

    message = str(refusal.value)
    assert "the_value" in message
    assert "client" in message
    assert "NOT FOUND" in message


def test_the_back_offset_rule_is_held_until_the_vendor_minimum_arrives() -> None:
    """The rule this check was written for, asserted against the real rulebook.

    Raj committed to the number — *"I will give a global minimum for that variable after checking
    with the vendor"* — so the rule is authored and waiting rather than unwritten, and the gate is
    what stops it reaching production in the meantime.
    """
    rule = _load_rulebook_rule("ct_back_offset_min_001.yaml")

    assert unresolved_client_parameters(rule) == ("back_offset_minimum",)
    assert not is_production_ready(rule)


def test_the_rules_that_only_need_project_configuration_are_still_releasable() -> None:
    """The regression that would matter most: #427 must not block the rulebook it was added to.

    CT-DEPTH-001 and CT-WIDTH-001 take a cabinet depth, an overhang and a field-cut size — all
    per-project configuration. An earlier draft of this check flagged them and would have held back
    most of the authored rules.
    """
    for filename in ("ct_depth_001.yaml", "ct_width_001.yaml", "ct_sink_cutout_depth_001.yaml"):
        rule = _load_rulebook_rule(filename)
        assert unresolved_client_parameters(rule) == (), filename
        assert is_production_ready(rule), filename
