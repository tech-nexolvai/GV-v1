"""The project as the deterministic core sees it.

A project is one finalized vendor and one brand, and it does two distinct jobs (ADR-0006):

* **A resolver key** — it supplies parameter overrides for a check (filler min/max, field cut
  size, tolerances), layering over global defaults exactly as the client's checklist describes
  with *"Global / Project Based Input"*.
* **An isolation boundary** — retrieval and matching filter by project, so one project's
  references can never be offered as evidence in another project's review.

This module carries **only** what the deterministic core needs: the identifier and the
overrides. Brand and vendor are business metadata belonging to the full project record in the
control plane, because `rules/` must not import `app/` (`docs/DESIGN.md` §2). Vendor is never a
rule key in any case — every vendor is held to the same rule for the same layout.

**Precedence between layers is deliberately not here.** `docs/DESIGN.md` §3.9 places
`GLOBAL -> PROJECT -> RUN` resolution in `rules/parameters.py`. This module supplies the project
layer that resolution consumes; implementing the precedence twice is how two answers to the same
question start to disagree.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from rules.schema import Quantity


class InvalidProjectScopeError(ValueError):
    """Raised when a project scope could not act as an isolation boundary."""


@dataclass(frozen=True, slots=True)
class ProjectScope:
    """A project's identity and its parameter overrides.

    Construct with a plain mapping; it is copied into an immutable one. A frozen dataclass
    holding a caller's dict is not actually immutable — the caller could mutate that dict
    afterwards and the "frozen" scope would change underneath, which for an isolation key is
    a bug worth preventing rather than documenting.
    """

    project_id: str
    parameter_overrides: Mapping[str, Quantity]

    def __post_init__(self) -> None:
        if not isinstance(self.project_id, str) or not self.project_id.strip():
            raise InvalidProjectScopeError(
                "project_id must be a non-empty string. It is the isolation key, and an "
                "empty one would match every project — letting one project's references be "
                "offered as evidence in another's review."
            )
        if not isinstance(self.parameter_overrides, Mapping):
            raise InvalidProjectScopeError("parameter_overrides must be a mapping")
        for name, quantity in self.parameter_overrides.items():
            if not isinstance(name, str) or not name.strip():
                raise InvalidProjectScopeError("parameter names must be non-empty strings")
            if not isinstance(quantity, Quantity):
                raise InvalidProjectScopeError(
                    f"override {name!r} must be a Quantity so the value stays exact, "
                    f"got {type(quantity).__name__}"
                )
        # Defensive copy into a read-only view, so the caller's dict cannot mutate this scope.
        object.__setattr__(
            self, "parameter_overrides", MappingProxyType(dict(self.parameter_overrides))
        )

    def override_for(self, name: str) -> Quantity | None:
        """Return this project's override for a parameter, or ``None`` if it sets none.

        ``None`` means **"this project sets no override"** — not "no value exists". Falling
        through to the global layer is `rules/parameters.py`'s decision (#65), not this type's.
        Treating the two as the same thing would put value-resolution policy inside a data
        structure, where it could quietly diverge from the real resolver.
        """
        return self.parameter_overrides.get(name)

    def overrides(self) -> tuple[str, ...]:
        """Names of the parameters this project overrides, sorted for stable reporting."""
        return tuple(sorted(self.parameter_overrides))

    def __contains__(self, name: object) -> bool:
        return name in self.parameter_overrides
