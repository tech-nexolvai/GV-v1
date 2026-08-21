"""Interrupt-safety tests for issue #247.

The pure tests explain input, expected outcome and why. PostgreSQL integration is covered by the
model/migration suite when ``DATABASE_URL`` is available; this file also pins the schema shape so a
missing claim or candidate link cannot disappear silently on CI.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from app.db.base import Base
from extraction.agent.checkpoints import (
    FAILED_CHECKPOINT_GRACE,
    InvocationClaim,
    InvocationClaimState,
    cleanup_checkpoints,
    guarded_node,
    node_invocation_key,
)
from extraction.agent.outcomes import AgentAbstention


def _key() -> str:
    return node_invocation_key(
        extraction_run_id=UUID("00000000-0000-0000-0000-000000000001"),
        node="vlm_read",
        region_id="region-7",
        model_id="nova-v1",
        prompt_id="prompt-v2",
        template_id="template-v3",
        crop_artifact_id=UUID("00000000-0000-0000-0000-000000000002"),
    )


def test_node_key_is_stable_and_structurally_sha256() -> None:
    """Input: identical task identity twice. Output: identical digest; no clock/randomness."""

    assert _key() == _key()
    assert _key().startswith("sha256:")
    assert len(_key()) == 71


def test_a_new_claim_executes_the_side_effect_once() -> None:
    """Input: newly-created claim. Output: handler executes because this caller owns it."""

    calls: list[str] = []

    def paid_call() -> str:
        calls.append("called")
        return "reading"

    result = guarded_node(paid_call)(InvocationClaim(_key(), InvocationClaimState.NEW), "region-7")

    assert result == "reading"
    assert calls == ["called"]


def test_a_completed_claim_reuses_its_result_without_calling_again() -> None:
    """Input: completed prior invocation. Output: recorded reading and zero new paid calls."""

    calls: list[str] = []
    result = guarded_node(lambda: calls.append("called") or "new")(
        InvocationClaim(_key(), InvocationClaimState.COMPLETED, "recorded"), "region-7"
    )

    assert result == "recorded"
    assert calls == []


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        (InvocationClaimState.IN_PROGRESS, "still in progress"),
        (InvocationClaimState.FAILED, "failed"),
    ],
)
def test_unresolved_prior_claim_abstains_instead_of_spending_again(
    state: InvocationClaimState, reason: str
) -> None:
    """Input: prior unresolved/failed claim. Output: REVIEW-bound abstention, no call."""

    calls: list[str] = []
    result = guarded_node(lambda: calls.append("called") or "new")(
        InvocationClaim(_key(), state), "region-7"
    )

    assert isinstance(result, AgentAbstention)
    assert reason in result.reason
    assert result.requires_review is True
    assert calls == []


class Cleaner:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete_thread(self, thread_id: str) -> None:
        self.deleted.append(thread_id)


def test_successful_terminal_checkpoint_is_deleted_immediately() -> None:
    """Input: successful terminal run. Output: deletion now because replay is unnecessary."""

    cleaner = Cleaner()
    removed = cleanup_checkpoints(
        cleaner,
        thread_id="thread-1",
        terminal=True,
        failed=False,
        finished_at=None,
        now=datetime(2026, 8, 21, tzinfo=UTC),
    )

    assert removed is True
    assert cleaner.deleted == ["thread-1"]


def test_failed_checkpoint_is_kept_for_seven_days_then_deleted() -> None:
    """Input: failed run around the grace boundary. Output: retained before, deleted at day 7."""

    cleaner = Cleaner()
    finished = datetime(2026, 8, 1, tzinfo=UTC)

    assert (
        cleanup_checkpoints(
            cleaner,
            thread_id="thread-1",
            terminal=True,
            failed=True,
            finished_at=finished,
            now=finished + FAILED_CHECKPOINT_GRACE - timedelta(seconds=1),
        )
        is False
    )
    assert cleaner.deleted == []
    assert (
        cleanup_checkpoints(
            cleaner,
            thread_id="thread-1",
            terminal=True,
            failed=True,
            finished_at=finished,
            now=finished + FAILED_CHECKPOINT_GRACE,
        )
        is True
    )
    assert cleaner.deleted == ["thread-1"]


def test_schema_has_a_separate_claim_and_direct_candidate_link() -> None:
    """Input: ORM metadata. Output: mutable claim plus append-only invocation attribution."""

    claim = Base.metadata.tables["agent_node_invocation_claims"]
    invocation = Base.metadata.tables["model_invocations"]

    assert {"node_invocation_key", "state", "model_invocation_id", "candidate_id"} <= set(
        claim.columns.keys()
    )
    assert invocation.columns["node_invocation_key"].unique is True
    assert invocation.columns["candidate_id"].unique is True


def test_completed_claim_without_recorded_result_is_refused() -> None:
    """Input: impossible completed claim. Output: refusal before a resume can return a gap."""

    with pytest.raises(ValueError, match="must carry"):
        InvocationClaim[str](_key(), InvocationClaimState.COMPLETED)
