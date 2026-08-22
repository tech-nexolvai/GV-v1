"""Candidate-only semantic retrieval over prose-like drawing content.

Bare identifiers are rejected before the database is called. PostgreSQL computes exact nearest
neighbours only after package, content-kind, model and dimension filters; no approximate-index tuning
or empirical similarity threshold is invented here.

Source: ``docs/DESIGN_EXTRACTION.md`` section 8 and issue #176.
Verification: ``tests/retrieval/lanes/test_dense.py``.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from time import perf_counter_ns
from typing import Protocol
from uuid import UUID

from pgvector import Vector

from retrieval.candidate import Lane, MatchCandidate
from vocabulary.dense_content import DenseContentKind

_BARE_IDENTIFIER = re.compile(r"(?=.*\d)[A-Za-z0-9][A-Za-z0-9._/-]*\Z")

DENSE_SQL = """
SELECT
    embedding.drawing_item_id,
    document.package_id,
    embedding.content_kind,
    embedding.model_id,
    embedding.model_version,
    CAST(embedding.embedding <=> CAST(:query_embedding AS vector) AS numeric) AS cosine_distance,
    CAST(1 - (embedding.embedding <=> CAST(:query_embedding AS vector)) AS numeric) AS similarity
FROM dense_embeddings AS embedding
JOIN drawing_items AS item ON item.id = embedding.drawing_item_id
JOIN drawing_views AS view ON view.id = item.drawing_view_id
JOIN pages AS page ON page.id = view.page_id
JOIN document_versions AS version ON version.id = page.document_version_id
JOIN documents AS document ON document.id = version.document_id
WHERE document.package_id = :package_id
  AND embedding.drawing_item_id <> :subject_item_id
  AND embedding.content_kind = ANY(CAST(:content_kinds AS text[]))
  AND embedding.model_id = :model_id
  AND embedding.model_version = :model_version
  AND embedding.dimensions = :dimensions
ORDER BY cosine_distance, embedding.drawing_item_id
LIMIT :limit
""".strip()


class DenseExecutor(Protocol):
    """Database seam implemented by the session-owning matching layer."""

    def fetch_all(
        self, statement: str, parameters: Mapping[str, object]
    ) -> Sequence[Mapping[str, object]]:
        """Execute one package-scoped vector read."""


@dataclass(frozen=True, slots=True)
class DenseQuery:
    """One model-versioned semantic query; identifiers cannot inhabit this boundary."""

    text: str
    embedding: tuple[float, ...]
    model_id: str
    model_version: str

    def __post_init__(self) -> None:
        for name in ("text", "model_id", "model_version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if _BARE_IDENTIFIER.fullmatch(self.text.strip()):
            raise ValueError("bare identifiers are not eligible for dense retrieval")
        if not isinstance(self.embedding, tuple) or not self.embedding:
            raise ValueError("embedding must be a non-empty tuple")
        if not all(isinstance(value, float) and math.isfinite(value) for value in self.embedding):
            raise TypeError("embedding must contain only finite float values")


@dataclass(frozen=True, slots=True)
class DenseEvaluation:
    """One candidate with the model and semantic content that produced it."""

    candidate: MatchCandidate
    content_kind: DenseContentKind
    model_id: str
    model_version: str
    cosine_distance: Decimal


@dataclass(frozen=True, slots=True)
class DenseLaneResult:
    """Ranked dense candidates plus the measured database latency."""

    candidates: tuple[MatchCandidate, ...]
    evaluations: tuple[DenseEvaluation, ...]
    query_latency_ns: int


class DenseRepositoryError(ValueError):
    """The vector query returned a row outside the requested contract."""


def _decimal(value: object, name: str, *, minimum: Decimal, maximum: Decimal) -> Decimal:
    if not isinstance(value, Decimal):
        raise DenseRepositoryError(f"{name} must be Decimal; float is not allowed")
    if not value.is_finite() or not minimum <= value <= maximum:
        raise DenseRepositoryError(f"{name} must be finite and between {minimum} and {maximum}")
    return value


def dense_match(
    subject_item_id: UUID,
    package_id: UUID,
    query: DenseQuery,
    *,
    limit: int,
    executor: DenseExecutor,
    clock_ns: Callable[[], int] = perf_counter_ns,
) -> DenseLaneResult:
    """Return exact pgvector neighbours for eligible prose, without approving any candidate."""

    if not isinstance(subject_item_id, UUID):
        raise TypeError("subject_item_id must be a UUID")
    if not isinstance(package_id, UUID):
        raise TypeError("package_id must be a UUID")
    if not isinstance(query, DenseQuery):
        raise TypeError("query must be a DenseQuery")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise ValueError("limit must be a positive integer")
    started = clock_ns()
    rows = executor.fetch_all(
        DENSE_SQL,
        {
            "subject_item_id": subject_item_id,
            "package_id": package_id,
            # Text is the driver's stable, explicit vector input format. Relying on tuple adaptation
            # would produce PostgreSQL record syntax unless every connection had registered pgvector.
            "query_embedding": Vector(list(query.embedding)).to_text(),
            "content_kinds": tuple(kind.value for kind in DenseContentKind),
            "model_id": query.model_id,
            "model_version": query.model_version,
            "dimensions": len(query.embedding),
            "limit": limit,
        },
    )
    finished = clock_ns()
    if not isinstance(started, int) or not isinstance(finished, int) or finished < started:
        raise ValueError("clock_ns must return monotonically increasing integers")
    evaluations: list[DenseEvaluation] = []
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise DenseRepositoryError("database rows must be mappings")
        item_id = raw.get("drawing_item_id")
        if not isinstance(item_id, UUID):
            raise DenseRepositoryError("drawing_item_id must be a UUID")
        if item_id == subject_item_id:
            raise DenseRepositoryError("database returned the subject as its own match")
        if raw.get("package_id") != package_id:
            raise DenseRepositoryError("database returned an item outside the requested package")
        raw_content_kind = raw.get("content_kind")
        try:
            if not isinstance(raw_content_kind, str):
                raise TypeError
            content_kind = DenseContentKind(raw_content_kind)
        except (TypeError, ValueError) as error:
            raise DenseRepositoryError("database returned an ineligible content kind") from error
        if raw.get("model_id") != query.model_id or raw.get("model_version") != query.model_version:
            raise DenseRepositoryError("database returned an embedding from a different model")
        distance = _decimal(
            raw.get("cosine_distance"), "cosine_distance", minimum=Decimal(0), maximum=Decimal(2)
        )
        similarity = _decimal(
            raw.get("similarity"), "similarity", minimum=Decimal(-1), maximum=Decimal(1)
        )
        candidate = MatchCandidate(subject_item_id, item_id, Lane.DENSE, similarity)
        evaluations.append(
            DenseEvaluation(candidate, content_kind, query.model_id, query.model_version, distance)
        )
    evaluations.sort(
        key=lambda evaluation: (
            evaluation.cosine_distance,
            evaluation.candidate.right_item_id.int,
        )
    )
    return DenseLaneResult(
        tuple(evaluation.candidate for evaluation in evaluations),
        tuple(evaluations),
        finished - started,
    )
