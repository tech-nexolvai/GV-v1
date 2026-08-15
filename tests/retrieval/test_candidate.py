"""Structural advisory-boundary tests for issue #173."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields
from decimal import Decimal
from enum import Enum
from uuid import UUID, uuid4

import pytest

from retrieval.candidate import Lane, MatchCandidate


def _candidate(**changes: object) -> MatchCandidate:
    values: dict[str, object] = {
        "left_item_id": uuid4(),
        "right_item_id": uuid4(),
        "lane": Lane.TRIGRAM,
        "score": Decimal("0.91"),
    }
    values.update(changes)
    return MatchCandidate(**values)  # type: ignore[arg-type]


def test_candidate_records_both_items_lane_and_diagnostic_score() -> None:
    """Input: trigram proposal. Outcome: exact metadata. Why: lanes have different meaning."""

    left_id = uuid4()
    right_id = uuid4()
    candidate = _candidate(left_item_id=left_id, right_item_id=right_id)

    assert candidate.left_item_id == left_id
    assert candidate.right_item_id == right_id
    assert candidate.lane is Lane.TRIGRAM
    assert candidate.score == Decimal("0.91")


def test_all_design_lanes_are_closed_enum_members() -> None:
    """Input: Lane enum. Outcome: eight named routes. Why: origin must never be free text."""

    assert issubclass(Lane, Enum)
    assert {lane.value for lane in Lane} == {
        "exact",
        "alias",
        "metadata",
        "geometry",
        "trigram",
        "lexical",
        "dense",
        "fusion",
    }


def test_match_candidate_has_no_approval_or_verdict_surface() -> None:
    """Input: candidate fields. Outcome: no authority field. Why: retrieval cannot approve."""

    field_names = {field.name for field in fields(MatchCandidate)}
    forbidden = {"approved", "approval", "status", "outcome", "verdict", "evidence_status"}

    assert field_names == {"left_item_id", "right_item_id", "lane", "score"}
    assert field_names.isdisjoint(forbidden)
    assert not hasattr(MatchCandidate, "approve")


def test_candidate_cannot_be_constructed_already_approved() -> None:
    """Input: approved=True. Outcome: TypeError. Why: approval must create another type."""

    with pytest.raises(TypeError, match="unexpected keyword argument 'approved'"):
        MatchCandidate(
            left_item_id=uuid4(),
            right_item_id=uuid4(),
            lane=Lane.EXACT,
            score=None,
            approved=True,  # type: ignore[call-arg]
        )


def test_candidate_is_immutable_after_construction() -> None:
    """Input: lane reassignment. Outcome: FrozenInstanceError. Why: provenance cannot change."""

    candidate = _candidate()
    with pytest.raises(FrozenInstanceError):
        candidate.lane = Lane.EXACT  # type: ignore[misc]


@pytest.mark.parametrize(
    ("changes", "message", "reason"),
    [
        ({"left_item_id": "left"}, "left_item_id must be a UUID", "untyped left identity"),
        ({"right_item_id": "right"}, "right_item_id must be a UUID", "untyped right identity"),
        ({"lane": "trigram"}, "lane must be a Lane", "free-text provenance"),
        ({"score": 0.91}, "score must be a Decimal", "binary floating-point score"),
    ],
)
def test_permissive_candidate_values_are_rejected(
    changes: dict[str, object], message: str, reason: str
) -> None:
    """Input: permissive field. Outcome: TypeError. Why: advisory provenance stays typed."""

    del reason
    with pytest.raises(TypeError, match=message):
        _candidate(**changes)


@pytest.mark.parametrize("score", [None, Decimal(0), Decimal(1), Decimal("-2.5")])
def test_score_is_optional_diagnostic_metadata_without_an_invented_range(
    score: Decimal | None,
) -> None:
    """Input: exact optional score. Outcome: preserved. Why: each lane owns score semantics."""

    assert _candidate(score=score).score == score


def test_ids_are_uuid_surrogate_keys() -> None:
    """Input: UUID text parsed first. Outcome: UUID retained. Why: malformed joins fail early."""

    identifier = UUID("12345678-1234-5678-1234-567812345678")
    candidate = _candidate(left_item_id=identifier)
    assert candidate.left_item_id is identifier
