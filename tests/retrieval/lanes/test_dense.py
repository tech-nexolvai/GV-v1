"""Safety and traceability examples for the pgvector lane in issue #176."""

from __future__ import annotations

import importlib.util
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from pgvector import Vector
from sqlalchemy import create_mock_engine

from app.db.base import Base
from app.models.drawing import DenseEmbedding
from retrieval.candidate import Lane, MatchCandidate
from retrieval.lanes.dense import (
    DENSE_SQL,
    DenseQuery,
    DenseRepositoryError,
    dense_match,
)
from vocabulary.dense_content import DenseContentKind


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


def query(text: str = "white quartz splash material") -> DenseQuery:
    return DenseQuery(text, (0.1, 0.2, 0.3), "bge-small-en-v1.5", "model-revision-7")


def row(
    item: int,
    *,
    package: UUID,
    kind: str = "material_description",
    distance: object = Decimal("0.2"),
    similarity: object = Decimal("0.8"),
) -> dict[str, object]:
    return {
        "drawing_item_id": UUID(int=item),
        "package_id": package,
        "content_kind": kind,
        "model_id": "bge-small-en-v1.5",
        "model_version": "model-revision-7",
        "cosine_distance": distance,
        "similarity": similarity,
    }


@pytest.mark.parametrize("code", ["X-223", "X-233", "CT009", "PL_05", "12345"])
def test_bare_and_near_miss_codes_cannot_reach_pgvector(code: str) -> None:
    """Input: bare coded identifier. Output: refusal. Why: embeddings blur near-miss codes."""

    with pytest.raises(ValueError, match="bare identifiers"):
        query(code)


def test_prose_returns_candidates_with_model_provenance() -> None:
    """Input: material prose. Output: candidate plus model identity. Why: model changes are visible."""

    package = UUID(int=20)
    result = dense_match(
        UUID(int=1),
        package,
        query(),
        limit=5,
        executor=FakeExecutor([row(2, package=package)]),
        clock_ns=Clock(1_000, 1_075),
    )

    assert result.candidates == (
        MatchCandidate(UUID(int=1), UUID(int=2), Lane.DENSE, Decimal("0.8")),
    )
    assert not hasattr(result.candidates[0], "approved_by")
    assert result.evaluations[0].model_id == "bge-small-en-v1.5"
    assert result.evaluations[0].model_version == "model-revision-7"
    assert result.query_latency_ns == 75


def test_query_is_package_model_dimension_and_content_scoped() -> None:
    """Input: explicit query boundary. Output: scoped SQL. Why: distance follows hard filters."""

    package = UUID(int=20)
    executor = FakeExecutor([])
    dense_match(
        UUID(int=1),
        package,
        query("sink surround material note"),
        limit=7,
        executor=executor,
        clock_ns=Clock(10, 11),
    )

    statement, parameters = executor.calls[0]
    assert statement == DENSE_SQL
    assert "document.package_id = :package_id" in statement
    assert "embedding.content_kind = ANY" in statement
    assert "embedding.model_id = :model_id" in statement
    assert "embedding.model_version = :model_version" in statement
    assert "embedding.dimensions = :dimensions" in statement
    assert "CAST(embedding.embedding <=>" in statement
    assert parameters["package_id"] == package
    serialized = parameters["query_embedding"]
    assert isinstance(serialized, str)
    assert Vector.from_text(serialized).to_list() == pytest.approx([0.1, 0.2, 0.3])
    assert parameters["dimensions"] == 3
    assert parameters["limit"] == 7
    assert parameters["content_kinds"] == tuple(kind.value for kind in DenseContentKind)


def test_equal_distances_use_uuid_as_a_deterministic_tie_breaker() -> None:
    """Input: reverse-ordered equal distances. Output: UUID order. Why: reruns reproduce."""

    package = UUID(int=20)
    result = dense_match(
        UUID(int=1),
        package,
        query(),
        limit=5,
        executor=FakeExecutor([row(4, package=package), row(2, package=package)]),
        clock_ns=Clock(1, 2),
    )

    assert [candidate.right_item_id for candidate in result.candidates] == [
        UUID(int=2),
        UUID(int=4),
    ]


def test_model_mismatch_and_cross_package_rows_are_refused() -> None:
    """Input: repository scope violations. Output: loud errors. Why: SQL is defended twice."""

    package = UUID(int=20)
    wrong_model = row(2, package=package)
    wrong_model["model_version"] = "different"
    with pytest.raises(DenseRepositoryError, match="different model"):
        dense_match(
            UUID(int=1),
            package,
            query(),
            limit=1,
            executor=FakeExecutor([wrong_model]),
            clock_ns=Clock(1, 2),
        )

    with pytest.raises(DenseRepositoryError, match="outside"):
        dense_match(
            UUID(int=1),
            package,
            query(),
            limit=1,
            executor=FakeExecutor([row(2, package=UUID(int=21))]),
            clock_ns=Clock(1, 2),
        )


@pytest.mark.parametrize("unsafe", [0.2, Decimal("NaN"), Decimal("Infinity"), Decimal(-1)])
def test_inexact_or_unsafe_distance_is_refused(unsafe: object) -> None:
    """Input: float/non-finite/negative DB distance. Output: refusal. Why: scores stay auditable."""

    package = UUID(int=20)
    with pytest.raises(DenseRepositoryError, match="cosine_distance"):
        dense_match(
            UUID(int=1),
            package,
            query(),
            limit=1,
            executor=FakeExecutor([row(2, package=package, distance=unsafe)]),
            clock_ns=Clock(1, 2),
        )


def test_content_vocabulary_has_no_identifier_member() -> None:
    """Input: reachable dense content surface. Output: prose only. Why: no caller can select codes."""

    assert set(DenseContentKind) == {
        DenseContentKind.NOTE,
        DenseContentKind.MATERIAL_DESCRIPTION,
        DenseContentKind.VIEW_TITLE,
    }
    assert all("identifier" not in kind.value for kind in DenseContentKind)


def test_model_and_migration_define_the_vector_boundary() -> None:
    """Input: schema contracts. Output: vector table and extension. Why: pgvector is real, not mock."""

    table = Base.metadata.tables["dense_embeddings"]
    assert set(table.columns.keys()) == {
        "id",
        "created_at",
        "drawing_item_id",
        "content_kind",
        "source_text_hash",
        "model_id",
        "model_version",
        "dimensions",
        "embedding",
    }
    assert "VECTOR" in str(table.c.embedding.type).upper()

    path = Path("alembic/versions/0021_dense_embeddings.py")
    spec = importlib.util.spec_from_file_location("migration_0021", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.down_revision == "0020_pg_trgm"
    assert module.CONTENT_KINDS == "'note', 'material_description', 'view_title'"


def test_metadata_bootstrap_enables_vector_before_creating_its_table() -> None:
    """Input: ORM schema bootstrap. Output: extension first. Why: create_all must work in CI."""

    statements: list[str] = []

    def record(statement: object, *multiparams: object, **params: object) -> None:
        del multiparams, params
        statements.append(str(statement.compile(dialect=engine.dialect)))  # type: ignore[attr-defined]

    engine = create_mock_engine("postgresql+psycopg://", record)
    DenseEmbedding.__table__.create(engine)

    extension_position = next(
        index for index, statement in enumerate(statements) if "CREATE EXTENSION" in statement
    )
    table_position = next(
        index
        for index, statement in enumerate(statements)
        if "CREATE TABLE dense_embeddings" in statement
    )
    assert extension_position < table_position
