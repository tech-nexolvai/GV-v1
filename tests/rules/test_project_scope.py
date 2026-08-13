"""A project scope is an isolation key as much as a data holder.

The tests that matter are therefore about what it refuses: an identifier that would match
everything, a parameter set belonging to somebody else, and — since #64 — any route by which a
project parameter could be read as a bare number with no record of who set it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from fractions import Fraction

import pytest

from rules.parameters import ParameterLayer, ParameterSet, ParameterValue, Provenance
from rules.project import InvalidProjectScopeError, ProjectScope
from rules.schema import Quantity
from units.measurement import Unit

PROJECT = "GV-2026-ABC"
WHEN = datetime(2026, 8, 13, 9, 30, tzinfo=UTC)


def _value(fraction: str = "1", *, unit: Unit = Unit.INCH) -> ParameterValue:
    return ParameterValue(
        value=Quantity(value=fraction, unit=unit),
        provenance=Provenance.GC_CLIENT,
        set_by="Raj",
        set_at=WHEN,
    )


def _parameters(
    *,
    project_id: str | None = PROJECT,
    layer: ParameterLayer = ParameterLayer.PROJECT,
    version: int = 1,
    **parameters: ParameterValue,
) -> ParameterSet:
    return ParameterSet(
        project_id=project_id,
        layer=layer,
        version=version,
        parameters=parameters or {"field_cut": _value()},
    )


def _scope(**overrides: object) -> ProjectScope:
    base: dict[str, object] = {
        "project_id": PROJECT,
        "parameter_set": _parameters(),
    }
    base.update(overrides)
    return ProjectScope(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Identity — the isolation key
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["", "   ", "\t"])
def test_a_blank_project_id_is_rejected(bad: str) -> None:
    """An empty isolation key matches everything, which is how one project's references end
    up as evidence in another's review — a finding that is internally consistent and
    completely wrong."""
    with pytest.raises(InvalidProjectScopeError, match="isolation key"):
        _scope(project_id=bad)


def test_a_non_string_project_id_is_rejected() -> None:
    with pytest.raises(InvalidProjectScopeError):
        _scope(project_id=None)


def test_two_projects_do_not_share_parameters() -> None:
    abc = ProjectScope(PROJECT, _parameters(field_cut=_value("1")))
    xyz = ProjectScope("GV-2026-XYZ", _parameters(project_id="GV-2026-XYZ", field_cut=_value("2")))

    assert abc.override_for("field_cut") != xyz.override_for("field_cut")
    assert abc.project_id != xyz.project_id


def test_a_scope_refuses_another_projects_parameter_set() -> None:
    """Serving one project's parameters to another is the isolation failure this type exists
    to prevent, and it would be invisible in the resulting finding."""
    with pytest.raises(InvalidProjectScopeError, match="isolation failure"):
        ProjectScope(PROJECT, _parameters(project_id="GV-2026-XYZ"))


@pytest.mark.parametrize("layer", [ParameterLayer.GLOBAL, ParameterLayer.RUN])
def test_a_scope_pins_a_project_layer_set_only(layer: ParameterLayer) -> None:
    """A company standard arriving as though it were a project override would hide the fact
    that nobody chose it for this project."""
    project_id = None if layer is ParameterLayer.GLOBAL else PROJECT
    with pytest.raises(InvalidProjectScopeError, match="project scope pins"):
        ProjectScope(PROJECT, _parameters(project_id=project_id, layer=layer))


# ---------------------------------------------------------------------------
# The pin — a version, never "latest"
# ---------------------------------------------------------------------------


def test_a_scope_names_the_exact_parameter_set_version_it_used() -> None:
    """A finding records this, so "which numbers judged this drawing?" stays answerable."""
    parameters = _parameters(version=7)
    scope = ProjectScope(PROJECT, parameters)

    assert scope.parameter_set_version == 7
    assert scope.parameter_set_id == parameters.set_id
    assert scope.parameter_set_id.startswith("sha256:")


def test_the_pin_does_not_follow_a_later_version() -> None:
    """The scope holds one immutable set rather than resolving "whatever is current", so a
    re-run cannot silently judge a drawing against different numbers."""
    pinned = _parameters(version=1, field_cut=_value("1"))
    scope = ProjectScope(PROJECT, pinned)

    _parameters(version=2, field_cut=_value("2"))  # published later; irrelevant to the pin

    kept = scope.override_for("field_cut")
    assert kept is not None
    assert kept.value.exact_value == Fraction(1)
    assert scope.parameter_set_version == 1


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------


def test_the_scope_itself_is_frozen() -> None:
    scope = _scope()
    with pytest.raises((AttributeError, TypeError)):
        scope.project_id = "GV-2026-XYZ"  # type: ignore[misc]


def test_the_pinned_set_cannot_be_written_through() -> None:
    scope = _scope()
    with pytest.raises(TypeError):
        scope.parameter_set.parameters["field_cut"] = _value("99")  # type: ignore[index]


# ---------------------------------------------------------------------------
# Every project parameter carries its provenance — no bare-value path exists
# ---------------------------------------------------------------------------


def test_reading_a_project_parameter_yields_the_provenance_carrying_type() -> None:
    """The project layer is where human-set overrides live, so it is the last place the record
    of who set a number should be missing."""
    value = _scope().override_for("field_cut")

    assert isinstance(value, ParameterValue)
    assert value.provenance is Provenance.GC_CLIENT
    assert value.set_by == "Raj"
    assert value.set_at == WHEN


def test_a_scope_cannot_be_built_from_a_bare_mapping_of_values() -> None:
    """The pre-#64 shape. Accepting it would put project parameters back in a form that
    records what a number is and loses who decided it."""
    with pytest.raises(InvalidProjectScopeError, match="carrying provenance"):
        ProjectScope(PROJECT, {"field_cut": Quantity(value="1", unit=Unit.INCH)})  # type: ignore[arg-type]


def test_no_field_stores_project_parameters_as_bare_values() -> None:
    """A structural guard, not a behavioural one: the duplicate provenance-free store is gone,
    so there is no second representation to drift from the authoritative one."""
    fields = {f.name for f in ProjectScope.__dataclass_fields__.values()}

    assert fields == {"project_id", "parameter_set"}
    assert "parameter_overrides" not in fields
    assert not hasattr(_scope(), "parameter_overrides")


def test_every_parameter_reachable_from_a_scope_records_its_provenance() -> None:
    scope = ProjectScope(
        PROJECT,
        _parameters(field_cut=_value("1"), filler_width_min=_value("1/2")),
    )

    for name in scope.overrides():
        value = scope.override_for(name)
        assert value is not None
        assert isinstance(value.provenance, Provenance)
        assert value.set_by


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------


def test_override_for_returns_the_projects_value() -> None:
    value = _scope().override_for("field_cut")

    assert value is not None
    assert value.value.exact_value == Fraction(1)
    assert value.value.unit is Unit.INCH


def test_override_for_returns_none_when_the_project_sets_none() -> None:
    """`None` means "this project sets no override", never "no value exists". Falling
    through to the global layer is #65's decision, not this type's."""
    assert _scope().override_for("backsplash_thickness") is None


def test_membership_and_listing() -> None:
    scope = ProjectScope(PROJECT, _parameters(field_cut=_value("1"), filler_max=_value("5")))

    assert "field_cut" in scope
    assert "overhang" not in scope
    assert scope.overrides() == ("field_cut", "filler_max")


def test_a_project_may_override_nothing() -> None:
    """Most projects take the company standards unchanged."""
    scope = ProjectScope(
        PROJECT,
        ParameterSet(project_id=PROJECT, layer=ParameterLayer.PROJECT, version=1, parameters={}),
    )

    assert scope.overrides() == ()
    assert scope.override_for("field_cut") is None


def test_an_override_keeps_the_authored_fraction() -> None:
    scope = ProjectScope(PROJECT, _parameters(tol=_value("1/16")))
    kept = scope.override_for("tol")

    assert kept is not None
    assert kept.value.exact_value == Fraction(1, 16)


# ---------------------------------------------------------------------------
# Boundaries this type deliberately does not cross
# ---------------------------------------------------------------------------


def test_scope_carries_no_brand_or_vendor() -> None:
    """ADR-0006: vendor is metadata, never a rule key, and lives on the control-plane
    record. `rules/` must not depend on `app/`."""
    fields = {f.name for f in ProjectScope.__dataclass_fields__.values()}

    assert "vendor" not in fields
    assert "brand" not in fields
