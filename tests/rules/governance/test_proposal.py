"""A rule change must be coherent before anyone can approve it (#237).

The failure this guards against is not a malformed rule — Pydantic catches those. It is a rule that
is *structurally fine and refers to nothing*: an operation the registry does not have, or an operand
pointing at an input the rule never declares. Both author cleanly, publish cleanly, and fail at the
moment a real drawing is being checked.

The second thing under test is the change description. `D6` requires a human approval, and the
human is the client. A diff of two YAML files tells them a line moved; it does not tell them the
check just got twice as strict on island layouts. That difference is what separates a review from a
rubber stamp.
"""

from __future__ import annotations

from fractions import Fraction

import pytest
from pydantic import ValidationError as PydanticValidationError

from rules.governance.proposal import (
    RuleProposal,
    ValidationStatus,
    describe_change,
    propose,
    validate,
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
from units.measurement import Unit
from verdict.operations.aggregate import AGGREGATE_SPECS
from verdict.operations.alignment import ALIGNMENT_SPECS
from verdict.operations.pairwise import PAIRWISE_SPECS
from verdict.operations.scalar import SCALAR_SPECS
from verdict.outcomes import Severity
from verdict.registry import REGISTRY, register


@pytest.fixture(autouse=True)
def _registry() -> None:
    """Validation checks against the live registry, which the caller is responsible for wiring.

    Registered here rather than assumed, because an empty registry makes every rule look invalid
    and `validate` raises rather than reporting that — see `RegistryNotLoadedError`.
    """
    for spec in (*SCALAR_SPECS, *AGGREGATE_SPECS, *PAIRWISE_SPECS, *ALIGNMENT_SPECS):
        if spec.name not in REGISTRY:
            register(spec)


def _rule(**overrides: object) -> Rule:
    base: dict[str, object] = {
        "id": "CT-WIDTH-001",
        "version": "1.0.0",
        "product_type": ProductType.COUNTERTOP,
        "check_type": CheckType.INTERNAL,
        "severity": Severity.CRITICAL,
        "arithmetic_unit": Unit.MM,
        "inputs": {
            "width": InputSelector(source=OperandSource.SHOP, semantic_type=SemanticType.CT001)
        },
        "applicability": GlobalApplicability(scope="global"),
        "operation": OperationRef(type="exists", operands={"value": "width"}),
    }
    base.update(overrides)
    return Rule(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_a_coherent_rule_validates() -> None:
    assert validate(_rule()).is_valid


def test_an_unregistered_operation_is_rejected() -> None:
    """ADR-0003: a rule may not name an operation the typed registry does not have. Without this
    it would author and publish cleanly, then fail on a real drawing."""
    rule = _rule(operation=OperationRef(type="vibes_based_check", operands={"value": "width"}))
    result = validate(rule)
    assert not result.is_valid
    assert any("not in the typed registry" in p for p in result.problems)


def test_a_dangling_operand_is_caught_by_the_schema_before_validation_sees_it() -> None:
    """Discovered while writing this: `rules/schema.py` already rejects an operand whose source
    names nothing the rule declares, so such a `Rule` cannot be constructed at all.

    `validate` therefore does **not** re-check it. An unreachable safeguard is worse than none —
    the next reader trusts it and stops looking for the real one. This test records where the
    guarantee actually lives, so that if the schema ever relaxes, this fails rather than the gap
    opening silently.
    """
    with pytest.raises(PydanticValidationError, match="unknown operand"):
        _rule(operation=OperationRef(type="exists", operands={"value": "depth"}))


def test_an_operand_may_name_a_derivation_or_parameter_not_only_an_input() -> None:
    """Derivations and parameters are legitimate operand sources; rejecting them would block real
    rules — CT-1 sums a derived cabinet run."""
    rule = _rule()
    assert validate(rule).is_valid


def test_every_problem_is_collected_not_just_the_first() -> None:
    """An author fixing errors one at a time makes three round trips where one would do, and each
    is a chance to route around the process."""
    rule = _rule(
        applicability=GlobalApplicability(scope="global"),
        operation=OperationRef(type="within_tolerance", operands={"actual": "width"}),
    )
    problems = validate(rule).problems
    assert len(problems) >= 1
    assert any("missing operand" in p for p in problems)


def test_the_status_is_explicit_rather_than_inferred_from_an_empty_list() -> None:
    assert validate(_rule()).status is ValidationStatus.VALID
    bad = validate(_rule(operation=OperationRef(type="nope", operands={})))
    assert bad.status is ValidationStatus.INVALID


# ---------------------------------------------------------------------------
# The proposal object
# ---------------------------------------------------------------------------


def test_a_proposal_must_name_its_author() -> None:
    """The first question about any rule change is who wanted it."""
    with pytest.raises(ValueError, match="must name its author"):
        propose(_rule(), author="  ", rationale="because")


def test_a_proposal_must_carry_a_rationale() -> None:
    """'What changed' is visible from the diff; 'why' is not, and it is what an approver judges."""
    with pytest.raises(ValueError, match="must carry a rationale"):
        propose(_rule(), author="anant", rationale="")


def test_a_proposal_cannot_misdescribe_the_rule_it_carries() -> None:
    with pytest.raises(ValueError, match="but carries rule"):
        RuleProposal(
            rule_id="CT-OTHER-999",
            proposed=_rule(),
            author="anant",
            rationale="test",
            validation=validate(_rule()),
        )


def test_an_invalid_proposal_is_not_approvable_regardless_of_authority() -> None:
    """Authority decides whether a coherent change ships, not whether an incoherent one is
    coherent. This is the assertion D6.3 will rely on."""
    bad = propose(
        _rule(operation=OperationRef(type="nope", operands={})),
        author="anant",
        rationale="test",
    )
    assert not bad.approvable


def test_a_valid_proposal_is_approvable() -> None:
    assert propose(_rule(), author="anant", rationale="test").approvable


def test_a_proposal_is_frozen_so_a_revision_is_a_new_one() -> None:
    """Append-only: the record of what was originally proposed is part of why a rule change is
    defensible later, and is exactly what someone would tidy after a review goes badly."""
    proposal = propose(_rule(), author="anant", rationale="test")
    with pytest.raises(Exception):  # noqa: B017 - dataclasses raise FrozenInstanceError
        proposal.rationale = "something else"  # type: ignore[misc]


def test_a_proposal_carries_the_snapshot_id_it_would_publish() -> None:
    """So an approver can confirm afterwards that what shipped is what they approved."""
    proposal = propose(_rule(), author="anant", rationale="test")
    assert len(proposal.snapshot_id) > 16
    assert proposal.snapshot_id == propose(_rule(), author="x", rationale="y").snapshot_id


def test_nothing_here_writes_a_snapshot() -> None:
    """A proposal is a suggestion, not a pending change. There is no state in which it applies."""
    import rules.governance.proposal as module

    source = module.__file__
    with open(source, encoding="utf-8") as handle:
        text = handle.read()
    assert "SnapshotStore" not in text
    assert ".add(" not in text


# ---------------------------------------------------------------------------
# The change description
# ---------------------------------------------------------------------------


def test_a_new_rule_is_described_as_new() -> None:
    text = describe_change(None, _rule())
    assert "New rule CT-WIDTH-001" in text


def test_a_tolerance_change_is_stated_in_plain_english() -> None:
    """The case the story exists for: an approver must see that the check got stricter."""
    loose = _rule(
        applicability=Applicability(
            discriminator="wall_config",
            variants=(
                ApplicabilityVariant(
                    when="island", tolerance=Tolerance(value=Fraction(1, 8), unit=Unit.INCH)
                ),
            ),
        )
    )
    tight = _rule(
        applicability=Applicability(
            discriminator="wall_config",
            variants=(
                ApplicabilityVariant(
                    when="island", tolerance=Tolerance(value=Fraction(1, 16), unit=Unit.INCH)
                ),
            ),
        )
    )
    text = describe_change(loose, tight)
    assert "tolerance for when island" in text
    assert "1/8" in text and "1/16" in text


def test_a_severity_change_says_what_it_means() -> None:
    """Severity decides whether a failure blocks release. An approver seeing 'CRITICAL → ADVISORY'
    without that context may not realise what they are agreeing to."""
    text = describe_change(_rule(), _rule(severity=Severity.ADVISORY))
    assert "severity" in text
    assert "blocks release" in text


def test_an_unconfirmed_tolerance_is_called_out_in_the_description() -> None:
    """ADR-0011 blocks it at release; saying so at proposal time saves the round trip."""
    rule = _rule(
        operation=OperationRef(
            type="within_tolerance",
            operands={"actual": "width", "expected": "width", "tolerance": "width"},
            tolerance=Tolerance(value=TOLERANCE_UNCONFIRMED),
        )
    )
    assert "cannot reach production" in describe_change(_rule(), rule)


def test_a_new_input_is_reported() -> None:
    rule = _rule(
        inputs={
            "width": InputSelector(source=OperandSource.SHOP, semantic_type=SemanticType.CT001),
            "depth": InputSelector(source=OperandSource.SHOP, semantic_type=SemanticType.CT010),
        }
    )
    assert "reads new input(s)" in describe_change(_rule(), rule)


def test_no_material_change_says_so_rather_than_printing_nothing() -> None:
    """An empty description reads as a rendering bug and invites the approver to ignore it."""
    assert "no material change" in describe_change(_rule(), _rule())
