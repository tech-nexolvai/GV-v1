"""A project scope is an isolation key as much as a data holder.

The tests that matter are therefore about what it refuses: an identifier that would match
everything, and overrides that could change after construction.
"""

from __future__ import annotations

from fractions import Fraction

import pytest

from rules.project import InvalidProjectScopeError, ProjectScope
from rules.schema import Quantity
from units.measurement import Unit


def _scope(**overrides: object) -> ProjectScope:
    base: dict[str, object] = {
        "project_id": "GV-2026-ABC",
        "parameter_overrides": {"field_cut": Quantity(value="1", unit=Unit.INCH)},
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


def test_two_projects_do_not_share_overrides() -> None:
    abc = ProjectScope("GV-2026-ABC", {"field_cut": Quantity(value="1", unit=Unit.INCH)})
    xyz = ProjectScope("GV-2026-XYZ", {"field_cut": Quantity(value="2", unit=Unit.INCH)})
    assert abc.override_for("field_cut") != xyz.override_for("field_cut")
    assert abc.project_id != xyz.project_id


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------


def test_the_scope_itself_is_frozen() -> None:
    scope = _scope()
    with pytest.raises((AttributeError, TypeError)):
        scope.project_id = "GV-2026-XYZ"  # type: ignore[misc]


def test_mutating_the_callers_dict_afterwards_does_not_change_the_scope() -> None:
    """A frozen dataclass holding the caller's dict is not actually immutable. For an
    isolation key that is a bug worth preventing rather than documenting."""
    source = {"field_cut": Quantity(value="1", unit=Unit.INCH)}
    scope = ProjectScope("GV-2026-ABC", source)

    source["field_cut"] = Quantity(value="99", unit=Unit.INCH)
    source["filler_max"] = Quantity(value="5", unit=Unit.INCH)

    kept = scope.override_for("field_cut")
    assert kept is not None
    assert kept.value == Fraction(1)
    assert scope.override_for("filler_max") is None


def test_the_overrides_mapping_cannot_be_written_through() -> None:
    scope = _scope()
    with pytest.raises(TypeError):
        scope.parameter_overrides["field_cut"] = Quantity(  # type: ignore[index]
            value="99", unit=Unit.INCH
        )


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------


def test_override_for_returns_the_projects_value() -> None:
    value = _scope().override_for("field_cut")
    assert value is not None
    assert value.value == Fraction(1)
    assert value.unit is Unit.INCH


def test_override_for_returns_none_when_the_project_sets_none() -> None:
    """`None` means "this project sets no override", never "no value exists". Falling
    through to the global layer is #65's decision, not this type's."""
    assert _scope().override_for("backsplash_thickness") is None


def test_membership_and_listing() -> None:
    scope = ProjectScope(
        "GV-2026-ABC",
        {
            "field_cut": Quantity(value="1", unit=Unit.INCH),
            "filler_max": Quantity(value="5", unit=Unit.INCH),
        },
    )
    assert "field_cut" in scope
    assert "overhang" not in scope
    assert scope.overrides() == ("field_cut", "filler_max")


def test_a_project_may_override_nothing() -> None:
    """Most projects take the company standards unchanged."""
    scope = ProjectScope("GV-2026-ABC", {})
    assert scope.overrides() == ()
    assert scope.override_for("field_cut") is None


# ---------------------------------------------------------------------------
# Values stay exact
# ---------------------------------------------------------------------------


def test_an_override_must_be_a_quantity_so_the_value_stays_exact() -> None:
    """A bare number would lose the unit, and a float would lose exactness — ADR-0001."""
    with pytest.raises(InvalidProjectScopeError, match="must be a Quantity"):
        _scope(parameter_overrides={"field_cut": 1.0})
    with pytest.raises(InvalidProjectScopeError, match="must be a Quantity"):
        _scope(parameter_overrides={"field_cut": "1 inch"})


def test_an_override_keeps_the_authored_fraction() -> None:
    scope = ProjectScope("GV-2026-ABC", {"tol": Quantity(value="1/16", unit=Unit.INCH)})
    kept = scope.override_for("tol")
    assert kept is not None
    assert kept.value == Fraction(1, 16)


def test_a_blank_parameter_name_is_rejected() -> None:
    with pytest.raises(InvalidProjectScopeError, match="non-empty"):
        _scope(parameter_overrides={"": Quantity(value="1", unit=Unit.INCH)})


# ---------------------------------------------------------------------------
# Boundaries this type deliberately does not cross
# ---------------------------------------------------------------------------


def test_scope_carries_no_brand_or_vendor() -> None:
    """ADR-0006: vendor is metadata, never a rule key, and lives on the control-plane
    record. `rules/` must not depend on `app/`."""
    fields = {f.name for f in ProjectScope.__dataclass_fields__.values()}
    assert fields == {"project_id", "parameter_overrides"}
    assert "vendor" not in fields
    assert "brand" not in fields
