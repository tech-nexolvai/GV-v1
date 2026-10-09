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

type PairingSource = Literal["code+ais", "code", "both-ais", "reviewer", "none"]
"""Who decided the pairing. Two independent judgments are needed for an automatic result:
`code+ais` means code's drawn-position pairing AND both AIs' identical pairing (including what each
architect dimension measures) agree. `code` alone or `both-ais` alone is one judgment: any result
resting on it, PASS or FAIL, needs a reviewer's confirmation of the pairing before it counts.
`reviewer` is a person's decision. `none` means nothing was paired."""


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
    architect_measures: tuple[tuple[UUID, str], ...] = ()
    """What both AIs agreed each architect dimension on the row's page measures (architect
    candidate id, kind such as `countertop`, `cabinet_run`, `blocking`, `fixture_centre`), when
    they agreed; used for the plain "not compared" reason."""
