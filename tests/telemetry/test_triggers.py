"""The F6 upgrade signals are time-series evidence, not automatic architecture decisions.

Source: issue #267 and ``docs/DESIGN_CONTROLS.md`` section 6.
Verification: ``app/telemetry/triggers.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.telemetry import triggers
from app.telemetry.triggers import (
    MEASUREMENTS,
    TriggerSample,
    UnknownTriggerMeasurement,
    UpgradeTrigger,
    record_sample,
)

NOW = datetime(2026, 8, 26, 9, 0, tzinfo=UTC)


def _sample(trigger: UpgradeTrigger, **values: int | None) -> TriggerSample:
    return TriggerSample(trigger, NOW, values)


def test_all_eight_deferred_upgrades_have_an_explicit_measurement_contract() -> None:
    assert len(UpgradeTrigger) == 8
    assert set(MEASUREMENTS) == set(UpgradeTrigger)
    assert all(MEASUREMENTS[trigger] for trigger in UpgradeTrigger)


def test_a_measured_trigger_uses_f2_for_status_and_numeric_series(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, int, dict[str, str]]] = []
    monkeypatch.setattr(
        triggers,
        "record",
        lambda metric, value, **dimensions: calls.append((metric, value, dimensions)),
    )

    record_sample(
        _sample(
            UpgradeTrigger.OPENSEARCH,
            upgrade_opensearch_bm25_corpus_size=3200,
            upgrade_opensearch_bm25_latency_ns=125_000,
        )
    )

    assert [call[0] for call in calls] == [
        "upgrade_trigger_measurement_status",
        "upgrade_opensearch_bm25_corpus_size",
        "upgrade_trigger_measurement_status",
        "upgrade_opensearch_bm25_latency_ns",
    ]
    assert all(call[2]["upgrade_trigger"] == "opensearch" for call in calls)


def test_not_measured_is_reported_without_a_default_numeric_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, int, dict[str, str]]] = []
    monkeypatch.setattr(
        triggers,
        "record",
        lambda metric, value, **dimensions: calls.append((metric, value, dimensions)),
    )

    record_sample(_sample(UpgradeTrigger.GRAPHRAG, upgrade_graphrag_need=None))

    assert calls == [
        (
            "upgrade_trigger_measurement_status",
            1,
            {
                "upgrade_trigger": "graphrag",
                "upgrade_measurement": "upgrade_graphrag_need",
                "measurement_status": "not_measured",
            },
        )
    ]


def test_repeated_samples_are_each_recorded_over_time(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, int, dict[str, str]]] = []
    monkeypatch.setattr(
        triggers,
        "record",
        lambda metric, value, **dimensions: calls.append((metric, value, dimensions)),
    )
    sample = _sample(
        UpgradeTrigger.TEMPORAL,
        upgrade_temporal_recovery_interventions=1,
    )

    record_sample(sample)
    record_sample(sample)

    numeric = [call for call in calls if call[0] == "upgrade_temporal_recovery_interventions"]
    assert len(numeric) == 2


def test_zero_is_a_real_measurement_not_an_absence(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, int, dict[str, str]]] = []
    monkeypatch.setattr(
        triggers,
        "record",
        lambda metric, value, **dimensions: calls.append((metric, value, dimensions)),
    )

    record_sample(
        _sample(
            UpgradeTrigger.MANAGED_POSTGRES,
            upgrade_managed_postgres_available=0,
            upgrade_managed_postgres_recovery_events=0,
        )
    )

    statuses = [call[2]["measurement_status"] for call in calls if call[0].endswith("status")]
    assert statuses == ["measured", "measured"]
    assert (
        "upgrade_managed_postgres_available",
        0,
        {"upgrade_trigger": "managed_postgres"},
    ) in calls


def test_a_sample_must_name_every_quantity_and_no_unknown_one() -> None:
    with pytest.raises(UnknownTriggerMeasurement, match="missing"):
        _sample(UpgradeTrigger.QDRANT, upgrade_qdrant_pgvector_latency_ns=20)
    with pytest.raises(UnknownTriggerMeasurement, match="unknown"):
        _sample(UpgradeTrigger.MCP, upgrade_mcp_need=None, guessed_threshold=5)


def test_measurements_are_exact_non_negative_integers_or_absent() -> None:
    with pytest.raises(TypeError, match="int or None"):
        _sample(UpgradeTrigger.MCP, upgrade_mcp_need=1.5)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="int or None"):
        _sample(UpgradeTrigger.MCP, upgrade_mcp_need=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="negative"):
        _sample(UpgradeTrigger.MCP, upgrade_mcp_need=-1)


def test_observation_time_must_be_timezone_aware() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        TriggerSample(
            UpgradeTrigger.MCP,
            NOW.replace(tzinfo=None),
            {"upgrade_mcp_need": None},
        )


def test_every_numeric_series_is_declared_on_the_shared_f2_path() -> None:
    from app.telemetry.metrics import METRICS

    declared = {name for names in MEASUREMENTS.values() for name in names}
    unavailable_only = {"upgrade_graphrag_need", "upgrade_mcp_need"}
    assert declared - unavailable_only <= set(METRICS)
    assert "upgrade_trigger_measurement_status" in METRICS
