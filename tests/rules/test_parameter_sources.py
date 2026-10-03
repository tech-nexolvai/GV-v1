"""Where each setting may come from, and what a value says about it (#827).

Verification for: `rules/parameter_sources.py`, and the `reference` `rules/parameters.py` gained.

The one that matters most for stored data is `test_a_value_stored_before_827_keeps_its_hash`: a set's
id is the hash of its content, so a reference that entered every hash would have moved every stored
set's id, and a finding that cites one would cite a set nobody can find.
"""

from __future__ import annotations

from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path

import pytest
import yaml

from rules.parameter_sources import (
    ALLOWED_SOURCES,
    CITABLE_SIDES,
    SOURCE_GUIDANCE,
    allowed_sources,
    citable_sides,
    citable_sources,
    undeclared,
)
from rules.parameters import (
    HUMAN_PROVENANCES,
    REFERENCE_MAX_LENGTH,
    ParameterLayer,
    ParameterSet,
    ParameterValue,
    Provenance,
    ResolvedParameter,
)
from rules.schema import Quantity
from units.measurement import Unit
from vocabulary.semantic_types import DocumentRole

RULEBOOK = Path(__file__).resolve().parents[2] / "rules" / "rulebook"


def _rulebook_settings() -> set[str]:
    names: set[str] = set()
    for path in RULEBOOK.glob("*.yaml"):
        names.update(yaml.safe_load(path.read_text(encoding="utf-8")).get("parameters") or {})
    return names


def _value(reference: str | None = None) -> ParameterValue:
    return ParameterValue(
        value=Quantity(value=Fraction(3, 4), unit=Unit.INCH),
        provenance=Provenance.GC_CLIENT,
        set_by="anant",
        set_at=datetime(2026, 10, 2, 9, 30, tzinfo=UTC),
        reference=reference,
    )


def _set(value: ParameterValue) -> ParameterSet:
    return ParameterSet(
        project_id="11111111-1111-1111-1111-111111111111",
        layer=ParameterLayer.PROJECT,
        version=1,
        parameters={"countertop_overhang": value},
    )


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------


def test_every_setting_a_rule_uses_says_where_it_may_come_from() -> None:
    """A newly authored rule whose setting has no row would have its value refused on save, and the
    reviewer told the setting does not exist. This fails first, at the author's desk."""
    assert undeclared(_rulebook_settings()) == ()


def test_the_table_names_no_setting_no_rule_uses() -> None:
    """A row for a renamed setting is a source nobody is asked for — the drift in the other direction."""
    assert set(ALLOWED_SOURCES) == _rulebook_settings()


def test_every_source_is_one_a_person_or_the_company_can_give() -> None:
    """None a model could claim: `Provenance` has no such member, and this keeps the table to it."""
    allowed = HUMAN_PROVENANCES | {Provenance.COMPANY_STANDARD}
    for name, sources in ALLOWED_SOURCES.items():
        assert sources, f"{name} allows no source, so no value for it could ever be saved"
        assert set(sources) <= allowed, name
        assert len(set(sources)) == len(sources), f"{name} lists a source twice"


@pytest.mark.parametrize(
    ("name", "sources"),
    [
        # Raj, 2026-08-25: "changes from fabricator to fabricator. GV doesn't decide that one."
        ("sink_cutout_clearance", (Provenance.COMPANY_STANDARD, Provenance.FABRICATOR)),
        # "Specified · G.C / Client · Project Specific".
        ("countertop_overhang", (Provenance.GC_CLIENT,)),
        ("backsplash_thickness", (Provenance.GC_CLIENT,)),
        ("cabinet_side_thickness", (Provenance.GC_CLIENT,)),
        # The admin, 2026-10-03: no source named, so the reviewer picks.
        ("single_door_cab_width_min", (Provenance.COMPANY_STANDARD, Provenance.GC_CLIENT)),
        ("front_offset_required", (Provenance.COMPANY_STANDARD,)),
    ],
)
def test_the_sources_are_the_ones_raj_and_the_admin_gave(
    name: str, sources: tuple[Provenance, ...]
) -> None:
    assert allowed_sources(name) == sources


def test_a_setting_the_table_does_not_know_has_no_source_at_all() -> None:
    """None rather than every source: an unassigned setting's honest source is unknown."""
    assert allowed_sources("cabinet_dpeth") == ()


def test_every_source_says_what_choosing_it_means() -> None:
    for sources in ALLOWED_SOURCES.values():
        for source in sources:
            assert SOURCE_GUIDANCE[source].strip(), source


def test_a_specified_setting_is_never_from_the_vendors_drawing() -> None:
    """Q10 in the one sentence the form shows beside every G.C / Client value."""
    assert "never from the vendor's drawing" in SOURCE_GUIDANCE[Provenance.GC_CLIENT]


# ---------------------------------------------------------------------------
# Which side of a package a source may be cited from (#849)
# ---------------------------------------------------------------------------


def test_every_source_says_which_sides_it_may_cite() -> None:
    """Exhaustive, so a new source cites nothing until somebody writes down what it may cite."""
    assert set(CITABLE_SIDES) == set(Provenance)


def test_no_source_may_cite_the_vendors_drawing() -> None:
    """**Q10.** The drawing under review never supplies a number it is then checked against."""
    for source, sides in CITABLE_SIDES.items():
        assert DocumentRole.SHOP not in sides, source


@pytest.mark.parametrize(
    ("source", "sides"),
    [
        (Provenance.GC_CLIENT, frozenset({DocumentRole.ARCH})),
        (Provenance.COMPANY_STANDARD, frozenset()),
        (Provenance.FABRICATOR, frozenset()),
        (Provenance.MEASURED, frozenset()),
    ],
)
def test_the_sides_are_the_ones_the_plan_gave(
    source: Provenance, sides: frozenset[DocumentRole]
) -> None:
    """Plan step 3.2 on #798: G.C / Client cites the architect's drawings; the rest cite nothing."""
    assert citable_sides(source) == sides


def test_a_setting_may_be_cited_only_through_a_source_that_cites_something() -> None:
    """The field cut and every company standard can never be read off a package; a setting the
    reviewer may give either way can be cited only as G.C / Client."""
    for setting in ("field_cut", "front_offset_required", "filler_min", "sink_cutout_clearance"):
        assert citable_sources(setting) == (), setting
    assert citable_sources("countertop_overhang") == (Provenance.GC_CLIENT,)
    assert citable_sources("single_door_cab_width_min") == (Provenance.GC_CLIENT,)
    assert citable_sources("cabinet_dpeth") == ()


# ---------------------------------------------------------------------------
# The reference a value carries
# ---------------------------------------------------------------------------


def test_a_value_stored_before_827_keeps_its_hash() -> None:
    """**The point.** The hash below was computed on main before #827 (62375e5) for this exact
    value. Unchanged, so every set stored before the reference existed keeps its id."""
    assert (
        _set(_value()).set_id
        == "sha256:d1f3b7d0b31e6185f3f4a452a55f5a7798edcd54e70b50f9e07834cc27dfc497"
    )


def test_a_reference_is_part_of_the_record() -> None:
    """Two values that cite different places are different records, as two set on different days are."""
    assert _set(_value("Architect A-501")).set_id != _set(_value()).set_id
    assert _set(_value("Architect A-501")).set_id != _set(_value("Project spec 12 36 61")).set_id


@pytest.mark.parametrize("reference", ["", "   ", "x" * (REFERENCE_MAX_LENGTH + 1)])
def test_a_blank_or_overlong_reference_is_refused(reference: str) -> None:
    """Blank reads as a citation that points nowhere; overlong is a copy of the document."""
    with pytest.raises(ValueError, match="reference"):
        _value(reference)


def test_the_source_reads_with_its_reference_when_there_is_one() -> None:
    assert _value().source_text == "G.C / Client"
    assert _value("Architect A-501, section 3").source_text == (
        "G.C / Client: Architect A-501, section 3"
    )


def test_the_explanation_on_a_finding_says_where_and_who() -> None:
    resolved = ResolvedParameter(
        name="countertop_overhang",
        value=_value("Architect A-501, section 3"),
        layer=ParameterLayer.PROJECT,
    )
    assert resolved.explain() == (
        "countertop_overhang = 3/4 in (project, G.C / Client: Architect A-501, section 3, "
        "set by anant)"
    )
