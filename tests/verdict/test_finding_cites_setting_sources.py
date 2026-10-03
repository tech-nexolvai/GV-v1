"""A finding says where every setting it used came from, and who set it (#827).

Verification for: `verdict/engine.py:execute`, the notes it writes from `ResolvedParameter.explain`.
The note is descriptive: the same check with the same numbers reaches the same verdict whatever source
a setting names, and `test_the_source_changes_no_verdict` holds the engine to that.
"""

from __future__ import annotations

from datetime import UTC, datetime
from fractions import Fraction

import pytest

from rules.parameters import ParameterLayer, ParameterValue, Provenance, ResolvedParameter
from rules.schema import (
    CheckType,
    GlobalApplicability,
    InputSelector,
    OperationRef,
    Parameter,
    Quantity,
    Rule,
)
from rules.semantic_types import OperandSource, ProductType, SemanticType
from rules.snapshot import publish
from units.measurement import Measurement, Unit
from verdict.engine import execute
from verdict.operands import EvidenceStatus, VerdictOperand
from verdict.operations.scalar import SCALAR_SPECS
from verdict.outcomes import Outcome, Severity
from verdict.registry import REGISTRY, register


@pytest.fixture(autouse=True)
def _registered_operations() -> object:
    previous = dict(REGISTRY)
    REGISTRY.clear()
    for spec in SCALAR_SPECS:
        register(spec)
    yield
    REGISTRY.clear()
    REGISTRY.update(previous)


def _rule() -> Rule:
    """The countertop's overhang equals the specified one: one drawn input, one setting."""
    return Rule(
        id="SOURCE-CITED",
        version="1.0.0",
        product_type=ProductType.COUNTERTOP,
        check_type=CheckType.INTERNAL,
        severity=Severity.CRITICAL,
        arithmetic_unit=Unit.INCH,
        inputs={
            "drawn_overhang": InputSelector(
                source=OperandSource.SHOP, semantic_type=SemanticType.CABINET_WIDTH
            )
        },
        parameters={"countertop_overhang": Parameter()},
        applicability=GlobalApplicability(scope="global"),
        operation=OperationRef(
            type="equals",
            operands={"actual": "drawn_overhang", "expected": "countertop_overhang"},
        ),
    )


def _drawn(value: Fraction) -> dict[str, VerdictOperand]:
    return {
        "drawn_overhang": VerdictOperand(
            name="drawn_overhang",
            value=Measurement(value, Unit.INCH, None),
            status=EvidenceStatus.HUMAN_CONFIRMED,
            source="SHOP",
            evidence_ref="p1:overhang",
        )
    }


def _overhang(provenance: Provenance, reference: str | None) -> dict[str, ResolvedParameter]:
    return {
        "countertop_overhang": ResolvedParameter(
            name="countertop_overhang",
            value=ParameterValue(
                value=Quantity(value=Fraction(3, 4), unit=Unit.INCH),
                provenance=provenance,
                set_by="anant",
                set_at=datetime(2026, 10, 3, tzinfo=UTC),
                reference=reference,
            ),
            layer=ParameterLayer.PROJECT,
        )
    }


def test_the_finding_names_the_source_and_reference_of_its_setting() -> None:
    """Outcome: PASS, and a note a reviewer can act on — where the ¾ came from and whom to ask."""
    finding = execute(
        publish(_rule()),
        _drawn(Fraction(3, 4)),
        _overhang(Provenance.GC_CLIENT, "Architect A-501, section 3"),
    )

    assert finding.outcome is Outcome.PASS
    assert (
        "countertop_overhang = 3/4 in (project, G.C / Client: Architect A-501, section 3, "
        "set by anant)"
    ) in finding.notes


def test_the_source_changes_no_verdict() -> None:
    """Same numbers, two different sources: the same outcome, and only the note differs."""
    rule = publish(_rule())
    specified = execute(rule, _drawn(Fraction(1)), _overhang(Provenance.GC_CLIENT, None))
    company = execute(rule, _drawn(Fraction(1)), _overhang(Provenance.COMPANY_STANDARD, "x"))

    assert specified.outcome is company.outcome is Outcome.FAIL
    assert specified.reason == company.reason
    assert specified.notes != company.notes
