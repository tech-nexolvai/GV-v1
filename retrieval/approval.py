"""Promote advisory match candidates through exactly two recorded approval paths.

A lane or score never grants authority. Deterministic approval repeats the exact normalized-
identifier check, while human approval requires a named reviewer. Revocation is a new immutable
record; it never edits or erases the approval that affected an earlier review.

Source: ``docs/DESIGN_EXTRACTION.md`` section 8 and issue #190.
Verification: ``tests/retrieval/test_approval.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from retrieval.candidate import MatchCandidate
from retrieval.identifiers import NormalizedIdentifier

type ApprovalSource = Literal["deterministic", "human"]


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _aware(value: object, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


@dataclass(frozen=True, slots=True)
class ApprovedMatch:
    """A recorded assertion derived from, but structurally different from, a candidate."""

    candidate: MatchCandidate
    source: ApprovalSource
    approved_by: str
    approved_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.candidate, MatchCandidate):
            raise TypeError("candidate must be a MatchCandidate")
        if self.source not in {"deterministic", "human"}:
            raise ValueError("source must be 'deterministic' or 'human'")
        _text(self.approved_by, "approved_by")
        _aware(self.approved_at, "approved_at")


@dataclass(frozen=True, slots=True)
class MatchRevocation:
    """Append-only record that an earlier approval must no longer be used."""

    approval: ApprovedMatch
    revoked_by: str
    reason: str
    revoked_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.approval, ApprovedMatch):
            raise TypeError("approval must be an ApprovedMatch")
        _text(self.revoked_by, "revoked_by")
        _text(self.reason, "reason")
        _aware(self.revoked_at, "revoked_at")
        if self.revoked_at < self.approval.approved_at:
            raise ValueError("revoked_at must not precede approved_at")


class DeterministicMatchRejected(ValueError):
    """The supplied identifiers do not independently prove the proposed correspondence."""


def approve_exact_identifier(
    candidate: MatchCandidate,
    *,
    left_identifier: NormalizedIdentifier,
    right_identifier: NormalizedIdentifier,
    approved_at: datetime,
) -> ApprovedMatch:
    """Approve only after repeating exact normalized-identifier agreement.

    The candidate's lane and score are deliberately ignored. A fuzzy lane may have surfaced the pair,
    but only the equality checked here supplies deterministic authority.
    """

    if not isinstance(candidate, MatchCandidate):
        raise TypeError("candidate must be a MatchCandidate")
    if not isinstance(left_identifier, NormalizedIdentifier):
        raise TypeError("left_identifier must be a NormalizedIdentifier")
    if not isinstance(right_identifier, NormalizedIdentifier):
        raise TypeError("right_identifier must be a NormalizedIdentifier")
    if left_identifier.canonical != right_identifier.canonical:
        raise DeterministicMatchRejected("normalized identifiers do not agree exactly")
    return ApprovedMatch(
        candidate=candidate,
        source="deterministic",
        approved_by="exact_identifier_agreement",
        approved_at=approved_at,
    )


def approve_by_human(
    candidate: MatchCandidate, *, reviewer: str, approved_at: datetime
) -> ApprovedMatch:
    """Record a named reviewer's approval of any advisory candidate."""

    return ApprovedMatch(
        candidate=candidate,
        source="human",
        approved_by=reviewer,
        approved_at=approved_at,
    )


def revoke_match(
    approval: ApprovedMatch, *, revoked_by: str, reason: str, revoked_at: datetime
) -> MatchRevocation:
    """Append a revocation record without changing the original approval."""

    return MatchRevocation(approval, revoked_by, reason, revoked_at)
