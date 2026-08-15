"""The only boundary allowed to seal evidence for deterministic verdicts.

The gate abstains explicitly when an observation is not qualified, authoritative,
present, and expressed in a known unit.  It never repairs or guesses evidence.

Source: ``docs/DESIGN.md`` section 3.14 and issue #121.
Verification: ``tests/evidence/test_gate.py``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from evidence.canonical import Authority, CanonicalObservation
from units.measurement import Measurement, Unit
from verdict.operands import QUALIFIED_STATUSES, EvidenceStatus, VerdictOperand

__all__ = [
    "QUALIFIED_STATUSES",
    "EvidenceStatus",
    "GateRefusal",
    "RefusalReason",
    "VerdictOperand",
    "seal",
]


class RefusalReason(StrEnum):
    """Why an observation was not allowed into deterministic arithmetic."""

    NOT_QUALIFIED = "NOT_QUALIFIED"
    ADVISORY = "ADVISORY"
    UNKNOWN_UNIT = "UNKNOWN_UNIT"
    NO_VALUE = "NO_VALUE"


@dataclass(frozen=True, slots=True)
class GateRefusal:
    """An explicit abstention returned instead of an invented operand."""

    reason: RefusalReason
    detail: str


def _evidence_ref(observation: CanonicalObservation) -> str:
    """Render exact page and polygon provenance as deterministic JSON."""

    return json.dumps(
        {
            "document_version_id": str(observation.document_version_id),
            "page": observation.page,
            "polygon": [[str(point.x), str(point.y)] for point in observation.polygon.points],
            "space": observation.polygon.space,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def seal(observation: CanonicalObservation, name: str) -> VerdictOperand | GateRefusal:
    """Seal qualified authoritative evidence, or state why it cannot be sealed.

    The authored ``Measurement`` is passed through unchanged.  Defence-in-depth
    checks for an absent value and unknown unit protect the boundary even if an
    observation arrived from corrupt or stale deserialised data that bypassed the
    canonical model's constructor.
    """

    if not isinstance(observation, CanonicalObservation):
        raise TypeError("observation must be a CanonicalObservation")
    if not isinstance(name, str):
        raise TypeError("name must be a string")
    if not name.strip():
        raise ValueError("a sealed verdict operand must be named")

    if observation.status not in QUALIFIED_STATUSES:
        return GateRefusal(
            RefusalReason.NOT_QUALIFIED,
            f"evidence status {observation.status.value} is not qualified for a verdict",
        )
    if observation.authority is Authority.ADVISORY:
        return GateRefusal(
            RefusalReason.ADVISORY,
            "advisory evidence may guide review but cannot enter a verdict",
        )

    value = cast(object, observation.value)
    if value is None:
        return GateRefusal(
            RefusalReason.NO_VALUE,
            "the observation has no value to seal",
        )
    if not isinstance(value, Measurement) or not isinstance(value.unit, Unit):
        return GateRefusal(
            RefusalReason.UNKNOWN_UNIT,
            "the observation does not carry a recognised authored unit",
        )

    return VerdictOperand(
        name=name,
        value=value,
        status=observation.status,
        source=observation.document_role.value,
        evidence_ref=_evidence_ref(observation),
    )
