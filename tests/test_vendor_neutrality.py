"""Vendor identity may never influence a verdict (ADR-0006).

Every vendor is held to the same rule for the same layout. Its one legitimate use is spotting
patterns — a vendor that repeatedly gets filler distribution wrong is a conversation, not a
different rulebook.

The reason this is a guard and not a paragraph: per-vendor scrutiny would not arrive as an obvious
mistake. It would arrive as *"vendor X's drawings use a different convention, can we relax that
check for them"* — reasonable on its face, easy to agree to in a meeting, and the exact point at
which the system starts deciding how carefully to check based on who it is checking.

A looser tolerance for a trusted supplier is a false PASS with a paper trail saying it was
intentional.

Reuses the transitive import walker from `tests/test_verdict_isolation.py` rather than writing a
second one — two graph walkers would eventually disagree, and the one that disagreed quietly would
be the one nobody ran.
"""

from __future__ import annotations

import sys
from dataclasses import fields
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_verdict_isolation import transitive_imports

from rules.parameters import ParameterLayer, ParameterSet
from rules.project import ProjectScope
from rules.schema import (
    RESERVED_DISCRIMINATORS,
    Applicability,
    ApplicabilityVariant,
    Tolerance,
)
from units.measurement import Unit

#: The packages that decide. Nothing here may reach vendor-derived data by any path.
DECIDING_PACKAGES = ("verdict", "rules")

#: Where vendor aggregation will live once D7.1 lands. Named now so the guard is already in place
#: when the module appears — a guard written after the thing it guards is a guard that was needed
#: earlier than it existed.
VENDOR_MODULES = ("reports.vendor_patterns", "reports")


# ---------------------------------------------------------------------------
# The import graph
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("package", DECIDING_PACKAGES)
def test_the_deciding_packages_cannot_reach_vendor_reporting(package: str) -> None:
    """Transitive, not direct. A vendor import two hops away still contaminates the decision."""
    reachable = transitive_imports(package)
    offending = {
        module: chain
        for module, chain in reachable.items()
        if any(module == v or module.startswith(f"{v}.") for v in VENDOR_MODULES)
    }
    assert not offending, (
        f"{package}/ can reach vendor reporting: "
        + "; ".join(f"{m} via {' -> '.join(c)}" for m, c in offending.items())
        + ". Vendor identity is metadata, never a rule key (ADR-0006)."
    )


def test_the_walker_is_shared_with_the_isolation_guard() -> None:
    """Imported, not reimplemented. Two graph walkers would drift, and the one that drifted
    quietly is the one that stops catching things."""
    assert transitive_imports.__module__ == "test_verdict_isolation"


# ---------------------------------------------------------------------------
# Applicability
# ---------------------------------------------------------------------------


def _variants() -> tuple[ApplicabilityVariant, ...]:
    return (
        ApplicabilityVariant(when="a", tolerance=Tolerance(value="1/8", unit=Unit.INCH)),
        ApplicabilityVariant(when="b", tolerance=Tolerance(value="1/16", unit=Unit.INCH)),
    )


@pytest.mark.parametrize("name", sorted(RESERVED_DISCRIMINATORS))
def test_vendor_cannot_select_an_applicability_variant(name: str) -> None:
    with pytest.raises(ValueError, match="metadata, never a rule key"):
        Applicability(discriminator=name, variants=_variants())


@pytest.mark.parametrize("name", ["VENDOR", "Vendor", "  vendor  "])
def test_the_check_is_not_defeated_by_capitals_or_padding(name: str) -> None:
    """The bypass anyone would find first."""
    with pytest.raises(ValueError, match="metadata, never a rule key"):
        Applicability(discriminator=name, variants=_variants())


def test_a_layout_discriminator_is_still_permitted() -> None:
    """The guard must not make legitimate applicability impossible — `wall_config` is the whole
    reason variants exist."""
    assert Applicability(discriminator="wall_config", variants=_variants()).discriminator == (
        "wall_config"
    )


def test_manufacturer_is_deliberately_permitted() -> None:
    """It identifies a *product*, not the party being reviewed.

    A rule may legitimately vary by which sink is specified — that is what `PRODUCT_SPEC` and
    ADR-0015 are for. It may never vary by who drew the drawing. Conflating the two would either
    block a real requirement or open the door this guard exists to close.
    """
    assert "manufacturer" not in RESERVED_DISCRIMINATORS
    Applicability(discriminator="manufacturer", variants=_variants())


def test_the_refusal_explains_what_to_do_instead() -> None:
    """A guard that only says no invites a workaround. This one names the legitimate route."""
    with pytest.raises(ValueError) as err:
        Applicability(discriminator="vendor", variants=_variants())
    assert "the difference is in the drawing" in str(err.value)


# ---------------------------------------------------------------------------
# Parameter resolution
# ---------------------------------------------------------------------------


def test_no_parameter_resolution_key_is_vendor_derived() -> None:
    """`docs/DESIGN.md` §3.12: the resolver keys are category, layout, project scope and effective
    version. Vendor is not among them, and adding it would let one supplier's overrides apply on
    another's drawings."""
    # Inspect the declaration, not an instance: a field added later is caught even if no test
    # happens to populate it.
    declared = {f.name for f in fields(ProjectScope)}
    offending = {f for f in declared if any(v in f.lower() for v in ("vendor", "supplier"))}
    assert not offending, (
        f"ProjectScope declares vendor-derived field(s): {sorted(offending)}. The resolver keys are "
        "category, layout, project scope and effective version (DESIGN.md §3.12) — adding vendor "
        "would let one supplier's overrides apply on another's drawings."
    )


def test_project_scope_isolates_by_project_not_by_vendor() -> None:
    """Two packages from the same vendor in different projects must not share overrides — the
    isolation key is the project, and only the project."""
    scope = ProjectScope(
        project_id="p-1",
        parameter_set=ParameterSet(
            project_id="p-1", layer=ParameterLayer.PROJECT, version=1, parameters={}
        ),
    )
    assert scope.project_id == "p-1"
    assert not hasattr(scope, "vendor")


# ---------------------------------------------------------------------------
# The guard must be able to fail
# ---------------------------------------------------------------------------


def test_the_reserved_set_is_not_empty() -> None:
    """A guard checking membership of an empty set passes everything. This is the one way it
    could silently stop working."""
    assert RESERVED_DISCRIMINATORS
    assert "vendor" in RESERVED_DISCRIMINATORS
