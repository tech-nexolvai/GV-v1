"""Persist an evaluation run, so a metric becomes a trend.

A metric printed to a terminal cannot answer *"was this better or worse than last week, and what
changed?"*. The release gate is phrased as *"does not regress critical false-PASS"* — a comparison —
and a comparison needs something to compare against.

**The precision decision, which is the one that matters here.**

`eval/metrics.py` computes metrics as exact `Fraction`s, deliberately: a release decision is made
from them. The `value` column is `NUMERIC(18, 9)`, and `1/3` does not fit in it — it stores as
`0.333333333`, which is a different number.

So `numerator` and `denominator` are the **authoritative** stored form, and `value` is a derived
convenience for ordering and for a human reading a row. `load_metric` reconstructs from the pair,
never from `value`.

Without that, two runs that computed the identical rate could compare unequal — one having stored
`1/3` and another `333333333/1000000000` — and a regression check would report a change that never
happened. A false regression is less dangerous than a missed one, but it is the failure that makes
people stop trusting the gate, which eventually produces the missed one.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from fractions import Fraction
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.evaluation import CaseResult, EvaluationRun, GoldSet, MetricResult
from eval.metrics import MetricResult as ComputedMetric
from verdict.outcomes import Outcome

#: Scale of the `value` column. Kept here so the rounding is visible at the point it happens rather
#: than only in a migration.
VALUE_SCALE = 9


class MissingProvenanceError(ValueError):
    """A run was recorded without something that could explain a later difference.

    Raised rather than defaulted. A run with an unknown code version is not a run with a blank
    field — it is a data point that cannot be attributed, and one of those in a series makes every
    comparison across it ambiguous.
    """


def _approximate(value: Fraction | None) -> Decimal | None:
    """The convenience form. Lossy for repeating fractions, and never read back as authoritative."""
    if value is None:
        return None
    return round(Decimal(value.numerator) / Decimal(value.denominator), VALUE_SCALE)


def record_run(
    session: Session,
    *,
    gold_set: GoldSet,
    code_version: str,
    rule_snapshot_ids: Sequence[str],
    extractor_versions: Mapping[str, str],
    results: Mapping[str, ComputedMetric],
    case_outcomes: Mapping[UUID, Mapping[str, tuple[Outcome, Outcome]]] | None = None,
    check_type: str = "all",
    is_baseline: bool = False,
) -> EvaluationRun:
    """Store one run, its metrics, and how each gold case fared.

    Every provenance field is required. `AGENTS.md` §9 makes the release gate a comparison, and a
    comparison against a run whose versions are unknown cannot attribute what moved.

    `case_outcomes` maps a gold case to `{check: (observed, expected)}`. Optional, because a caller
    scoring only aggregate metrics is a legitimate thing to do — but a run recorded without it can
    only ever be compared as rates, and `eval/regression.py` will say so rather than implying the
    cases were identical.
    """
    missing = [
        name
        for name, value in (
            ("code_version", code_version),
            ("gold_set_version", gold_set.version),
        )
        if not str(value).strip()
    ]
    if not rule_snapshot_ids:
        missing.append("rule_snapshot_ids")
    if missing:
        raise MissingProvenanceError(
            f"an evaluation run must record {', '.join(missing)}. Without it the run is a data "
            "point that cannot be attributed, and every comparison across it is ambiguous."
        )

    run = EvaluationRun(
        gold_set_id=gold_set.id,
        gold_set_version=gold_set.version,
        code_version=code_version,
        rule_snapshot_ids=list(rule_snapshot_ids),
        extractor_versions=dict(extractor_versions),
        is_baseline=is_baseline,
    )
    session.add(run)
    session.flush()  # assign run.id before the metric rows reference it

    for key, computed in results.items():
        session.add(
            MetricResult(
                evaluation_run_id=run.id,
                metric=key,
                check_type=check_type,
                value=_approximate(computed.value),
                numerator=computed.numerator,
                denominator=computed.denominator,
                note=computed.note or None,
            )
        )

    for gold_case_id, per_check in (case_outcomes or {}).items():
        for check, (observed, expected) in per_check.items():
            session.add(
                CaseResult(
                    evaluation_run_id=run.id,
                    gold_case_id=gold_case_id,
                    check=check,
                    outcome=observed.value,
                    expected=expected.value,
                )
            )
    session.flush()
    return run


def load_metric(row: MetricResult) -> ComputedMetric:
    """Rebuild the computed metric from its **exact** stored form.

    From `numerator`/`denominator`, never from `value`. A denominator of zero means the metric was
    not measured, and reconstructs as `None` rather than as zero — the distinction the whole
    metrics layer is built around.
    """
    value = Fraction(row.numerator, row.denominator) if row.denominator else None
    return ComputedMetric(
        key=row.metric,
        value=value,
        numerator=row.numerator,
        denominator=row.denominator,
        note=row.note or "",
    )


def metrics_for(session: Session, run: EvaluationRun) -> dict[str, ComputedMetric]:
    """Every metric from one run, keyed as `eval/metrics.py` produces them."""
    rows = session.scalars(
        select(MetricResult).where(MetricResult.evaluation_run_id == run.id)
    ).all()
    return {row.metric: load_metric(row) for row in rows}


def series(
    session: Session, *, metric: str, check_type: str = "all", limit: int = 50
) -> list[tuple[EvaluationRun, ComputedMetric]]:
    """One metric across runs, oldest first.

    Ordered by `created_at` rather than by insertion, and returned with the run beside each value:
    a number without the versions that produced it cannot answer *what changed*, which is the only
    reason to look at a series at all.
    """
    rows = session.execute(
        select(EvaluationRun, MetricResult)
        .join(MetricResult, MetricResult.evaluation_run_id == EvaluationRun.id)
        .where(MetricResult.metric == metric, MetricResult.check_type == check_type)
        .order_by(EvaluationRun.created_at)
        .limit(limit)
    ).all()
    return [(run, load_metric(row)) for run, row in rows]


def baseline(session: Session, *, gold_set_version: str) -> EvaluationRun | None:
    """The run designated as the comparison point, for this gold-set version.

    Scoped to the version deliberately. A baseline from a different gold set is not a baseline —
    the cases changed, so a difference says nothing about the code. `F4.2` (#263) refuses rather
    than comparing across versions.
    """
    return session.scalars(
        select(EvaluationRun)
        .where(
            EvaluationRun.is_baseline.is_(True),
            EvaluationRun.gold_set_version == gold_set_version,
        )
        .order_by(EvaluationRun.created_at.desc())
        .limit(1)
    ).first()
