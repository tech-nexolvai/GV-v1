"""Durable execution records from workflow dispatch through model invocation.

Every row retains the identity of the run that produced the next one. Model calls are
append-only records, including rejected and failed calls, so cost and provenance reports
do not silently omit unsuccessful work.

Source: ``DESIGN_PLATFORM.md`` section 3.1, ``DESIGN_AI.md`` section 4.5, and issue #194.
Verification: ``tests/db/test_run_models.py``.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Final
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    ColumnElement,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Immutable, TimestampedUUID, UTCDateTime, utc_now


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
    BUDGET_REFUSED = "budget_refused"
    """Not sent: the drawing set's AI budget would have been exceeded (#1206). No answer, no cost."""
    ACCOUNT_REFUSED = "account_refused"
    """The provider refused the account, unserved (HTTP 402: out of credit, #1206). No answer, no
    cost; the reading stops and waits for a top-up."""


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

    dpi: Mapped[int | None] = mapped_column(default=None)
    """The resolution this run read at, which is the frame its candidates' polygons are in (#530).

    On the run rather than the page, because it is a property of the reading and not of the drawing:
    the same page read at 150 and at 300 produces candidates in two different image spaces, and a
    transform rebuilt with the wrong one lands every polygon in the wrong place. `config_hash`
    already carries it as text, and parsing `dpi=150` back out of an identity string would be
    reading a value out of a fingerprint.

    Null for runs recorded before #530.
    """

    __table_args__ = (
        CheckConstraint("extractor <> ''", name="extraction_run_extractor"),
        CheckConstraint("extractor_version <> ''", name="extraction_run_extractor_version"),
        CheckConstraint("config_hash <> ''", name="extraction_run_config_hash"),
        CheckConstraint("dpi IS NULL OR dpi > 0", name="extraction_run_dpi"),
    )


#: Why a drawing, or one of its pages, could not be read.
#:
#: A closed vocabulary rather than free text, and the check constraint below ties each value to
#: whether a page index is present — so "the document would not parse" can never be filed against a
#: page, and "this page would not parse" can never be filed without one.
#:
#: `document_digest_mismatch` is a different kind of failure from the other two and belongs here
#: anyway: the file is readable, and it is not the file that was uploaded. Recording it beside them
#: is what lets one query answer "which drawings did this run decline to read, and why" — and the
#: alternative, a separate table, would leave a reader having to know there were two places to look.
FAILURE_REASONS: Final = ("document_unreadable", "page_unreadable", "document_digest_mismatch")

_FAILURE_REASON_VALUES: Final = ", ".join(f"'{reason}'" for reason in FAILURE_REASONS)


class ExtractionFailure(Base, TimestampedUUID, Immutable):
    """A drawing that could not be read, written down so it is not mistaken for a drawing with nothing on it.

    **The gap this closes.** `workflow/stages.py` caught `UnreadablePdf` and moved on, so a document
    that would not parse contributed no pages, no candidates and no result — and the package still
    reported extraction as complete. A drawing nobody could read then looked exactly like a drawing
    that had no dimensions, which is the package-level shape of a false PASS.

    **Recorded rather than raised, and that is the difference between this and a storage failure.**
    `_fetch` raises, because a fetch that failed once may well succeed next time and `run_stage` will
    redeliver it. A corrupt PDF is not transient: raising would roll the claim back and retry the same
    broken file for ever, paying for it each time and recording nothing on any attempt. So the failure
    is a row, written once, and the stage carries on with the drawings it *can* read.

    **No message column, deliberately.** A `pdfminer` error can quote the bytes it choked on, and
    `AGENTS.md` §6 forbids drawing content in a trace for exactly the reason it should not sit in a
    table either. `reason` carries the meaning and `error_type` carries the diagnosis; a free-text
    message would need a redaction story before it earned a place here.

    Append-only, so a later successful re-read adds rows elsewhere rather than erasing this one. That
    a drawing once could not be read is a fact about the package, and a reviewer may want it.
    """

    __tablename__ = "extraction_failures"

    extraction_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("extraction_runs.id", ondelete="RESTRICT"), index=True
    )
    document_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("document_versions.id", ondelete="RESTRICT"), index=True
    )
    #: `None` means the whole document, not "page zero" — hence the constraint pairing it with `reason`.
    page_index: Mapped[int | None] = mapped_column(default=None)
    reason: Mapped[str] = mapped_column(String(32))
    error_type: Mapped[str] = mapped_column(String(100))

    __table_args__ = (
        CheckConstraint(f"reason IN ({_FAILURE_REASON_VALUES})", name="extraction_failure_reason"),
        CheckConstraint(
            "page_index IS NULL OR page_index >= 0", name="extraction_failure_page_index"
        ),
        # The pairing, enforced rather than trusted: a document-level failure has no page and a
        # page-level one must name its page. Either mismatch would be a row nobody could interpret.
        CheckConstraint(
            "(reason = 'document_unreadable' AND page_index IS NULL) "
            "OR (reason = 'document_digest_mismatch' AND page_index IS NULL) "
            "OR (reason = 'page_unreadable' AND page_index IS NOT NULL)",
            name="extraction_failure_scope",
        ),
        CheckConstraint("error_type <> ''", name="extraction_failure_error_type"),
    )


class ModelInvocation(Base, TimestampedUUID, Immutable):
    """Append-only record of a model call, whether it succeeded or not."""

    __tablename__ = "model_invocations"

    extraction_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("extraction_runs.id", ondelete="RESTRICT"), index=True, default=None
    )
    """The extractor run this call belongs to, or `None` for a call that is not an extraction."""

    package_revision_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("package_revisions.id", ondelete="RESTRICT"), index=True, default=None
    )
    """The package revision a review-time call belongs to — reviewer chat, findings narration.

    **Added because those calls had nowhere to go (ADR-0019, #694).** They were anchored to the
    package's *latest* extraction run, which made a narration claim provenance it did not have, and
    raised outright on a package that had never been extracted — so a reviewer chat on a typed
    package failed after the model had already been paid.
    """

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
    assembled_context: Mapped[dict[str, object] | None] = mapped_column(
        JSONB(none_as_null=True), default=None
    )
    bound_pt: Mapped[Decimal | None] = mapped_column(Numeric(), default=None)
    input_tokens: Mapped[int]
    output_tokens: Mapped[int]
    cost_micros: Mapped[int | None] = mapped_column(default=None)
    """What the call cost in millionths of a US dollar, or `NULL` when no price was stated for the
    model (#700). Never a stand-in zero: a zero here means the call used no tokens."""
    latency_ms: Mapped[int]
    outcome: Mapped[str] = mapped_column(String(32))
    rejection_reason: Mapped[str | None] = mapped_column(String(500), default=None)
    """Code-authored reason a call did not give a usable answer, when known.

    For a `rejected` row, why the local code refused the answer. Since #1215 also for a call that
    failed, timed out, was refused or was never sent: the reader's failure kind (for example
    `PictureWouldBeResized`, `SpendCapExceeded`) or, for a Bedrock call that failed outright, its
    error class and a short sanitised message. Never on an `ok` row, and never empty. A reader of
    this column that means "malformed answer" must also check `outcome = 'rejected'`.

    Null is allowed for historic rows, because this table is append-only and a migration may not
    rewrite old invocations to invent a reason they did not record at the time.
    """
    private_raw_response: Mapped[str | None] = mapped_column(Text(), default=None)
    """Exact raw provider answer, when returned. Private audit data; never a public response field."""
    reader_page_index: Mapped[int | None] = mapped_column(Integer(), default=None)
    reader_attempt_number: Mapped[int | None] = mapped_column(Integer(), default=None)
    reader_question_packet: Mapped[dict[str, object] | None] = mapped_column(
        JSONB(none_as_null=True), default=None
    )
    """Hash-bound description of the exact two images and transform used for this attempt."""

    __table_args__ = (
        CheckConstraint("model_id <> ''", name="model_invocation_model_id"),
        CheckConstraint("prompt_id <> ''", name="model_invocation_prompt_id"),
        CheckConstraint("template_id <> ''", name="model_invocation_template_id"),
        CheckConstraint(
            "node_invocation_key IS NULL OR node_invocation_key ~ '^sha256:[0-9a-f]{64}$'",
            name="model_invocation_node_key",
        ),
        CheckConstraint(
            "(assembled_context IS NULL) = (bound_pt IS NULL)",
            name="model_invocation_context_pair",
        ),
        CheckConstraint(
            "bound_pt IS NULL OR bound_pt >= 0",
            name="model_invocation_context_bound",
        ),
        CheckConstraint("input_tokens >= 0", name="model_invocation_input_tokens"),
        CheckConstraint("output_tokens >= 0", name="model_invocation_output_tokens"),
        CheckConstraint("cost_micros >= 0", name="model_invocation_cost"),
        CheckConstraint("latency_ms >= 0", name="model_invocation_latency"),
        CheckConstraint(
            f"outcome IN ({MODEL_INVOCATION_OUTCOMES})",
            name="model_invocation_outcome",
        ),
        CheckConstraint(
            "rejection_reason IS NULL OR (outcome <> 'ok' AND rejection_reason <> '')",
            name="model_invocation_rejection_reason",
        ),
        CheckConstraint(
            "(reader_page_index IS NULL) = (reader_attempt_number IS NULL) "
            "AND (reader_page_index IS NULL OR reader_page_index >= 0) "
            "AND (reader_attempt_number IS NULL OR reader_attempt_number >= 1)",
            name="model_invocation_reader_attempt_position",
        ),
        # **Exactly one origin, never both and never neither** (ADR-0019). Neither leaves a paid
        # call attributable to nothing, which is the same as not recording it and worse, because
        # the row looks like an answer. Both would let one call be counted against two packages.
        CheckConstraint(
            "(extraction_run_id IS NULL) <> (package_revision_id IS NULL)",
            name="model_invocation_one_origin",
        ),
        Index("ix_model_invocations_created_at", "created_at"),
    )


#: The question-packet key of a row that records a reused stored answer, not a call (#1112). Its
#: value is the id of the invocation that was actually asked. The packet's own `packet_sha256` is
#: the question's hash and never covers this key: the question is the same, only the way the
#: answer was obtained differs.
REUSED_FROM_KEY: Final = "reused_from"


def made_a_call() -> ColumnElement[bool]:
    """True for an invocation row that records a real model call; false for a reused answer.

    A reused answer (#1112) is recorded so the run's evidence stays complete, with no tokens and no
    cost; anything that counts calls — usage, ceilings, attribution — filters it out with this.
    """
    unmarked: ColumnElement[bool] = ModelInvocation.reader_question_packet[
        REUSED_FROM_KEY
    ].astext.is_(None)
    return unmarked


class AgentNodeInvocationClaim(Base, TimestampedUUID):
    """Mutable reservation; final invocation and candidate records remain append-only."""

    __tablename__ = "agent_node_invocation_claims"

    node_invocation_key: Mapped[str] = mapped_column(String(71), unique=True)
    extraction_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("extraction_runs.id", ondelete="RESTRICT"), index=True
    )
    status: Mapped[str] = mapped_column(String(32))
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
            "status IN ('in_progress', 'completed', 'failed')",
            name="claim_status",
        ),
        CheckConstraint(
            "(status = 'in_progress' AND model_invocation_id IS NULL AND candidate_id IS NULL) OR "
            "(status = 'completed' AND model_invocation_id IS NOT NULL AND candidate_id IS NOT NULL) OR "
            "(status = 'failed' AND model_invocation_id IS NOT NULL AND candidate_id IS NULL)",
            name="claim_result_links",
        ),
    )


#: The parts of a reading a reviewer can be told about (#1178), in the order they run. The words a
#: person sees for each live in `app/runs/reading_progress.py`; the database holds only the code.
READING_STAGES: Final = (
    "waiting",
    "ingest",
    "pages",
    "architect",
    "preparing",
    "rows",
    "dimensions",
    "chains",
    "architect_pairing",
    "whole_pages",
    "saving",
    "match",
    "validate",
    "form",
)

_READING_STAGE_VALUES: Final = ", ".join(f"'{stage}'" for stage in READING_STAGES)


class ReadingProgress(Base, TimestampedUUID):
    """Where the reading of one package revision is, while it runs (#1178). One row per revision.

    **Execution telemetry, not business truth, which is why it is updated in place.** It says only
    which part of the reading is running and, where the part walks pages, how many are done. Nothing
    reads it to decide anything: the package's state, the stage claims and the readings themselves
    stay where they were, written in the stage's own transaction. This row is written in its own short
    transaction so a reviewer sees it while the stage is still running, and a stage that rolls back
    leaves it saying how far the attempt got, which is true.

    `pages_done` is known only where the part walks pages one by one or asks questions per page;
    `pages_total` may be known while `pages_done` is not. Neither is ever a guess.
    """

    __tablename__ = "reading_progress"

    package_revision_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), unique=True)
    """The revision being read. **Deliberately not a foreign key.** A stage holds a row lock on its
    revision for as long as it runs (`app/lifecycle/events.py:_next_sequence` takes `FOR UPDATE`
    to number the state events), and a foreign-key check from another transaction must lock that
    same row — so every progress line would wait for the stage to finish, which is the one thing it
    exists not to do (measured: the worker hung on its first progress line). Revisions are never
    deleted, so nothing is left pointing nowhere; the API reads this row only by a revision it has
    already found."""
    stage: Mapped[str] = mapped_column(String(32))
    pages_done: Mapped[int | None] = mapped_column(Integer(), default=None)
    pages_total: Mapped[int | None] = mapped_column(Integer(), default=None)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)

    __table_args__ = (
        CheckConstraint(f"stage IN ({_READING_STAGE_VALUES})", name="reading_progress_stage"),
        CheckConstraint(
            "pages_total IS NULL OR pages_total >= 0", name="reading_progress_pages_total"
        ),
        CheckConstraint(
            "pages_done IS NULL OR (pages_total IS NOT NULL AND pages_done >= 0 "
            "AND pages_done <= pages_total)",
            name="reading_progress_pages_done",
        ),
    )
