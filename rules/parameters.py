"""Values the rules need that appear on no drawing.

Field cut size, filler minimum and maximum, countertop overhang, sink front offset: the client's
checklist calls these *"Global / Project Based Input"*, and none of them can be read off a
drawing. Somebody decides them, and a check is only as trustworthy as the record of who.

So a parameter here is never a bare number. It carries its exact value, where it came from, who
set it and when. A finding that used a parameter can therefore answer "why was the tolerance
this?" months later — which is the whole point of `AGENTS.md` §2.7.

**Versioned and immutable, the same way rules are.** A parameter set is identified by the hash of
its own content, so an edited set is a different set by construction rather than by convention.
`ParameterSetStore` enforces one content hash per `(project_id, layer, version)`: changing a
published set without bumping its version is a loud error, exactly as ADR-0006 requires of rules.
Without that, "the parameter set version 3" would name two different things and a finding could
not be reproduced.

**This module stores parameters. It does not resolve them.** `GLOBAL -> PROJECT -> RUN`
precedence is #65's, and implementing it in two places is how two answers to the same question
start to disagree.

See `docs/DESIGN.md` §3.9 and plan §F5.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType

from rules.schema import Quantity

#: Prefix on every parameter-set identifier, so the algorithm is visible in stored data.
HASH_ALGORITHM = "sha256"


class ParameterLayer(StrEnum):
    """Which layer set a parameter (`docs/DESIGN.md` §3.9)."""

    GLOBAL = "global"
    """Company-wide standard, applying to every project unless overridden."""

    PROJECT = "project"
    """This project's override — the client's *"Global / Project Based Input"*."""

    RUN = "run"
    """Set for one review, e.g. a dimension measured on site that day."""


class Provenance(StrEnum):
    """Where a parameter's value came from.

    A controlled vocabulary rather than a free string, for the reason ADR-0007 gave for
    `ProductType`: a typo'd or improvised source publishes cleanly and then misleads a reviewer
    about the authority behind a number. Provenance is the field a reviewer reads when deciding
    whether to trust a value, so it is the last one that should be free text.

    These are the three sources #64 names. `docs/V1_RESEARCH_AND_PLAN.md` §F5 records the
    client's own phrasing in six variants; mapping the remaining three onto these is a client
    vocabulary question, raised on the issue rather than guessed at here.
    """

    GC_CLIENT = "G.C / Client"
    """The general contractor or the client chose it — e.g. countertop overhang."""

    COMPANY_STANDARD = "Company standard"
    """GV's own standard — e.g. the 4 inch sink front offset minimum."""

    MEASURED = "Measured"
    """Someone measured it on site — e.g. the field wall-to-wall dimension."""


class ParameterSetConflictError(Exception):
    """Raised when a `(project_id, layer, version)` would map to a second content hash.

    A finding pins the parameter-set version it used, so that version has to identify exactly
    one set of values. Two sets sharing version 3 with different filler minimums would leave a
    reviewer unable to tell which numbers judged their drawing — and the check would still have
    run, producing a confident verdict from parameters nobody could later identify.

    The fix is always the same: bump the version.
    """


@dataclass(frozen=True, slots=True)
class ParameterValue:
    """One parameter, with the record of where it came from.

    ``set_at`` is supplied by the caller, never read from a clock here. This module records when
    someone set a value; it does not observe the passing of time, and a module that read a clock
    could not be tested deterministically.
    """

    value: Quantity
    provenance: Provenance
    set_by: str
    set_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.value, Quantity):
            raise TypeError(
                f"a parameter value must be a Quantity so it stays exact, got "
                f"{type(self.value).__name__}. A float here would reintroduce the rounding "
                "error ADR-0001 exists to prevent."
            )
        if not self.set_by.strip():
            raise ValueError(
                "set_by must name who set this parameter. An unattributed value cannot be "
                "questioned later, which is the reason provenance is recorded at all."
            )

    def canonical_form(self) -> dict[str, str]:
        """This value as sorted primitive fields, for hashing.

        The exact value is emitted as a string — ``"1/8"``, never ``0.125`` — because ADR-0001
        forbids floats reaching arithmetic, and a float in the hashed bytes would mean the
        identifier itself was derived from an inexact number.
        """
        return {
            "value": str(self.value.exact_value),
            "unit": self.value.unit.value,
            "provenance": self.provenance.value,
            "set_by": self.set_by,
            "set_at": self.set_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class ParameterSet:
    """One project's parameters at one layer, frozen at a version.

    Immutability is structural rather than a convention: the dataclass is frozen, the mapping is
    copied into a read-only view, and :attr:`set_id` is the hash of the content — so there is no
    way to change a value while keeping the identifier.
    """

    project_id: str | None
    """The project these belong to, or ``None`` for the company-wide GLOBAL layer."""

    layer: ParameterLayer
    version: int
    parameters: Mapping[str, ParameterValue]

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError(f"version starts at 1, got {self.version}")
        if self.layer is ParameterLayer.GLOBAL:
            if self.project_id is not None:
                raise ValueError(
                    "the GLOBAL layer is company-wide, so it carries no project_id. A "
                    "project-specific value belongs in a PROJECT set, where it can be seen to "
                    "be an override rather than a standard."
                )
        elif not (self.project_id or "").strip():
            raise ValueError(f"a {self.layer.value} parameter set must name its project")

        for name, parameter in self.parameters.items():
            if not name.strip():
                raise ValueError("parameter names must be non-empty")
            if not isinstance(parameter, ParameterValue):
                raise TypeError(
                    f"parameter {name!r} must be a ParameterValue carrying its provenance, got "
                    f"{type(parameter).__name__}. A bare value would record what the number is "
                    "and lose who decided it."
                )

        # Defensive copy: a frozen dataclass holding a caller's dict is not actually immutable.
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))

    # -- identity ---------------------------------------------------------

    def canonical_json(self) -> str:
        """The set as canonical JSON: sorted keys, no insignificant whitespace.

        Follows `rules/snapshot.py`, which in turn follows RFC 8785, so the identifier stays
        independent of the order parameters were written in — two logically identical sets must
        not hash differently because one listed the filler minimum first.
        """
        payload = {
            "project_id": self.project_id,
            "layer": self.layer.value,
            "version": self.version,
            "parameters": {
                name: value.canonical_form() for name, value in sorted(self.parameters.items())
            },
        }
        return json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )

    @property
    def set_id(self) -> str:
        """The content identifier, e.g. ``sha256:1a2b...``.

        A finding stores this to pin the exact parameters that judged a drawing.

        ``set_at`` is deliberately *inside* the hash, which is the opposite of ADR-0006's rule
        for rule snapshots — and for a reason. There, the excluded timestamp was *publication*
        time, a clock read at publish that would have broken "identical input, identical
        identifier". Here the timestamp is authored data: two sets recording the same number
        measured on different days are genuinely different records, and collapsing them would
        lose the distinction a reviewer needs.
        """
        digest = hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()
        return f"{HASH_ALGORITHM}:{digest}"

    @property
    def label(self) -> str:
        """A human-readable description for a report, e.g. ``PRJ-1 project v3 (1a2b3c4d)``."""
        scope = self.project_id or "company-wide"
        return f"{scope} {self.layer.value} v{self.version} ({self.set_id.split(':', 1)[1][:8]})"

    # -- reading ----------------------------------------------------------

    def get(self, name: str) -> ParameterValue | None:
        """Return the parameter, or ``None`` when this set does not carry it.

        ``None`` means **"this set does not set it"**, never "no value exists". Falling through
        to another layer is `resolve()`'s decision (#65), not this type's — putting the
        precedence in a data structure is how it quietly diverges from the real resolver.
        """
        return self.parameters.get(name)

    def names(self) -> tuple[str, ...]:
        """The parameters this set carries, sorted for stable reporting."""
        return tuple(sorted(self.parameters))

    def __contains__(self, name: object) -> bool:
        return name in self.parameters


class ParameterSetStore:
    """Published parameter sets, append-only.

    Republishing identical content is idempotent. Publishing *different* content under a version
    that already exists is refused, which is what makes a version number worth pinning.
    """

    def __init__(self) -> None:
        self._by_id: dict[str, ParameterSet] = {}
        self._by_version: dict[tuple[str | None, ParameterLayer, int], str] = {}

    def add(self, parameter_set: ParameterSet) -> ParameterSet:
        """Store a set, or return the existing one when it is already present."""
        existing = self._by_id.get(parameter_set.set_id)
        if existing is not None:
            return existing

        key = (parameter_set.project_id, parameter_set.layer, parameter_set.version)
        clash = self._by_version.get(key)
        if clash is not None:
            raise ParameterSetConflictError(
                f"{parameter_set.label} is already published as {clash[:15]}..., and this is "
                f"different content ({parameter_set.set_id[:15]}...). A published parameter set "
                "cannot be changed in place — bump the version instead. Otherwise a finding "
                "that pinned this version could not name the values that judged it."
            )

        self._by_id[parameter_set.set_id] = parameter_set
        self._by_version[key] = parameter_set.set_id
        return parameter_set

    def get(self, set_id: str) -> ParameterSet:
        """Return the set with this identifier.

        Raises ``KeyError`` when absent. A finding referencing an unknown parameter set is an
        integrity problem rather than a cache miss, so there is deliberately no default.
        """
        return self._by_id[set_id]

    def __contains__(self, set_id: object) -> bool:
        return set_id in self._by_id

    def __len__(self) -> int:
        return len(self._by_id)
