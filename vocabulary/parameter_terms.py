"""The words a passage stating a setting would use, for searching a package's own text (#849).

`workflow/parameter_proposals.py` looks for the passage that states a setting by searching the
package's phrases (`retrieval/package_text.py`) for these. **Words only.** No number, no unit and no
typical value: a term finds a passage, and what the passage says is read from its runs by code and
confirmed by a person. A number here would be a guess about the drawing that the search then went
looking for.

Each term is a query in the search's web-style syntax. A quoted term matches only those words,
adjacent and in that order, so `"side panel"` does not find a line that says `PANEL` and later
`SIDE`.

**Only the settings a passage in the architect's set could state.** The client's checklist gives the
side panel, the overhang and the backsplash as *Specified · G.C / Client*, and the cabinet depth per
project (`rules/parameter_sources.py` cites each). Left out on purpose:

- GV's company standards, the field cut and the fabricator's clearance, which may be cited from
  nothing in a package (`rules.parameter_sources.CITABLE_SIDES`);
- the six cabinet width bounds, which no drawing prints as a bound: a search for single-door
  cabinets would find each cabinet's own width, which is a different number;
- the sink's interior width and depth, which come from the sink's cut sheet (step 4 of #798).

Like the rest of `vocabulary/`, this imports nothing from the project.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

__all__ = ["PARAMETER_TERMS", "search_terms"]

#: Each setting a passage could state, and the queries that would find it, best first.
PARAMETER_TERMS: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "countertop_overhang": ("overhang",),
        "backsplash_thickness": ("backsplash", '"back splash"'),
        "cabinet_side_thickness": ('"side panel"',),
        "cabinet_depth": ('"cabinet depth"',),
    }
)


def search_terms(setting: str) -> tuple[str, ...]:
    """The queries for `setting`, or none for a setting no passage is searched for."""
    if setting not in PARAMETER_TERMS:
        return ()
    return PARAMETER_TERMS[setting]
