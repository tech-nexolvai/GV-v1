"""The day-to-day numbers, dimensioned so a regression is attributable (#260, F2.2).

Backend §12 asks for task duration, retries, OCR disagreement, VLM call rate, token cost, queue wait
and reviewer minutes. The acceptance criterion is the interesting part: *dimensioned so a regression
is attributable, not just visible*. "Extraction got slower last week" is a shrug. "Extraction got
slower last week, on `nova2-lite-2026-08`, for `arch_vs_shop` checks only" is a cause.

**One declaration, like `SPAN_ATTRS`.** Every metric and every dimension is named once here, and
`record()` refuses anything else. Two spellings of `extractor_version` is a dashboard that silently
splits one number into two, and the second spelling is always an accident. Each metric also declares
which dimensions it *requires*, because a metric emitted without them cannot be attributed to
anything and is therefore the visible-but-not-attributable case the story exists to prevent.

**Counters, not pre-computed rates.** The issue's sketch named `ocr_disagreement_rate` and
`vlm_call_rate`. Both are emitted here as counter pairs — `ocr_readings` with `ocr_disagreements`,
`vlm_calls` with `model_calls` — and the rate is derived at read time by `rate()`.

A rate computed at emit time cannot be re-aggregated. Averaging one extractor's 2% over another's
20% gives 11%, which is not the disagreement rate of anything: it depends on how many readings each
made. Since slicing by dimension is the entire point of this story, a metric that cannot be sliced
correctly would have been the wrong shape however faithfully it matched the sketch.

**Measurement never fails the work.** A metrics backend that is down, slow or misconfigured must not
fail a package review. `record()` catches everything the meter raises and counts it in
`collection_failures()`, so a silent outage is still visible from inside the process.

**A caller's mistake still raises, and that is not a contradiction.** An unknown metric name or a
missing dimension does not depend on data, network or load — it cannot reach production without
failing the first test that exercises the line. Swallowing it would produce a metric that never
records and nobody notices, which is the failure this module exists to prevent, arrived at by a
different route.

**Exact numbers only.** Token cost is `token_cost_micros`, an integer count of millionths, for the
same reason the verdict path uses `Fraction` — a float cost accumulated over a million calls is a
number nobody can reconcile against an invoice.

Source: backend proposal §12; `AGENTS.md` §9 · Verification: ``tests/telemetry/test_metrics.py``
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

from opentelemetry import metrics
from opentelemetry.metrics import Counter, Histogram

from app.telemetry.tracing import INSTRUMENTATION_NAME, current_trace_id

__all__ = [
    "DIMENSIONS",
    "METRICS",
    "MetricSpec",
    "UnknownDimension",
    "UnknownMetric",
    "collection_failures",
    "rate",
    "record",
    "reset_collection_failures",
]

logger = logging.getLogger(__name__)

#: The dimensions a metric may carry, declared once.
#:
#: Closed for the same reason `SPAN_ATTRS` is: a dashboard joins on these names, and one call site
#: spelling it `extractorVersion` splits a series in two without any error appearing anywhere.
DIMENSIONS: Final = (
    # `internal`, `arch_vs_shop` or `global`. A regression in one check type is a different
    # investigation from a regression in all three.
    "check_type",
    # Which extractor produced the reading. The dimension that turns "extraction got slower" into a
    # release to look at.
    "extractor_version",
    # ADR-0005. A behaviour change is attributable to a rule change only if the snapshot is on the
    # series; without it, a new tolerance and a new bug look identical on a dashboard.
    "rule_snapshot_id",
    "workflow",
    "task",
    # PASS, FAIL or which abstention. Findings counted without it hide the number that matters:
    # abstentions rising is the system getting more careful or the input getting worse, and either
    # way it is not visible in a total.
    "outcome",
    # F6.1. Kept as dimensions on the shared F2 path so an absent upgrade measurement is visible
    # without creating a second telemetry system.
    "upgrade_trigger",
    "upgrade_measurement",
    "measurement_status",
)


@dataclass(frozen=True, slots=True)
class MetricSpec:
    """One metric: what it measures, in what unit, and what it must be dimensioned by.

    `requires` is the part that makes the acceptance criterion enforceable. A duration with no
    extractor version attached is visible and unattributable — exactly the case this story exists to
    rule out — so the requirement is declared beside the metric rather than left to each call site to
    remember.
    """

    name: str
    kind: Literal["histogram", "counter"]
    unit: str
    description: str
    requires: tuple[str, ...]

    def __post_init__(self) -> None:
        for dimension in self.requires:
            if dimension not in DIMENSIONS:
                raise UnknownDimension(
                    f"{self.name} requires {dimension!r}, which is not a declared dimension"
                )


class UnknownMetric(ValueError):
    """A metric name outside `METRICS` was recorded."""


class UnknownDimension(ValueError):
    """A dimension name outside `DIMENSIONS`, or a required one that was missing."""


#: Every metric this project emits.
#:
#: `task_duration_ms` and `queue_wait_ms` are milliseconds as integers rather than seconds as floats:
#: a duration is counted, not measured to arbitrary precision, and an integer is one less thing that
#: can drift.
METRICS: Final[Mapping[str, MetricSpec]] = {
    spec.name: spec
    for spec in (
        MetricSpec(
            name="task_duration_ms",
            kind="histogram",
            unit="ms",
            description="How long one task took, end to end.",
            # Not `rule_snapshot_id`: most tasks are extraction and run no rule at all, and a
            # required dimension nobody can supply becomes a dimension everybody passes "unknown"
            # for, which is worse than not having it.
            requires=("workflow", "task", "extractor_version"),
        ),
        MetricSpec(
            name="retries",
            kind="counter",
            unit="1",
            description="Attempts after the first. A rising retry count is the shape of a "
            "dependency degrading before it fails outright.",
            requires=("workflow", "task"),
        ),
        MetricSpec(
            name="queue_wait_ms",
            kind="histogram",
            unit="ms",
            description="From enqueue to the start of work. This is the scale-out signal F6.1 "
            "reads: workers saturated shows here long before anything times out.",
            requires=("workflow", "task"),
        ),
        MetricSpec(
            name="ocr_readings",
            kind="counter",
            unit="1",
            description="Readings the extractor produced. The denominator of the disagreement "
            "rate.",
            requires=("extractor_version",),
        ),
        MetricSpec(
            name="ocr_disagreements",
            kind="counter",
            unit="1",
            description="Readings where two readers did not agree. The early warning for "
            "extraction quality: this moves before the false-PASS rate does, because a "
            "disagreement becomes an abstention rather than a wrong answer.",
            requires=("extractor_version",),
        ),
        MetricSpec(
            name="model_calls",
            kind="counter",
            unit="1",
            description="Model calls made. The denominator of the VLM call rate.",
            requires=("extractor_version",),
        ),
        MetricSpec(
            name="vlm_calls",
            kind="counter",
            unit="1",
            description="Calls that went to a vision model — the expensive lane. A rising share "
            "means cheaper extraction is failing more often.",
            requires=("extractor_version",),
        ),
        MetricSpec(
            name="token_cost_micros",
            kind="counter",
            unit="1",
            description="Spend in millionths of a currency unit, as an integer. Never a float: a "
            "cost accumulated over a million calls has to reconcile against an invoice.",
            requires=("extractor_version",),
        ),
        MetricSpec(
            name="reviewer_minutes",
            kind="histogram",
            unit="min",
            description="Minutes a person spent on a package. The number the whole product is "
            "meant to reduce, and the one nobody measures until it is asked for.",
            requires=("check_type",),
        ),
        MetricSpec(
            name="findings",
            kind="counter",
            unit="1",
            description="Findings produced, by outcome and rule version. A behaviour change is "
            "attributable to a rule change only if the snapshot is on the series.",
            requires=("check_type", "rule_snapshot_id", "outcome"),
        ),
        MetricSpec(
            name="upgrade_trigger_measurement_status",
            kind="counter",
            unit="1",
            description="Whether one F6 upgrade-trigger quantity was measured in this sample.",
            requires=("upgrade_trigger", "upgrade_measurement", "measurement_status"),
        ),
        *(
            MetricSpec(
                name=name,
                kind="histogram",
                unit=unit,
                description=description,
                requires=("upgrade_trigger",),
            )
            for name, unit, description in (
                (
                    "upgrade_separate_worker_pools_concurrent_packages",
                    "1",
                    "Concurrent packages observed for the separate-worker-pools trigger.",
                ),
                (
                    "upgrade_separate_worker_pools_queue_depth",
                    "1",
                    "Worker queue depth observed for the separate-worker-pools trigger.",
                ),
                (
                    "upgrade_managed_postgres_available",
                    "1",
                    "Database availability sample: one available, zero unavailable.",
                ),
                (
                    "upgrade_managed_postgres_recovery_events",
                    "1",
                    "Database recovery events observed for the managed-Postgres trigger.",
                ),
                (
                    "upgrade_temporal_recovery_interventions",
                    "1",
                    "Manual workflow recovery interventions observed for the Temporal trigger.",
                ),
                (
                    "upgrade_qdrant_pgvector_latency_ns",
                    "ns",
                    "pgvector query latency observed for the dedicated-vector-service trigger.",
                ),
                (
                    "upgrade_qdrant_transaction_latency_ns",
                    "ns",
                    "Transactional latency observed beside pgvector load.",
                ),
                (
                    "upgrade_opensearch_bm25_corpus_size",
                    "1",
                    "BM25 corpus size observed for the OpenSearch trigger.",
                ),
                (
                    "upgrade_opensearch_bm25_latency_ns",
                    "ns",
                    "BM25 query latency observed for the OpenSearch trigger.",
                ),
                (
                    "upgrade_self_hosted_vlm_managed_cost_micros",
                    "1",
                    "Recorded managed-VLM spend in integer micros.",
                ),
                (
                    "upgrade_self_hosted_vlm_gpu_baseline_micros",
                    "1",
                    "Measured GPU-hour baseline in integer micros.",
                ),
            )
        ),
    )
}

_meter: metrics.Meter | None = None
_instruments: dict[str, Counter | Histogram] = {}
_collection_failures = 0


def _instrument(spec: MetricSpec) -> Counter | Histogram:
    """The instrument for one metric, created once.

    Lazily, because creating a meter at import time would fix the provider before an application had
    a chance to configure one — the same reason `configure_tracing()` is a call rather than an
    import side effect.
    """
    global _meter
    if _meter is None:
        _meter = metrics.get_meter(INSTRUMENTATION_NAME)
    if spec.name not in _instruments:
        if spec.kind == "counter":
            _instruments[spec.name] = _meter.create_counter(
                spec.name, unit=spec.unit, description=spec.description
            )
        else:
            _instruments[spec.name] = _meter.create_histogram(
                spec.name, unit=spec.unit, description=spec.description
            )
    return _instruments[spec.name]


def record(metric: str, value: int, **dimensions: str) -> None:
    """Record one measurement, dimensioned.

    Raises `UnknownMetric` or `UnknownDimension` for a caller's mistake, and swallows anything the
    metrics backend raises. The two are different failures: a typo'd name is deterministic and fails
    the first test that runs the line, while a backend being down is a Tuesday. Only one of them
    should be able to fail a package review.

    `value` is an `int`. Every metric here is a count, a cost in millionths or a duration in whole
    milliseconds — there is no quantity in this set that needs a fraction, and accepting a float
    would invite one into a total that later has to reconcile.
    """
    spec = METRICS.get(metric)
    if spec is None:
        raise UnknownMetric(
            f"{metric!r} is not a metric this project emits. The set is fixed so a dashboard can "
            f"join on it: {', '.join(sorted(METRICS))}."
        )
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(
            f"{metric} takes an int, not {type(value).__name__}. Costs are millionths and durations "
            "are whole milliseconds; a float here is a total nobody can reconcile later."
        )
    if value < 0:
        # A counter is monotonic — OpenTelemetry drops a negative `add` and says so in a log nobody
        # reads, so the series would simply be short by that amount with no error anywhere. A
        # negative duration or count is a caller subtracting somewhere it should not, and it is
        # worth failing on rather than silently under-reporting the number somebody will act on.
        raise ValueError(
            f"{metric} was given {value}. Every metric here is a count, a cost or a duration, and "
            "none of them go backwards; a counter would drop this and leave the series quietly low."
        )

    for name, given in dimensions.items():
        if name not in DIMENSIONS:
            raise UnknownDimension(
                f"{name!r} is not a declared dimension. The set is fixed so a series is not "
                f"silently split in two: {', '.join(DIMENSIONS)}."
            )
        if not isinstance(given, str):
            raise UnknownDimension(
                f"{name} must be a string, not {type(given).__name__}. A dimension is a label a "
                "dashboard groups by, and a value of another type groups by its repr — so the same "
                "extractor arrives under two labels depending on how the caller spelled it."
            )
    # `strip()`, not truthiness: "   " is truthy and attributes nothing, and it is exactly the shape
    # a call site produces when it does not have the value to hand.
    missing = [name for name in spec.requires if not dimensions.get(name, "").strip()]
    if missing:
        raise UnknownDimension(
            f"{metric} must be dimensioned by {', '.join(missing)}. A metric without them is "
            "visible and unattributable, which is the case this module exists to rule out."
        )

    try:
        instrument = _instrument(spec)
        if spec.kind == "counter":
            instrument.add(value, attributes=dict(dimensions))  # type: ignore[union-attr]
        else:
            instrument.record(value, attributes=dict(dimensions))  # type: ignore[union-attr]
    except Exception:  # measurement must never fail the measured work
        global _collection_failures
        _collection_failures += 1
        # Logged, not raised. A metrics outage that is invisible from inside the process is an
        # outage nobody notices until somebody asks why a dashboard is flat.
        #
        # With the trace id, because the question that follows a dropped measurement is "dropped for
        # which work?" — and this module is imported by the code that answers it.
        logger.warning(
            "metric %s could not be recorded (trace %s)",
            metric,
            current_trace_id() or "none",
            exc_info=True,
        )


def rate(numerator: int, denominator: int) -> float | None:
    """A rate derived at read time, or `None` when there is nothing to divide.

    `None` rather than `0.0`: a disagreement rate of zero means the extractor agreed with itself
    every time, and no readings at all means nobody looked. Reporting the second as the first is how
    an extractor that stopped running comes to look like an extractor that got perfect.

    Derived here rather than emitted, so it can be computed over whatever slice is being asked
    about. A rate averaged over pre-computed rates is not the rate of anything.
    """
    if denominator < 0 or numerator < 0:
        raise ValueError("a rate is computed from counts, which are not negative")
    if denominator == 0:
        return None
    return numerator / denominator


def collection_failures() -> int:
    """How many measurements were dropped because the backend raised.

    Readable from inside the process so a silent metrics outage is still detectable — the one number
    that cannot be reported by the system it is reporting on.
    """
    return _collection_failures


def reset_collection_failures() -> None:
    """Reset the dropped-measurement count. For tests, and for a process that has re-configured."""
    global _collection_failures
    _collection_failures = 0
