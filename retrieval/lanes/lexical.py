"""Search one package for rare codes and drawing terms without approving a match.

PostgreSQL performs tokenisation and matching. Ranking adds inverse document frequency computed from
the same package-scoped corpus, so a rare exact code contributes more than a common drawing word.
The selected alias-table version is supplied by the caller after project configuration resolution;
this module never reaches into ``rules/`` or guesses which vocabulary applies.

Source: ``docs/DESIGN_EXTRACTION.md`` section 8 and issue #175.
Verification: ``tests/retrieval/lanes/test_lexical.py``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from time import perf_counter_ns
from typing import Protocol
from uuid import UUID

from retrieval.candidate import Lane, MatchCandidate

LEXICAL_SQL = """
WITH corpus AS (
    SELECT
        di.id AS drawing_item_id,
        d.package_id,
        concat_ws(
            ' ',
            di.item_type,
            string_agg(DISTINCT ii.value_as_printed, ' '),
            string_agg(DISTINCT a.spelling, ' '),
            string_agg(DISTINCT a.canonical_term, ' ')
        ) AS search_text
    FROM drawing_items AS di
    JOIN drawing_views AS dv ON dv.id = di.drawing_view_id
    JOIN pages AS p ON p.id = dv.page_id
    JOIN document_versions AS document_version ON document_version.id = p.document_version_id
    JOIN documents AS d ON d.id = document_version.document_id
    LEFT JOIN item_identifiers AS ii ON ii.drawing_item_id = di.id
    LEFT JOIN aliases AS a
      ON a.canonical_term = di.item_type
     AND a.rulebook_version = :vocabulary_version
    WHERE d.package_id = :package_id
      AND di.id <> :subject_item_id
    GROUP BY di.id, d.package_id
),
query AS (
    SELECT websearch_to_tsquery('simple', CAST(:query AS text)) AS value
),
query_terms AS (
    SELECT DISTINCT term
    FROM regexp_split_to_table(lower(CAST(:query AS text)), '[^[:alnum:]_]+') AS term
    WHERE term <> ''
),
matched AS (
    SELECT
        corpus.*,
        to_tsvector('simple', corpus.search_text) AS document_vector
    FROM corpus, query
    WHERE to_tsvector('simple', corpus.search_text) @@ query.value
),
document_frequency AS (
    SELECT query_terms.term, count(*) AS document_count
    FROM query_terms
    JOIN corpus
      ON to_tsvector('simple', corpus.search_text)
         @@ plainto_tsquery('simple', query_terms.term)
    GROUP BY query_terms.term
),
ranked AS (
    SELECT
        matched.drawing_item_id,
        matched.package_id,
        matched.search_text,
        CAST(
            sum(
                1 + ln(
                    ((SELECT count(*) FROM corpus) + 1)::numeric
                    / (document_frequency.document_count + 1)::numeric
                )
            ) + CAST(ts_rank_cd(matched.document_vector, query.value, 32) AS numeric)
            AS numeric
        ) AS lexical_score
    FROM matched
    CROSS JOIN query
    JOIN query_terms
      ON matched.document_vector @@ plainto_tsquery('simple', query_terms.term)
    JOIN document_frequency ON document_frequency.term = query_terms.term
    GROUP BY matched.drawing_item_id, matched.package_id, matched.search_text,
             matched.document_vector, query.value
),
totals AS (
    SELECT count(*) AS corpus_size FROM corpus
)
SELECT
    ranked.drawing_item_id,
    ranked.package_id,
    ranked.search_text,
    ranked.lexical_score,
    totals.corpus_size
FROM totals
LEFT JOIN ranked ON TRUE
ORDER BY ranked.lexical_score DESC NULLS LAST, ranked.drawing_item_id
""".strip()


class LexicalExecutor(Protocol):
    """Database seam implemented by the session-owning matching layer."""

    def fetch_all(
        self, statement: str, parameters: Mapping[str, object]
    ) -> Sequence[Mapping[str, object]]:
        """Execute one scoped read and return mapping-shaped rows."""


@dataclass(frozen=True, slots=True)
class LexicalEvaluation:
    """One advisory candidate and the exact text and score returned for it."""

    candidate: MatchCandidate
    matched_text: str


@dataclass(frozen=True, slots=True)
class LexicalMetrics:
    """Measurements used to decide whether PostgreSQL should later yield to OpenSearch."""

    corpus_size: int
    query_latency_ns: int


@dataclass(frozen=True, slots=True)
class LexicalLaneResult:
    """Ranked candidates plus replayable search diagnostics."""

    candidates: tuple[MatchCandidate, ...]
    evaluations: tuple[LexicalEvaluation, ...]
    metrics: LexicalMetrics


class LexicalRepositoryError(ValueError):
    """The database returned a row outside the requested lexical-search contract."""


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _score(value: object) -> Decimal:
    if not isinstance(value, Decimal):
        raise LexicalRepositoryError("lexical_score must be Decimal; float is not allowed")
    if not value.is_finite() or value < 0:
        raise LexicalRepositoryError("lexical_score must be finite and non-negative")
    return value


def lexical_match(
    subject_item_id: UUID,
    package_id: UUID,
    query: str,
    *,
    vocabulary_version: str,
    executor: LexicalExecutor,
    clock_ns: Callable[[], int] = perf_counter_ns,
) -> LexicalLaneResult:
    """Return package-scoped lexical candidates with measured corpus size and latency."""

    if not isinstance(subject_item_id, UUID):
        raise TypeError("subject_item_id must be a UUID")
    if not isinstance(package_id, UUID):
        raise TypeError("package_id must be a UUID")
    query = _text(query, "query")
    vocabulary_version = _text(vocabulary_version, "vocabulary_version")
    started = clock_ns()
    rows = executor.fetch_all(
        LEXICAL_SQL,
        {
            "subject_item_id": subject_item_id,
            "package_id": package_id,
            "query": query,
            "vocabulary_version": vocabulary_version,
        },
    )
    finished = clock_ns()
    if not isinstance(started, int) or not isinstance(finished, int) or finished < started:
        raise ValueError("clock_ns must return monotonically increasing integers")
    if not rows:
        raise LexicalRepositoryError("lexical query must return its corpus-size row")

    corpus_sizes: set[int] = set()
    evaluations: list[LexicalEvaluation] = []
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise LexicalRepositoryError("database rows must be mappings")
        corpus_size = raw.get("corpus_size")
        if not isinstance(corpus_size, int) or isinstance(corpus_size, bool) or corpus_size < 0:
            raise LexicalRepositoryError("corpus_size must be a non-negative integer")
        corpus_sizes.add(corpus_size)
        item_id = raw.get("drawing_item_id")
        score = raw.get("lexical_score")
        matched_text = raw.get("search_text")
        row_package_id = raw.get("package_id")
        if item_id is None and score is None and matched_text is None and row_package_id is None:
            continue
        if not isinstance(item_id, UUID):
            raise LexicalRepositoryError("drawing_item_id must be a UUID")
        if item_id == subject_item_id:
            raise LexicalRepositoryError("database returned the subject as its own match")
        if row_package_id != package_id:
            raise LexicalRepositoryError("database returned an item outside the requested package")
        try:
            matched_text = _text(matched_text, "search_text")
        except ValueError as error:
            raise LexicalRepositoryError(str(error)) from error
        candidate = MatchCandidate(subject_item_id, item_id, Lane.LEXICAL, _score(score))
        evaluations.append(LexicalEvaluation(candidate, matched_text))
    if len(corpus_sizes) != 1:
        raise LexicalRepositoryError("database returned inconsistent corpus sizes")
    evaluations.sort(
        key=lambda evaluation: (
            -evaluation.candidate.score if evaluation.candidate.score is not None else Decimal(0),
            evaluation.candidate.right_item_id.int,
        )
    )
    return LexicalLaneResult(
        candidates=tuple(evaluation.candidate for evaluation in evaluations),
        evaluations=tuple(evaluations),
        metrics=LexicalMetrics(corpus_sizes.pop(), finished - started),
    )
