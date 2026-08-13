"""The execution order is the safety property, so the tests assert the order itself.

Checking qualification *after* computing would still produce the right outcome — and would mean
the engine had already done arithmetic on evidence it was not entitled to use. So several tests
below record whether the operation function was ever called, not merely what came back: an
unqualified operand, a missing one and a mixed-unit pair must each abstain **before** any
arithmetic runs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from fractions import Fraction

import pytest

from rules.parameters import (
    ParameterLayer,
    ParameterSet,
    ParameterValue,
    Provenance,
    resolve,
)
from rules.schema import (
    TOLERANCE_UNCONFIRMED,
    Applicability,
    ApplicabilityVariant,
    CheckType,
    GlobalApplicability,
    InputSelector,
    OperationRef,
    Quantity,
    Rule,
    Tolerance,
)
from rules.semantic_types import OperandSource, ProductType, SemanticType
from rules.snapshot import publish
from units.measurement import Measurement, Unit
from verdict.engine import ENGINE_VERSION, execute
from verdict.operands import EvidenceStatus, VerdictOperand
from verdict.outcomes import Outcome, Severity
from verdict.registry import REGISTRY, Arity, OperationResult, OperationSpec, register
from verdict.trace import CalculationTrace

WHEN = datetime(2026, 8, 14, 9, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# A spy operation, so we can tell *when* arithmetic happened
# ---------------------------------------------------------------------------

CALLS: list[dict[str, object]] = []


def _spy(**kwargs: object) -> OperationResult:
    CALLS.append(kwargs)
    return OperationResult(
        outcome=Outcome.PASS,
        delta=None,
        trace=CalculationTrace(
            operation="spy",
            operands=(),
            intermediates=(),
            comparison="spy passed",
            tolerance=None,
            arithmetic_unit=Unit.MM,
            outcome=Outcome.PASS,
            engine_version=ENGINE_VERSION,
            operation_version="1.0.0",
        ),
    )


@pytest.fixture(autouse=True)
def _clean_registry() -> object:
    CALLS.clear()
    REGISTRY.pop("spy", None)
    register(OperationSpec(name="spy", version="1.0.0", operands={"x": Arity.SCALAR}, fn=_spy))
    yield
    REGISTRY.pop("spy", None)
    CALLS.clear()


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _rule(**overrides: object) -> Rule:
    base: dict[str, object] = {
        "id": "CT-WIDTH-001",
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
        "operation": OperationRef(type="spy", operands={"x": "width"}),
    }
    base.update(overrides)
    return Rule(**base)  # type: ignore[arg-type]


def _operand(
    value: object = Measurement(Fraction(6012), Unit.MM, "6012"),
    *,
    status: EvidenceStatus = EvidenceStatus.CORROBORATED,
    name: str = "width",
) -> VerdictOperand:
    return VerdictOperand(
        name=name, value=value, status=status, source="SHOP", evidence_ref="p3:poly-1"
    )


# ---------------------------------------------------------------------------
# The happy path exists, so the abstentions mean something
# ---------------------------------------------------------------------------


def test_a_qualified_operand_reaches_the_operation_and_decides() -> None:
    finding = execute(publish(_rule()), {"width": _operand()})
    assert finding.outcome is Outcome.PASS
    assert len(CALLS) == 1
    assert finding.trace is not None
    assert finding.snapshot_id.startswith("sha256:")


# ---------------------------------------------------------------------------
# Step 1 — applicability
# ---------------------------------------------------------------------------


def _with_variants() -> Rule:
    return _rule(
        applicability=Applicability(
            discriminator="wall_config",
            variants=(
                ApplicabilityVariant(
                    when="back_left_right", tolerance=Tolerance(value="1/8", unit=Unit.INCH)
                ),
            ),
        )
    )


def test_an_unestablished_discriminator_abstains_before_arithmetic() -> None:
    finding = execute(publish(_with_variants()), {"width": _operand()}, discriminators={})
    assert finding.outcome is Outcome.REVIEW_REQUIRED
    assert not CALLS, "the layout was unknown; nothing should have been computed"
    assert "not guessed" in finding.reason


def test_an_uncovered_layout_is_no_applicable_rule_not_a_pass() -> None:
    """ADR-0004. An island countertop matches no variant, and silence would read as clean."""
    finding = execute(
        publish(_with_variants()), {"width": _operand()}, discriminators={"wall_config": "island"}
    )
    assert finding.outcome is Outcome.NO_APPLICABLE_RULE
    assert finding.outcome is not Outcome.PASS
    assert not CALLS


def test_the_matching_variant_is_recorded_on_the_finding() -> None:
    finding = execute(
        publish(_with_variants()),
        {"width": _operand()},
        discriminators={"wall_config": "back_left_right"},
    )
    assert finding.variant == "back_left_right"


def test_an_unconfirmed_tolerance_can_never_decide() -> None:
    """No tolerance exists in the client material yet (#10); a rule may be authored, but it must
    not produce PASS or FAIL from a value nobody supplied."""
    rule = _rule(
        operation=OperationRef(
            type="spy", operands={"x": "width"}, tolerance=Tolerance(value=TOLERANCE_UNCONFIRMED)
        )
    )
    finding = execute(publish(rule), {"width": _operand()})
    assert finding.outcome is Outcome.REVIEW_REQUIRED
    assert "not zero" in finding.reason
    assert not CALLS


# ---------------------------------------------------------------------------
# Step 2 — qualification, before arithmetic
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [EvidenceStatus.RAW_CANDIDATE, EvidenceStatus.CONFLICTING, EvidenceStatus.REJECTED],
)
def test_an_unqualified_operand_abstains_before_arithmetic(status: EvidenceStatus) -> None:
    finding = execute(publish(_rule()), {"width": _operand(status=status)})
    assert finding.outcome is Outcome.REVIEW_REQUIRED
    assert not CALLS, f"{status.value} reached the operation; step 2 ran too late"
    assert status.value in finding.reason


def test_conflicting_evidence_is_never_resolved_by_preference() -> None:
    finding = execute(publish(_rule()), {"width": _operand(status=EvidenceStatus.CONFLICTING)})
    assert finding.outcome is Outcome.REVIEW_REQUIRED
    assert "preferring" in finding.reason


@pytest.mark.parametrize("status", [EvidenceStatus.CORROBORATED, EvidenceStatus.HUMAN_CONFIRMED])
def test_the_two_qualified_states_may_proceed(status: EvidenceStatus) -> None:
    execute(publish(_rule()), {"width": _operand(status=status)})
    assert len(CALLS) == 1


# ---------------------------------------------------------------------------
# Step 3 — presence, before arithmetic
# ---------------------------------------------------------------------------


def test_a_missing_operand_is_not_found_and_never_computed() -> None:
    finding = execute(publish(_rule()), {"width": _operand(value=None)})
    assert finding.outcome is Outcome.NOT_FOUND
    assert not CALLS
    assert "not a passing value" in finding.reason


def test_an_empty_string_counts_as_missing() -> None:
    finding = execute(publish(_rule()), {"width": _operand(value="  ")})
    assert finding.outcome is Outcome.NOT_FOUND


def test_zero_is_a_real_value_and_proceeds() -> None:
    """ADR-0012. `0 mm` is a flush edge, not an absence — treating it as missing would turn a
    real measurement into NOT_FOUND."""
    finding = execute(
        publish(_rule()), {"width": _operand(value=Measurement(Fraction(0), Unit.MM, "0"))}
    )
    assert finding.outcome is Outcome.PASS
    assert len(CALLS) == 1


# ---------------------------------------------------------------------------
# Step 4 — one authored unit system, before arithmetic
# ---------------------------------------------------------------------------


def _two_operands(second_unit: Unit) -> dict[str, VerdictOperand]:
    return {
        "width": _operand(),
        "other": _operand(value=Measurement(Fraction(236), second_unit, "236"), name="other"),
    }


def test_mixed_authored_units_abstain_before_arithmetic() -> None:
    """ADR-0001: on the real drawing the two renderings differ by up to 1.600 mm against a
    1/16 inch tolerance of 1.5875 mm, so a silent conversion can spend the whole budget."""
    rule = _rule(
        inputs={
            "width": InputSelector(
                source=OperandSource.SHOP,
                semantic_type=SemanticType.COUNTERTOP_OVERALL_WIDTH,
            ),
            "other": InputSelector(
                source=OperandSource.SHOP, semantic_type=SemanticType.CABINET_WIDTH
            ),
        }
    )
    finding = execute(publish(rule), _two_operands(Unit.INCH))
    assert finding.outcome is Outcome.REVIEW_REQUIRED
    assert not CALLS
    assert "cross-unit allowance" in finding.reason


def test_a_declared_allowance_permits_the_comparison_and_is_noted() -> None:
    rule = _rule(
        cross_unit_allowance=Quantity(value="1/16", unit=Unit.INCH),
        inputs={
            "width": InputSelector(
                source=OperandSource.SHOP,
                semantic_type=SemanticType.COUNTERTOP_OVERALL_WIDTH,
            ),
            "other": InputSelector(
                source=OperandSource.SHOP, semantic_type=SemanticType.CABINET_WIDTH
            ),
        },
    )
    finding = execute(publish(rule), _two_operands(Unit.INCH))
    assert finding.outcome is Outcome.PASS
    assert any("allowance" in n for n in finding.notes)


def test_one_authored_unit_needs_no_allowance() -> None:
    rule = _rule(
        inputs={
            "width": InputSelector(
                source=OperandSource.SHOP,
                semantic_type=SemanticType.COUNTERTOP_OVERALL_WIDTH,
            ),
            "other": InputSelector(
                source=OperandSource.SHOP, semantic_type=SemanticType.CABINET_WIDTH
            ),
        }
    )
    assert execute(publish(rule), _two_operands(Unit.MM)).outcome is Outcome.PASS


# ---------------------------------------------------------------------------
# The ordering itself
# ---------------------------------------------------------------------------


def test_qualification_is_checked_before_presence() -> None:
    """An operand that is both unqualified and absent must report the qualification problem:
    the earlier gate wins, so the reviewer is told the first thing that went wrong."""
    finding = execute(
        publish(_rule()),
        {"width": _operand(value=None, status=EvidenceStatus.RAW_CANDIDATE)},
    )
    assert finding.outcome is Outcome.REVIEW_REQUIRED
    assert "RAW_CANDIDATE" in finding.reason


def test_applicability_is_checked_before_qualification() -> None:
    finding = execute(
        publish(_with_variants()),
        {"width": _operand(status=EvidenceStatus.REJECTED)},
        discriminators={},
    )
    assert "not guessed" in finding.reason


def test_no_abstention_path_ever_yields_pass() -> None:
    """The property that matters most: four gates, none of which can produce a decision."""
    cases = [
        ({"width": _operand(status=EvidenceStatus.RAW_CANDIDATE)}, {}),
        ({"width": _operand(value=None)}, {}),
    ]
    for operands, disc in cases:
        finding = execute(publish(_rule()), operands, discriminators=disc)
        assert finding.outcome is not Outcome.PASS
        assert not finding.is_decision


# ---------------------------------------------------------------------------
# Step 7 — the finding carries what defends it
# ---------------------------------------------------------------------------


def test_a_decision_carries_a_trace_and_an_abstention_does_not() -> None:
    decided = execute(publish(_rule()), {"width": _operand()})
    assert decided.trace is not None

    abstained = execute(publish(_rule()), {"width": _operand(value=None)})
    assert abstained.trace is None, "nothing was calculated; an empty trace would imply it was"


def test_the_finding_names_the_snapshot_that_produced_it() -> None:
    snapshot = publish(_rule())
    finding = execute(snapshot, {"width": _operand()})
    assert finding.snapshot_id == snapshot.snapshot_id
    assert finding.engine_version == ENGINE_VERSION


def test_evidence_references_survive_onto_the_finding() -> None:
    finding = execute(publish(_rule()), {"width": _operand()})
    assert "p3:poly-1" in finding.evidence_refs


def test_an_overridden_company_standard_is_noted_on_the_finding() -> None:
    """#65 recorded the override; the finding is where a reviewer actually sees it."""
    standard = ParameterValue(
        value=Quantity(value="4", unit=Unit.INCH),
        provenance=Provenance.COMPANY_STANDARD,
        set_by="GV",
        set_at=WHEN,
    )
    override = ParameterValue(
        value=Quantity(value="3", unit=Unit.INCH),
        provenance=Provenance.GC_CLIENT,
        set_by="Raj",
        set_at=WHEN,
    )
    resolved = resolve(
        "sink_front_offset",
        ParameterSet(None, ParameterLayer.GLOBAL, 1, {"sink_front_offset": standard}),
        ParameterSet("GV-2026-ABC", ParameterLayer.PROJECT, 1, {"sink_front_offset": override}),
    )
    finding = execute(publish(_rule()), {"width": _operand()}, {"sink_front_offset": resolved})
    assert any("overrides a company standard" in n for n in finding.notes)


def test_a_critical_failure_is_identifiable() -> None:
    """The primary release metric is false-PASS on CRITICAL rules, so a finding has to say."""
    finding = execute(publish(_rule()), {"width": _operand()})
    assert finding.severity is Severity.CRITICAL
    assert not finding.is_critical_failure  # it passed


def test_an_operation_that_raises_abstains_rather_than_passing() -> None:
    """An unexpected failure means we do not know the answer. AGENTS.md §2.4 — never turn
    uncertainty into a pass."""

    def _explode(**_: object) -> OperationResult:
        raise RuntimeError("something went wrong mid-calculation")

    REGISTRY.pop("spy", None)
    register(OperationSpec(name="spy", version="1.0.0", operands={"x": Arity.SCALAR}, fn=_explode))

    finding = execute(publish(_rule()), {"width": _operand()})
    assert finding.outcome is Outcome.REVIEW_REQUIRED
    assert finding.outcome is not Outcome.PASS
    assert "could not be completed" in finding.reason
