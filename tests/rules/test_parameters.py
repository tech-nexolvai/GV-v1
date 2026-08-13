"""A parameter appears on no drawing, so the record of who set it is all the evidence there is.

These tests are mostly about that record surviving: that a value cannot be stored without its
provenance, that a set cannot be edited in place, and that a version pins one and only one set of
numbers. A parameter that changed silently would change a verdict silently.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from rules.parameters import (
    ParameterLayer,
    ParameterSet,
    ParameterSetConflictError,
    ParameterSetStore,
    ParameterValue,
    Provenance,
)
from rules.schema import Quantity
from units.measurement import Unit

# Fixed rather than "now": this module records when someone set a value, and a test that read a
# clock could not assert on an identifier derived from it.
WHEN = datetime(2026, 8, 13, 9, 30, tzinfo=UTC)
LATER = datetime(2026, 8, 14, 9, 30, tzinfo=UTC)


def _value(
    fraction: str = "1",
    *,
    unit: Unit = Unit.INCH,
    provenance: Provenance = Provenance.GC_CLIENT,
    set_by: str = "Raj",
    set_at: datetime = WHEN,
) -> ParameterValue:
    return ParameterValue(
        value=Quantity(value=fraction, unit=unit),
        provenance=provenance,
        set_by=set_by,
        set_at=set_at,
    )


def _set(
    *,
    project_id: str | None = "PRJ-1",
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


# ---------------------------------------------------------------------------
# Every parameter records where it came from
# ---------------------------------------------------------------------------


def test_a_parameter_carries_value_provenance_and_who_set_it_when() -> None:
    parameter = _value("3/4", provenance=Provenance.COMPANY_STANDARD, set_by="GV standards")

    assert str(parameter.value.exact_value) == "3/4"
    assert parameter.value.unit is Unit.INCH
    assert parameter.provenance is Provenance.COMPANY_STANDARD
    assert parameter.set_by == "GV standards"
    assert parameter.set_at == WHEN


def test_a_bare_value_cannot_be_stored_as_a_parameter() -> None:
    """A number without provenance records what it is and loses who decided it."""
    with pytest.raises(TypeError, match="ParameterValue"):
        ParameterSet(
            project_id="PRJ-1",
            layer=ParameterLayer.PROJECT,
            version=1,
            parameters={"field_cut": Quantity(value="1", unit=Unit.INCH)},  # type: ignore[dict-item]
        )


def test_a_value_must_stay_exact() -> None:
    with pytest.raises(TypeError, match="Quantity"):
        ParameterValue(
            value=0.125,  # type: ignore[arg-type]
            provenance=Provenance.GC_CLIENT,
            set_by="Raj",
            set_at=WHEN,
        )


def test_an_unattributed_value_is_refused() -> None:
    with pytest.raises(ValueError, match="who set this parameter"):
        _value(set_by="   ")


def test_provenance_is_a_controlled_vocabulary() -> None:
    """A typo'd source publishes cleanly and then misleads a reviewer about authority."""
    assert {p.value for p in Provenance} == {
        "G.C / Client",
        "Company standard",
        "Measured",
    }


# ---------------------------------------------------------------------------
# Versioned and immutable
# ---------------------------------------------------------------------------


def test_a_set_cannot_be_mutated() -> None:
    parameter_set = _set()

    with pytest.raises((AttributeError, TypeError)):
        parameter_set.version = 2  # type: ignore[misc]


def test_the_caller_dict_cannot_mutate_a_constructed_set() -> None:
    """A frozen dataclass holding a caller's dict is not actually immutable."""
    supplied = {"field_cut": _value()}
    parameter_set = ParameterSet(
        project_id="PRJ-1", layer=ParameterLayer.PROJECT, version=1, parameters=supplied
    )

    supplied["filler_width_min"] = _value("1/2")

    assert parameter_set.names() == ("field_cut",)


def test_a_parameter_cannot_be_added_through_the_stored_mapping() -> None:
    parameter_set = _set()

    with pytest.raises(TypeError):
        parameter_set.parameters["filler_width_min"] = _value()  # type: ignore[index]


def test_the_same_content_always_yields_the_same_identifier() -> None:
    assert _set().set_id == _set().set_id


def test_declaration_order_does_not_change_the_identifier() -> None:
    """Two logically identical sets written in a different order are the same set."""
    forwards = _set(field_cut=_value("1"), filler_width_min=_value("1/2"))
    backwards = _set(filler_width_min=_value("1/2"), field_cut=_value("1"))

    assert forwards.set_id == backwards.set_id


@pytest.mark.parametrize(
    "changed",
    [
        pytest.param({"field_cut": _value("2")}, id="value"),
        pytest.param({"field_cut": _value(provenance=Provenance.MEASURED)}, id="provenance"),
        pytest.param({"field_cut": _value(set_by="someone else")}, id="who"),
        pytest.param({"field_cut": _value(set_at=LATER)}, id="when"),
        pytest.param({"field_cut": _value(unit=Unit.MM)}, id="unit"),
    ],
)
def test_any_change_yields_a_different_identifier(changed: dict[str, ParameterValue]) -> None:
    assert _set().set_id != _set(**changed).set_id


def test_a_tolerance_is_hashed_exactly_never_as_a_float() -> None:
    body = _set(field_cut=_value("1/8")).canonical_json()

    assert '"1/8"' in body
    assert "0.125" not in body


def test_version_starts_at_one() -> None:
    with pytest.raises(ValueError, match="version starts at 1"):
        _set(version=0)


# ---------------------------------------------------------------------------
# A version pins one set of numbers
# ---------------------------------------------------------------------------


def test_republishing_identical_content_is_idempotent() -> None:
    store = ParameterSetStore()

    store.add(_set())
    store.add(_set())

    assert len(store) == 1


def test_editing_a_published_set_without_bumping_the_version_is_an_error() -> None:
    """Otherwise "parameter set version 1" names two different sets of numbers, and a finding
    that pinned it could not say which judged the drawing."""
    store = ParameterSetStore()
    store.add(_set(field_cut=_value("1")))

    with pytest.raises(ParameterSetConflictError, match="bump the version"):
        store.add(_set(field_cut=_value("2")))


def test_bumping_the_version_is_the_way_through() -> None:
    store = ParameterSetStore()
    store.add(_set(version=1, field_cut=_value("1")))
    store.add(_set(version=2, field_cut=_value("2")))

    assert len(store) == 2


def test_two_projects_may_share_a_version_number() -> None:
    store = ParameterSetStore()
    store.add(_set(project_id="PRJ-1"))
    store.add(_set(project_id="PRJ-2", field_cut=_value("2")))

    assert len(store) == 2


def test_the_same_version_at_different_layers_does_not_clash() -> None:
    store = ParameterSetStore()
    store.add(_set(layer=ParameterLayer.PROJECT))
    store.add(_set(layer=ParameterLayer.RUN, field_cut=_value("2")))

    assert len(store) == 2


def test_an_unknown_set_is_an_integrity_problem_not_a_cache_miss() -> None:
    with pytest.raises(KeyError):
        ParameterSetStore().get("sha256:0000")


# ---------------------------------------------------------------------------
# Layers
# ---------------------------------------------------------------------------


def test_the_global_layer_carries_no_project() -> None:
    company_wide = _set(project_id=None, layer=ParameterLayer.GLOBAL)

    assert company_wide.project_id is None


def test_a_global_set_may_not_name_a_project() -> None:
    """A project-specific value must be visibly an override, not a standard."""
    with pytest.raises(ValueError, match="company-wide"):
        _set(project_id="PRJ-1", layer=ParameterLayer.GLOBAL)


@pytest.mark.parametrize("layer", [ParameterLayer.PROJECT, ParameterLayer.RUN])
def test_a_project_or_run_set_must_name_its_project(layer: ParameterLayer) -> None:
    with pytest.raises(ValueError, match="must name its project"):
        _set(project_id=None, layer=layer)


# ---------------------------------------------------------------------------
# Reading, without deciding precedence
# ---------------------------------------------------------------------------


def test_a_set_reports_what_it_carries() -> None:
    parameter_set = _set(field_cut=_value("1"), filler_width_min=_value("1/2"))

    assert parameter_set.names() == ("field_cut", "filler_width_min")
    assert "field_cut" in parameter_set
    assert parameter_set.get("field_cut") is not None


def test_a_parameter_this_set_does_not_carry_is_none_not_an_error() -> None:
    """None means "this set does not set it", never "no value exists" — falling through to
    another layer is resolve()'s decision (#65), not this type's."""
    assert _set().get("countertop_overhang") is None


def test_the_label_names_the_project_layer_and_version() -> None:
    label = _set(project_id="PRJ-1", version=3).label

    assert label.startswith("PRJ-1 project v3 (")
