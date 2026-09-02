"""The reviewer's override summary, and the blanks they still have to fill.

Source: `docs/CLIENT_FACTS.md` Q10 — each check has a GLOBAL default, the reviewer may set PROJECT
overrides before a run, the system reports every global-versus-project difference, and a required
field left blank prompts the reviewer. Also Q10: an override is never read off a drawing.
Verification for: `rules/overrides.py`.

The two tests worth reading are the last group: that a drawing cannot supply an override, and that a
parameter with a default is not mistaken for a missing one.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from rules.overrides import as_text, effective_values, override_report
from rules.parameters import ParameterLayer, ParameterSet, ParameterValue, Provenance
from rules.schema import Quantity, Rule
from units.measurement import Unit

WHEN = datetime(2026, 8, 25, tzinfo=UTC)
PROJECT = "westin-towers"
RULEBOOK = Path(__file__).resolve().parents[2] / "rules" / "rulebook"


def _value(
    amount: str,
    *,
    provenance: Provenance = Provenance.GC_CLIENT,
    set_by: str = "Raj",
    unit: Unit = Unit.INCH,
) -> ParameterValue:
    return ParameterValue(
        value=Quantity(value=amount, unit=unit),
        provenance=provenance,
        set_by=set_by,
        set_at=WHEN,
    )


def _global(**parameters: ParameterValue) -> ParameterSet:
    return ParameterSet(
        project_id=None, layer=ParameterLayer.GLOBAL, version=1, parameters=parameters
    )


def _project(**parameters: ParameterValue) -> ParameterSet:
    return ParameterSet(
        project_id=PROJECT, layer=ParameterLayer.PROJECT, version=1, parameters=parameters
    )


def _run(**parameters: ParameterValue) -> ParameterSet:
    return ParameterSet(
        project_id=PROJECT, layer=ParameterLayer.RUN, version=1, parameters=parameters
    )


def _load(name: str) -> Rule:
    return Rule.model_validate(yaml.safe_load((RULEBOOK / name).read_text(encoding="utf-8")))


# ---------------------------------------------------------------------------
# The summary Q10 asks for
# ---------------------------------------------------------------------------


def test_a_project_override_of_a_company_standard_is_reported() -> None:
    """Raj's own example: the standard front offset is 4", the reviewer says 3.5" is fine here."""
    report = override_report(
        [],
        _global(front_offset_required=_value("4", provenance=Provenance.COMPANY_STANDARD)),
        _project(front_offset_required=_value("3 1/2", set_by="site reviewer")),
    )

    assert len(report.overrides) == 1
    override = report.overrides[0]
    assert override.name == "front_offset_required"
    assert override.layer is ParameterLayer.PROJECT
    assert override.displaces_a_company_standard
    assert "overrides 4 in (global, Company standard)" in override.explain()


def test_a_parameter_nobody_overrode_is_not_listed_as_a_difference() -> None:
    """The report is of differences. Listing every parameter would bury the two that changed."""
    report = override_report(
        [],
        _global(a=_value("1"), b=_value("2")),
        _project(a=_value("9")),
    )

    assert [o.name for o in report.overrides] == ["a"]


def test_a_run_value_over_a_project_override_shows_both_displaced() -> None:
    """Three layers, and the reviewer needs to see the whole chain.

    Reporting only the immediately displaced value would say a run measurement overrode a project
    setting while hiding that a company standard was set aside two steps down — which is the fact
    Q10 asks to be surfaced.
    """
    report = override_report(
        [],
        _global(field_dimension=_value("88", provenance=Provenance.COMPANY_STANDARD)),
        _project(field_dimension=_value("89")),
        _run(field_dimension=_value("90", provenance=Provenance.MEASURED, set_by="site")),
    )

    override = report.overrides[0]
    assert override.layer is ParameterLayer.RUN
    assert len(override.displaced) == 2
    assert override.displaces_a_company_standard


def test_company_standards_displaced_narrows_to_the_literal_ask() -> None:
    """Q10 asks specifically for global-versus-project. A project value that displaced nothing
    global is an override, but not the one the reviewer is being asked to countersign."""
    report = override_report(
        [],
        _global(a=_value("1", provenance=Provenance.COMPANY_STANDARD)),
        _project(a=_value("2"), b=_value("3")),
        _run(b=_value("4")),
    )

    assert [o.name for o in report.overrides] == ["a", "b"]
    assert [o.name for o in report.company_standards_displaced] == ["a"]


# ---------------------------------------------------------------------------
# The mandatory blank form field
# ---------------------------------------------------------------------------


def test_a_required_parameter_nobody_supplied_is_listed_as_outstanding() -> None:
    """Raj: "imagine filling a form, there are some mandatory entries."

    The sink's interior width has no default on purpose — every sink is different — so a run without
    it cannot decide. That has to reach the reviewer *before* the run, not as a NOT_FOUND after it.
    """
    report = override_report([_load("ct_sink_cutout_width_001.yaml")], _global(), _project())

    assert "sink_interior_width" in report.outstanding


def test_a_parameter_with_a_default_is_not_outstanding() -> None:
    """**The distinction the report exists to make.**

    The cutout clearance is declared with a 1/4" default, so nobody has to supply it — the author
    already answered. Listing it beside a genuinely missing value would train a reviewer to skim the
    outstanding list, which is the one list they must not skim.
    """
    report = override_report([_load("ct_sink_cutout_width_001.yaml")], _global(), _project())

    assert "sink_cutout_clearance" not in report.outstanding


def test_supplying_the_value_clears_it_from_outstanding() -> None:
    report = override_report(
        [_load("ct_sink_cutout_width_001.yaml")],
        _global(),
        _run(sink_interior_width=_value("33", provenance=Provenance.MEASURED, set_by="reviewer")),
    )

    assert report.outstanding == ()


def test_one_parameter_shared_by_two_rules_is_listed_once() -> None:
    """`sink_interior_*` differ, but a shared name must not produce a line per rule that reads it —
    a reviewer fills one field, not one per check."""
    report = override_report(
        [_load("ct_sink_cutout_width_001.yaml"), _load("ct_sink_cutout_depth_001.yaml")],
        _global(),
        _project(),
    )

    assert report.outstanding.count("sink_interior_width") == 1
    assert sorted(report.outstanding) == list(report.outstanding)


# ---------------------------------------------------------------------------
# What the report says when nothing happened
# ---------------------------------------------------------------------------


def test_an_empty_report_says_so_in_both_halves() -> None:
    """**"Nothing was overridden" and "overrides were not checked" must not look alike.**

    The reviewer is being asked to confirm the first. A report that printed nothing would be
    indistinguishable from a report that never ran, and they would countersign either.
    """
    text = as_text(override_report([], _global(a=_value("1"))))

    assert "No parameters overridden" in text
    assert "No required parameter is missing" in text


def test_the_text_lists_every_override_and_every_blank() -> None:
    report = override_report(
        [_load("ct_sink_cutout_width_001.yaml")],
        _global(sink_cutout_clearance=_value("1/4", provenance=Provenance.COMPANY_STANDARD)),
        _project(sink_cutout_clearance=_value("1/8", set_by="site reviewer")),
    )
    text = as_text(report)

    assert "sink_cutout_clearance" in text
    assert "site reviewer" in text
    assert "sink_interior_width" in text


# ---------------------------------------------------------------------------
# An override never comes off a drawing
# ---------------------------------------------------------------------------


def test_the_override_module_cannot_reach_extraction_or_evidence() -> None:
    """**The substance of Q10, asserted structurally rather than trusted.**

    A shop drawing reading `4" TYP U.N.O.` states a value; it does not authorise one. If a note on
    the drawing could set the parameter, the drawing under review would be choosing the standard it
    is judged against — a vendor marking their own homework, and invisible in the output because the
    check would pass.

    Asserted on the imports because that is the only way it stays true. A test that merely showed the
    current code not doing it would pass just as happily the day somebody adds the import.
    """
    tree = ast.parse((Path(__file__).resolve().parents[2] / "rules" / "overrides.py").read_text())
    forbidden = {"extraction", "evidence", "retrieval", "app"}

    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            roots.add(node.module.split(".")[0])

    assert not (roots & forbidden), (
        f"overrides.py imports {sorted(roots & forbidden)}. An override that could come off a "
        "drawing lets the drawing choose the standard it is judged against."
    )


def test_effective_values_and_the_report_agree() -> None:
    """Two callers, one answer. A report describing different numbers from the ones the run uses
    would be worse than no report — it would be a signed record of the wrong thing."""
    layers = (
        _global(front_offset_required=_value("4", provenance=Provenance.COMPANY_STANDARD)),
        _project(front_offset_required=_value("3 1/2")),
    )
    report = override_report([], *layers)
    effective = effective_values(*layers)

    assert report.overrides[0].effective == effective["front_offset_required"].value


@pytest.mark.parametrize("name", ["front_offset_required", "sink_cutout_clearance"])
def test_the_effective_value_is_the_highest_layer_that_set_it(name: str) -> None:
    """Precedence lives in `rules/parameters.py` and is not reimplemented here; this pins that the
    report reads it rather than deciding for itself."""
    report = override_report(
        [],
        _global(**{name: _value("4", provenance=Provenance.COMPANY_STANDARD)}),
        _project(**{name: _value("3")}),
    )

    assert report.overrides[0].effective.value.exact_value == 3


def test_the_report_writes_dimensions_the_way_a_drawing_writes_them() -> None:
    """**A regression this module already had once.**

    The first version printed `7/2 in`, which is correct arithmetic and unreadable on a review: the
    drawing says `3 1/2` and the reviewer is comparing the two by eye. Asserted on both halves of
    the line, because the effective value and the value it displaced are rendered by different code
    paths and only one of them was fixed the first time.
    """
    line = (
        override_report(
            [],
            _global(front_offset_required=_value("4", provenance=Provenance.COMPANY_STANDARD)),
            _project(front_offset_required=_value("3 1/2", set_by="site reviewer")),
        )
        .overrides[0]
        .explain()
    )

    assert "3 1/2 in" in line
    assert "7/2" not in line
    assert "overrides 4 in (global, Company standard)" in line


def test_a_displaced_fraction_is_also_written_as_a_mixed_number() -> None:
    """The other half of the line — the one the first fix missed."""
    line = (
        override_report(
            [],
            _global(overhang=_value("1 1/2", provenance=Provenance.COMPANY_STANDARD)),
            _project(overhang=_value("2")),
        )
        .overrides[0]
        .explain()
    )

    assert "overrides 1 1/2 in (global, Company standard)" in line
    assert "3/2" not in line


def test_every_rendered_value_names_its_provenance() -> None:
    """**The field that was silently missing while the docstring claimed the shapes matched.**

    Provenance distinguishes a company standard from a number the client sent from something measured
    on site. `3 1/2` reads identically whichever it was, and a reviewer countersigning an override
    needs to know which of those they are setting aside. Asserted on both halves of the line, since
    the effective value and the displaced value are rendered by separate code.
    """
    line = (
        override_report(
            [],
            _global(overhang=_value("1", provenance=Provenance.COMPANY_STANDARD)),
            _project(overhang=_value("2", provenance=Provenance.MEASURED, set_by="site")),
        )
        .overrides[0]
        .explain()
    )

    assert Provenance.MEASURED.value in line
    assert Provenance.COMPANY_STANDARD.value in line
