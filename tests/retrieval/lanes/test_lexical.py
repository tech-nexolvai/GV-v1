"""Examples for the package-scoped lexical retrieval lane in issue #175."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from uuid import UUID

import pytest

from retrieval.candidate import Lane, MatchCandidate
from retrieval.lanes.lexical import (
    LEXICAL_SQL,
    LexicalRepositoryError,
    lexical_match,
)


class FakeExecutor:
    def __init__(self, rows: Sequence[Mapping[str, object]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, Mapping[str, object]]] = []

    def fetch_all(
        self, statement: str, parameters: Mapping[str, object]
    ) -> Sequence[Mapping[str, object]]:
        self.calls.append((statement, parameters))
        return self.rows


class Clock:
    def __init__(self, *values: int) -> None:
        self.values = iter(values)

    def __call__(self) -> int:
        return next(self.values)


def row(item: int, score: str, *, package: UUID, text: str, corpus: int = 3) -> dict[str, object]:
    return {
        "drawing_item_id": UUID(int=item),
        "package_id": package,
        "search_text": text,
        "lexical_score": Decimal(score),
        "corpus_size": corpus,
    }


def test_rare_exact_code_ranks_above_common_drawing_term() -> None:
    """Input: rare CT009 and common cabinet. Output: CT009 first. Why: rarity identifies items."""

    package = UUID(int=20)
    executor = FakeExecutor(
        [
            row(3, "0.2", package=package, text="cabinet width"),
            row(2, "0.9", package=package, text="CT009 back offset"),
        ]
    )
    result = lexical_match(
        UUID(int=1),
        package,
        '"CT009" OR cabinet',
        vocabulary_version="project-v4",
        executor=executor,
        clock_ns=Clock(1_000, 1_075),
    )

    assert result.candidates == (
        MatchCandidate(UUID(int=1), UUID(int=2), Lane.LEXICAL, Decimal("0.9")),
        MatchCandidate(UUID(int=1), UUID(int=3), Lane.LEXICAL, Decimal("0.2")),
    )
    assert not hasattr(result.candidates[0], "approved_by")


def test_sql_scopes_the_corpus_and_project_vocabulary() -> None:
    """Input: package and vocabulary version. Output: scoped SQL. Why: no global corpus leaks in."""

    package = UUID(int=20)
    executor = FakeExecutor(
        [
            {
                "drawing_item_id": None,
                "package_id": None,
                "search_text": None,
                "lexical_score": None,
                "corpus_size": 0,
            }
        ]
    )
    lexical_match(
        UUID(int=1),
        package,
        "sink cutout",
        vocabulary_version="project-v4",
        executor=executor,
        clock_ns=Clock(10, 11),
    )

    statement, parameters = executor.calls[0]
    assert statement == LEXICAL_SQL
    assert "d.package_id = :package_id" in statement
    assert "a.rulebook_version = :vocabulary_version" in statement
    assert "websearch_to_tsquery('simple'" in statement
    assert "document_frequency" in statement
    assert "ln(" in statement
    assert "CAST(ts_rank_cd" in statement
    assert parameters["package_id"] == package
    assert parameters["vocabulary_version"] == "project-v4"


def test_corpus_size_and_measured_latency_are_recorded() -> None:
    """Input: three-document corpus and clock. Output: metrics. Why: OpenSearch needs evidence."""

    package = UUID(int=20)
    result = lexical_match(
        UUID(int=1),
        package,
        "CT009",
        vocabulary_version="project-v4",
        executor=FakeExecutor([row(2, "0.8", package=package, text="CT009", corpus=3)]),
        clock_ns=Clock(4_000, 4_125),
    )

    assert result.metrics.corpus_size == 3
    assert result.metrics.query_latency_ns == 125


def test_equal_scores_use_item_uuid_as_a_reproducible_tie_breaker() -> None:
    """Input: equal scores in reverse order. Output: UUID order. Why: reruns must agree."""

    package = UUID(int=20)
    result = lexical_match(
        UUID(int=1),
        package,
        "filler",
        vocabulary_version="project-v4",
        executor=FakeExecutor(
            [
                row(4, "0.5", package=package, text="filler right"),
                row(2, "0.5", package=package, text="filler left"),
            ]
        ),
        clock_ns=Clock(1, 2),
    )

    assert [candidate.right_item_id for candidate in result.candidates] == [
        UUID(int=2),
        UUID(int=4),
    ]


@pytest.mark.parametrize("unsafe_score", [0.8, Decimal("NaN"), Decimal("Infinity"), Decimal(-1)])
def test_inexact_or_unsafe_database_score_is_refused(unsafe_score: object) -> None:
    """Input: float/non-finite/negative score. Output: refusal. Why: unsafe ranking is not hidden."""

    package = UUID(int=20)
    unsafe = row(2, "0.8", package=package, text="CT009")
    unsafe["lexical_score"] = unsafe_score
    with pytest.raises(LexicalRepositoryError, match="lexical_score"):
        lexical_match(
            UUID(int=1),
            package,
            "CT009",
            vocabulary_version="project-v4",
            executor=FakeExecutor([unsafe]),
            clock_ns=Clock(1, 2),
        )


def test_database_cannot_return_a_candidate_from_another_package() -> None:
    """Input: out-of-package row. Output: refusal. Why: package scoping is defended twice."""

    with pytest.raises(LexicalRepositoryError, match="outside"):
        lexical_match(
            UUID(int=1),
            UUID(int=20),
            "CT009",
            vocabulary_version="project-v4",
            executor=FakeExecutor([row(2, "0.8", package=UUID(int=21), text="CT009", corpus=1)]),
            clock_ns=Clock(1, 2),
        )


def test_empty_match_set_still_records_the_corpus_and_latency() -> None:
    """Input: no hit in four items. Output: no candidates plus metrics. Why: absence is measured."""

    result = lexical_match(
        UUID(int=1),
        UUID(int=20),
        "unseen phrase",
        vocabulary_version="project-v4",
        executor=FakeExecutor(
            [
                {
                    "drawing_item_id": None,
                    "package_id": None,
                    "search_text": None,
                    "lexical_score": None,
                    "corpus_size": 4,
                }
            ]
        ),
        clock_ns=Clock(20, 29),
    )

    assert result.candidates == ()
    assert result.metrics.corpus_size == 4
    assert result.metrics.query_latency_ns == 9
