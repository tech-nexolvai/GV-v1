"""Layered resolution: GLOBAL -> PROJECT -> RUN, last wins.

The tests that matter are not about the merge — that part is simple. They are about what the
merge must never do quietly: hide an override, mix two projects, or substitute a value nobody
supplied.
"""

from __future__ import annotations

from datetime import UTC, datetime
from fractions import Fraction

import pytest

from rules.parameters import (
    LAYER_PRECEDENCE,
    LayerConflictError,
    ParameterLayer,
    ParameterMissingError,
    ParameterSet,
    ParameterValue,
    Provenance,
    Quantity,
    resolve,
    resolve_all,
)
from units.measurement import Unit

PROJECT = "GV-2026-ABC"
WHEN = datetime(2026, 8, 13, 9, 30, tzinfo=UTC)


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


def _project(project_id: str = PROJECT, **parameters: ParameterValue) -> ParameterSet:
    return ParameterSet(
        project_id=project_id, layer=ParameterLayer.PROJECT, version=1, parameters=parameters
    )


def _run(project_id: str = PROJECT, **parameters: ParameterValue) -> ParameterSet:
    return ParameterSet(
        project_id=project_id, layer=ParameterLayer.RUN, version=1, parameters=parameters
    )


# ---------------------------------------------------------------------------
# Precedence
# ---------------------------------------------------------------------------


def test_precedence_order_is_global_then_project_then_run() -> None:
    assert LAYER_PRECEDENCE == (
        ParameterLayer.GLOBAL,
        ParameterLayer.PROJECT,
        ParameterLayer.RUN,
    )


def test_project_overrides_global() -> None:
    resolved = resolve(
        "sink_front_offset",
        _global(sink_front_offset=_value("4", provenance=Provenance.COMPANY_STANDARD)),
        _project(sink_front_offset=_value("3")),
    )
    assert resolved.value.value.exact_value == Fraction(3)
    assert resolved.layer is ParameterLayer.PROJECT


def test_run_overrides_project_and_global() -> None:
    resolved = resolve(
        "field_dimension",
        _global(field_dimension=_value("84")),
        _project(field_dimension=_value("85")),
        _run(field_dimension=_value("86", provenance=Provenance.MEASURED, set_by="site")),
    )
    assert resolved.value.value.exact_value == Fraction(86)
    assert resolved.layer is ParameterLayer.RUN
    assert resolved.value.provenance is Provenance.MEASURED


def test_a_lower_layer_supplies_what_the_higher_ones_do_not() -> None:
    """A project overriding one parameter does not discard the rest of the standards."""
    resolved = resolve(
        "sink_front_offset",
        _global(
            sink_front_offset=_value("4", provenance=Provenance.COMPANY_STANDARD),
            door_thickness=_value("3/4"),
        ),
        _project(door_thickness=_value("1")),
    )
    assert resolved.layer is ParameterLayer.GLOBAL
    assert resolved.value.value.exact_value == Fraction(4)


def test_argument_order_does_not_change_the_result() -> None:
    """Precedence comes from the layer, not from how the caller happened to pass the sets."""
    g = _global(overhang=_value("1"))
    p = _project(overhang=_value("2"))
    assert resolve("overhang", g, p).layer is resolve("overhang", p, g).layer


def test_resolution_works_with_a_single_layer() -> None:
    resolved = resolve("door_thickness", _global(door_thickness=_value("3/4")))
    assert resolved.layer is ParameterLayer.GLOBAL
    assert resolved.shadowed == ()


# ---------------------------------------------------------------------------
# Overriding a company standard is recorded, not silent
# ---------------------------------------------------------------------------


def test_an_override_records_what_it_displaced() -> None:
    """Reporting only the winner would satisfy "which layer supplied it" while still hiding
    that a company standard was set aside. That is the thing a reviewer needs to see."""
    resolved = resolve(
        "sink_front_offset",
        _global(sink_front_offset=_value("4", provenance=Provenance.COMPANY_STANDARD)),
        _project(sink_front_offset=_value("3")),
    )
    assert len(resolved.shadowed) == 1
    assert resolved.shadowed[0].layer is ParameterLayer.GLOBAL
    assert resolved.shadowed[0].value.value.exact_value == Fraction(4)


def test_overriding_a_company_standard_is_flagged() -> None:
    overridden = resolve(
        "sink_front_offset",
        _global(sink_front_offset=_value("4", provenance=Provenance.COMPANY_STANDARD)),
        _project(sink_front_offset=_value("3")),
    )
    assert overridden.overrides_a_company_standard

    untouched = resolve(
        "sink_front_offset",
        _global(sink_front_offset=_value("4", provenance=Provenance.COMPANY_STANDARD)),
    )
    assert not untouched.overrides_a_company_standard


def test_a_project_only_parameter_overrides_nothing() -> None:
    """Setting a value nobody had set before is not an override, and must not read as one."""
    resolved = resolve("backsplash_thickness", _project(backsplash_thickness=_value("3/4")))
    assert resolved.shadowed == ()
    assert not resolved.overrides_a_company_standard


def test_two_displaced_layers_are_both_recorded_most_recent_first() -> None:
    resolved = resolve(
        "field_dimension",
        _global(field_dimension=_value("84")),
        _project(field_dimension=_value("85")),
        _run(field_dimension=_value("86")),
    )
    assert [s.layer for s in resolved.shadowed] == [
        ParameterLayer.PROJECT,
        ParameterLayer.GLOBAL,
    ]


def test_explain_names_the_value_its_source_and_what_it_overrode() -> None:
    line = resolve(
        "sink_front_offset",
        _global(sink_front_offset=_value("4", provenance=Provenance.COMPANY_STANDARD)),
        _project(sink_front_offset=_value("3", set_by="Raj")),
    ).explain()
    assert "sink_front_offset = 3 in" in line
    assert "project" in line and "Raj" in line
    assert "overrides 4 in (global)" in line


# ---------------------------------------------------------------------------
# Missing — never a default
# ---------------------------------------------------------------------------


def test_a_parameter_no_layer_sets_raises() -> None:
    with pytest.raises(ParameterMissingError, match="never a default"):
        resolve("filler_width_max", _global(door_thickness=_value("3/4")), _project())


def test_the_error_says_where_it_looked() -> None:
    with pytest.raises(ParameterMissingError, match="global, project"):
        resolve("nothing_sets_this", _global(), _project())


def test_resolving_with_no_layers_at_all_raises() -> None:
    with pytest.raises(ParameterMissingError):
        resolve("anything")


def test_no_fallback_value_is_ever_substituted() -> None:
    """AGENTS.md §2.4. A defaulted parameter is an invented value wearing a plausible number,
    and the "typical" figures in the client checklist are seeded values a human confirms."""
    with pytest.raises(ParameterMissingError):
        resolve("field_cut", _global(), _project(), _run())


# ---------------------------------------------------------------------------
# Ambiguity is an error, not a merge
# ---------------------------------------------------------------------------


def test_two_sets_at_the_same_layer_is_an_error() -> None:
    """'Last wins' has no defined meaning between two PROJECT sets — the same class of
    ambiguity as two rule snapshots sharing a version (ADR-0006)."""
    with pytest.raises(LayerConflictError, match="no defined meaning"):
        resolve("overhang", _project(overhang=_value("1")), _project(overhang=_value("2")))


def test_sets_from_different_projects_are_refused() -> None:
    """The isolation failure ADR-0006 describes: a finding that is internally consistent and
    completely wrong, which no tolerance check would catch."""
    with pytest.raises(LayerConflictError, match="more than one project"):
        resolve(
            "overhang",
            _project("GV-2026-ABC", overhang=_value("1")),
            _run("GV-2026-XYZ", overhang=_value("2")),
        )


def test_the_global_layer_is_project_agnostic() -> None:
    """GLOBAL carries no project_id, so pairing it with any project is legitimate."""
    resolved = resolve(
        "overhang", _global(overhang=_value("1")), _project("GV-2026-XYZ", overhang=_value("2"))
    )
    assert resolved.layer is ParameterLayer.PROJECT


# ---------------------------------------------------------------------------
# Resolving everything at once
# ---------------------------------------------------------------------------


def test_resolve_all_returns_the_effective_parameter_set() -> None:
    effective = resolve_all(
        _global(
            sink_front_offset=_value("4", provenance=Provenance.COMPANY_STANDARD),
            door_thickness=_value("3/4"),
        ),
        _project(sink_front_offset=_value("3"), overhang=_value("1")),
        _run(field_dimension=_value("6012", unit=Unit.MM, provenance=Provenance.MEASURED)),
    )
    assert sorted(effective) == [
        "door_thickness",
        "field_dimension",
        "overhang",
        "sink_front_offset",
    ]
    assert effective["sink_front_offset"].layer is ParameterLayer.PROJECT
    assert effective["door_thickness"].layer is ParameterLayer.GLOBAL
    assert effective["field_dimension"].layer is ParameterLayer.RUN


def test_resolve_all_surfaces_every_override() -> None:
    effective = resolve_all(
        _global(a=_value("1", provenance=Provenance.COMPANY_STANDARD), b=_value("2")),
        _project(a=_value("9")),
    )
    assert effective["a"].overrides_a_company_standard
    assert not effective["b"].overrides_a_company_standard


def test_resolve_all_with_no_parameters_is_empty_not_an_error() -> None:
    """A project that overrides nothing is normal, not a failure."""
    assert resolve_all(_global(), _project()) == {}


# ---------------------------------------------------------------------------
# Values stay exact and keep their provenance
# ---------------------------------------------------------------------------


def test_the_resolved_value_keeps_its_full_provenance() -> None:
    resolved = resolve(
        "field_dimension",
        _run(
            field_dimension=_value(
                "6012", unit=Unit.MM, provenance=Provenance.MEASURED, set_by="site team"
            )
        ),
    )
    assert resolved.value.provenance is Provenance.MEASURED
    assert resolved.value.set_by == "site team"
    assert resolved.value.set_at == WHEN


def test_an_exact_fraction_survives_resolution() -> None:
    resolved = resolve("tolerance", _project(tolerance=_value("1/16")))
    assert resolved.value.value.exact_value == Fraction(1, 16)
