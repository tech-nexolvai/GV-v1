"""Dimensioned so a regression is attributable, and never able to fail the work it measures.

Source: backend proposal §12; `AGENTS.md` §9 · Verification: ``app/telemetry/metrics.py``.

Two tests carry this file.

**A metrics backend that is down does not fail a package review.** Asserted by making the meter
raise and checking the caller returns normally — the failure is real, not simulated by a flag.

**A metric that cannot be attributed is refused.** "Extraction got slower" is a shrug; the whole
point of the story is that the dimension is there when somebody asks *which* extractor. A required
dimension left off is caught at the call site rather than discovered on a dashboard six weeks later.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.telemetry import metrics as metrics_module
from app.telemetry.metrics import (
    DIMENSIONS,
    METRICS,
    UnknownDimension,
    UnknownMetric,
    collection_failures,
    rate,
    record,
    reset_collection_failures,
)


class RecordingInstrument:
    """A meter instrument that remembers what it was given."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, dict[str, str]]] = []

    def add(self, value: int, attributes: dict[str, str] | None = None) -> None:
        self.calls.append((value, dict(attributes or {})))

    def record(self, value: int, attributes: dict[str, str] | None = None) -> None:
        self.calls.append((value, dict(attributes or {})))


@pytest.fixture(autouse=True)
def clean_instruments(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test gets its own instrument cache and a zeroed failure count.

    The module caches instruments on purpose — creating one per measurement would be wasteful — but
    a cache shared across tests makes the assertions depend on the order they ran in.
    """
    monkeypatch.setattr(metrics_module, "_instruments", {})
    monkeypatch.setattr(metrics_module, "_meter", None)
    reset_collection_failures()


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> dict[str, RecordingInstrument]:
    """Replace instrument creation, so the tests assert on what was recorded rather than on OTel."""
    made: dict[str, RecordingInstrument] = {}

    def fake(spec: Any) -> RecordingInstrument:
        return made.setdefault(spec.name, RecordingInstrument())

    monkeypatch.setattr(metrics_module, "_instrument", fake)
    return made


# ---------------------------------------------------------------------------
# Measurement never fails the work
# ---------------------------------------------------------------------------


def test_a_metrics_backend_that_is_down_does_not_fail_the_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The acceptance criterion, and the one that matters at three in the morning.

    A package review must not fail because a collector is unreachable. Asserted by making the meter
    actually raise rather than by a flag that says it would have.
    """

    def exploding(spec: Any) -> Any:
        raise RuntimeError("the collector is unreachable")

    monkeypatch.setattr(metrics_module, "_instrument", exploding)

    record("retries", 1, workflow="review", task="extract")

    assert collection_failures() == 1, "a dropped measurement must still be countable"


def test_a_dropped_measurement_is_counted_so_the_outage_is_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one number that cannot be reported by the system it reports on.

    Without this, a metrics outage looks exactly like a quiet week — flat lines and no errors.
    """

    def exploding(spec: Any) -> Any:
        raise RuntimeError("down")

    monkeypatch.setattr(metrics_module, "_instrument", exploding)

    for _ in range(3):
        record("retries", 1, workflow="review", task="extract")

    assert collection_failures() == 3


def test_a_backend_failure_is_logged_rather_than_silent(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Swallowed is not the same as hidden. A caller reading the logs should be able to see that the
    number they are looking for was never recorded."""

    def exploding(spec: Any) -> Any:
        raise RuntimeError("down")

    monkeypatch.setattr(metrics_module, "_instrument", exploding)

    with caplog.at_level("WARNING"):
        record("retries", 1, workflow="review", task="extract")

    assert "retries" in caplog.text


# ---------------------------------------------------------------------------
# A caller's mistake still raises
# ---------------------------------------------------------------------------


def test_an_unknown_metric_is_refused(recorded: dict[str, RecordingInstrument]) -> None:
    """Not swallowed, and that is not a contradiction with the test above.

    A typo'd name does not depend on data, network or load: it cannot reach production without
    failing the first test that runs the line. Swallowing it would produce a metric that never
    records and nobody notices — the same failure, reached by a different route.
    """
    with pytest.raises(UnknownMetric, match="not a metric this project emits"):
        record("task_duration_seconds", 12, workflow="review", task="extract")


def test_an_undeclared_dimension_is_refused(recorded: dict[str, RecordingInstrument]) -> None:
    """One call site spelling it `extractorVersion` splits a series in two, with no error anywhere
    and two half-height lines on a dashboard nobody reconciles."""
    with pytest.raises(UnknownDimension, match="not a declared dimension"):
        record("retries", 1, workflow="review", task="extract", extractorVersion="v1")


def test_a_metric_missing_a_required_dimension_is_refused(
    recorded: dict[str, RecordingInstrument],
) -> None:
    """ "Extraction got slower last week" is a shrug. The dimension is what makes it a cause, so a
    measurement that cannot be attributed is refused rather than emitted."""
    with pytest.raises(UnknownDimension, match="extractor_version"):
        record("task_duration_ms", 1200, workflow="review", task="extract")


def test_a_blank_required_dimension_is_refused(
    recorded: dict[str, RecordingInstrument],
) -> None:
    """An empty string satisfies "the key is present" and attributes nothing. It is the shape a
    call site reaches for when it does not have the value to hand."""
    with pytest.raises(UnknownDimension, match="extractor_version"):
        record("task_duration_ms", 1200, workflow="review", task="extract", extractor_version="")


def test_a_float_measurement_is_refused(recorded: dict[str, RecordingInstrument]) -> None:
    """Costs are millionths and durations are whole milliseconds. A float here is a total that later
    has to reconcile against an invoice and will not."""
    with pytest.raises(TypeError, match="takes an int"):
        record("token_cost_micros", 1.5, extractor_version="v1")  # type: ignore[arg-type]


def test_a_bool_is_not_accepted_as_a_count(recorded: dict[str, RecordingInstrument]) -> None:
    """`True` is an `int` in Python and would record as 1, which is a measurement nobody meant."""
    with pytest.raises(TypeError, match="takes an int"):
        record("retries", True, workflow="review", task="extract")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# What actually gets recorded
# ---------------------------------------------------------------------------


def test_a_measurement_arrives_with_its_dimensions(
    recorded: dict[str, RecordingInstrument],
) -> None:
    record(
        "task_duration_ms",
        1200,
        workflow="review",
        task="extract",
        extractor_version="nova2-lite-2026-08",
    )

    value, attributes = recorded["task_duration_ms"].calls[0]
    assert value == 1200
    assert attributes["extractor_version"] == "nova2-lite-2026-08"
    assert attributes["workflow"] == "review"


def test_a_finding_is_counted_by_outcome_and_rule_version(
    recorded: dict[str, RecordingInstrument],
) -> None:
    """A behaviour change is attributable to a rule change only if the snapshot is on the series.

    Without it, a new tolerance and a new bug produce the same shape on a dashboard.
    """
    snapshot = "sha256:" + "a" * 64
    record(
        "findings",
        1,
        check_type="arch_vs_shop",
        rule_snapshot_id=snapshot,
        outcome="REVIEW_REQUIRED",
    )

    _, attributes = recorded["findings"].calls[0]
    assert attributes["rule_snapshot_id"] == snapshot
    assert attributes["outcome"] == "REVIEW_REQUIRED"


def test_the_ocr_disagreement_signal_is_emitted(
    recorded: dict[str, RecordingInstrument],
) -> None:
    """The early warning for extraction quality.

    It moves before the critical false-PASS rate does, because a disagreement becomes an abstention
    rather than a wrong answer — so this is the number that gives warning while the system is still
    behaving safely.
    """
    record("ocr_readings", 100, extractor_version="v1")
    record("ocr_disagreements", 7, extractor_version="v1")

    assert recorded["ocr_readings"].calls[0][0] == 100
    assert recorded["ocr_disagreements"].calls[0][0] == 7


def test_queue_wait_is_emitted_for_the_scale_out_trigger(
    recorded: dict[str, RecordingInstrument],
) -> None:
    """F6.1 reads this. Workers saturated shows up here long before anything times out, which is the
    difference between scaling out and explaining an outage."""
    record("queue_wait_ms", 45_000, workflow="review", task="extract")

    assert recorded["queue_wait_ms"].calls[0][0] == 45_000


def test_reviewer_minutes_are_emitted(recorded: dict[str, RecordingInstrument]) -> None:
    """The number the whole product is meant to reduce, and the one nobody measures until it is
    asked for."""
    record("reviewer_minutes", 18, check_type="internal")

    assert recorded["reviewer_minutes"].calls[0][0] == 18


# ---------------------------------------------------------------------------
# Rates are derived, not emitted
# ---------------------------------------------------------------------------


def test_a_rate_is_derived_from_counts() -> None:
    assert rate(7, 100) == 0.07


def test_a_rate_with_no_observations_is_none_rather_than_zero() -> None:
    """A disagreement rate of zero means the extractor agreed with itself every time. No readings at
    all means nobody looked. Reporting the second as the first is how an extractor that stopped
    running comes to look like an extractor that got perfect."""
    assert rate(0, 0) is None


def test_a_rate_cannot_be_built_from_negative_counts() -> None:
    with pytest.raises(ValueError, match="counts"):
        rate(1, -1)


def test_pre_computed_rates_are_not_in_the_metric_set() -> None:
    """The issue's sketch named `ocr_disagreement_rate` and `vlm_call_rate`, and both are counter
    pairs here instead.

    A rate computed at emit time cannot be re-aggregated: averaging one extractor's 2% over
    another's 20% gives 11%, which is the disagreement rate of nothing — it depends on how many
    readings each made. Since slicing by dimension is the entire point of this story, a metric that
    cannot be sliced correctly is the wrong shape however faithfully it matches the sketch.
    """
    assert "ocr_disagreement_rate" not in METRICS
    assert "vlm_call_rate" not in METRICS
    for pair in (("ocr_disagreements", "ocr_readings"), ("vlm_calls", "model_calls")):
        for name in pair:
            assert name in METRICS, f"{name} is needed to derive the rate"


# ---------------------------------------------------------------------------
# The declaration itself
# ---------------------------------------------------------------------------


def test_every_metric_backend_two_asks_for_is_declared() -> None:
    """Backend §12's list, checked against the module rather than trusted.

    Named explicitly so dropping one is a failing test. `retries` in particular is the sort of
    metric that gets removed as noise and then wanted the week a dependency starts degrading.
    """
    for name in (
        "task_duration_ms",
        "retries",
        "queue_wait_ms",
        "token_cost_micros",
        "reviewer_minutes",
    ):
        assert name in METRICS


def test_every_metric_requires_at_least_one_dimension() -> None:
    """An undimensioned metric is the visible-but-unattributable case the story exists to rule out,
    so there is no way to declare one."""
    for name, spec in METRICS.items():
        assert spec.requires, f"{name} can be emitted with nothing attached to it"


def test_every_required_dimension_is_a_declared_dimension() -> None:
    """The two lists cannot drift: a metric requiring a dimension the vocabulary does not have would
    be impossible to satisfy, and every call to it would raise."""
    for name, spec in METRICS.items():
        for dimension in spec.requires:
            assert dimension in DIMENSIONS, f"{name} requires undeclared {dimension}"


# ---------------------------------------------------------------------------
# It actually works against the real SDK
# ---------------------------------------------------------------------------


def test_a_measurement_reaches_a_real_opentelemetry_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every test above records through a fake instrument, which cannot see an integration break.

    If `create_counter` were called with the wrong arguments, the fake would still accept the
    measurement, the backend-down test would still pass, and the module would emit nothing at all in
    production while its whole suite was green. This is the test that fails in that case.
    """
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    monkeypatch.setattr(metrics_module, "_meter", provider.get_meter("gv-test"))

    record("ocr_disagreements", 7, extractor_version="nova2-lite-2026-08")
    record("task_duration_ms", 1200, workflow="review", task="extract", extractor_version="v1")

    collected = reader.get_metrics_data()
    assert collected is not None

    seen: dict[str, list[object]] = {}
    for resource in collected.resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                seen.setdefault(metric.name, []).extend(metric.data.data_points)

    assert "ocr_disagreements" in seen, "the counter never reached the reader"
    assert "task_duration_ms" in seen, "the histogram never reached the reader"

    (point,) = seen["ocr_disagreements"]
    assert point.value == 7  # type: ignore[attr-defined]
    assert dict(point.attributes)["extractor_version"] == "nova2-lite-2026-08"  # type: ignore[attr-defined]

    assert collection_failures() == 0, "a real recording must not have been swallowed"


def test_a_failure_while_emitting_is_swallowed_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """The more likely runtime failure, and the one the other tests miss.

    Instrument creation happens once; emission happens on every measurement. A collector that goes
    away mid-run fails at `add`, not at `create_counter`, so a guard that only covered creation
    would let the realistic outage through.
    """

    class Failing:
        def add(self, value: int, attributes: dict[str, str] | None = None) -> None:
            raise RuntimeError("the collector went away")

        def record(self, value: int, attributes: dict[str, str] | None = None) -> None:
            raise RuntimeError("the collector went away")

    monkeypatch.setattr(metrics_module, "_instrument", lambda spec: Failing())

    record("retries", 1, workflow="review", task="extract")
    record("queue_wait_ms", 10, workflow="review", task="extract")

    assert collection_failures() == 2


@pytest.mark.parametrize("value", [-1, -1000])
def test_a_negative_measurement_is_refused(
    value: int, recorded: dict[str, RecordingInstrument]
) -> None:
    """A counter is monotonic. OpenTelemetry drops a negative `add` and logs it somewhere nobody
    reads, so the series would simply be short by that amount with no error anywhere — a number
    somebody acts on, quietly low."""
    with pytest.raises(ValueError, match="go backwards"):
        record("retries", value, workflow="review", task="extract")


@pytest.mark.parametrize("given", ["   ", "\t", "\n"])
def test_a_whitespace_only_dimension_is_refused(
    given: str, recorded: dict[str, RecordingInstrument]
) -> None:
    """`"   "` is truthy and attributes nothing. Checking truthiness rather than content is how a
    blank label reaches a dashboard looking like a real one."""
    with pytest.raises(UnknownDimension, match="extractor_version"):
        record("task_duration_ms", 1200, workflow="review", task="extract", extractor_version=given)


def test_a_non_string_dimension_is_refused(recorded: dict[str, RecordingInstrument]) -> None:
    """A dimension is a label a dashboard groups by. A non-string groups by its repr, so the same
    extractor arrives under two labels depending on how each caller spelled it."""
    with pytest.raises(UnknownDimension, match="must be a string"):
        record("retries", 1, workflow="review", task=3)  # type: ignore[arg-type]


def test_every_dimension_given_reaches_the_measurement(
    recorded: dict[str, RecordingInstrument],
) -> None:
    """All of them, not just the one being asserted. A dimension silently dropped between the call
    and the instrument is a series that cannot be sliced by it, which is the whole story."""
    record(
        "findings",
        1,
        check_type="internal",
        rule_snapshot_id="sha256:" + "c" * 64,
        outcome="FAIL",
    )

    _, attributes = recorded["findings"].calls[0]
    assert attributes == {
        "check_type": "internal",
        "rule_snapshot_id": "sha256:" + "c" * 64,
        "outcome": "FAIL",
    }


def test_a_rate_cannot_be_built_from_a_negative_numerator() -> None:
    """Both sides, because only the denominator was checked at first and a negative numerator
    produces a negative rate that looks like a real reading."""
    with pytest.raises(ValueError, match="counts"):
        rate(-1, 100)
