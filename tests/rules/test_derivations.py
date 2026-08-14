"""Derivation rules form a named, backward-only DAG without executable expressions.

Source: issue #54; plan F6; ADR-0003 as amended by ADR-0008.
Verification: this file.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from rules.derivations import Derivation
from rules.schema import (
    Cardinality,
    CheckType,
    GlobalApplicability,
    InputSelector,
    OperationRef,
    Parameter,
    Rule,
)
from rules.semantic_types import OperandSource, ProductType, SemanticType
from units.measurement import Unit
from verdict.outcomes import Severity


def _selector() -> InputSelector:
    return InputSelector(
        source=OperandSource.SHOP,
        semantic_type=SemanticType.CABINET_WIDTH,
        cardinality=Cardinality.ONE,
    )


def _rule(
    *,
    inputs: tuple[str, ...] = ("observed",),
    parameters: tuple[str, ...] = (),
    derivations: tuple[Derivation, ...],
    result: str,
) -> Rule:
    return Rule(
        id="DERIVATION-TEST",
        version="1.0.0",
        product_type=ProductType.COUNTERTOP,
        applicability=GlobalApplicability(scope="global"),
        check_type=CheckType.INTERNAL,
        severity=Severity.CRITICAL,
        arithmetic_unit=Unit.MM,
        inputs={name: _selector() for name in inputs},
        parameters={name: Parameter() for name in parameters},
        derivations=derivations,
        operation=OperationRef(type="exists", operands={"x": result}),
    )


def test_named_operands_may_bind_inputs_parameters_and_earlier_derivations() -> None:
    rule = _rule(
        inputs=("depth",),
        parameters=("overhang",),
        derivations=(
            Derivation(
                name="overall",
                operation="sum",
                operands={"values": ("depth", "overhang")},
            ),
            Derivation(
                name="difference",
                operation="difference_between",
                operands={"a": "overall", "b": "depth"},
            ),
        ),
        result="difference",
    )

    assert rule.derivations[1].operands == {"a": "overall", "b": "depth"}


def test_list_binding_preserves_order_and_repeated_references() -> None:
    derivation = Derivation(
        name="sink_cabinet_width",
        operation="sum",
        operands={
            "values": (
                "cabinet_side_thickness",
                "door_width",
                "left_reveal",
                "right_reveal",
                "cabinet_side_thickness",
            )
        },
    )

    assert derivation.operands["values"] == (
        "cabinet_side_thickness",
        "door_width",
        "left_reveal",
        "right_reveal",
        "cabinet_side_thickness",
    )


@pytest.mark.parametrize("reference", ["later", "self"])
def test_forward_and_self_references_name_the_broken_edge(reference: str) -> None:
    derivations = (
        Derivation(name="self", operation="sum", operands={"values": (reference,)}),
        Derivation(name="later", operation="sum", operands={"values": ("observed",)}),
    )

    with pytest.raises(ValidationError) as error:
        _rule(derivations=derivations, result="self")

    message = str(error.value)
    assert "derivation 'self'" in message
    assert "operand 'values'" in message
    assert repr(reference) in message
    assert "only look backwards" in message


def test_a_derivation_cannot_shadow_an_existing_name() -> None:
    with pytest.raises(ValidationError, match="redefines an existing name"):
        _rule(
            derivations=(
                Derivation(
                    name="observed",
                    operation="sum",
                    operands={"values": ("observed",)},
                ),
            ),
            result="observed",
        )


@pytest.mark.parametrize(
    "operands",
    [
        {},
        {"values": ()},
        {"values": ""},
        {"values": ("observed", "")},
        {"": "observed"},
    ],
)
def test_empty_derivation_bindings_are_rejected(operands: object) -> None:
    with pytest.raises(ValidationError):
        Derivation(name="derived", operation="sum", operands=operands)  # type: ignore[arg-type]


def test_derivation_cannot_contain_code_or_an_expression_field() -> None:
    with pytest.raises(ValidationError, match="never contain executable text"):
        Derivation(
            name="derived",
            operation="eval('observed')",
            operands={"value": "observed"},
        )

    with pytest.raises(ValidationError, match="expression"):
        Derivation(
            name="derived",
            operation="sum",
            operands={"values": ("observed",)},
            expression="observed + 1",  # type: ignore[call-arg]
        )


def test_ct004_formula_is_expressible_with_a_repeated_parameter() -> None:
    rule = _rule(
        inputs=("ct011", "ct012", "ct013"),
        parameters=("cab_side_thk",),
        derivations=(
            Derivation(
                name="ct004",
                operation="sum",
                operands={
                    "values": (
                        "cab_side_thk",
                        "ct011",
                        "ct012",
                        "ct013",
                        "cab_side_thk",
                    )
                },
            ),
        ),
        result="ct004",
    )

    encoded = json.loads(rule.model_dump_json())
    assert encoded["derivations"][0]["operands"]["values"].count("cab_side_thk") == 2


def test_ct010_formula_is_expressible_as_a_five_term_sum() -> None:
    rule = _rule(
        inputs=("ct007", "ct008", "ct009"),
        parameters=("countertop_overhang", "backsplash_thickness"),
        derivations=(
            Derivation(
                name="ct010",
                operation="sum",
                operands={
                    "values": (
                        "countertop_overhang",
                        "ct007",
                        "ct008",
                        "ct009",
                        "backsplash_thickness",
                    )
                },
            ),
        ),
        result="ct010",
    )

    assert tuple(rule.derivations[0].operands["values"]) == (
        "countertop_overhang",
        "ct007",
        "ct008",
        "ct009",
        "backsplash_thickness",
    )
