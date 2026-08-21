"""Pure checkpoint and idempotency contracts for interrupt-safe agent nodes.

LangGraph checkpoints record execution position only. They do **not** make a paid model call
atomic with an evidence write. The application adapter owns that separate database transaction.
A node first reserves a stable key; a resumed in-progress node abstains, while a completed node
reuses its recorded result.

Successful terminal runs delete their checkpoints immediately. Failed terminal runs retain them
for ``FAILED_CHECKPOINT_GRACE`` so an operator can inspect the failure, then delete them. Seven days
is the admin-approved operational window; it is unrelated to verdict arithmetic.

Source: backend proposal sections 6.2 and 9.2, issue #247.
Verification: ``tests/extraction/agent/test_checkpoints.py``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres import PostgresSaver

from extraction.agent.outcomes import AgentAbstention

FAILED_CHECKPOINT_GRACE = timedelta(days=7)
KEY_PREFIX = "sha256:"


class InvocationClaimState(StrEnum):
    """State visible to a node deciding whether a side effect may run."""

    NEW = "new"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class InvocationClaim[ResultT]:
    """Pure claim result supplied by the application persistence adapter."""

    key: str
    state: InvocationClaimState
    result: ResultT | None = None

    def __post_init__(self) -> None:
        if not self.key.startswith(KEY_PREFIX) or len(self.key) != 71:
            raise ValueError("key must be a sha256-prefixed digest")
        if self.state is InvocationClaimState.COMPLETED and self.result is None:
            raise ValueError("a completed claim must carry its recorded result")
        if self.state is not InvocationClaimState.COMPLETED and self.result is not None:
            raise ValueError("only a completed claim may carry a result")


def node_invocation_key(
    *,
    extraction_run_id: UUID,
    node: str,
    region_id: str,
    model_id: str,
    prompt_id: str,
    template_id: str,
    crop_artifact_id: UUID | None,
) -> str:
    """Return a stable node identity containing no clock, randomness or salted ``hash()``."""

    values: Mapping[str, object] = {
        "crop_artifact_id": None if crop_artifact_id is None else str(crop_artifact_id),
        "extraction_run_id": str(extraction_run_id),
        "model_id": model_id,
        "node": node,
        "prompt_id": prompt_id,
        "region_id": region_id,
        "template_id": template_id,
    }
    for name in ("node", "region_id", "model_id", "prompt_id", "template_id"):
        value = values[name]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string")
    payload = json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
    return f"{KEY_PREFIX}{hashlib.sha256(payload).hexdigest()}"


type NodeFn[ResultT] = Callable[[], ResultT]


def guarded_node[ResultT](
    fn: NodeFn[ResultT],
) -> Callable[[InvocationClaim[ResultT], str], ResultT | AgentAbstention]:
    """Permit a new claim once, reuse completion, and abstain for unresolved prior work."""

    def run(claim: InvocationClaim[ResultT], region_id: str) -> ResultT | AgentAbstention:
        if claim.state is InvocationClaimState.COMPLETED:
            if claim.result is None:
                raise AssertionError("completed claim lost its recorded result")
            return claim.result
        if claim.state is InvocationClaimState.NEW:
            return fn()
        reason = (
            "a prior node invocation is still in progress; the paid call was not repeated"
            if claim.state is InvocationClaimState.IN_PROGRESS
            else "the prior node invocation failed; manual review is required"
        )
        return AgentAbstention(region_id=region_id, reason=reason)

    return run


@contextmanager
def checkpointer(connection_string: str) -> Iterator[BaseCheckpointSaver[str]]:
    """Configure LangGraph's PostgreSQL saver from an explicit connection string."""

    if not isinstance(connection_string, str) or not connection_string.strip():
        raise ValueError("connection_string must be a non-empty string")
    with PostgresSaver.from_conn_string(connection_string) as saver:
        saver.setup()
        yield saver


class CheckpointCleaner(Protocol):
    """The deletion surface shared by PostgresSaver and test doubles."""

    def delete_thread(self, thread_id: str) -> None:
        """Delete every checkpoint belonging to one graph run."""


def cleanup_checkpoints(
    saver: CheckpointCleaner,
    *,
    thread_id: str,
    terminal: bool,
    failed: bool,
    finished_at: datetime | None,
    now: datetime,
) -> bool:
    """Apply the approved terminal cleanup policy and report whether deletion occurred."""

    if not thread_id.strip():
        raise ValueError("thread_id must be a non-empty string")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if not terminal:
        return False
    if not failed:
        saver.delete_thread(thread_id)
        return True
    if finished_at is None or finished_at.tzinfo is None or finished_at.utcoffset() is None:
        raise ValueError("a failed terminal run requires a timezone-aware finished_at")
    if now - finished_at < FAILED_CHECKPOINT_GRACE:
        return False
    saver.delete_thread(thread_id)
    return True
