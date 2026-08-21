"""Durable execution records from workflow dispatch through model invocation.

Every row retains the identity of the run that produced the next one. Model calls are
append-only records, including rejected and failed calls, so cost and provenance reports
do not silently omit unsuccessful work.

Source: ``DESIGN_PLATFORM.md`` section 3.1, ``DESIGN_AI.md`` section 4.5, and issue #194.
Verification: ``tests/db/test_run_models.py``.
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Immutable, TimestampedUUID


class ModelInvocationOutcome(StrEnum):
    """Closed outcomes retained for every attempted model call.

    `FAILED` is the catch-all for a call that did not come back with an answer and was neither a
    timeout nor a refusal — a transport error, a malformed response, a provider 500. Without it such a
    call could not be stored at all: the `CHECK` constraint rejected the insert, so the record of a
    paid attempt was simply lost (#313). Two shipped stories rely on it existing — `E2.3` (#251) says
    every call is recorded including failures, and `F5.3` (#266) attributes spend from these rows,
    where a missing failure understates the bill in the direction that flatters us.

    Adding a member here is only half the change. The database enforces this set through a `CHECK`
    constraint installed by a **migration**, and the migration hardcodes its values — so a new member
    needs a new migration too. `tests/db/test_run_models.py` compares the two and fails if they
    disagree, because that is exactly how `failed` came to be missing for as long as it was.
    """

    OK = "ok"
    REJECTED = "rejected"
    TIMEOUT = "timeout"
    REFUSED = "refused"
    FAILED = "failed"


class AgentNodeInvocationState(StrEnum):
    """Durable state of the claim that prevents a repeated paid node call."""

    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


#: The enum rendered as SQL literals, for the ORM's own `CHECK` constraint below.
#:
#: This is generated from the enum, so the two can never disagree — which is why the drift that
#: mattered was never here. It was between the enum and the migration, and only a test that reads what
#: the migrations install can see it.
MODEL_INVOCATION_OUTCOMES = ", ".join(f"'{outcome.value}'" for outcome in ModelInvocationOutcome)


class WorkflowRun(Base, TimestampedUUID):
    """One workflow execution for an immutable package revision."""

    __tablename__ = "workflow_runs"

    package_revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("package_revisions.id", ondelete="RESTRICT"), index=True
    )
    engine_run_id: Mapped[str] = mapped_column(String(500), unique=True)

    __table_args__ = (CheckConstraint("engine_run_id <> ''", name="workflow_engine_run_id"),)


class TaskRun(Base, TimestampedUUID):
    """One idempotent task attempt belonging to a workflow run."""

    __tablename__ = "task_runs"

    workflow_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="RESTRICT"), index=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(500), unique=True)
    task_type: Mapped[str] = mapped_column(String(200))
    attempt: Mapped[int]
    outcome: Mapped[str] = mapped_column(String(100))

    __table_args__ = (
        CheckConstraint("idempotency_key <> ''", name="task_run_idempotency_key"),
        CheckConstraint("task_type <> ''", name="task_run_task_type"),
        CheckConstraint("outcome <> ''", name="task_run_outcome"),
    )


class ExtractionRun(Base, TimestampedUUID):
    """A version-pinned extractor execution within a task run."""

    __tablename__ = "extraction_runs"

    task_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("task_runs.id", ondelete="RESTRICT"), index=True
    )
    extractor: Mapped[str] = mapped_column(String(200))
    extractor_version: Mapped[str] = mapped_column(String(200))
    config_hash: Mapped[str] = mapped_column(String(200))

    __table_args__ = (
        CheckConstraint("extractor <> ''", name="extraction_run_extractor"),
        CheckConstraint("extractor_version <> ''", name="extraction_run_extractor_version"),
        CheckConstraint("config_hash <> ''", name="extraction_run_config_hash"),
    )


class ModelInvocation(Base, TimestampedUUID, Immutable):
    """Append-only record of a model call, whether it succeeded or not."""

    __tablename__ = "model_invocations"

    extraction_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("extraction_runs.id", ondelete="RESTRICT"), index=True
    )
    model_id: Mapped[str] = mapped_column(String(300))
    prompt_id: Mapped[str] = mapped_column(String(300))
    template_id: Mapped[str] = mapped_column(String(300))
    # Evidence artifacts land in C1.6. Retain their identity now without incorrectly
    # treating a generated crop as an uploaded source artifact.
    crop_artifact_id: Mapped[UUID | None] = mapped_column(default=None)
    node_invocation_key: Mapped[str | None] = mapped_column(String(71), unique=True, default=None)
    candidate_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("observation_candidates.id", ondelete="RESTRICT"), unique=True, default=None
    )
    input_tokens: Mapped[int]
    output_tokens: Mapped[int]
    cost_micros: Mapped[int]
    latency_ms: Mapped[int]
    outcome: Mapped[str] = mapped_column(String(32))

    __table_args__ = (
        CheckConstraint("model_id <> ''", name="model_invocation_model_id"),
        CheckConstraint("prompt_id <> ''", name="model_invocation_prompt_id"),
        CheckConstraint("template_id <> ''", name="model_invocation_template_id"),
        CheckConstraint(
            "node_invocation_key IS NULL OR node_invocation_key ~ '^sha256:[0-9a-f]{64}$'",
            name="model_invocation_node_key",
        ),
        CheckConstraint("input_tokens >= 0", name="model_invocation_input_tokens"),
        CheckConstraint("output_tokens >= 0", name="model_invocation_output_tokens"),
        CheckConstraint("cost_micros >= 0", name="model_invocation_cost"),
        CheckConstraint("latency_ms >= 0", name="model_invocation_latency"),
        CheckConstraint(
            f"outcome IN ({MODEL_INVOCATION_OUTCOMES})",
            name="model_invocation_outcome",
        ),
        Index("ix_model_invocations_created_at", "created_at"),
    )


class AgentNodeInvocationClaim(Base, TimestampedUUID):
    """Mutable reservation; final invocation and candidate records remain append-only."""

    __tablename__ = "agent_node_invocation_claims"

    node_invocation_key: Mapped[str] = mapped_column(String(71), unique=True)
    extraction_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("extraction_runs.id", ondelete="RESTRICT"), index=True
    )
    state: Mapped[str] = mapped_column(String(32))
    model_invocation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("model_invocations.id", ondelete="RESTRICT"), unique=True, default=None
    )
    candidate_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("observation_candidates.id", ondelete="RESTRICT"), unique=True, default=None
    )

    __table_args__ = (
        CheckConstraint(
            "node_invocation_key ~ '^sha256:[0-9a-f]{64}$'",
            name="agent_node_invocation_claim_key",
        ),
        CheckConstraint(
            "state IN ('in_progress', 'completed', 'failed')",
            name="agent_node_invocation_claim_state",
        ),
        CheckConstraint(
            "(state = 'in_progress' AND model_invocation_id IS NULL AND candidate_id IS NULL) OR "
            "(state = 'completed' AND model_invocation_id IS NOT NULL AND candidate_id IS NOT NULL) OR "
            "(state = 'failed' AND model_invocation_id IS NOT NULL AND candidate_id IS NULL)",
            name="agent_node_invocation_claim_completion",
        ),
    )
