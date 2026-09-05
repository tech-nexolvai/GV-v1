"""The rulebook is dynamic where it should be, and the demonstration is a verdict.

Abhishek asked on the 2026-08-25 call that the rulebook be *"dynamic — project to project, product
to product, vendor-to-vendor… not hard-coded"*, and Raj answered that *"to some extent it has to be
hard-coded."* Both are right, and the architecture already draws the line between them. This file
proves the part that is dynamic by making the same published rule reach **different verdicts**,
without a line of Python changing between the two runs.

**Resolved through the real layers, not hand-fed.** `tests/rules/test_parameter_layering.py` proves
precedence and `tests/rules/test_project_scope.py` proves two projects do not share a set — but both
stop at resolution, and `tests/rules/test_applicability.py` never calls `execute` at all. A seam that
resolves correctly and is never carried into a decision is only half demonstrated, and the half that
was missing is the half a reviewer sees.

**What stays hard-coded, deliberately.** The arithmetic. A parameter changes the *numbers* a rule
compares; nothing here lets configuration change *how* it compares them, and no value in this file
comes from a model. `docs/decisions/DYNAMIC_RULEBOOK.md` sets out which seams exist and what is
deferred; `tests/test_vendor_neutrality.py` holds the one line that is not dynamic at all — a vendor
may not select a variant or key a parameter, because every vendor is held to GV's standards.
"""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime
from fractions import Fraction

import pytest
import yaml

from rules.parameters import (
    ParameterLayer,
    ParameterSet,
    ParameterValue,
    Provenance,
    resolve_all,
)
from rules.schema import Quantity, Rule
from rules.snapshot import publish
from units.measurement import Measurement, Unit
from verdict.engine import execute
from verdict.finding import Finding
from verdict.operands import EvidenceStatus, VerdictOperand
from verdict.operations import register_all
from verdict.outcomes import Outcome
from verdict.registry import REGISTRY

RULEBOOK = pathlib.Path(__file__).resolve().parents[2] / "rules" / "rulebook"
WHEN = datetime(2026, 9, 5, tzinfo=UTC)

#: One shop reading, used unchanged in every test here.
#:
#: Holding the measurement fixed is the point: if the verdict moves, only the project's settings moved
#: with it. A test that varied both would show that *something* changed and prove nothing about which.
MEASURED_DEPTH = Fraction(51, 2)  # 25 1/2"


@pytest.fixture(autouse=True)
def _registered_operations() -> object:
    """Fill the operation registry, and put it back afterwards.

    The registry is global and empty until something fills it. Without this every operation lookup
    fails and the engine converts the failure to REVIEW_REQUIRED — so a rule that decides perfectly
    well reads as an abstention, and the dynamism this file is demonstrating looks absent rather than
    unregistered. The same trap has now caught `run_checks`, `eval/harness.py` and this module.

    Saved and restored rather than merely filled, matching the sibling rule tests: a module that left
    the registry populated would hide the same gap in whatever ran after it.
    """
    previous = dict(REGISTRY)
    REGISTRY.clear()
    register_all()
    yield
    REGISTRY.clear()
    REGISTRY.update(previous)


def _rule(name: str) -> Rule:
    return Rule.model_validate(yaml.safe_load((RULEBOOK / name).read_text(encoding="utf-8")))


def _operand(name: str, value: Fraction) -> VerdictOperand:
    return VerdictOperand(
        name=name,
        value=Measurement(value, Unit.INCH, str(value)),
        status=EvidenceStatus.HUMAN_CONFIRMED,
        source="SHOP",
        evidence_ref=f"reviewer:{name}",
    )


def _project_set(project_id: str, values: dict[str, Fraction]) -> ParameterSet:
    """One project's settings, as the layer a reviewer's entries actually land in.

    `Provenance.MEASURED` because a person supplied them — `rules/parameters.py` restricts the human
    provenances to a closed set with no member a model could claim, which is the mechanism that keeps
    "dynamic" from sliding into "the model decides".
    """
    return ParameterSet(
        project_id=project_id,
        layer=ParameterLayer.PROJECT,
        version=1,
        parameters={
            name: ParameterValue(
                value=Quantity(value=value, unit=Unit.INCH),
                provenance=Provenance.MEASURED,
                set_by="project reviewer",
                set_at=WHEN,
            )
            for name, value in values.items()
        },
    )


def _depth_finding(project: ParameterSet, *extra: ParameterSet) -> Finding:
    """Run the depth rule for one project, resolving its parameters the way the pipeline does."""
    return execute(
        publish(_rule("ct_depth_001.yaml")),
        {"countertop_depth": _operand("countertop_depth", MEASURED_DEPTH)},
        resolve_all(project, *extra),
    )


def test_the_same_rule_gives_different_verdicts_in_two_projects() -> None:
    """**The dynamism, stated as a verdict.**

    One published rule, one shop reading of 25 1/2", two projects. The first builds on 24" cabinets
    with a 1 1/2" overhang and the countertop is right; the second uses a 1" overhang and the same
    countertop is a half inch too deep.

    Nothing in Python differs between the two runs. The rule is the same bytes, the measurement is
    the same fraction, and only the project's own settings moved.
    """
    agrees = _depth_finding(
        _project_set("apex", {"cabinet_depth": Fraction(24), "countertop_overhang": Fraction(3, 2)})
    )
    disagrees = _depth_finding(
        _project_set(
            "ridgewood", {"cabinet_depth": Fraction(24), "countertop_overhang": Fraction(1)}
        )
    )

    assert agrees.outcome is Outcome.PASS
    assert disagrees.outcome is Outcome.FAIL


def test_neither_project_changed_the_rule() -> None:
    """The two verdicts above come from **one** rule, and this is what makes that claim checkable.

    If the snapshots differed, the demonstration would be worthless — it would show that two rules
    disagree, which is unremarkable. Identical snapshot ids are what turn it into a statement about
    configuration.
    """
    first = publish(_rule("ct_depth_001.yaml"))
    second = publish(_rule("ct_depth_001.yaml"))

    assert first.snapshot_id == second.snapshot_id


def test_a_run_value_overrides_the_project_for_one_review_only() -> None:
    """The third layer, and the reason it exists.

    A field measurement is true of the day somebody took it. Recording it as a project setting would
    make it authoritative for every later review, which is how a stale dimension outlives the wall it
    described.
    """
    project = _project_set(
        "apex", {"cabinet_depth": Fraction(24), "countertop_overhang": Fraction(1)}
    )
    run = ParameterSet(
        project_id="apex",
        layer=ParameterLayer.RUN,
        version=1,
        parameters={
            "countertop_overhang": ParameterValue(
                value=Quantity(value=Fraction(3, 2), unit=Unit.INCH),
                provenance=Provenance.MEASURED,
                set_by="site reviewer",
                set_at=WHEN,
            )
        },
    )

    assert _depth_finding(project).outcome is Outcome.FAIL
    assert _depth_finding(project, run).outcome is Outcome.PASS


def test_the_resolved_value_says_which_layer_decided() -> None:
    """A verdict that changed with configuration has to be able to say which configuration.

    Without the layer on the resolved value, a reviewer looking at a surprising FAIL cannot tell a
    project setting from a company standard from something typed for this review alone.
    """
    project = _project_set(
        "apex", {"cabinet_depth": Fraction(24), "countertop_overhang": Fraction(1)}
    )
    resolved = resolve_all(project)

    assert resolved["countertop_overhang"].layer is ParameterLayer.PROJECT
    assert resolved["countertop_overhang"].value.set_by == "project reviewer"


@pytest.mark.parametrize(
    ("layout", "expected"),
    [("back_left_right", Outcome.PASS), ("back_only", Outcome.FAIL)],
)
def test_the_layout_changes_the_verdict_without_touching_the_rule(
    layout: str, expected: Outcome
) -> None:
    """**Product and layout dynamism, at the level of a decision.**

    `tests/rules/test_ct1_width.py` already shows each layout applying its own field-cut count, and
    `tests/rules/test_applicability.py` shows the resolver choosing variants — but neither carries a
    single *published snapshot* through two contexts to two different verdicts, which is the claim
    Abhishek's question is actually about.

    Two 1" field cuts on a three-wall run; the same numbers against a back wall need none. Same rule,
    same measurements, different answer, no code.
    """
    cabinets = (Fraction(24), Fraction(30), Fraction(24))
    fillers = (Fraction(2), Fraction(2))
    field_cut = Fraction(1)
    # 78 + 4 + two 1" cuts = 84, which is what a three-wall run measures.
    overall = sum(cabinets) + sum(fillers) + 2 * field_cut

    snapshot = publish(_rule("ct_width_001.yaml"))
    operands = {
        "countertop_width": _operand("countertop_width", overall),
        "cabinet_widths": VerdictOperand(
            name="cabinet_widths",
            value=tuple(Measurement(c, Unit.INCH, str(c)) for c in cabinets),
            status=EvidenceStatus.HUMAN_CONFIRMED,
            source="SHOP",
            evidence_ref="reviewer:cabinets",
        ),
        "filler_widths": VerdictOperand(
            name="filler_widths",
            value=tuple(Measurement(f, Unit.INCH, str(f)) for f in fillers),
            status=EvidenceStatus.HUMAN_CONFIRMED,
            source="SHOP",
            evidence_ref="reviewer:fillers",
        ),
    }
    project = _project_set("apex", {"field_cut": field_cut})

    finding = execute(
        snapshot, operands, resolve_all(project), discriminators={"wall_config": layout}
    )

    assert finding.outcome is expected


def test_a_republished_rule_is_a_new_snapshot_and_the_old_one_still_decides() -> None:
    """**Versioned publish: a rule can change at runtime without rewriting history.**

    A snapshot is content-addressed, so an edited rule is a different snapshot rather than the same
    one meaning something new. That is what lets a finding cite the exact text that judged it — a
    rulebook that changed underneath an old finding would make every past verdict unexplainable.
    """
    original = _rule("ct_depth_001.yaml")
    revised = original.model_copy(update={"version": "1.1.0"})

    before, after = publish(original), publish(revised)

    assert before.snapshot_id != after.snapshot_id
    # And the old snapshot goes on deciding exactly as it did, from its own pinned text.
    settings = _project_set(
        "apex", {"cabinet_depth": Fraction(24), "countertop_overhang": Fraction(3, 2)}
    )
    finding = execute(
        before,
        {"countertop_depth": _operand("countertop_depth", MEASURED_DEPTH)},
        resolve_all(settings),
    )
    assert finding.outcome is Outcome.PASS
    assert finding.snapshot_id == before.snapshot_id
