"""Where each project setting may come from — Raj's checklist, as a table (#827).

A reviewer typing the overhang into the form used to have it stored as `Measured`, the one word the
form could record, though nobody measured it: it came from the architect's drawings or the project
spec. This table says, per setting, which sources are honest, so the form offers only those and the
API refuses the rest.

**The words are the client's, and the assignments are his.** `Countertop_Checks_Updated.xlsx` gives
each value a "who specifies it" column — *Specified · G.C / Client · Project Specific* for the side
panel, overhang and backsplash (rows 44, 45, 47), *Global minimum · company standard (U.N.O.)* for the
sink's front offset — and `docs/decisions/CT_CHECKS_FORMAT.md` transcribes it. Where his materials
name no source (the six cabinet width bounds, deck slide 2), the admin decided on 2026-10-03 that the
reviewer chooses between the two that could apply.

**Here rather than in `vocabulary/`**, which imports nothing from the project (a test asserts it), and
`Provenance` lives in `rules/parameters.py`. **Not in the rule files**: a source belongs to the setting,
and `countertop_overhang` is one setting shared by two rules, which would otherwise each have to say
the same thing and could say different things.

**Which side of a package each source may be cited from** is `CITABLE_SIDES` (#849): a G.C / Client
value from the architect's drawings only, and a company standard, a fabricator's number or a site
measurement from nothing in the package at all. `workflow/parameter_proposals.py` holds every
passage it points at to that table.

**What this cannot do yet:** prove a typed number did not come from the vendor's drawing under
review. A free-text reference is the reviewer's word. Holding a typed value to the passage it cites
is step 3.3 of #798.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import Final

from rules.parameters import Provenance
from vocabulary.semantic_types import DocumentRole

__all__ = [
    "ALLOWED_SOURCES",
    "CITABLE_SIDES",
    "SOURCE_GUIDANCE",
    "allowed_sources",
    "citable_sides",
    "citable_sources",
    "undeclared",
]

_COMPANY: Final = (Provenance.COMPANY_STANDARD,)
_SPECIFIED: Final = (Provenance.GC_CLIENT,)

#: Each setting a published rule uses, and the sources a value for it may honestly claim, in the
#: order the form offers them. Exhaustive over the rulebook — see `undeclared` and its test.
ALLOWED_SOURCES: Final[Mapping[str, tuple[Provenance, ...]]] = MappingProxyType(
    {
        # "Global minimum · company standard (U.N.O.)".
        "back_offset_minimum": _COMPANY,
        "front_offset_required": _COMPANY,
        # Raj's written rule: "a mutable variable", 1" / 2" (#809).
        "filler_min": _COMPANY,
        "filler_max": _COMPANY,
        # ¼" by default, and "changes from fabricator to fabricator. GV doesn't decide that one."
        "sink_cutout_clearance": (Provenance.COMPANY_STANDARD, Provenance.FABRICATOR),
        # "Specified · G.C / Client · Project Specific" (rows 44, 45, 47).
        "countertop_overhang": _SPECIFIED,
        "backsplash_thickness": _SPECIFIED,
        "cabinet_side_thickness": _SPECIFIED,
        # "Depth of the carcass + thickness of door (¾" typical)", per project.
        "cabinet_depth": _SPECIFIED,
        # "1" is typical", "customizable per project": GV's own fabrication allowance.
        "field_cut": _COMPANY,
        # "Mandatory entries" per project, with no source named: the reviewer picks (admin, 2026-10-03).
        "single_door_cab_width_min": (Provenance.COMPANY_STANDARD, Provenance.GC_CLIENT),
        "single_door_cab_width_max": (Provenance.COMPANY_STANDARD, Provenance.GC_CLIENT),
        "double_door_cab_width_min": (Provenance.COMPANY_STANDARD, Provenance.GC_CLIENT),
        "double_door_cab_width_max": (Provenance.COMPANY_STANDARD, Provenance.GC_CLIENT),
        "drawer_cab_width_min": (Provenance.COMPANY_STANDARD, Provenance.GC_CLIENT),
        "drawer_cab_width_max": (Provenance.COMPANY_STANDARD, Provenance.GC_CLIENT),
        # "Specified · client (sink spec)". A number typed from the cut sheet is the reviewer's
        # input; a number *read* from an uploaded cut sheet is `PRODUCT_SPEC` evidence (ADR-0015).
        "sink_interior_width": _SPECIFIED,
        "sink_interior_depth": _SPECIFIED,
    }
)

#: What the form says beside a source, so the reviewer knows what it permits. The G.C / Client line is
#: Q10 in one sentence: a value the client specifies, copied from the drawing under review, would have
#: the vendor checked against themselves.
SOURCE_GUIDANCE: Final[Mapping[Provenance, str]] = MappingProxyType(
    {
        Provenance.COMPANY_STANDARD: "GV's own standard for this job.",
        Provenance.GC_CLIENT: (
            "From the architect's drawings or the project spec — never from the vendor's drawing."
        ),
        Provenance.FABRICATOR: "Set by the stone fabricator for this job.",
        Provenance.MEASURED: "Measured on site.",
    }
)


#: The document sides a value from each source may be cited from — Q10 as a table (#849).
#:
#: **The vendor's drawing is on no row.** It is the thing under review, and a setting read off it
#: would have the vendor checked against their own number (`rules/overrides.py`). A G.C / Client
#: value comes from the architect's drawings. The other three cite nothing in a package: GV's own
#: standard and the field cut are GV's, the fabricator's clearance is the fabricator's, and a
#: measurement is taken on site. An empty set is the refusal, written out for every source so a new
#: `Provenance` member fails the test that holds this table exhaustive rather than inheriting a side
#: by accident.
CITABLE_SIDES: Final[Mapping[Provenance, frozenset[DocumentRole]]] = MappingProxyType(
    {
        Provenance.GC_CLIENT: frozenset({DocumentRole.ARCH}),
        Provenance.COMPANY_STANDARD: frozenset(),
        Provenance.FABRICATOR: frozenset(),
        Provenance.MEASURED: frozenset(),
    }
)


def citable_sides(source: Provenance) -> frozenset[DocumentRole]:
    """The sides a value from `source` may be cited from, or none for a source this table lacks."""
    if source not in CITABLE_SIDES:
        return frozenset()
    return CITABLE_SIDES[source]


def citable_sources(name: str) -> tuple[Provenance, ...]:
    """The sources a value for `name` may claim *and* cite from a package, in the form's order.

    Empty for a company standard and the field cut, so no passage anywhere in a package can propose
    one, and for a setting this table does not know.
    """
    return tuple(source for source in allowed_sources(name) if citable_sides(source))


def allowed_sources(name: str) -> tuple[Provenance, ...]:
    """The sources a value for `name` may claim, or none for a setting this table does not know.

    None rather than every source: a setting nobody has assigned is one whose honest source is
    unknown, and the form should refuse it rather than offer a guess. Spelled as a membership test
    rather than `.get(name, ())`: no value is defaulted here, and the empty answer is the refusal.
    """
    if name not in ALLOWED_SOURCES:
        return ()
    return ALLOWED_SOURCES[name]


def undeclared(names: Iterable[str]) -> tuple[str, ...]:
    """The settings among `names` with no entry here, sorted — what a newly authored rule forgot."""
    return tuple(sorted(name for name in set(names) if name not in ALLOWED_SOURCES))
