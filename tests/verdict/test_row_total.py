"""A countertop row from whichever complete source was given, and never a choice between two (#991).

Verification for: `verdict.operations.aggregate.row_total`, `OperationSpec.optional` and the engine's
narrow "not supplied" binding (`verdict.engine._not_supplied`).
"""

from __future__ import annotations

from fractions import Fraction

import pytest

from rules.derivations import Derivation
from rules.schema import (
    CheckType,
    GlobalApplicability,
    InputSelector,
    OperationRef,
    Parameter,
    Rule,
)
from rules.semantic_types import OperandSource, ProductType, SemanticType
from rules.snapshot import publish
from units.measurement import Measurement, MixedUnitError, Unit
from verdict.engine import execute
from verdict.operands import EvidenceStatus, VerdictOperand
from verdict.operations import register_all
from verdict.operations.aggregate import row_total
from verdict.outcomes import Outcome, Severity
from verdict.registry import (
    REGISTRY,
    Arity,
    DerivationResult,
    OperationKind,
    OperationResult,
    OperationSpec,
    RuleAuthoringError,
    validate_operands,
)


def inch(value: int | Fraction) -> Measurement:
    return Measurement(Fraction(value), Unit.INCH, str(value))


def inches(*values: int | Fraction) -> tuple[Measurement, ...]:
    return tuple(inch(value) for value in values)


PIECES = inches(2, Fraction(105, 8), Fraction(3, 4), Fraction(87, 4), 2)  # 39 5/8
CABINETS = inches(Fraction(105, 8), Fraction(87, 4))  # 34 7/8
FILLERS = inches(2, Fraction(3, 4), 2)  # 4 3/4


# ---------------------------------------------------------------------------
# The operation's decision table
# ---------------------------------------------------------------------------


def test_pieces_alone_are_summed_exactly() -> None:
    result = row_total(pieces=PIECES, cabinets=None, fillers=None)

    assert isinstance(result, DerivationResult)
    assert result.value == Measurement(Fraction(317, 8), Unit.INCH, None)
    assert isinstance(result.value.exact, Fraction)
    assert dict(result.intermediates)["source"] == "pieces"
    assert result.expression == "pieces 2 + 13 1/8 + 3/4 + 21 3/4 + 2 = 39 5/8 in"


def test_cabinets_and_fillers_alone_are_summed_as_before() -> None:
    result = row_total(pieces=None, cabinets=CABINETS, fillers=FILLERS)

    assert isinstance(result, DerivationResult)
    assert result.value == Measurement(Fraction(317, 8), Unit.INCH, None)
    facts = dict(result.intermediates)
    assert facts["source"] == "cabinets and fillers"
    assert facts["cabinet_total"] == Measurement(Fraction(279, 8), Unit.INCH, None)
    assert facts["filler_total"] == Measurement(Fraction(19, 4), Unit.INCH, None)


def test_both_statements_agreeing_give_the_one_total() -> None:
    result = row_total(pieces=PIECES, cabinets=CABINETS, fillers=FILLERS)

    assert isinstance(result, DerivationResult)
    assert result.value == Measurement(Fraction(317, 8), Unit.INCH, None)
    assert dict(result.intermediates)["source"] == "pieces, confirmed by cabinets and fillers"


@pytest.mark.parametrize("off", [Fraction(1, 16), -Fraction(1, 16), Fraction(2)])
def test_both_statements_disagreeing_go_to_the_reviewer(off: Fraction) -> None:
    """Exact: a sixteenth either way is a disagreement, and neither total is chosen."""
    fillers = (*FILLERS[:-1], inch(2 + off))

    result = row_total(pieces=PIECES, cabinets=CABINETS, fillers=fillers)

    assert isinstance(result, OperationResult)
    assert result.outcome is Outcome.REVIEW_REQUIRED
    assert result.delta is None
    assert "39 5/8 in" in result.comparison
    assert "disagree" in result.comparison


@pytest.mark.parametrize(
    ("cabinets", "fillers"), [(CABINETS, None), (None, FILLERS)], ids=["cabinets", "fillers"]
)
def test_pieces_beside_half_a_second_statement_go_to_the_reviewer(
    cabinets: tuple[Measurement, ...] | None, fillers: tuple[Measurement, ...] | None
) -> None:
    result = row_total(pieces=PIECES, cabinets=cabinets, fillers=fillers)

    assert isinstance(result, OperationResult)
    assert result.outcome is Outcome.REVIEW_REQUIRED


@pytest.mark.parametrize(
    ("cabinets", "fillers"),
    [(None, None), (CABINETS, None), (None, FILLERS)],
    ids=["nothing", "cabinets-only", "fillers-only"],
)
def test_no_complete_statement_is_not_found(
    cabinets: tuple[Measurement, ...] | None, fillers: tuple[Measurement, ...] | None
) -> None:
    result = row_total(pieces=None, cabinets=cabinets, fillers=fillers)

    assert isinstance(result, OperationResult)
    assert result.outcome is Outcome.NOT_FOUND


def test_an_empty_list_is_refused_rather_than_summed_to_zero() -> None:
    with pytest.raises(ValueError, match="empty sum is not zero"):
        row_total(pieces=(), cabinets=None, fillers=None)


def test_mixed_units_across_the_statements_raise() -> None:
    millimetre_fillers = tuple(Measurement(Fraction(51), Unit.MM, "51") for _ in FILLERS)

    with pytest.raises(MixedUnitError):
        row_total(pieces=PIECES, cabinets=CABINETS, fillers=millimetre_fillers)


# ---------------------------------------------------------------------------
# The registry: an optional operand, and only on a derivation
# ---------------------------------------------------------------------------


def _fn(**_: object) -> DerivationResult:  # pragma: no cover - never called
    raise AssertionError


def test_a_verdict_operation_may_not_take_an_optional_operand() -> None:
    with pytest.raises(RuleAuthoringError, match="only a derivation"):
        OperationSpec("x", "1.0.0", {"a": Arity.LIST}, _fn, optional=frozenset({"a"}))


def test_an_unknown_optional_operand_is_refused() -> None:
    with pytest.raises(RuleAuthoringError, match="unknown operand"):
        OperationSpec(
            "x", "1.0.0", {"a": Arity.LIST}, _fn, OperationKind.DERIVATION, frozenset({"b"})
        )


def test_none_is_accepted_only_for_an_optional_operand() -> None:
    spec = OperationSpec(
        "x",
        "1.0.0",
        {"a": Arity.LIST, "b": Arity.LIST},
        _fn,
        OperationKind.DERIVATION,
        frozenset({"a"}),
    )

    validate_operands(spec, {"a": None, "b": (inch(1),)})
    with pytest.raises(RuleAuthoringError, match="list arity"):
        validate_operands(spec, {"a": (inch(1),), "b": None})


# ---------------------------------------------------------------------------
# The engine: "not supplied" is a rule input nobody gave, and nothing else
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _registered_operations() -> object:
    previous = dict(REGISTRY)
    REGISTRY.clear()
    register_all()
    yield
    REGISTRY.clear()
    REGISTRY.update(previous)


def _selector(semantic: SemanticType) -> InputSelector:
    return InputSelector(source=OperandSource.SHOP, semantic_type=semantic, cardinality="many")


def _rule(*, cabinets_from_parameter: bool = False) -> Rule:
    """A toy row check: the overall equals the row. Optionally binds `cabinets` to a *parameter*,
    which the exception must not cover."""
    inputs = {
        "overall": InputSelector(
            source=OperandSource.SHOP, semantic_type=SemanticType.COUNTERTOP_OVERALL_WIDTH
        ),
        "pieces": _selector(SemanticType.COUNTERTOP_PIECE_WIDTH),
        "fillers": _selector(SemanticType.FILLER_WIDTH),
    }
    parameters: dict[str, Parameter] = {}
    if cabinets_from_parameter:
        parameters["cabinets"] = Parameter()
    else:
        inputs["cabinets"] = _selector(SemanticType.CABINET_WIDTH)
    return Rule(
        id="TOY-ROW-001",
        version="1.0.0",
        product_type=ProductType.COUNTERTOP,
        check_type=CheckType.INTERNAL,
        severity=Severity.FLAG,
        arithmetic_unit=Unit.INCH,
        inputs=inputs,
        parameters=parameters,
        derivations=(
            Derivation(
                name="row",
                operation="row_total",
                operands={"pieces": "pieces", "cabinets": "cabinets", "fillers": "fillers"},
            ),
        ),
        applicability=GlobalApplicability(scope="global"),
        operation=OperationRef(type="equals", operands={"actual": "overall", "expected": "row"}),
    )


def _operand(name: str, value: Measurement | tuple[Measurement, ...]) -> VerdictOperand:
    return VerdictOperand(name=name, value=value, status=EvidenceStatus.CORROBORATED, source="SHOP")


def test_an_input_nobody_supplied_reaches_the_operation_as_not_given() -> None:
    finding = execute(
        publish(_rule()),
        {
            "overall": _operand("overall", inch(Fraction(317, 8))),
            "pieces": _operand("pieces", PIECES),
        },
    )

    assert finding.outcome is Outcome.PASS
    assert finding.trace is not None
    row = dict(finding.trace.intermediates)["row"]
    inputs = dict(dict(row)["inputs"])  # type: ignore[arg-type]
    assert inputs["cabinets"] is None and inputs["fillers"] is None


def test_an_optional_operand_bound_to_a_missing_parameter_is_still_not_found() -> None:
    """The exception is for rule inputs only. A missing parameter is a value the rule needs."""
    finding = execute(
        publish(_rule(cabinets_from_parameter=True)),
        {
            "overall": _operand("overall", inch(Fraction(317, 8))),
            "pieces": _operand("pieces", PIECES),
        },
    )

    assert finding.outcome is Outcome.NOT_FOUND
    assert "cabinets" in finding.reason
    assert finding.trace is None
