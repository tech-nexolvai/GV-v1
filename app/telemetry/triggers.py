"""Emit the observations behind F6's eight deferred-technology decisions.

This module records evidence for an architecture decision; it never makes that decision. Every
sample travels through F2's telemetry path, and a missing value emits a ``not_measured`` status but
no numeric zero. Repeated calls form the time series needed to distinguish a sustained condition
from one unusual package.

Source: issue #267 and ``docs/DESIGN_CONTROLS.md`` section 6.
Verification: ``tests/telemetry/test_triggers.py``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from app.telemetry.metrics import record

__all__ = [
    "MEASUREMENTS",
    "TriggerSample",
    "UnknownTriggerMeasurement",
    "UpgradeTrigger",
    "record_sample",
]


class UpgradeTrigger(StrEnum):
    """The eight deferred architecture upgrades named by F6."""

    SEPARATE_WORKER_POOLS = "separate_worker_pools"
    MANAGED_POSTGRES = "managed_postgres"
    TEMPORAL = "temporal"
    QDRANT = "qdrant"
    OPENSEARCH = "opensearch"
    GRAPHRAG = "graphrag"
    MCP = "mcp"
    SELF_HOSTED_VLM = "self_hosted_vlm"


class UnknownTriggerMeasurement(ValueError):
    """A sample named a quantity that does not belong to its upgrade trigger."""


# Numeric metric names are deliberately fixed and include the upgrade they serve. GraphRAG and MCP
# have no documented source quantity yet; their placeholder names let callers report that absence
# explicitly without manufacturing a zero.
MEASUREMENTS: Final[Mapping[UpgradeTrigger, tuple[str, ...]]] = MappingProxyType(
    {
        UpgradeTrigger.SEPARATE_WORKER_POOLS: (
            "upgrade_separate_worker_pools_concurrent_packages",
            "upgrade_separate_worker_pools_queue_depth",
        ),
        UpgradeTrigger.MANAGED_POSTGRES: (
            "upgrade_managed_postgres_available",
            "upgrade_managed_postgres_recovery_events",
        ),
        UpgradeTrigger.TEMPORAL: ("upgrade_temporal_recovery_interventions",),
        UpgradeTrigger.QDRANT: (
            "upgrade_qdrant_pgvector_latency_ns",
            "upgrade_qdrant_transaction_latency_ns",
        ),
        UpgradeTrigger.OPENSEARCH: (
            "upgrade_opensearch_bm25_corpus_size",
            "upgrade_opensearch_bm25_latency_ns",
        ),
        UpgradeTrigger.GRAPHRAG: ("upgrade_graphrag_need",),
        UpgradeTrigger.MCP: ("upgrade_mcp_need",),
        UpgradeTrigger.SELF_HOSTED_VLM: (
            "upgrade_self_hosted_vlm_managed_cost_micros",
            "upgrade_self_hosted_vlm_gpu_baseline_micros",
        ),
    }
)


@dataclass(frozen=True, slots=True)
class TriggerSample:
    """One time-stamped set of observations for one upgrade trigger.

    A value of ``None`` means not measured. It is distinct from zero, which is a valid observation
    for queue depth, events, spend and database availability.
    """

    trigger: UpgradeTrigger
    observed_at: datetime
    values: Mapping[str, int | None]

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        expected = set(MEASUREMENTS[self.trigger])
        given = set(self.values)
        if given != expected:
            missing = sorted(expected - given)
            unknown = sorted(given - expected)
            detail = []
            if missing:
                detail.append(f"missing {', '.join(missing)}")
            if unknown:
                detail.append(f"unknown {', '.join(unknown)}")
            raise UnknownTriggerMeasurement(f"{self.trigger.value} sample has {'; '.join(detail)}")
        for name, value in self.values.items():
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                raise TypeError(f"{name} must be an int or None")
            if value is not None and value < 0:
                raise ValueError(f"{name} must not be negative")
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))


def record_sample(sample: TriggerSample) -> None:
    """Record one sample through F2, including explicit availability for every quantity."""

    if not isinstance(sample, TriggerSample):
        raise TypeError("sample must be a TriggerSample")
    trigger = sample.trigger.value
    for measurement, value in sample.values.items():
        status = "not_measured" if value is None else "measured"
        record(
            "upgrade_trigger_measurement_status",
            1,
            upgrade_trigger=trigger,
            upgrade_measurement=measurement,
            measurement_status=status,
        )
        if value is not None:
            record(measurement, value, upgrade_trigger=trigger)
