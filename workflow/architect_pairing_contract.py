"""What the vendor-vs-architect check reads about one countertop row's pairing (#1053, #1054).

The pairing (#1053) decides which architect span measures the same physical thing as which vendor
piece. The check (#1054) only reads the effective result through these types, so the two can be
built and tested separately. The effective record is the reviewer's latest decision when there is
one, otherwise the latest automatic record.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

__all__ = ["EffectivePair", "EffectivePairing", "PairingSource"]

type PairingSource = Literal["code", "both-ais", "reviewer", "none"]
"""Who decided the pairing. `both-ais` is AI-only information: a PASS resting on it needs a
reviewer's confirmation before it counts (the walls rule, Decision log 2026-10-08)."""


@dataclass(frozen=True, slots=True)
class EffectivePair:
    """One architect span and the vendor piece(s) it measures the same thing as."""

    kind: Literal["piece", "overall"]
    architect_candidate_id: UUID
    """The architect-side ObservationCandidate (unheld, on the drawn outline)."""
    vendor_slot_indices: tuple[int, ...]
    """The vendor row's `slot:<i>` indices, contiguous; empty for the overall."""


@dataclass(frozen=True, slots=True)
class EffectivePairing:
    """The pairing that counts for one vendor countertop row."""

    record_id: UUID
    source: PairingSource
    status: str
    """`paired`, `ambiguous`, `no_fit`, `no_scale`, `nothing_comparable`, `ais-disagree`,
    `ais-refused`, or `reviewer` for a person's decision."""
    pairs: tuple[EffectivePair, ...]
    reasons: tuple[str, ...]
    """Plain English: why this pairing, and why any architect span was left out."""
