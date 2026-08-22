"""Filter and rank possible matches using explicitly configured drawing metadata.

Known hard mismatches remove a pair. Missing metadata remains ``UNKNOWN`` rather than pretending to
disagree and silently dropping a possible correspondence. Every comparison is retained in a sidecar
evaluation while the proposal collection itself contains only advisory ``MatchCandidate`` values.

Source: ``docs/DESIGN_EXTRACTION.md`` section 8 and issue #189.
Verification: ``tests/retrieval/lanes/test_metadata.py``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum, auto
from types import MappingProxyType
from uuid import UUID

from retrieval.candidate import Lane, MatchCandidate


class MetadataField(StrEnum):
    """The complete metadata vocabulary used by lane 3."""

    SHEET = auto()
    VIEW = auto()
    ITEM_TYPE = auto()
    MATERIAL = auto()


class ComparisonStatus(StrEnum):
    """What exact comparison established for one field."""

    MATCH = auto()
    MISMATCH = auto()
    UNKNOWN = auto()


@dataclass(frozen=True, slots=True)
class MetadataItem:
    """One item's identity and literal authored metadata; absence stays explicit."""

    item_id: UUID
    sheet: str | None
    view: str | None
    item_type: str | None
    material: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.item_id, UUID):
            raise TypeError("item_id must be a UUID")
        for name in MetadataField:
            value = getattr(self, name.value)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{name.value} must be a non-empty string or None")

    def value_for(self, name: MetadataField) -> str | None:
        """Return one field without accepting caller-supplied attribute names."""

        if not isinstance(name, MetadataField):
            raise TypeError("name must be a MetadataField")
        value = getattr(self, name.value)
        return value if isinstance(value, str) else None


@dataclass(frozen=True, slots=True)
class MetadataPolicy:
    """Explicit hard filters and exact ranking weights; no business defaults are inferred."""

    hard_fields: frozenset[MetadataField]
    weights: Mapping[MetadataField, Decimal]
    _weights: Mapping[MetadataField, Decimal] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.hard_fields, frozenset) or not all(
            isinstance(name, MetadataField) for name in self.hard_fields
        ):
            raise TypeError("hard_fields must be a frozenset of MetadataField values")
        if not isinstance(self.weights, Mapping):
            raise TypeError("weights must be a mapping")
        if set(self.weights) != set(MetadataField):
            raise ValueError("weights must explicitly define every metadata field")
        copied: dict[MetadataField, Decimal] = {}
        for name, weight in self.weights.items():
            if not isinstance(name, MetadataField):
                raise TypeError("weight keys must be MetadataField values")
            if not isinstance(weight, Decimal):
                raise TypeError("metadata weights must be Decimal; float is not allowed")
            if not weight.is_finite() or weight <= 0:
                raise ValueError("metadata weights must be finite and positive")
            copied[name] = weight
        frozen = MappingProxyType(copied)
        object.__setattr__(self, "weights", frozen)
        object.__setattr__(self, "_weights", frozen)

    def weight_for(self, name: MetadataField) -> Decimal:
        """Return the immutable exact weight for one field."""

        return self._weights[name]


@dataclass(frozen=True, slots=True)
class MetadataComparison:
    """An explainable comparison, including whether disagreement removes the pair."""

    field: MetadataField
    left_value: str | None
    right_value: str | None
    status: ComparisonStatus
    hard_filter: bool


@dataclass(frozen=True, slots=True)
class MetadataEvaluation:
    """The complete explanation for one considered pair."""

    left_item_id: UUID
    right_item_id: UUID
    candidate: MatchCandidate | None
    comparisons: tuple[MetadataComparison, ...]

    @property
    def filtered(self) -> bool:
        """Return whether a known hard mismatch removed this pair."""

        return self.candidate is None


@dataclass(frozen=True, slots=True)
class MetadataLaneResult:
    """Ranked advisory candidates plus explanations for retained and rejected pairs."""

    candidates: tuple[MatchCandidate, ...]
    evaluations: tuple[MetadataEvaluation, ...]


def _compare(
    left: MetadataItem, right: MetadataItem, policy: MetadataPolicy
) -> tuple[MetadataComparison, ...]:
    comparisons: list[MetadataComparison] = []
    for name in MetadataField:
        left_value = left.value_for(name)
        right_value = right.value_for(name)
        if left_value is None or right_value is None:
            status = ComparisonStatus.UNKNOWN
        elif left_value == right_value:
            status = ComparisonStatus.MATCH
        else:
            status = ComparisonStatus.MISMATCH
        comparisons.append(
            MetadataComparison(
                field=name,
                left_value=left_value,
                right_value=right_value,
                status=status,
                hard_filter=name in policy.hard_fields,
            )
        )
    return tuple(comparisons)


def _score(comparisons: tuple[MetadataComparison, ...], policy: MetadataPolicy) -> Decimal:
    matched = sum(
        (
            policy.weight_for(item.field)
            for item in comparisons
            if item.status is ComparisonStatus.MATCH
        ),
        start=Decimal(0),
    )
    total = sum((policy.weight_for(name) for name in MetadataField), start=Decimal(0))
    return matched / total


def _rank_key(candidate: MatchCandidate) -> tuple[Decimal, int]:
    if candidate.score is None:
        raise ValueError("metadata candidates must carry their exact diagnostic score")
    return -candidate.score, candidate.right_item_id.int


def metadata_match(
    subject: MetadataItem,
    possible_matches: Sequence[MetadataItem],
    *,
    policy: MetadataPolicy,
) -> MetadataLaneResult:
    """Filter known impossibilities and rank every remaining pair by exact weighted agreement."""

    if not isinstance(subject, MetadataItem):
        raise TypeError("subject must be a MetadataItem")
    if not isinstance(policy, MetadataPolicy):
        raise TypeError("policy must be a MetadataPolicy")
    evaluations: list[MetadataEvaluation] = []
    retained: list[MatchCandidate] = []
    for possible in possible_matches:
        if not isinstance(possible, MetadataItem):
            raise TypeError("possible_matches must contain only MetadataItem values")
        comparisons = _compare(subject, possible, policy)
        impossible = any(
            item.hard_filter and item.status is ComparisonStatus.MISMATCH for item in comparisons
        )
        candidate = None
        if not impossible:
            candidate = MatchCandidate(
                left_item_id=subject.item_id,
                right_item_id=possible.item_id,
                lane=Lane.METADATA,
                score=_score(comparisons, policy),
            )
            retained.append(candidate)
        evaluations.append(
            MetadataEvaluation(subject.item_id, possible.item_id, candidate, comparisons)
        )
    ranked = tuple(sorted(retained, key=_rank_key))
    return MetadataLaneResult(ranked, tuple(evaluations))
