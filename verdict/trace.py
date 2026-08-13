"""Immutable audit records for deterministic verdict calculations."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from units.measurement import Measurement, Unit
from verdict.outcomes import Outcome


@dataclass(frozen=True, slots=True)
class TracedOperand:
    """One exact input and the evidence provenance used for it."""

    name: str
    value: Measurement | Fraction | str | None
    source: str
    evidence_ref: str | None


@dataclass(frozen=True, slots=True)
class CalculationTrace:
    """A complete, versioned explanation of one deterministic calculation."""

    operation: str
    operands: tuple[TracedOperand, ...]
    intermediates: tuple[tuple[str, object], ...]
    comparison: str
    tolerance: Measurement | None
    arithmetic_unit: Unit
    outcome: Outcome
    engine_version: str
    operation_version: str
