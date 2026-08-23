"""The two CT-3 sink checks that are not blocked: cutout depth and front offset.

Source: issue #426; client facts Q5 (front offset exact, configurable 4 inches), Q7 (the reviewer
supplies the sink's dimensions per drawing set), Q2 (exact matching for V1).
Verification: ``rules/rulebook/ct_sink_cutout_depth_001.yaml`` and
``rules/rulebook/ct_sink_offset_front_001.yaml``.

**The cutout width rule is deliberately absent.** Its formula takes the sink's interior dimension,
and the client's notes call that a depth while his diagram shows a width (Q15, still open). Authoring
it would mean choosing between his text and his drawing — a guess on a dimension that gets cut. It
stays on #60 until he answers.

Most of what follows is about the two ways these checks can be wrong without looking wrong: a
parameter treated as a constant, and an equality quietly behaving as a minimum.
"""

from __future__ import annotations

from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path

import pytest
import yaml

from rules.parameters import ParameterLayer, ParameterValue, Provenance, ResolvedParameter
from rules.publication import is_production_ready, tolerances_of
from rules.schema import Quantity, Rule
from rules.semantic_types import SemanticType
from rules.snapshot import publish
from units.measurement import Measurement, Unit
from verdict.engine import execute
from verdict.operands import EvidenceStatus, VerdictOperand
from verdict.operations import register_all
from verdict.outcomes import Outcome, Severity
from verdict.registry import REGISTRY

RULEBOOK = Path(__file__).resolve().parents[2] / "rules" / "rulebook"
DEPTH_RULE_PATH = RULEBOOK / "ct_sink_cutout_depth_001.yaml"
FRONT_OFFSET_RULE_PATH = RULEBOOK / "ct_sink_offset_front_001.yaml"
BACK_OFFSET_RULE_PATH = RULEBOOK / "ct_back_offset_min_001.yaml"
WHEN = datetime(2026, 8, 24, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _registered_operations() -> object:
    previous = dict(REGISTRY)
    REGISTRY.clear()
    register_all()
    yield
    REGISTRY.clear()
    REGISTRY.update(previous)


def _load(path: Path) -> Rule:
    return Rule.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def _inch(value: int | Fraction) -> Measurement:
    return Measurement(Fraction(value), Unit.INCH, str(value))


def _operand(name: str, value: int | Fraction) -> VerdictOperand:
    return VerdictOperand(
        name=name,
        value=_inch(value),
        status=EvidenceStatus.CORROBORATED,
        source="SHOP",
        evidence_ref=f"shop:p1:{name}",
    )


def _parameter(
    name: str, value: int | Fraction, layer: ParameterLayer = ParameterLayer.PROJECT
) -> ResolvedParameter:
    return ResolvedParameter(
        name=name,
        value=ParameterValue(
            value=Quantity(value=value, unit=Unit.INCH),
            provenance=Provenance.GC_CLIENT,
            set_by="project reviewer",
            set_at=WHEN,
        ),
        layer=layer,
    )


def _depth_parameters(
    interior: int | Fraction = 18, clearance: int | Fraction = Fraction(1, 4)
) -> dict[str, ResolvedParameter]:
    return {
        "sink_interior_depth": _parameter("sink_interior_depth", interior, ParameterLayer.RUN),
        "sink_cutout_clearance": _parameter("sink_cutout_clearance", clearance),
    }


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------


def test_both_rules_are_exact_critical_inch_checks_with_no_tolerance() -> None:
    """Input: the two YAML files. Outcome: exact equality, no band. Why: Q2 settled exact match,
    so a tolerance here would be a band nobody asked for."""
    for path in (DEPTH_RULE_PATH, FRONT_OFFSET_RULE_PATH):
        rule = _load(path)
        assert rule.severity is Severity.CRITICAL
        assert rule.arithmetic_unit is Unit.INCH
        assert rule.operation.type == "equals"
        assert tolerances_of(rule) == ()


def test_the_three_sink_rules_have_distinct_ids() -> None:
    """The client's workbook labels three different checks CT-3, so ours must not.

    Asserted against the rulebook rather than against literals: a future copy-paste that reuses an
    id fails here, which is the mistake worth catching.
    """
    ids = [_load(p).id for p in (DEPTH_RULE_PATH, FRONT_OFFSET_RULE_PATH, BACK_OFFSET_RULE_PATH)]
    assert len(set(ids)) == 3, f"sink rule ids collide: {ids}"
    assert "CT-3" not in ids


def test_each_rule_reads_the_semantic_type_it_claims() -> None:
    """A rule pointed at the wrong CT code would check a real number against the wrong quantity."""
    assert _load(DEPTH_RULE_PATH).inputs["cutout_depth"].semantic_type is SemanticType.CT008
    assert _load(FRONT_OFFSET_RULE_PATH).inputs["front_offset"].semantic_type is SemanticType.CT007


def test_both_rules_may_be_released() -> None:
    """Neither carries an unconfirmed tolerance, so neither is held back.

    The depth rule's `sink_interior_depth` has no default and still does not block release: it is
    run-scoped, supplied by the reviewer per drawing set, so its absence at publish says nothing
    about its absence at run time. That distinction is the one #427 has to preserve.
    """
    assert is_production_ready(_load(DEPTH_RULE_PATH))
    assert is_production_ready(_load(FRONT_OFFSET_RULE_PATH))


# ---------------------------------------------------------------------------
# Cutout depth
# ---------------------------------------------------------------------------


def test_cutout_depth_passes_when_it_equals_the_interior_less_both_clearances() -> None:
    """Input: interior 18, clearance 1/4, drawn cutout 17.5. Outcome: PASS."""
    finding = execute(
        publish(_load(DEPTH_RULE_PATH)),
        {"cutout_depth": _operand("cutout_depth", Fraction(35, 2))},
        _depth_parameters(),
    )

    assert finding.outcome is Outcome.PASS
    assert finding.trace is not None
    assert finding.trace.tolerance is None


def test_one_sixteenth_out_fails_rather_than_being_absorbed() -> None:
    """Input: cutout 1/16 deeper than expected. Outcome: FAIL. Why: there is no hidden band."""
    finding = execute(
        publish(_load(DEPTH_RULE_PATH)),
        {"cutout_depth": _operand("cutout_depth", Fraction(35, 2) + Fraction(1, 16))},
        _depth_parameters(),
    )

    assert finding.outcome is Outcome.FAIL


def test_the_clearance_is_a_parameter_and_not_a_constant() -> None:
    """The client calls the quarter inch typical and says it varies by fabricator.

    A literal would be right for most drawings and silently wrong for the rest, so this proves the
    computed expectation moves when the parameter does: at clearance 1/2, the same drawn 17.5 that
    passed above must now fail, and 17 must pass.
    """
    rule = publish(_load(DEPTH_RULE_PATH))
    half = _depth_parameters(clearance=Fraction(1, 2))

    assert (
        execute(rule, {"cutout_depth": _operand("cutout_depth", Fraction(35, 2))}, half).outcome
        is Outcome.FAIL
    )
    assert (
        execute(rule, {"cutout_depth": _operand("cutout_depth", 17)}, half).outcome is Outcome.PASS
    )


def test_a_missing_sink_interior_depth_is_not_found_rather_than_assumed() -> None:
    """Input: no sink dimension supplied. Outcome: NOT_FOUND.

    Every sink is different, so there is no typical interior depth to fall back on. A plausible
    default here would size a hole that gets cut — `AGENTS.md` §2.4.
    """
    finding = execute(
        publish(_load(DEPTH_RULE_PATH)),
        {"cutout_depth": _operand("cutout_depth", Fraction(35, 2))},
        {"sink_cutout_clearance": _parameter("sink_cutout_clearance", Fraction(1, 4))},
    )

    assert finding.outcome is Outcome.NOT_FOUND


def test_the_depth_rule_declares_no_default_interior_depth() -> None:
    """The test above proves the behaviour; this proves the rule cannot acquire a default quietly."""
    assert _load(DEPTH_RULE_PATH).parameters["sink_interior_depth"].default is None


# ---------------------------------------------------------------------------
# Front offset — exact, not a minimum
# ---------------------------------------------------------------------------


def _required(value: int | Fraction = 4) -> dict[str, ResolvedParameter]:
    """The resolved required offset.

    Passed explicitly because `execute` does not read the rule's declared `default` — resolving
    that is the parameter layer's job (`rules/parameters.py`), and no caller wires it into the
    engine yet. `cab_filler_001` is tested the same way. The declaration is asserted separately
    below, so both halves are pinned: what the rule promises, and what the engine currently does.
    """
    return {"front_offset_required": _parameter("front_offset_required", value)}


def test_the_front_offset_passes_at_exactly_four_inches() -> None:
    finding = execute(
        publish(_load(FRONT_OFFSET_RULE_PATH)),
        {"front_offset": _operand("front_offset", 4)},
        _required(),
    )

    assert finding.outcome is Outcome.PASS


@pytest.mark.parametrize("drawn", [Fraction(39, 10), Fraction(41, 10)])
def test_the_front_offset_fails_on_either_side_of_four(drawn: Fraction) -> None:
    """Boundary-exact, both directions — the test that separates equality from a minimum.

    A minimum would pass 4.1 inches, and an offset larger than intended is a visible error on the
    countertop's front edge. Q5's answer is a configurable value checked exactly, not a floor.
    """
    finding = execute(
        publish(_load(FRONT_OFFSET_RULE_PATH)),
        {"front_offset": _operand("front_offset", drawn)},
        _required(),
    )

    assert finding.outcome is Outcome.FAIL


def test_the_required_offset_is_configurable_per_project() -> None:
    """The client asked to change it "under special circumstances", so it must not be a constant."""
    rule = publish(_load(FRONT_OFFSET_RULE_PATH))
    five = _required(5)

    assert (
        execute(rule, {"front_offset": _operand("front_offset", 5)}, five).outcome is Outcome.PASS
    )
    assert (
        execute(rule, {"front_offset": _operand("front_offset", 4)}, five).outcome is Outcome.FAIL
    )


def test_the_four_inch_default_is_declared_on_the_rule() -> None:
    """So the default is visible in the rulebook rather than living in a caller."""
    default = _load(FRONT_OFFSET_RULE_PATH).parameters["front_offset_required"].default
    assert default is not None
    assert default.exact_value == Fraction(4)
    assert default.unit is Unit.INCH


def test_a_missing_front_offset_is_not_found() -> None:
    """The offset is read off the drawing; absent, there is nothing to check.

    The required value is supplied, so this isolates the missing *drawn* value — otherwise the
    test would pass whichever of the two was absent, and prove neither.
    """
    finding = execute(publish(_load(FRONT_OFFSET_RULE_PATH)), {}, _required())

    assert finding.outcome is Outcome.NOT_FOUND


def test_an_unresolved_required_offset_is_not_found_rather_than_defaulted() -> None:
    """The engine does not silently fall back on the rule's declared default.

    That default belongs to the parameter layer, which resolves GLOBAL then PROJECT then RUN. Until
    a caller wires it in, an unresolved parameter must abstain rather than let the engine invent the
    four inches itself — which is `AGENTS.md` §2.4, and the reason the declaration and the
    resolution are separate things.
    """
    finding = execute(
        publish(_load(FRONT_OFFSET_RULE_PATH)),
        {"front_offset": _operand("front_offset", 4)},
        {},
    )

    assert finding.outcome is Outcome.NOT_FOUND
