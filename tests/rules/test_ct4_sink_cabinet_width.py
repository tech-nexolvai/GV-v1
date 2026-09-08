"""The sink cabinet's width against what it has to contain (#537).

Verification for: `rules/rulebook/ct_sink_cabinet_width_001.yaml`.

Authored from `C_Tops_Checks_New.pptx` (2026-09-07), slide 8:
`CT004 = CAB_SIDE_THK + CT011 + CT012 + CT013 + CAB_SIDE_THK`.

This is what the old "width of countertop = cutout + F + G" was actually about, now that the deck
names the parts: not the countertop's width at all, but the **sink cabinet's** — two side panels, the
cutout, and the clearance either side of it. `CLIENT_FACTS` Q19.

No drawings and no semantic typing are involved: the relation is arithmetic the client wrote down,
and every operand is supplied here the way a reviewer supplies one.
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

RULE_PATH = (
    Path(__file__).resolve().parents[2] / "rules" / "rulebook" / "ct_sink_cabinet_width_001.yaml"
)
WHEN = datetime(2026, 9, 7, tzinfo=UTC)

#: A 36-inch sink cabinet: 3/4 panel + 4 1/2 clearance + 26 1/2 cutout + 3 1/2 clearance + 3/4 panel.
#:
#: The numbers add up to exactly 36 and none of them is symmetric — a sink is not always centred, and
#: two equal clearances would let a rule that added the same one twice pass this test.
PANEL = Fraction(3, 4)
CLEARANCE_LEFT = Fraction(9, 2)
CUTOUT = Fraction(53, 2)
CLEARANCE_RIGHT = Fraction(7, 2)
CABINET = Fraction(36)


@pytest.fixture(autouse=True)
def _registered_operations() -> object:
    previous = dict(REGISTRY)
    REGISTRY.clear()
    register_all()
    yield
    REGISTRY.clear()
    REGISTRY.update(previous)


def _load() -> Rule:
    return Rule.model_validate(yaml.safe_load(RULE_PATH.read_text(encoding="utf-8")))


def _operand(name: str, value: int | Fraction) -> VerdictOperand:
    return VerdictOperand(
        name=name,
        value=Measurement(Fraction(value), Unit.INCH, str(value)),
        status=EvidenceStatus.CORROBORATED,
        source="SHOP",
        evidence_ref=f"shop:p1:{name}",
    )


def _parameter(name: str, value: int | Fraction) -> ResolvedParameter:
    return ResolvedParameter(
        name=name,
        value=ParameterValue(
            value=Quantity(value=value, unit=Unit.INCH),
            provenance=Provenance.GC_CLIENT,
            set_by="project reviewer",
            set_at=WHEN,
        ),
        layer=ParameterLayer.PROJECT,
    )


def _inputs(cabinet: int | Fraction = CABINET) -> dict[str, VerdictOperand]:
    return {
        "sink_cabinet_width": _operand("sink_cabinet_width", cabinet),
        "clearance_left": _operand("clearance_left", CLEARANCE_LEFT),
        "cutout_width": _operand("cutout_width", CUTOUT),
        "clearance_right": _operand("clearance_right", CLEARANCE_RIGHT),
    }


def test_the_rule_is_an_exact_v1_flag_inch_check() -> None:
    """Q2: V1 compares exactly, so this is `equals` and carries no tolerance band.

    `is_production_ready` matters here: unlike `CAB-ARCH-VS-SHOP-001`, this relation needs no value
    the client still owes, so it is publishable to production as authored rather than held.
    """
    rule = _load()

    assert rule.id == "CT-SINK-CABINET-WIDTH-001"
    assert rule.severity is Severity.FLAG
    assert rule.arithmetic_unit is Unit.INCH
    assert rule.operation.type == "equals"
    assert tolerances_of(rule) == ()
    assert is_production_ready(rule)


def test_the_rule_reads_the_client_codes_the_deck_names() -> None:
    """The four operands are the deck's own codes, not our descriptive names.

    Asserted because the codes are anchored to a diagram and the names are not: `CT011` and `CT013`
    are the left and right clearances *positionally*, and a rule authored against a generic
    "clearance" would not know which side it had.
    """
    rule = _load()

    assert rule.inputs["sink_cabinet_width"].semantic_type is SemanticType.CT004
    assert rule.inputs["clearance_left"].semantic_type is SemanticType.CT011
    assert rule.inputs["cutout_width"].semantic_type is SemanticType.CT012
    assert rule.inputs["clearance_right"].semantic_type is SemanticType.CT013


def test_a_cabinet_that_contains_its_parts_exactly_passes() -> None:
    """Input: 3/4 + 4 1/2 + 26 1/2 + 3 1/2 + 3/4 against a 36-inch cabinet. Outcome: PASS."""
    finding = execute(
        publish(_load()),
        _inputs(),
        {"cabinet_side_thickness": _parameter("cabinet_side_thickness", PANEL)},
    )

    assert finding.outcome is Outcome.PASS
    assert finding.trace is not None


def test_a_sixteenth_out_is_a_fail() -> None:
    """The error this catches is one nobody sees by eye and every fabricator feels.

    A sixteenth is far too small to notice on a drawing and far too large in a cabinet that has to
    receive a sink of a fixed size.
    """
    finding = execute(
        publish(_load()),
        _inputs(CABINET + Fraction(1, 16)),
        {"cabinet_side_thickness": _parameter("cabinet_side_thickness", PANEL)},
    )

    assert finding.outcome is Outcome.FAIL


def test_both_side_panels_are_counted() -> None:
    """**Two panels, not one.** The deck writes `CAB_SIDE_THK` at each end of the sum.

    Asserted directly because the failure would be quiet and plausible: counting one panel makes
    every sink cabinet appear three quarters of an inch too wide, which reads as a fabrication error
    in the drawing rather than an arithmetic error in the rule. A cabinet short by exactly one panel
    must fail.
    """
    finding = execute(
        publish(_load()),
        _inputs(CABINET - PANEL),
        {"cabinet_side_thickness": _parameter("cabinet_side_thickness", PANEL)},
    )

    assert finding.outcome is Outcome.FAIL


def test_a_missing_side_panel_thickness_abstains_rather_than_assuming_three_quarters() -> None:
    """`CAB_SIDE_THK` is `Specified` — the client gives it, and nothing here may stand in for it.

    Three quarters of an inch is the obvious guess and it is still a guess. A rule that assumed it
    would decide a CRITICAL width from a number nobody supplied, and `AGENTS.md` §2.2 makes a missing
    input `NOT_FOUND` rather than a plausible default.
    """
    rule = _load()
    finding = execute(publish(rule), _inputs(), {})

    assert rule.parameters["cabinet_side_thickness"].default is None
    assert finding.outcome is Outcome.NOT_FOUND
    assert "cabinet_side_thickness" in finding.reason
