"""Rank possible OCR variants through PostgreSQL ``pg_trgm`` without approving them.

The SQL casts ``similarity()`` from PostgreSQL ``real`` to ``numeric`` before it crosses the
boundary. Python therefore receives ``Decimal`` rather than disguising a binary float as an exact
score. Callers supply the eligible item IDs, keeping package scope outside model-produced text.

Source: ``docs/DESIGN_EXTRACTION.md`` section 8 and issue #174.
Verification: ``tests/retrieval/lanes/test_trigram.py``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from retrieval.candidate import Lane, MatchCandidate

TRIGRAM_SQL = """
SELECT
    drawing_item_id,
    value_as_printed,
    CAST(similarity(value_as_printed, CAST(:query AS text)) AS numeric) AS similarity
FROM item_identifiers
WHERE drawing_item_id = ANY(CAST(:eligible_item_ids AS uuid[]))
  AND CAST(similarity(value_as_printed, CAST(:query AS text)) AS numeric) >= :threshold
ORDER BY similarity DESC, drawing_item_id, value_as_printed
""".strip()


class TrigramExecutor(Protocol):
    """Database seam implemented by the session-owning worker layer."""

    def fetch_all(
        self, statement: str, parameters: Mapping[str, object]
    ) -> Sequence[Mapping[str, object]]:
        """Execute one scoped read and return mapping-shaped rows."""


@dataclass(frozen=True, slots=True)
class TrigramEvaluation:
    """One candidate with the exact query, matched text, score and configured threshold."""

    candidate: MatchCandidate
    query: str
    matched_identifier: str
    similarity: Decimal
    threshold: Decimal


@dataclass(frozen=True, slots=True)
class TrigramLaneResult:
    """Ranked candidate-only results and their replayable threshold metadata."""

    candidates: tuple[MatchCandidate, ...]
    evaluations: tuple[TrigramEvaluation, ...]


class TrigramRepositoryError(ValueError):
    """The database returned a row outside the requested safe contract."""


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _unit_interval(value: object, name: str) -> Decimal:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be Decimal; float is not allowed")
    if not value.is_finite() or not Decimal(0) <= value <= Decimal(1):
        raise ValueError(f"{name} must be a finite Decimal from zero to one")
    return value


def _row(
    value: Mapping[str, object], *, eligible: frozenset[UUID], threshold: Decimal
) -> tuple[UUID, str, Decimal]:
    item_id = value.get("drawing_item_id")
    identifier = value.get("value_as_printed")
    similarity = value.get("similarity")
    if not isinstance(item_id, UUID):
        raise TrigramRepositoryError("drawing_item_id must be a UUID")
    if item_id not in eligible:
        raise TrigramRepositoryError("database returned an item outside the eligible scope")
    try:
        identifier = _text(identifier, "value_as_printed")
        similarity = _unit_interval(similarity, "similarity")
    except (TypeError, ValueError) as error:
        raise TrigramRepositoryError(str(error)) from error
    if similarity < threshold:
        raise TrigramRepositoryError(
            "database returned a similarity below the configured threshold"
        )
    return item_id, identifier, similarity


def trigram_match(
    subject_item_id: UUID,
    query: str,
    eligible_item_ids: Sequence[UUID],
    *,
    threshold: Decimal,
    executor: TrigramExecutor,
) -> TrigramLaneResult:
    """Run one scoped ``pg_trgm`` query and return ranked advisory candidates only."""

    if not isinstance(subject_item_id, UUID):
        raise TypeError("subject_item_id must be a UUID")
    query = _text(query, "query")
    threshold = _unit_interval(threshold, "threshold")
    eligible = frozenset(eligible_item_ids)
    if not eligible:
        return TrigramLaneResult((), ())
    if not all(isinstance(item_id, UUID) for item_id in eligible):
        raise TypeError("eligible_item_ids must contain only UUID values")
    if subject_item_id in eligible:
        raise ValueError("the subject item cannot be one of its own eligible matches")
    rows = executor.fetch_all(
        TRIGRAM_SQL,
        {
            "query": query,
            "eligible_item_ids": tuple(sorted(eligible, key=lambda value: value.int)),
            "threshold": threshold,
        },
    )
    best: dict[UUID, tuple[str, Decimal]] = {}
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise TrigramRepositoryError("database rows must be mappings")
        item_id, identifier, similarity = _row(raw, eligible=eligible, threshold=threshold)
        previous = best.get(item_id)
        if previous is None or (-similarity, identifier) < (-previous[1], previous[0]):
            best[item_id] = (identifier, similarity)
    evaluations = tuple(
        TrigramEvaluation(
            candidate=MatchCandidate(subject_item_id, item_id, Lane.TRIGRAM, similarity),
            query=query,
            matched_identifier=identifier,
            similarity=similarity,
            threshold=threshold,
        )
        for item_id, (identifier, similarity) in sorted(
            best.items(), key=lambda item: (-item[1][1], item[0].int, item[1][0])
        )
    )
    return TrigramLaneResult(
        candidates=tuple(evaluation.candidate for evaluation in evaluations),
        evaluations=evaluations,
    )
