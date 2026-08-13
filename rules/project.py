"""The project as the deterministic core sees it.

A project is one finalized vendor and one brand, and it does two distinct jobs (ADR-0006):

* **A resolver key** — it supplies parameter overrides for a check (filler min/max, field cut
  size, tolerances), layering over global defaults exactly as the client's checklist describes
  with *"Global / Project Based Input"*.
* **An isolation boundary** — retrieval and matching filter by project, so one project's
  references can never be offered as evidence in another project's review.

This module carries **only** what the deterministic core needs: the identifier and a pinned
parameter set. Brand and vendor are business metadata belonging to the full project record in the
control plane, because `rules/` must not import `app/` (`docs/DESIGN.md` §2). Vendor is never a
rule key in any case — every vendor is held to the same rule for the same layout.

**The project layer is a `ParameterSet`, not a mapping of bare values.** It used to be a plain
`Mapping[str, Quantity]`, which recorded what a number was and lost who decided it. Since #64
every parameter carries its provenance, who set it and when, at every layer — and the project
layer is exactly where human-set overrides live, so it is the last place that record should be
missing. The accessors below survive as read-through delegations; what went is the second copy
of the data, not the convenience.

**The pin is a specific version, never "latest".** A project points at one immutable, content-
addressed set, so a finding can name the exact numbers that judged a drawing months later. A
scope that resolved "whatever is current" would answer that question differently on every re-run.

**Precedence between layers is deliberately not here.** `docs/DESIGN.md` §3.9 places
`GLOBAL -> PROJECT -> RUN` resolution in `rules/parameters.py`. This module supplies the project
layer that resolution consumes; implementing the precedence twice is how two answers to the same
question start to disagree.
"""

from __future__ import annotations

from dataclasses import dataclass

from rules.parameters import ParameterLayer, ParameterSet, ParameterValue


class InvalidProjectScopeError(ValueError):
    """Raised when a project scope could not act as an isolation boundary."""


@dataclass(frozen=True, slots=True)
class ProjectScope:
    """A project's identity and the exact parameter set pinned for it."""

    project_id: str

    parameter_set: ParameterSet
    """The pinned project-layer set — one immutable version, never "latest".

    Holding the set itself rather than an identifier to look up is what makes the pin
    structural: there is no resolution step here that could return something different on a
    later run.
    """

    def __post_init__(self) -> None:
        if not isinstance(self.project_id, str) or not self.project_id.strip():
            raise InvalidProjectScopeError(
                "project_id must be a non-empty string. It is the isolation key, and an "
                "empty one would match every project — letting one project's references be "
                "offered as evidence in another's review."
            )
        if not isinstance(self.parameter_set, ParameterSet):
            raise InvalidProjectScopeError(
                f"parameter_set must be a ParameterSet carrying provenance and a version, got "
                f"{type(self.parameter_set).__name__}. A bare mapping of values would record "
                "what the numbers are and lose who set them — and this is the layer where "
                "human-set overrides live."
            )
        if self.parameter_set.layer is not ParameterLayer.PROJECT:
            raise InvalidProjectScopeError(
                f"a project scope pins a {ParameterLayer.PROJECT.value} set, got "
                f"{self.parameter_set.layer.value}. A global standard reaching a project as "
                "though it were an override would hide the fact that nobody chose it for this "
                "project."
            )
        if self.parameter_set.project_id != self.project_id:
            raise InvalidProjectScopeError(
                f"parameter set belongs to {self.parameter_set.project_id!r} but this scope is "
                f"{self.project_id!r}. Serving one project's parameters to another is the "
                "isolation failure this type exists to prevent."
            )

    @property
    def parameter_set_id(self) -> str:
        """The content identifier of the pinned set, for a finding to record."""
        return self.parameter_set.set_id

    @property
    def parameter_set_version(self) -> int:
        """The pinned version, for a report to name in plain English."""
        return self.parameter_set.version

    def override_for(self, name: str) -> ParameterValue | None:
        """Return this project's override for a parameter, or ``None`` if it sets none.

        A read-through delegation to the pinned set, kept because callers find it convenient —
        but it returns the provenance-carrying value, so no caller can read a project parameter
        as a bare number.

        ``None`` means **"this project sets no override"** — not "no value exists". Falling
        through to the global layer is `rules/parameters.py`'s decision (#65), not this type's.
        Treating the two as the same thing would put value-resolution policy inside a data
        structure, where it could quietly diverge from the real resolver.
        """
        return self.parameter_set.get(name)

    def overrides(self) -> tuple[str, ...]:
        """Names of the parameters this project overrides, sorted for stable reporting."""
        return self.parameter_set.names()

    def __contains__(self, name: object) -> bool:
        return name in self.parameter_set
