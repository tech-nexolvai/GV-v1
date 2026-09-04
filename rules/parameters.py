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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType

from rules.schema import Quantity
from units.imperial import format_inches
from verdict.outcomes import Outcome

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


# ---------------------------------------------------------------------------
# Layered resolution — GLOBAL -> PROJECT -> RUN (#65)
# ---------------------------------------------------------------------------


class ParameterMissingError(Exception):
    """Raised when no layer supplies a parameter the check needs.

    The caller turns this into ``NOT_FOUND``. There is deliberately no fallback path:
    `AGENTS.md` §2.4 forbids inventing a value, and a defaulted parameter is an invented value
    wearing a plausible number. The "typical" figures in the client's checklist — a 3/4 inch
    door, a 1 inch field cut — are seeded values a human confirms, never silent defaults.
    """


class LayerConflictError(Exception):
    """Raised when two parameter sets occupy the same layer, or belong to different projects.

    Both are ambiguities rather than merges. "Last wins" has no defined meaning between two
    PROJECT sets, and resolving one project's parameters against another's is the isolation
    failure ADR-0006 describes — a finding that is internally consistent and completely wrong,
    which no tolerance check would catch.
    """


#: Lowest precedence first. Resolution walks this order, so a later layer shadows an earlier
#: one. Written as data rather than as a sequence of ``if`` statements because the order *is*
#: the policy, and it should be legible in one line.
LAYER_PRECEDENCE: tuple[ParameterLayer, ...] = (
    ParameterLayer.GLOBAL,
    ParameterLayer.PROJECT,
    ParameterLayer.RUN,
)


@dataclass(frozen=True, slots=True)
class ShadowedParameter:
    """A value that was overridden, kept so the override is visible rather than silent."""

    value: ParameterValue
    layer: ParameterLayer


@dataclass(frozen=True, slots=True)
class ResolvedParameter:
    """The value a check will use, and the record of what it displaced.

    Carrying ``shadowed`` is what makes an override auditable. Reporting only the winner would
    satisfy "which layer supplied it" while still hiding that a company standard was displaced
    — and that is precisely the thing a reviewer needs to see. It is cheap to record here and
    impossible to reconstruct afterwards.
    """

    name: str
    value: ParameterValue
    layer: ParameterLayer
    shadowed: tuple[ShadowedParameter, ...] = ()

    @property
    def overrides_a_company_standard(self) -> bool:
        """True when this value displaced a GLOBAL company standard.

        The case a reviewer most needs surfaced: GV's own standard was set aside for this
        project or this run, and somebody should be able to see that at a glance.
        """
        return any(s.layer is ParameterLayer.GLOBAL for s in self.shadowed)

    def explain(self) -> str:
        """One line of plain English for a report or a finding."""
        head = (
            f"{self.name} = {format_inches(self.value.value.exact_value)} "
            f"{self.value.value.unit.value} "
            f"({self.layer.value}, {self.value.provenance.value}, set by {self.value.set_by})"
        )
        if not self.shadowed:
            return head
        displaced = ", ".join(
            f"{format_inches(s.value.value.exact_value)} {s.value.value.unit.value} "
            f"({s.layer.value})"
            for s in self.shadowed
        )
        return f"{head}; overrides {displaced}"


def _ordered(sets: Sequence[ParameterSet]) -> tuple[ParameterSet, ...]:
    """Return the sets in precedence order, refusing the two ambiguous arrangements.

    Checked here rather than at the call site because a caller that assembled the wrong sets
    has no way to notice: the resolution would succeed and quietly answer the wrong question.
    """
    by_layer: dict[ParameterLayer, ParameterSet] = {}
    for parameter_set in sets:
        if parameter_set.layer in by_layer:
            raise LayerConflictError(
                f"two parameter sets at the {parameter_set.layer.value} layer. 'Last wins' has "
                "no defined meaning between them, so this is an ambiguity rather than a merge."
            )
        by_layer[parameter_set.layer] = parameter_set

    projects = {s.project_id for s in sets if s.project_id is not None}
    if len(projects) > 1:
        raise LayerConflictError(
            f"parameter sets from more than one project: {sorted(projects)}. Resolving one "
            "project's parameters against another's would produce a finding that is internally "
            "consistent and completely wrong, and no tolerance check would catch it."
        )

    return tuple(by_layer[layer] for layer in LAYER_PRECEDENCE if layer in by_layer)


def resolve(name: str, *sets: ParameterSet) -> ResolvedParameter:
    """Resolve one parameter across the layers, highest precedence winning.

    ``GLOBAL -> PROJECT -> RUN``, last wins. The result records which layer supplied the value
    and every value it displaced.

    Raises :class:`ParameterMissingError` when no layer supplies it — the caller turns that into
    ``NOT_FOUND`` rather than substituting anything.
    """
    ordered = _ordered(sets)

    found: list[ShadowedParameter] = []
    for parameter_set in ordered:
        value = parameter_set.get(name)
        if value is not None:
            found.append(ShadowedParameter(value=value, layer=parameter_set.layer))

    if not found:
        looked_in = ", ".join(s.layer.value for s in ordered) or "no layers"
        raise ParameterMissingError(
            f"{name!r} is set by no layer (looked in: {looked_in}). A missing parameter is "
            "NOT_FOUND, never a default — AGENTS.md §2.4."
        )

    winner = found[-1]
    return ResolvedParameter(
        name=name,
        value=winner.value,
        layer=winner.layer,
        # Reversed so the most recently displaced value reads first.
        shadowed=tuple(reversed(found[:-1])),
    )


def resolve_all(*sets: ParameterSet) -> dict[str, ResolvedParameter]:
    """Resolve every parameter named by any layer.

    Useful for showing a reviewer the full effective parameter set for a review, including
    which values were overridden and by whom.
    """
    ordered = _ordered(sets)
    names = sorted({name for s in ordered for name in s.names()})
    return {name: resolve(name, *sets) for name in names}


# ---------------------------------------------------------------------------
# USER_INPUT — the operand a human types in (#66)
# ---------------------------------------------------------------------------


class UserInputError(ValueError):
    """Raised when a value claiming to be user input could not have come from a person."""


#: Provenances that represent a human deciding or measuring something, as opposed to a value
#: read off a drawing. Used to check that a `USER_INPUT` operand really is one.
HUMAN_PROVENANCES: frozenset[Provenance] = frozenset({Provenance.MEASURED, Provenance.GC_CLIENT})


def user_input(
    value: Quantity,
    *,
    set_by: str,
    set_at: datetime,
    provenance: Provenance = Provenance.MEASURED,
) -> ParameterValue:
    """Build the value behind a ``USER_INPUT`` operand.

    The field wall-to-wall dimension is the case this exists for: someone measures the room and
    types the number in, because it is on no drawing. It is a RUN-layer parameter — set for one
    review — carrying who supplied it and when.

    **A model cannot produce one of these.** Not because this function refuses, but because
    :class:`Provenance` is a closed vocabulary with no member a model could claim, and `rules/`
    cannot import extraction or retrieval at all. The check below is the last of three, not the
    only one.
    """
    if provenance not in HUMAN_PROVENANCES:
        raise UserInputError(
            f"{provenance.value!r} is not a human source. A USER_INPUT operand is measured or "
            "specified by a person; a value from anywhere else is evidence and belongs in the "
            "canonical observation path, where the evidence gate can qualify it."
        )
    return ParameterValue(value=value, provenance=provenance, set_by=set_by, set_at=set_at)


def user_input_set(
    project_id: str,
    version: int,
    parameters: Mapping[str, ParameterValue],
) -> ParameterSet:
    """Collect user inputs for one review into a RUN-layer parameter set.

    RUN rather than PROJECT because these are measured for a single review: the room was that
    width on the day somebody stood in it. Recording them as project settings would imply they
    apply to every later review of the same project, which is exactly the assumption that makes
    a stale field dimension look authoritative.
    """
    for name, parameter in parameters.items():
        if parameter.provenance not in HUMAN_PROVENANCES:
            raise UserInputError(
                f"{name!r} has provenance {parameter.provenance.value!r}, which is not a human "
                "source. A run-layer input is something a person measured or specified."
            )
    return ParameterSet(
        project_id=project_id,
        layer=ParameterLayer.RUN,
        version=version,
        parameters=parameters,
    )


def is_user_input(resolved: ResolvedParameter) -> bool:
    """True when this value came from a person rather than from a drawing.

    A finding shows user inputs differently: a reviewer checking a failed cabinet-filler check
    needs to see at a glance that the field dimension was typed in by someone, not read off the
    shop drawing, because that is the number most likely to be wrong or out of date.
    """
    return resolved.layer is ParameterLayer.RUN and resolved.value.provenance in HUMAN_PROVENANCES


# ---------------------------------------------------------------------------
# Missing is NOT FOUND, never a default (#67)
# ---------------------------------------------------------------------------


def outcome_for_missing_parameter() -> Outcome:
    """The outcome a check must produce when a parameter no layer supplies is required.

    One function rather than each caller choosing, because the choice is not theirs to make.
    `AGENTS.md` §2.4 forbids inventing a value, and the failure mode of getting this wrong is
    not a crash — it is a check that quietly proceeds on a substituted number and returns a
    confident PASS.

    Deliberately returns ``NOT_FOUND`` rather than ``REVIEW_REQUIRED``: the two mean different
    things to a reviewer. ``NOT_FOUND`` says a required input is absent, which sends them to
    supply it. ``REVIEW_REQUIRED`` says the inputs conflict or need judgement, which sends them
    to adjudicate something. A missing parameter is the former.
    """
    return Outcome.NOT_FOUND


def resolve_required(name: str, *sets: ParameterSet) -> ResolvedParameter | Outcome:
    """Resolve a parameter, returning :data:`Outcome.NOT_FOUND` instead of raising.

    For callers that want the outcome rather than the exception — the engine executing a rule,
    for instance, which must record an outcome either way. Behaviour is otherwise identical to
    :func:`resolve`, including the absence of any fallback: this returns the *outcome* of the
    value being missing, never a stand-in for the value.
    """
    try:
        return resolve(name, *sets)
    except ParameterMissingError:
        return outcome_for_missing_parameter()


def seed_company_standards(
    parameters: Mapping[str, ParameterValue], *, version: int = 1
) -> ParameterSet:
    """Build the GLOBAL set holding GV's company standards.

    This exists to make a distinction visible that is otherwise easy to lose. The client's
    checklist gives "typical" figures — a 3/4 inch door thickness, a 1 inch field cut — and
    there are two very different ways to honour them.

    As a **code default**, a typical value is applied silently to every project, and nobody can
    tell afterwards whether a number was chosen or merely assumed. As a **seeded standard**, it
    is a value with `Company standard` provenance, attributed to whoever set it, sitting in a
    layer any project can override and a reviewer can see. Same numbers, entirely different
    accountability.

    Everything here is therefore an ordinary parameter that a person confirmed, not a fallback.
    A parameter absent from this set is still :class:`ParameterMissingError` — seeding is how
    standards are recorded, not a way to avoid the missing case.
    """
    for name, parameter in parameters.items():
        if parameter.provenance is not Provenance.COMPANY_STANDARD:
            raise ValueError(
                f"{name!r} has provenance {parameter.provenance.value!r}. The GLOBAL layer holds "
                "company standards; a project-specific or measured value belongs in a layer "
                "where it can be seen to be an override rather than a standard."
            )
    return ParameterSet(
        project_id=None, layer=ParameterLayer.GLOBAL, version=version, parameters=parameters
    )
