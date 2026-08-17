"""Evaluation history: gold sets, gold cases, runs and metric results.

A metric printed to a terminal cannot answer *"was this better or worse than last week, and what
changed?"*. That question is the whole reason the release gate is phrased as *"does not regress
critical false-PASS"* — a comparison needs something to compare against, and until these tables
exist there is nothing.

Two things this schema is built to make impossible.

**An annotation silently applied to different bytes.** A gold case binds to a document version *and*
its content hash. If the PDF changes, the annotation is invalid, and the metric computed from it
would be confidently wrong — the one failure a gold set exists to prevent.

**A run that cannot be attributed.** An evaluation records every version that could explain a
difference: code, rule snapshots, extractor versions, gold-set version. Missing any one of them
turns "the number moved" into an unanswerable question.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Immutable, TimestampedUUID, UTCDateTime


class GoldSet(Base, TimestampedUUID):
    """A named, versioned collection of reviewed cases.

    Mutable by design — cases are added as more drawings are annotated. What must not change is a
    *published* version, which is why `version` is part of the identity a run records.
    """

    __tablename__ = "gold_sets"

    name: Mapped[str] = mapped_column(String(200))
    version: Mapped[str] = mapped_column(String(32))
    notes: Mapped[str | None] = mapped_column(String(1000), default=None)

    __table_args__ = (UniqueConstraint("name", "version"),)


class GoldCase(Base, TimestampedUUID, Immutable):
    """One reviewed package and its answer key.

    Immutable. Editing an annotation after runs have scored against it would silently change what
    every historical result meant.
    """

    __tablename__ = "gold_cases"

    gold_set_id: Mapped[UUID] = mapped_column(ForeignKey("gold_sets.id"))

    document_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("document_versions.id", ondelete="RESTRICT")
    )

    content_hash: Mapped[str] = mapped_column(String(64))
    """SHA-256 of the bytes annotated.

    Stored alongside the version id rather than trusting it alone: the hash is what lets a loader
    prove the PDF has not changed under the annotation (B1.2).
    """

    annotations: Mapped[dict[str, object]] = mapped_column(JSONB)
    annotated_by: Mapped[str] = mapped_column(String(200))
    annotated_on: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    """Timezone-aware like every other timestamp here.

    A bare `datetime` maps to a naive column, which the round-trip test caught: an annotation date
    recorded in one timezone and read in another would silently shift, and the whole point of a
    gold case is that it is the fixed reference everything else is measured against.
    """

    __table_args__ = (
        UniqueConstraint("gold_set_id", "document_version_id"),
        Index("ix_gold_cases_gold_set_id", "gold_set_id"),
    )


class EvaluationRun(Base, TimestampedUUID, Immutable):
    """One scoring run, with everything that could explain its result.

    Immutable, because comparison over time is the entire purpose. A run that could be edited would
    make every trend built from it meaningless.
    """

    __tablename__ = "evaluation_runs"

    gold_set_id: Mapped[UUID] = mapped_column(ForeignKey("gold_sets.id"))
    gold_set_version: Mapped[str] = mapped_column(String(32))
    """Denormalised on purpose. A run must remain interpretable even if the gold set is later
    renamed or re-versioned — the row is the record, not a pointer to a mutable one."""

    code_version: Mapped[str] = mapped_column(String(64))
    rule_snapshot_ids: Mapped[list[str]] = mapped_column(JSONB)
    extractor_versions: Mapped[dict[str, str]] = mapped_column(JSONB)

    is_baseline: Mapped[bool] = mapped_column(default=False)
    """Whether this run is the comparison point for regression (F4.2, #263)."""

    __table_args__ = (Index("ix_evaluation_runs_created_at", "created_at"),)


class MetricResult(Base, TimestampedUUID, Immutable):
    """One metric, for one check type, from one run.

    Per check type because release gates are per check type (`AGENTS.md` §9). A single aggregate
    number would hide a check type failing inside an average that passes.
    """

    __tablename__ = "metric_results"

    evaluation_run_id: Mapped[UUID] = mapped_column(ForeignKey("evaluation_runs.id"))
    metric: Mapped[str] = mapped_column(String(64))
    check_type: Mapped[str] = mapped_column(String(32))

    # NUMERIC, never DOUBLE PRECISION. A float here would put a rounded number behind a release
    # decision, and `eval/metrics.py` computes these as exact rationals precisely to avoid that.
    value: Mapped[Decimal | None] = mapped_column(Numeric(18, 9), default=None)
    """`None` means NOT MEASURED, and is distinct from zero.

    A critical false-PASS rate of `0` over zero cases renders as a perfect score; it means nobody
    measured anything. The column is nullable so those two cannot be confused in storage either.
    """

    numerator: Mapped[int] = mapped_column(default=0)
    denominator: Mapped[int] = mapped_column(default=0)
    gate_threshold: Mapped[Decimal | None] = mapped_column(Numeric(18, 9), default=None)
    passed: Mapped[bool | None] = mapped_column(default=None)
    note: Mapped[str | None] = mapped_column(String(500), default=None)

    __table_args__ = (
        UniqueConstraint("evaluation_run_id", "metric", "check_type"),
        # F4.2 compares a metric across runs, not one run at a time.
        Index("ix_metric_results_metric_check_type", "metric", "check_type"),
    )


class CaseResult(Base, TimestampedUUID, Immutable):
    """How one gold case fared on one check, in one run.

    `metric_results` says a rate moved. This says which cases moved, which is the difference between
    a morning's debugging and an afternoon's: going from 1% to 4% on fifty cases is three cases, and
    the useful question is always *which* three.

    It also gives a second route to attribution. `eval/regression.py:attribute()` returns `None` when
    more than one version changed, which is the honest answer — but if the three newly-failing cases
    are all scanned pages, that points at the OCR lane whatever else moved.

    `Immutable`, like every other evaluation record. A result edited after the fact silently changes
    what a historical comparison meant.

    **No finding reference yet.** The scope for `#315` asks for the finding each result came from,
    and there is no findings table — that is `#199` (C1.9), which is deferred behind `#195`/`#198`.
    Rather than invent a column pointing at nothing, the outcome is stored now and the reference is
    added by a later migration once findings exist. The per-case comparison this story exists for
    needs the outcome; the finding link makes the debugging faster, not the regression visible.
    """

    __tablename__ = "case_results"

    evaluation_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("evaluation_runs.id", ondelete="RESTRICT"), index=True
    )
    gold_case_id: Mapped[UUID] = mapped_column(
        ForeignKey("gold_cases.id", ondelete="RESTRICT"), index=True
    )

    check: Mapped[str] = mapped_column(String(200), index=True)
    """Which check this result is for. A case carries several, and a regression is usually one of
    them moving rather than the whole case."""

    outcome: Mapped[str] = mapped_column(String(32))
    """What the system concluded — a `verdict.outcomes.Outcome` value.

    Stored as text rather than a database enum for the same reason `metric_results.metric` is: the
    vocabulary belongs to `verdict/`, and a migration every time it gains a member would put schema
    churn in the path of the deterministic core.
    """

    expected: Mapped[str] = mapped_column(String(32))
    """What the gold set said should happen. Stored beside the outcome rather than joined at read
    time, because the answer key is versioned and a comparison has to mean what it meant then."""

    __table_args__ = (
        UniqueConstraint(
            "evaluation_run_id", "gold_case_id", "check", name="uq_case_results_run_case_check"
        ),
        # `check` is a reserved SQL keyword, so it has to be quoted in raw constraint text.
        # SQLAlchemy quotes it for us in the DDL it generates; this string it passes through as
        # written, and unquoted it is a syntax error rather than a constraint.
        CheckConstraint("\"check\" <> ''", name="case_result_check_present"),
        CheckConstraint("outcome <> ''", name="case_result_outcome_present"),
        CheckConstraint("expected <> ''", name="case_result_expected_present"),
        # A comparison walks every case of one run, then the same for the baseline.
        Index("ix_case_results_run_check", "evaluation_run_id", "check"),
    )
