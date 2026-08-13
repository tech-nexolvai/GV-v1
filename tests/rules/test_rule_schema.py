"""A malformed rule must be rejected loudly, and a nearly-valid one especially so.

The dangerous input here is not a rule that crashes the parser — it is one that validates
and then behaves differently from what its author intended. Most of these tests are
therefore about rejection, not acceptance.
"""

from __future__ import annotations

import json
from fractions import Fraction

import pytest
from pydantic import ValidationError

from rules.schema import (
    TOLERANCE_UNCONFIRMED,
    Applicability,
    ApplicabilityVariant,
    Cardinality,
    CheckType,
    Derivation,
    GlobalApplicability,
    InputSelector,
    OperationRef,
    Parameter,
    Quantity,
    Rule,
    Scope,
    Tolerance,
    rule_json_schema,
)
from rules.semantic_types import OperandSource, ProductType, SemanticType
from units.measurement import Unit
from verdict.outcomes import Outcome, Severity


def _minimal_rule(**overrides: object) -> Rule:
    base: dict[str, object] = {
        "id": "CT-WIDTH-001",
        "version": "1.0.0",
        "product_type": ProductType.COUNTERTOP,
        # Required since ADR-0007: a rule states its applicability rather than omitting it.
        "applicability": GlobalApplicability(scope="global"),
        "check_type": CheckType.INTERNAL,
        "severity": Severity.CRITICAL,
        "arithmetic_unit": Unit.MM,
        "inputs": {
            "countertop_width": InputSelector(
                source=OperandSource.SHOP,
                semantic_type=SemanticType.COUNTERTOP_OVERALL_WIDTH,
                cardinality=Cardinality.ONE,
            )
        },
        "operation": OperationRef(type="exists", operands={"x": "countertop_width"}),
    }
    base.update(overrides)
    return Rule(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Exact numbers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        (3, Fraction(3)),
        ("1/8", Fraction(1, 8)),
        ("38 3/4", Fraction(155, 4)),
        ("2.375", Fraction(19, 8)),
        ('5"', Fraction(5)),
    ],
)
def test_authored_values_become_exact_fractions(written: object, expected: Fraction) -> None:
    assert Quantity(value=written, unit=Unit.INCH).value == expected  # type: ignore[arg-type]


def test_2_375_and_2_3_8_are_the_same_number() -> None:
    """The client writes both forms. They must not become different tolerances."""
    assert (
        Quantity(value="2.375", unit=Unit.INCH).value
        == Quantity(value="2 3/8", unit=Unit.INCH).value
    )


def test_a_float_is_rejected_outright() -> None:
    """Accepting a float would let binary rounding into a tolerance — the failure ADR-0001
    exists to prevent. The author must write the exact value as text instead."""
    with pytest.raises(ValidationError) as err:
        Quantity(value=0.125, unit=Unit.INCH)  # type: ignore[arg-type]
    assert "float is not allowed" in str(err.value)


def test_a_boolean_is_not_a_measurement() -> None:
    with pytest.raises(ValidationError):
        Quantity(value=True, unit=Unit.INCH)  # type: ignore[arg-type]


def test_nonsense_text_is_rejected_rather_than_guessed() -> None:
    with pytest.raises(ValidationError):
        Quantity(value="about eight inches", unit=Unit.INCH)


# ---------------------------------------------------------------------------
# Tolerance
# ---------------------------------------------------------------------------


def test_unconfirmed_tolerance_is_publishable_but_cannot_be_compared() -> None:
    """No tolerance exists in the client material yet (#10). A rule may be authored, but
    the value must never be substituted."""
    tol = Tolerance(value=TOLERANCE_UNCONFIRMED)
    assert not tol.is_confirmed
    with pytest.raises(ValueError, match="REVIEW_REQUIRED"):
        tol.as_measurement()


def test_unconfirmed_tolerance_is_not_zero() -> None:
    """A zero tolerance fails everything; a guessed one passes the wrong things."""
    tol = Tolerance(value=TOLERANCE_UNCONFIRMED)
    assert tol.value != 0
    assert tol.value != Fraction(0)


def test_confirmed_tolerance_requires_a_unit() -> None:
    with pytest.raises(ValidationError, match="must state its unit"):
        Tolerance(value="1/8")


def test_confirmed_tolerance_converts_exactly() -> None:
    tol = Tolerance(value="1/8", unit=Unit.INCH)
    assert tol.is_confirmed
    assert tol.as_measurement().mm == Fraction(1, 8) * Fraction(127, 5)


# ---------------------------------------------------------------------------
# Strictness
# ---------------------------------------------------------------------------


def test_an_unknown_field_is_rejected_not_ignored() -> None:
    """The headline safety property. 'tolerence' must not silently mean no tolerance."""
    with pytest.raises(ValidationError) as err:
        Tolerance(value="1/8", unit=Unit.INCH, tolerence="1/16")  # type: ignore[call-arg]
    assert "tolerence" in str(err.value)


def test_a_rule_with_an_unknown_field_is_rejected() -> None:
    with pytest.raises(ValidationError) as err:
        _minimal_rule(sevrity="CRITICAL")
    assert "sevrity" in str(err.value)


def test_a_validated_rule_cannot_be_mutated() -> None:
    """A mutable rule could differ at execution time from the one that was reviewed."""
    rule = _minimal_rule()
    with pytest.raises(ValidationError):
        rule.severity = Severity.MINOR  # type: ignore[misc]


def test_version_must_be_semver() -> None:
    with pytest.raises(ValidationError):
        _minimal_rule(version="1.0")


# ---------------------------------------------------------------------------
# Abstention outcomes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [Outcome.PASS, Outcome.FAIL])
def test_on_missing_may_never_be_a_decision(bad: Outcome) -> None:
    """One YAML line must not be able to defeat the evidence gate."""
    with pytest.raises(ValidationError, match="must abstain"):
        _minimal_rule(on_missing=bad)


@pytest.mark.parametrize(
    "ok", [Outcome.NOT_FOUND, Outcome.REVIEW_REQUIRED, Outcome.NO_APPLICABLE_RULE]
)
def test_on_missing_accepts_every_abstention(ok: Outcome) -> None:
    assert _minimal_rule(on_missing=ok).on_missing is ok


# ---------------------------------------------------------------------------
# Derivations (ADR-0003)
# ---------------------------------------------------------------------------


def test_a_derivation_may_reference_an_earlier_derivation() -> None:
    rule = _minimal_rule(
        parameters={"overhang": Parameter(), "backsplash": Parameter()},
        derivations=(
            Derivation(name="front", operation="sum", inputs=("overhang", "backsplash")),
            Derivation(name="total", operation="sum", inputs=("front", "countertop_width")),
        ),
        operation=OperationRef(type="exists", operands={"x": "total"}),
    )
    assert [d.name for d in rule.derivations] == ["front", "total"]


def test_a_derivation_referencing_a_later_one_is_rejected() -> None:
    """Backwards-only references are what keep the graph acyclic."""
    with pytest.raises(ValidationError, match="may only look"):
        _minimal_rule(
            derivations=(
                Derivation(name="a", operation="sum", inputs=("b",)),
                Derivation(name="b", operation="sum", inputs=("countertop_width",)),
            )
        )


def test_a_self_referencing_derivation_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _minimal_rule(derivations=(Derivation(name="a", operation="sum", inputs=("a",)),))


def test_a_derivation_cannot_shadow_an_input() -> None:
    with pytest.raises(ValidationError, match="redefines"):
        _minimal_rule(
            derivations=(
                Derivation(name="countertop_width", operation="sum", inputs=("countertop_width",)),
            )
        )


def test_a_derivation_with_no_inputs_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Derivation(name="a", operation="sum", inputs=())


# ---------------------------------------------------------------------------
# Operations and operands
# ---------------------------------------------------------------------------


def test_operation_referencing_an_unknown_operand_is_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown operand"):
        _minimal_rule(operation=OperationRef(type="exists", operands={"x": "not_a_thing"}))


def test_operation_type_cannot_carry_executable_text() -> None:
    """`AGENTS.md` §2.2 — a rule supplies a registry name, never code."""
    with pytest.raises(ValidationError, match="never contain executable"):
        OperationRef(type="__import__('os').system('ls')")


# ---------------------------------------------------------------------------
# Applicability
# ---------------------------------------------------------------------------


def _wall_config() -> Applicability:
    return Applicability(
        discriminator="wall_config",
        variants=(
            ApplicabilityVariant(
                when="back_left_right",
                tolerance=Tolerance(value="1/8", unit=Unit.INCH),
                extras={"field_cut_count": 2},
            ),
            ApplicabilityVariant(
                when="island",
                tolerance=Tolerance(value="1/8", unit=Unit.INCH),
                extras={"field_cut_count": 0},
            ),
        ),
    )


def test_variant_lookup_returns_the_matching_branch() -> None:
    variant = _wall_config().variant_for("back_left_right")
    assert variant is not None
    assert variant.extras["field_cut_count"] == 2


def test_an_unmatched_discriminator_returns_none_not_a_default() -> None:
    """None means REVIEW_REQUIRED. Falling back to the first variant would apply the wrong
    tolerance to the wrong layout (ADR-0004)."""
    assert _wall_config().variant_for("back_only") is None


def test_duplicate_variants_are_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate"):
        Applicability(
            discriminator="wall_config",
            variants=(
                ApplicabilityVariant(
                    when="island", tolerance=Tolerance(value=TOLERANCE_UNCONFIRMED)
                ),
                ApplicabilityVariant(
                    when="island", tolerance=Tolerance(value=TOLERANCE_UNCONFIRMED)
                ),
            ),
        )


def test_applicability_needs_at_least_one_variant() -> None:
    with pytest.raises(ValidationError, match="at least one variant"):
        Applicability(discriminator="wall_config", variants=())


def test_has_confirmed_tolerance_is_false_while_any_is_unconfirmed() -> None:
    rule = _minimal_rule(
        applicability=Applicability(
            discriminator="wall_config",
            variants=(
                ApplicabilityVariant(
                    when="island", tolerance=Tolerance(value="1/8", unit=Unit.INCH)
                ),
                ApplicabilityVariant(
                    when="back_only", tolerance=Tolerance(value=TOLERANCE_UNCONFIRMED)
                ),
            ),
        )
    )
    assert not rule.has_confirmed_tolerance


# ---------------------------------------------------------------------------
# Round trip and JSON Schema
# ---------------------------------------------------------------------------


def test_round_trip_is_lossless() -> None:
    """model -> JSON -> model must preserve the exact tolerance, not a decimal of it."""
    original = _minimal_rule(
        applicability=_wall_config(),
        operation=OperationRef(
            type="within_tolerance",
            operands={"actual": "countertop_width"},
            tolerance=Tolerance(value="1/16", unit=Unit.INCH),
        ),
    )
    restored = Rule.model_validate(json.loads(original.model_dump_json()))
    assert restored == original
    assert restored.operation.tolerance is not None
    assert restored.operation.tolerance.value == Fraction(1, 16)


def test_json_schema_is_generated_and_forbids_extra_fields() -> None:
    schema = rule_json_schema()
    assert schema["additionalProperties"] is False
    for required in ("id", "version", "severity", "arithmetic_unit", "operation"):
        assert required in schema["properties"]  # type: ignore[operator]


def test_scope_and_cardinality_defaults_are_the_conservative_ones() -> None:
    selector = InputSelector(source=OperandSource.SHOP, semantic_type=SemanticType.CABINET_WIDTH)
    assert selector.cardinality is Cardinality.ONE
    assert selector.scope is Scope.SAME_ASSEMBLY
