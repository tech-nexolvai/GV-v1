"""Scoped PostgreSQL trigram lane examples for issue #174."""

from __future__ import annotations

import importlib.util
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import create_mock_engine

from app.models.drawing import ItemIdentifier
from retrieval.candidate import Lane, MatchCandidate
from retrieval.lanes.trigram import (
    TRIGRAM_SQL,
    TrigramRepositoryError,
    trigram_match,
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


@pytest.mark.parametrize(
    ("query", "ocr_variant"),
    [("PL-05", "PL-O5"), ("CAB-11", "CAB-l1"), ("S-501", "S-S01")],
)
def test_real_ocr_confusions_are_returned_as_candidates_only(query: str, ocr_variant: str) -> None:
    """Input: 0/O, 1/l or 5/S confusion. Output: candidate. Why: fuzzy means propose only."""

    executor = FakeExecutor(
        [
            {
                "drawing_item_id": UUID(int=2),
                "value_as_printed": ocr_variant,
                "similarity": Decimal("0.75"),
            }
        ]
    )
    result = trigram_match(
        UUID(int=1), query, [UUID(int=2)], threshold=Decimal("0.6"), executor=executor
    )

    assert result.candidates == (
        MatchCandidate(UUID(int=1), UUID(int=2), Lane.TRIGRAM, Decimal("0.75")),
    )
    assert not hasattr(result.candidates[0], "approved_by")
    assert result.evaluations[0].matched_identifier == ocr_variant
    assert result.evaluations[0].threshold == Decimal("0.6")


def test_query_is_scoped_and_casts_postgres_real_to_numeric() -> None:
    """Input: eligible IDs and threshold. Output: exact SQL call. Why: no cross-package float path."""

    executor = FakeExecutor([])
    trigram_match(
        UUID(int=1),
        "PL-05",
        [UUID(int=3), UUID(int=2)],
        threshold=Decimal("0.6"),
        executor=executor,
    )

    statement, parameters = executor.calls[0]
    assert statement == TRIGRAM_SQL
    assert "ANY(CAST(:eligible_item_ids AS uuid[]))" in statement
    assert "CAST(similarity" in statement
    assert "AS numeric" in statement
    assert parameters == {
        "query": "PL-05",
        "eligible_item_ids": (UUID(int=2), UUID(int=3)),
        "threshold": Decimal("0.6"),
    }


def test_candidates_are_ranked_by_score_then_uuid() -> None:
    """Input: unordered hits with a tie. Output: score then UUID. Why: reruns reproduce."""

    executor = FakeExecutor(
        [
            {
                "drawing_item_id": UUID(int=4),
                "value_as_printed": "X-O4",
                "similarity": Decimal("0.7"),
            },
            {
                "drawing_item_id": UUID(int=2),
                "value_as_printed": "X-O2",
                "similarity": Decimal("0.9"),
            },
            {
                "drawing_item_id": UUID(int=3),
                "value_as_printed": "X-O3",
                "similarity": Decimal("0.7"),
            },
        ]
    )

    result = trigram_match(
        UUID(int=1),
        "X-02",
        [UUID(int=2), UUID(int=3), UUID(int=4)],
        threshold=Decimal("0.5"),
        executor=executor,
    )

    assert [candidate.right_item_id for candidate in result.candidates] == [
        UUID(int=2),
        UUID(int=3),
        UUID(int=4),
    ]


def test_best_identifier_is_kept_when_one_item_has_multiple_printed_codes() -> None:
    """Input: two identifiers on one item. Output: one best candidate. Why: pair uniqueness holds."""

    executor = FakeExecutor(
        [
            {
                "drawing_item_id": UUID(int=2),
                "value_as_printed": "PL-15",
                "similarity": Decimal("0.7"),
            },
            {
                "drawing_item_id": UUID(int=2),
                "value_as_printed": "PL-O5",
                "similarity": Decimal("0.8"),
            },
        ]
    )

    result = trigram_match(
        UUID(int=1), "PL-05", [UUID(int=2)], threshold=Decimal("0.5"), executor=executor
    )

    assert len(result.candidates) == 1
    assert result.evaluations[0].matched_identifier == "PL-O5"


@pytest.mark.parametrize("threshold", [0.6, Decimal("NaN"), Decimal("Infinity"), Decimal("-0.1")])
def test_inexact_or_unsafe_threshold_is_refused(threshold: object) -> None:
    """Input: float/non-finite/out-of-range threshold. Output: refusal. Why: no hidden policy."""

    expected = TypeError if isinstance(threshold, float) else ValueError
    with pytest.raises(expected):
        trigram_match(
            UUID(int=1),
            "PL-05",
            [UUID(int=2)],
            threshold=threshold,  # type: ignore[arg-type]
            executor=FakeExecutor([]),
        )


def test_repository_cannot_return_an_out_of_scope_or_below_threshold_hit() -> None:
    """Input: unsafe database rows. Output: loud refusal. Why: query scope is defended twice."""

    outside = FakeExecutor(
        [
            {
                "drawing_item_id": UUID(int=3),
                "value_as_printed": "PL-O5",
                "similarity": Decimal("0.8"),
            }
        ]
    )
    with pytest.raises(TrigramRepositoryError, match="outside"):
        trigram_match(
            UUID(int=1), "PL-05", [UUID(int=2)], threshold=Decimal("0.6"), executor=outside
        )

    below = FakeExecutor(
        [
            {
                "drawing_item_id": UUID(int=2),
                "value_as_printed": "PL-15",
                "similarity": Decimal("0.5"),
            }
        ]
    )
    with pytest.raises(TrigramRepositoryError, match="below"):
        trigram_match(UUID(int=1), "PL-05", [UUID(int=2)], threshold=Decimal("0.6"), executor=below)


def test_empty_eligible_scope_performs_no_database_query() -> None:
    """Input: no eligible items. Output: empty result. Why: an empty package is not a global search."""

    executor = FakeExecutor([])
    result = trigram_match(UUID(int=1), "PL-05", [], threshold=Decimal("0.6"), executor=executor)

    assert result.candidates == ()
    assert executor.calls == []


def test_migration_enables_pg_trgm_and_its_identifier_index() -> None:
    """Input: migration. Output: extension and GIN index. Why: SQL must be executable efficiently."""

    path = Path("alembic/versions/0020_pg_trgm.py")
    spec = importlib.util.spec_from_file_location("migration_0020", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    calls: list[str] = []
    original = module.op.execute
    module.op.execute = calls.append
    try:
        module.upgrade()
    finally:
        module.op.execute = original

    assert calls[0] == "CREATE EXTENSION IF NOT EXISTS pg_trgm"
    assert "gin_trgm_ops" in calls[1]
    assert module.down_revision == "0019_model_context"


def test_metadata_bootstrap_enables_pg_trgm_before_creating_its_index() -> None:
    """Input: ORM schema bootstrap. Output: extension first. Why: create_all must work in CI."""

    statements: list[str] = []

    def record(statement: object, *multiparams: object, **params: object) -> None:
        del multiparams, params
        statements.append(str(statement.compile(dialect=engine.dialect)))  # type: ignore[attr-defined]

    engine = create_mock_engine("postgresql+psycopg://", record)
    ItemIdentifier.__table__.create(engine)

    extension_position = next(
        index for index, statement in enumerate(statements) if "CREATE EXTENSION" in statement
    )
    trigram_index_position = next(
        index for index, statement in enumerate(statements) if "gin_trgm_ops" in statement
    )
    assert extension_position < trigram_index_position
