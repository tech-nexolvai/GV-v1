"""The CT-3 sink checks: cutout width, cutout depth and front offset.

Source: issue #426 and #60; client facts Q5 (front offset exact, configurable 4 inches), Q7 (the
reviewer supplies the sink's dimensions per drawing set), Q2 (exact matching for V1), Q15 (cutout
width comes from the interior *width*, answered on the 2026-08-25 call).
Verification: ``rules/rulebook/ct_sink_cutout_width_001.yaml``,
``rules/rulebook/ct_sink_cutout_depth_001.yaml`` and
``rules/rulebook/ct_sink_offset_front_001.yaml``.

**The cutout width rule used to be deliberately absent**, because the client's notes called its
input a depth while his diagram showed a width, and authoring it would have meant choosing between
his text and his drawing — a guess on a dimension that gets cut in stone. We asked instead. On the
2026-08-25 call Raj confirmed the diagram ("my CT012 is width") and it is authored here now. The
answer matched the natural reading, which is exactly the point: being right by luck and being right
on purpose look identical afterwards, and only one of them is repeatable.

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
from rules.schema import ParameterScope, Quantity, Rule
from rules.semantic_types import SemanticType
from rules.snapshot import publish
from units.measurement import Measurement, Unit
from verdict.engine import execute
from verdict.operands import EvidenceStatus, VerdictOperand
from verdict.operations import register_all
from verdict.outcomes import Outcome, Severity
from verdict.registry import REGISTRY

RULEBOOK = Path(__file__).resolve().parents[2] / "rules" / "rulebook"
WIDTH_RULE_PATH = RULEBOOK / "ct_sink_cutout_width_001.yaml"
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


def _width_parameters(
    interior: int | Fraction = 33, clearance: int | Fraction = Fraction(1, 4)
) -> dict[str, ResolvedParameter]:
    """The sink's interior width, and the undermount clearance taken off each side."""
    return {
        "sink_interior_width": _parameter("sink_interior_width", interior, ParameterLayer.RUN),
        "sink_cutout_clearance": _parameter("sink_cutout_clearance", clearance),
    }


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


def test_every_sink_rule_is_an_exact_inch_check_with_no_tolerance() -> None:
    """Input: the three YAML files. Outcome: exact equality, no band. Why: Q2 settled exact match,
    so a tolerance here would be a band nobody asked for."""
    for path in (WIDTH_RULE_PATH, DEPTH_RULE_PATH, FRONT_OFFSET_RULE_PATH):
        rule = _load(path)
        assert rule.arithmetic_unit is Unit.INCH
        assert rule.operation.type == "equals"
        assert tolerances_of(rule) == ()


def test_the_new_width_rule_does_not_declare_itself_critical() -> None:
    """**Q4: V1 flags everything, with no severity split.**

    The client chose that deliberately — whether a flag is serious depends on the project, and they
    will not know until 10–50 projects have run. So `critical_false_pass_rate` reading NOT MEASURED
    is the designed state, not a gap in the evaluation.

    Asserted on the rule authored *after* that answer. The six older rules still say CRITICAL, which
    predates Q4 and contradicts it — see the note in this module's companion issue rather than
    reading their silence here as agreement.
    """
    assert _load(WIDTH_RULE_PATH).severity is not Severity.CRITICAL


def test_the_four_sink_rules_have_distinct_ids() -> None:
    """The client's workbook labels three different checks CT-3, so ours must not.

    Asserted against the rulebook rather than against literals: a future copy-paste that reuses an
    id fails here, which is the mistake worth catching.
    """
    ids = [
        _load(p).id
        for p in (WIDTH_RULE_PATH, DEPTH_RULE_PATH, FRONT_OFFSET_RULE_PATH, BACK_OFFSET_RULE_PATH)
    ]
    assert len(set(ids)) == 4, f"sink rule ids collide: {ids}"
    assert "CT-3" not in ids


def test_each_rule_reads_the_semantic_type_it_claims() -> None:
    """A rule pointed at the wrong CT code would check a real number against the wrong quantity."""
    assert _load(WIDTH_RULE_PATH).inputs["cutout_width"].semantic_type is SemanticType.CT012
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
# Cutout width — width from width (Q15, answered on the 2026-08-25 call)
# ---------------------------------------------------------------------------


def test_cutout_width_passes_when_it_equals_the_interior_width_less_both_clearances() -> None:
    """Input: interior width 33, clearance 1/4, drawn cutout 32 1/2. Outcome: PASS."""
    finding = execute(
        publish(_load(WIDTH_RULE_PATH)),
        {"cutout_width": _operand("cutout_width", Fraction(65, 2))},
        _width_parameters(),
    )

    assert finding.outcome is Outcome.PASS
    assert finding.trace is not None
    assert finding.trace.tolerance is None


def test_an_eighth_out_fails_rather_than_being_absorbed() -> None:
    """Input: cutout 1/8 wider than expected. Outcome: FAIL. Why: exact match, no hidden band."""
    finding = execute(
        publish(_load(WIDTH_RULE_PATH)),
        {"cutout_width": _operand("cutout_width", Fraction(65, 2) + Fraction(1, 8))},
        _width_parameters(),
    )

    assert finding.outcome is Outcome.FAIL


def test_the_width_rule_reads_the_interior_width_and_never_the_depth() -> None:
    """**The whole reason this rule was held for a month.**

    The client's spreadsheet said the cutout width came from the interior *depth* (S19) while his
    diagram showed it coming from the width. Authoring from the text would have produced a rule that
    computes a real number, passes its own tests, and sizes the hole from the wrong dimension — the
    failure that survives review because nothing about it looks broken.

    Pinned two ways: the rule consumes `sink_interior_width`, and supplying only the depth leaves it
    unable to decide rather than quietly reaching for whatever else is present.
    """
    rule = _load(WIDTH_RULE_PATH)
    assert "sink_interior_width" in rule.parameters
    assert "sink_interior_depth" not in rule.parameters

    finding = execute(
        publish(rule),
        {"cutout_width": _operand("cutout_width", Fraction(65, 2))},
        {
            "sink_interior_depth": _parameter("sink_interior_depth", 18, ParameterLayer.RUN),
            "sink_cutout_clearance": _parameter("sink_cutout_clearance", Fraction(1, 4)),
        },
    )

    assert finding.outcome is Outcome.NOT_FOUND


def test_the_width_clearance_is_editable_and_moves_the_verdict() -> None:
    """Raj: "1/4, but make sure that is editable, sometimes it's 1/8 — a project-specific variable."

    A constant would be right for most fabricators and silently wrong for the rest. At 1/8 the same
    drawn 32 1/2 that passed above must fail, and 32 3/4 must pass — which is only true if the
    parameter genuinely feeds the arithmetic.
    """
    rule = publish(_load(WIDTH_RULE_PATH))
    eighth = _width_parameters(clearance=Fraction(1, 8))

    assert (
        execute(rule, {"cutout_width": _operand("cutout_width", Fraction(65, 2))}, eighth).outcome
        is Outcome.FAIL
    )
    assert (
        execute(rule, {"cutout_width": _operand("cutout_width", Fraction(131, 4))}, eighth).outcome
        is Outcome.PASS
    )


def test_the_width_and_depth_rules_share_one_clearance_parameter() -> None:
    """One name, so one project override sets the undermount clearance for both cutout checks.

    Parameters resolve by name across the layers, so a reviewer who says "this fabricator works to
    1/8" says it once. Two names would let the pair drift — a cutout narrowed to the new clearance
    and deepened to the old one, each rule internally consistent and the hole wrong.
    """
    assert "sink_cutout_clearance" in _load(WIDTH_RULE_PATH).parameters
    assert "sink_cutout_clearance" in _load(DEPTH_RULE_PATH).parameters


def test_the_clearance_is_scoped_per_project_and_the_sink_width_per_run() -> None:
    """**Scope is the whole point of Q15's answer, and nothing else here would notice it changing.**

    Raj asked for the clearance to be editable *per project* because it varies by fabricator. Scoped
    GLOBAL it would still have a 1/4 default, still compute, and still pass every arithmetic test in
    this module — while quietly becoming a company-wide constant a reviewer cannot set for their
    project, which is the thing he specifically said it must not be. It would also change what the
    publication gate does with it (A6_3 D6).

    The sink's own width is RUN-scoped for the opposite reason: it comes off a cut sheet with the
    drawing set, and is not a standard anybody configures.
    """
    parameters = _load(WIDTH_RULE_PATH).parameters

    assert parameters["sink_cutout_clearance"].scope is ParameterScope.PROJECT
    assert parameters["sink_interior_width"].scope is ParameterScope.RUN


def test_a_missing_sink_interior_width_is_not_found_rather_than_assumed() -> None:
    """Every sink is different, so there is no typical interior width to fall back on."""
    finding = execute(
        publish(_load(WIDTH_RULE_PATH)),
        {"cutout_width": _operand("cutout_width", Fraction(65, 2))},
        {"sink_cutout_clearance": _parameter("sink_cutout_clearance", Fraction(1, 4))},
    )

    assert finding.outcome is Outcome.NOT_FOUND


def test_the_width_rule_declares_no_default_interior_width() -> None:
    """The test above proves the behaviour; this proves the rule cannot acquire a default quietly."""
    assert _load(WIDTH_RULE_PATH).parameters["sink_interior_width"].default is None


def test_the_width_rule_may_be_released() -> None:
    """It carries no unconfirmed tolerance and no missing client-owed standard, so nothing holds it.

    Its `sink_interior_width` has no default and still does not block release: run-scoped, supplied
    per drawing set, so its absence at publish says nothing about its absence at run time.
    """
    assert is_production_ready(_load(WIDTH_RULE_PATH))


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


# ---------------------------------------------------------------------------
# Abstention paths — the states where a false PASS would be worst
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status", [EvidenceStatus.RAW_CANDIDATE, EvidenceStatus.CONFLICTING, EvidenceStatus.REJECTED]
)
def test_an_unqualified_reading_never_reaches_the_arithmetic(status: EvidenceStatus) -> None:
    """Input: a drawn offset that is not CORROBORATED or HUMAN_CONFIRMED. Outcome: REVIEW REQUIRED.

    Only two statuses may enter a verdict. A `CONFLICTING` reading is the sharp case: two readers
    disagreed about the number, and deciding either way would be picking a winner by arithmetic.
    """
    finding = execute(
        publish(_load(FRONT_OFFSET_RULE_PATH)),
        {
            "front_offset": VerdictOperand(
                name="front_offset",
                value=_inch(4),
                status=status,
                source="SHOP",
                evidence_ref="shop:p1:front_offset",
            )
        },
        _required(),
    )

    assert finding.outcome is Outcome.REVIEW_REQUIRED


def test_a_millimetre_reading_does_not_silently_convert_into_an_inch_rule() -> None:
    """Input: CT007 authored in mm against an inch rule. Outcome: REVIEW REQUIRED.

    ADR-0001 and client fact Q12: inches are authoritative on these drawings, and mm is the
    vendor's machine reference. Converting would produce a confident verdict from a number the
    rule was not written against — 101.6 mm and 4 inches are equal, and the point is that the
    engine must not be the thing that decides they are.
    """
    finding = execute(
        publish(_load(FRONT_OFFSET_RULE_PATH)),
        {
            "front_offset": VerdictOperand(
                name="front_offset",
                value=Measurement(Fraction(508, 5), Unit.MM, "101.6"),
                status=EvidenceStatus.CORROBORATED,
                source="SHOP",
                evidence_ref="shop:p1:front_offset",
            )
        },
        _required(),
    )

    assert finding.outcome is Outcome.REVIEW_REQUIRED


def test_an_unqualified_cutout_depth_abstains_too() -> None:
    """The same guard on the other rule, so neither can drift from it."""
    finding = execute(
        publish(_load(DEPTH_RULE_PATH)),
        {
            "cutout_depth": VerdictOperand(
                name="cutout_depth",
                value=_inch(Fraction(35, 2)),
                status=EvidenceStatus.RAW_CANDIDATE,
                source="SHOP",
                evidence_ref="shop:p1:cutout_depth",
            )
        },
        _depth_parameters(),
    )

    assert finding.outcome is Outcome.REVIEW_REQUIRED


def test_a_missing_clearance_is_not_found_rather_than_the_typical_quarter_inch() -> None:
    """Input: interior depth supplied, clearance unresolved. Outcome: NOT_FOUND.

    The rule declares a quarter-inch default because the client calls it typical, but the engine
    does not read that declaration — resolving it is the parameter layer's job. So an unresolved
    clearance abstains here rather than sizing a cutout against a value nobody set for this
    project.
    """
    finding = execute(
        publish(_load(DEPTH_RULE_PATH)),
        {"cutout_depth": _operand("cutout_depth", Fraction(35, 2))},
        {"sink_interior_depth": _parameter("sink_interior_depth", 18, ParameterLayer.RUN)},
    )

    assert finding.outcome is Outcome.NOT_FOUND
