"""Evaluation history tables (#201).

Most of these assert against `Base.metadata` rather than a live database, deliberately: they are
statements about the *schema*, they must run everywhere, and a test that silently skips without
`DATABASE_URL` is a test that protects nothing on most people's machines.

Two properties matter more than the rest.

**No float column anywhere.** A release decision is made from `metric_results.value`, and
`eval/metrics.py` computes those as exact rationals precisely so a rounded number never gets behind
that decision.

**`value` is nullable.** NOT MEASURED and zero are different facts — a critical false-PASS rate of
`0` over zero cases renders as a perfect score and means nobody measured anything. If the column
were `NOT NULL DEFAULT 0` the distinction would be lost at the storage layer, whatever the code
above it believed.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Integer, Numeric, String

from app.db.base import Base, Immutable
from app.models import EvaluationRun, GoldCase, GoldSet, MetricResult

EVALUATION_TABLES = ("gold_sets", "gold_cases", "evaluation_runs", "metric_results")


# ---------------------------------------------------------------------------
# Registration — the silent failure this guards
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table", EVALUATION_TABLES)
def test_every_table_is_registered_in_the_metadata(table: str) -> None:
    """A model registers only when its module is imported. Without `app/models/__init__.py`
    importing them, Alembic autogenerates against an empty schema and a migrations round-trip
    check passes while comparing two empty sets — green, and checking nothing."""
    assert table in Base.metadata.tables


def test_importing_the_models_package_is_enough() -> None:
    """`alembic/env.py` imports `app.models` and nothing else. If that stopped registering the
    tables, migrations would silently stop being generated for them."""
    import app.models

    assert {*EVALUATION_TABLES} <= set(Base.metadata.tables)
    assert app.models.GoldSet is GoldSet


# ---------------------------------------------------------------------------
# Exactness
# ---------------------------------------------------------------------------


def test_no_evaluation_table_has_a_float_column() -> None:
    """ADR-0001 in the storage layer. Asserted over every column of every table rather than the
    two obvious ones, so a later migration cannot add one quietly."""
    offending: list[str] = []
    for name in EVALUATION_TABLES:
        for column in Base.metadata.tables[name].columns:
            kind = str(column.type).upper()
            if "FLOAT" in kind or "DOUBLE" in kind or "REAL" in kind:
                offending.append(f"{name}.{column.name} ({kind})")
    assert not offending, f"float columns in the evaluation schema: {offending}"


def test_metric_values_are_numeric() -> None:
    columns = Base.metadata.tables["metric_results"].columns
    assert isinstance(columns["value"].type, Numeric)
    assert isinstance(columns["gate_threshold"].type, Numeric)


def test_a_metric_value_may_be_null_because_unmeasured_is_not_zero() -> None:
    """The most consequential column property in the schema.

    `NOT NULL DEFAULT 0` would make an unmeasured gate indistinguishable from a perfect one at the
    storage layer, whatever the code above it believed.
    """
    assert Base.metadata.tables["metric_results"].columns["value"].nullable
    assert Base.metadata.tables["metric_results"].columns["passed"].nullable


def test_counts_are_integers_not_decimals() -> None:
    columns = Base.metadata.tables["metric_results"].columns
    assert isinstance(columns["numerator"].type, Integer)
    assert isinstance(columns["denominator"].type, Integer)


# ---------------------------------------------------------------------------
# Provenance — a run must be attributable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "column",
    ["gold_set_id", "gold_set_version", "code_version", "rule_snapshot_ids", "extractor_versions"],
)
def test_a_run_records_every_version_that_could_explain_a_difference(column: str) -> None:
    """Missing any one of these turns "the number moved" into an unanswerable question."""
    columns = Base.metadata.tables["evaluation_runs"].columns
    assert column in columns
    assert not columns[column].nullable, f"{column} must not be optional — it is the attribution"


def test_the_gold_set_version_is_denormalised_onto_the_run() -> None:
    """A run must stay interpretable if the gold set is later renamed or re-versioned. The row is
    the record, not a pointer to a mutable one."""
    assert isinstance(
        Base.metadata.tables["evaluation_runs"].columns["gold_set_version"].type, String
    )


def test_a_gold_case_binds_to_both_a_version_and_a_content_hash() -> None:
    """The version id says which document; the hash proves the bytes have not changed under the
    annotation. An annotation silently applied to different bytes produces a confidently wrong
    metric — the one failure a gold set exists to prevent."""
    columns = Base.metadata.tables["gold_cases"].columns
    assert not columns["document_version_id"].nullable
    assert not columns["content_hash"].nullable


def test_the_document_version_foreign_key_is_deliberately_absent_for_now() -> None:
    """`document_versions` arrives with C1.3 (#193), still open.

    Recorded as a test rather than only a comment so the gap is visible: when #193 lands, this
    fails and whoever sees it adds the constraint in a new migration. An unconstrained UUID
    silently permits a gold case pointing at a document version that was never stored.
    """
    foreign_keys = {fk.column.table.name for fk in Base.metadata.tables["gold_cases"].foreign_keys}
    assert "gold_sets" in foreign_keys
    assert "document_versions" not in foreign_keys, (
        "document_versions now exists — add the foreign key on gold_cases.document_version_id in a "
        "NEW migration and update this test."
    )


# ---------------------------------------------------------------------------
# Immutability and keys
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model", [GoldCase, EvaluationRun, MetricResult])
def test_history_tables_are_marked_immutable(model: type) -> None:
    """C1.12 revokes UPDATE and DELETE on everything carrying the marker. A run that could be
    edited would make every trend built from it meaningless."""
    assert issubclass(model, Immutable)


def test_a_gold_set_is_not_immutable_because_cases_are_added_to_it() -> None:
    """The set grows as more drawings are annotated. What must not change is a *published* version,
    which is why the run records the version rather than trusting the set."""
    assert not issubclass(GoldSet, Immutable)


def test_one_metric_per_check_type_per_run() -> None:
    """Without this a run could carry two contradictory values for the same metric and nothing
    would say which was used for the gate."""
    constraints = {
        tuple(sorted(c.columns.keys()))
        for c in Base.metadata.tables["metric_results"].constraints
        if c.__class__.__name__ == "UniqueConstraint"
    }
    assert ("check_type", "evaluation_run_id", "metric") in constraints


def test_one_gold_case_per_document_version_in_a_set() -> None:
    constraints = {
        tuple(sorted(c.columns.keys()))
        for c in Base.metadata.tables["gold_cases"].constraints
        if c.__class__.__name__ == "UniqueConstraint"
    }
    assert ("document_version_id", "gold_set_id") in constraints


def test_metric_results_are_indexed_for_comparison_across_runs() -> None:
    """F4.2 compares a metric over time, not one run at a time."""
    indexes = {i.name for i in Base.metadata.tables["metric_results"].indexes}
    assert "ix_metric_results_metric_check_type" in indexes
