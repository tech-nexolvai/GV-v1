"""Deterministic, human and revocation paths for issue #190."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from retrieval.approval import (
    ApprovedMatch,
    DeterministicMatchRejected,
    MatchRevocation,
    approve_by_human,
    approve_exact_identifier,
    revoke_match,
)
from retrieval.candidate import Lane, MatchCandidate
from retrieval.identifiers import normalize_identifier

NOW = datetime(2026, 8, 22, 9, 0, tzinfo=UTC)


def _candidate(
    *, lane: Lane = Lane.DENSE, score: Decimal | None = Decimal("0.999")
) -> MatchCandidate:
    return MatchCandidate(UUID(int=1), UUID(int=2), lane, score)


def test_exact_identifier_check_can_approve_without_trusting_lane_or_score() -> None:
    """Input: dense high-score proposal plus equal IDs. Output: approval. Why: equality decides."""

    candidate = _candidate()
    approval = approve_exact_identifier(
        candidate,
        left_identifier=normalize_identifier("PL-02"),
        right_identifier=normalize_identifier("pl_02"),
        approved_at=NOW,
    )

    assert approval == ApprovedMatch(candidate, "deterministic", "exact_identifier_agreement", NOW)
    assert approval is not candidate


@pytest.mark.parametrize("lane", list(Lane))
def test_lane_alone_never_approves_disagreeing_identifiers(lane: Lane) -> None:
    """Input: any lane with unequal IDs. Output: refusal. Why: lane has no authority."""

    with pytest.raises(DeterministicMatchRejected, match="do not agree"):
        approve_exact_identifier(
            _candidate(lane=lane),
            left_identifier=normalize_identifier("X-223"),
            right_identifier=normalize_identifier("X-233"),
            approved_at=NOW,
        )


@pytest.mark.parametrize("score", [None, Decimal(0), Decimal(1), Decimal(999999)])
def test_score_never_changes_a_deterministic_refusal(score: Decimal | None) -> None:
    """Input: every diagnostic score shape. Output: refusal. Why: confidence is not authority."""

    with pytest.raises(DeterministicMatchRejected):
        approve_exact_identifier(
            _candidate(score=score),
            left_identifier=normalize_identifier("CAB-1"),
            right_identifier=normalize_identifier("CAB-2"),
            approved_at=NOW,
        )


def test_named_human_can_approve_an_advisory_candidate() -> None:
    """Input: candidate and reviewer. Output: human approval. Why: this is the second path."""

    approval = approve_by_human(_candidate(), reviewer="keyur", approved_at=NOW)

    assert approval.source == "human"
    assert approval.approved_by == "keyur"
    assert approval.approved_at == NOW


@pytest.mark.parametrize("reviewer", ["", "   "])
def test_an_unnamed_human_cannot_approve(reviewer: str) -> None:
    """Input: blank reviewer. Output: refusal. Why: every human decision is attributable."""

    with pytest.raises(ValueError, match="reviewer|approved_by"):
        approve_by_human(_candidate(), reviewer=reviewer, approved_at=NOW)


def test_revocation_is_a_new_record_and_preserves_the_approval() -> None:
    """Input: approved match later rejected. Output: append-only revocation. Why: history remains."""

    approval = approve_by_human(_candidate(), reviewer="keyur", approved_at=NOW)
    revocation = revoke_match(
        approval,
        revoked_by="anant",
        reason="shop item was reassigned after review",
        revoked_at=NOW + timedelta(hours=1),
    )

    assert revocation == MatchRevocation(
        approval,
        "anant",
        "shop item was reassigned after review",
        NOW + timedelta(hours=1),
    )
    assert revocation.approval is approval
    assert not hasattr(approval, "revoked_at")


def test_revocation_cannot_predate_approval() -> None:
    """Input: reversed timestamps. Output: refusal. Why: the audit timeline must be possible."""

    approval = approve_by_human(_candidate(), reviewer="keyur", approved_at=NOW)

    with pytest.raises(ValueError, match="must not precede"):
        revoke_match(
            approval,
            revoked_by="anant",
            reason="incorrect association",
            revoked_at=NOW - timedelta(seconds=1),
        )


def test_approval_and_revocation_are_immutable() -> None:
    """Input: attempted overwrite. Output: frozen error. Why: decisions are append-only."""

    approval = approve_by_human(_candidate(), reviewer="keyur", approved_at=NOW)
    revocation = revoke_match(approval, revoked_by="anant", reason="incorrect", revoked_at=NOW)

    with pytest.raises(FrozenInstanceError):
        approval.approved_by = "somebody-else"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        revocation.reason = "rewritten"  # type: ignore[misc]


def test_approval_timestamps_must_be_timezone_aware() -> None:
    """Input: naive timestamp. Output: refusal. Why: audit events need one known timeline."""

    with pytest.raises(ValueError, match="timezone-aware"):
        approve_by_human(
            _candidate(),
            reviewer="keyur",
            approved_at=datetime(2026, 8, 22, 9, 0),  # noqa: DTZ001 - refusal fixture
        )
