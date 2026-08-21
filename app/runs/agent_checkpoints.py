"""PostgreSQL adapter for interrupt-safe agent node side effects.

The reservation transaction must commit before the caller spends. Completion then writes the
candidate, immutable invocation, and claim links in one caller-owned transaction. This adapter never
commits, so callers can induce rollback and neither half-written evidence nor a false completion
survives.

Source: issue #247. Verification: tests/extraction/agent/test_checkpoints.py.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.evidence import ObservationCandidate
from app.models.runs import (
    AgentNodeInvocationClaim,
    AgentNodeInvocationState,
    ModelInvocation,
)
from app.runs.invocations import record
from extraction.models.invocations import InvocationRecord


@dataclass(frozen=True, slots=True)
class DatabaseClaim:
    """Persisted reservation and whether this transaction created it."""

    row: AgentNodeInvocationClaim
    created: bool


def claim(session: Session, *, node_invocation_key: str, extraction_run_id: UUID) -> DatabaseClaim:
    """Reserve one stable node identity; a duplicate returns the existing claim."""

    row = AgentNodeInvocationClaim(
        node_invocation_key=node_invocation_key,
        extraction_run_id=extraction_run_id,
        status=AgentNodeInvocationState.IN_PROGRESS.value,
        model_invocation_id=None,
        candidate_id=None,
    )
    savepoint = session.begin_nested()
    try:
        session.add(row)
        session.flush()
    except IntegrityError:
        savepoint.rollback()
        prior = session.scalar(
            select(AgentNodeInvocationClaim).where(
                AgentNodeInvocationClaim.node_invocation_key == node_invocation_key
            )
        )
        if prior is None:
            raise
        return DatabaseClaim(prior, False)
    else:
        savepoint.commit()
        return DatabaseClaim(row, True)


def complete(
    session: Session,
    *,
    reserved: AgentNodeInvocationClaim,
    invocation: InvocationRecord,
    candidate: ObservationCandidate,
) -> ModelInvocation:
    """Atomically persist a successful result and mark its reservation complete."""

    if reserved.status != AgentNodeInvocationState.IN_PROGRESS.value:
        raise ValueError("only an in-progress claim can be completed")
    if invocation.extraction_run_id != reserved.extraction_run_id:
        raise ValueError("invocation and claim must belong to the same extraction run")
    if candidate.extraction_run_id != reserved.extraction_run_id:
        raise ValueError("candidate and claim must belong to the same extraction run")
    if invocation.outcome != "ok":
        raise ValueError("a completed claim requires an invocation with outcome 'ok'")
    session.add(candidate)
    stored = record(
        session,
        replace(
            invocation,
            node_invocation_key=reserved.node_invocation_key,
            candidate_id=candidate.id,
        ),
    )
    reserved.status = AgentNodeInvocationState.COMPLETED.value
    reserved.model_invocation_id = stored.id
    reserved.candidate_id = candidate.id
    session.flush()
    return stored


def fail(
    session: Session,
    *,
    reserved: AgentNodeInvocationClaim,
    invocation: InvocationRecord,
) -> ModelInvocation:
    """Record a failed paid attempt and make every resume abstain."""

    if reserved.status != AgentNodeInvocationState.IN_PROGRESS.value:
        raise ValueError("only an in-progress claim can fail")
    if invocation.extraction_run_id != reserved.extraction_run_id:
        raise ValueError("invocation and claim must belong to the same extraction run")
    if invocation.outcome == "ok":
        raise ValueError("a failed claim cannot carry an invocation with outcome 'ok'")
    stored = record(
        session,
        replace(invocation, node_invocation_key=reserved.node_invocation_key, candidate_id=None),
    )
    reserved.status = AgentNodeInvocationState.FAILED.value
    reserved.model_invocation_id = stored.id
    session.flush()
    return stored
