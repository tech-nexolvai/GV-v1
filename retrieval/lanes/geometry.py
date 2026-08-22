"""Create advisory match candidates from document-independent geometric relationships.

Signals describe relationships already established by the extraction geometry layer: corresponding
views, spatial adjacency and consistent dimensional structure. Raw polygons from different documents
are never compared here because they do not share a coordinate plane.

Source: ``docs/DESIGN_EXTRACTION.md`` section 8 and issue #182.
Verification: ``tests/retrieval/lanes/test_geometry.py``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum, auto
from types import MappingProxyType
from uuid import UUID

from retrieval.candidate import Lane, MatchCandidate


class GeometrySignalKind(StrEnum):
    """The closed set of structural relationships lane 4 understands."""

    SHARED_VIEW = auto()
    SPATIAL_ADJACENCY = auto()
    DIMENSIONAL_RELATIONSHIP = auto()


@dataclass(frozen=True, slots=True)
class GeometrySignal:
    """One source-linked geometric conclusion produced upstream.

    ``strength`` is exact diagnostic support from zero to one. It is not a probability and grants no
    approval authority. A non-supporting signal carries zero strength and records a contradiction.
    """

    kind: GeometrySignalKind
    supports_match: bool
    strength: Decimal
    explanation: str
    evidence_ids: tuple[UUID, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.kind, GeometrySignalKind):
            raise TypeError("kind must be a GeometrySignalKind")
        if not isinstance(self.supports_match, bool):
            raise TypeError("supports_match must be a bool")
        if not isinstance(self.strength, Decimal):
            raise TypeError("strength must be Decimal; float is not allowed")
        if not self.strength.is_finite() or not Decimal(0) <= self.strength <= Decimal(1):
            raise ValueError("strength must be a finite Decimal from zero to one")
        if not self.supports_match and self.strength != 0:
            raise ValueError("a contradictory signal must have zero strength")
        if not isinstance(self.explanation, str) or not self.explanation.strip():
            raise ValueError("explanation must be a non-empty string")
        if not isinstance(self.evidence_ids, tuple) or not self.evidence_ids:
            raise ValueError("evidence_ids must be a non-empty tuple")
        if not all(isinstance(value, UUID) for value in self.evidence_ids):
            raise TypeError("evidence_ids must contain only UUID values")


@dataclass(frozen=True, slots=True)
class GeometryPair:
    """All geometric evidence considered for one possible item correspondence."""

    left_item_id: UUID
    right_item_id: UUID
    signals: tuple[GeometrySignal, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.left_item_id, UUID):
            raise TypeError("left_item_id must be a UUID")
        if not isinstance(self.right_item_id, UUID):
            raise TypeError("right_item_id must be a UUID")
        if self.left_item_id == self.right_item_id:
            raise ValueError("left and right items must be distinct")
        if not isinstance(self.signals, tuple) or not self.signals:
            raise ValueError("signals must be a non-empty tuple of GeometrySignal values")
        if not all(isinstance(signal, GeometrySignal) for signal in self.signals):
            raise TypeError("signals must contain only GeometrySignal values")
        kinds = [signal.kind for signal in self.signals]
        if len(kinds) != len(set(kinds)):
            raise ValueError("a geometry pair may carry only one signal of each kind")


@dataclass(frozen=True, slots=True)
class GeometryPolicy:
    """Required signal kinds and exact diagnostic weights, supplied without defaults."""

    required_kinds: frozenset[GeometrySignalKind]
    weights: Mapping[GeometrySignalKind, Decimal]
    _weights: Mapping[GeometrySignalKind, Decimal] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.required_kinds, frozenset) or not all(
            isinstance(kind, GeometrySignalKind) for kind in self.required_kinds
        ):
            raise TypeError("required_kinds must be a frozenset of GeometrySignalKind values")
        if not isinstance(self.weights, Mapping):
            raise TypeError("weights must be a mapping")
        if set(self.weights) != set(GeometrySignalKind):
            raise ValueError("weights must explicitly define every geometry signal kind")
        copied: dict[GeometrySignalKind, Decimal] = {}
        for kind, weight in self.weights.items():
            if not isinstance(kind, GeometrySignalKind):
                raise TypeError("weight keys must be GeometrySignalKind values")
            if not isinstance(weight, Decimal):
                raise TypeError("geometry weights must be Decimal; float is not allowed")
            if not weight.is_finite() or weight <= 0:
                raise ValueError("geometry weights must be finite and positive")
            copied[kind] = weight
        frozen = MappingProxyType(copied)
        object.__setattr__(self, "weights", frozen)
        object.__setattr__(self, "_weights", frozen)

    def weight_for(self, kind: GeometrySignalKind) -> Decimal:
        """Return one immutable exact signal weight."""

        return self._weights[kind]


@dataclass(frozen=True, slots=True)
class GeometryEvaluation:
    """A candidate or refusal together with every geometric reason that produced it."""

    pair: GeometryPair
    candidate: MatchCandidate | None
    reason: str

    @property
    def filtered(self) -> bool:
        """Return whether required geometric support was absent or contradictory."""

        return self.candidate is None


@dataclass(frozen=True, slots=True)
class GeometryLaneResult:
    """Ranked geometry candidates and auditable evaluations for every considered pair."""

    candidates: tuple[MatchCandidate, ...]
    evaluations: tuple[GeometryEvaluation, ...]


def _score(pair: GeometryPair, policy: GeometryPolicy) -> Decimal:
    weighted = sum(
        (
            policy.weight_for(signal.kind) * signal.strength
            for signal in pair.signals
            if signal.supports_match
        ),
        start=Decimal(0),
    )
    total = sum((policy.weight_for(kind) for kind in GeometrySignalKind), start=Decimal(0))
    return weighted / total


def _rank_key(candidate: MatchCandidate) -> tuple[Decimal, int]:
    if candidate.score is None:
        raise ValueError("geometry candidates must carry their exact diagnostic score")
    return -candidate.score, candidate.right_item_id.int


def geometry_match(pairs: Sequence[GeometryPair], *, policy: GeometryPolicy) -> GeometryLaneResult:
    """Filter unsupported structures and rank the remaining advisory geometry proposals."""

    if not isinstance(policy, GeometryPolicy):
        raise TypeError("policy must be a GeometryPolicy")
    evaluations: list[GeometryEvaluation] = []
    retained: list[MatchCandidate] = []
    for pair in pairs:
        if not isinstance(pair, GeometryPair):
            raise TypeError("pairs must contain only GeometryPair values")
        by_kind = {signal.kind: signal for signal in pair.signals}
        missing = policy.required_kinds - by_kind.keys()
        contradicted = {
            kind
            for kind in policy.required_kinds
            if kind in by_kind and not by_kind[kind].supports_match
        }
        candidate = None
        if missing:
            names = ", ".join(sorted(kind.value for kind in missing))
            reason = f"required geometric evidence is absent: {names}"
        elif contradicted:
            names = ", ".join(sorted(kind.value for kind in contradicted))
            reason = f"required geometric evidence contradicts the match: {names}"
        else:
            candidate = MatchCandidate(
                pair.left_item_id,
                pair.right_item_id,
                Lane.GEOMETRY,
                _score(pair, policy),
            )
            retained.append(candidate)
            reason = "required geometry supports an advisory correspondence"
        evaluations.append(GeometryEvaluation(pair, candidate, reason))
    return GeometryLaneResult(tuple(sorted(retained, key=_rank_key)), tuple(evaluations))
