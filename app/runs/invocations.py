"""Storing every model call, and reading back what the model was shown.

The other half of `extraction/models/invocations.py`. That module owns what must be recorded about a
model call and the one rule the database cannot enforce; this one owns turning it into a row and
finding it again. The split is not cosmetic: `docs/DESIGN_AI.md` §2 forbids `extraction/models/` from
reaching `rules/`, and it reaches it in two hops the moment it imports `app.models`. `app/` is the
layer allowed to know both the table and the caller, so the persistence lives here and the extraction
side stays clean by construction rather than by a rule somebody has to remember.

**Which layer guarantees what.** Worth stating exactly, because "every call is recorded" is easy to
over-claim:

* The **database** refuses a row with a blank model, prompt or template identifier, a negative token
  count, a negative cost, a negative latency, or an outcome outside the closed set in
  `ModelInvocationOutcome`. It refuses an `extraction_run_id` naming no run.
  `alembic/versions/0013_append_only.py` additionally refuses every `UPDATE` and `DELETE`, so a row
  written here cannot later be edited or removed by ordinary means.
* `InvocationRecord` refuses a cost or a count that is not exactly an integer — the one check the
  database cannot make, because by the time a rounded value reaches it, it looks like an honest
  integer.
* This **module** decides only *when* those refusals surface. `record` flushes, so a row the database
  rejects raises at the call that wrote it rather than at some later commit elsewhere in the request.
  It adds no checks of its own: a Python guard in front of a database guard only tells you which of
  the two somebody went around.
* **Nothing at any layer makes a call impossible to omit.** `record` cannot be called by code that
  does not call it. Completeness is a property of the adapter wrapping the model client — it must
  record on the error path as well as the success path — and these two modules can only make that
  easy and give the failure paths a first-class vocabulary. "Every call is recorded" is a claim about
  the caller, not a guarantee this file delivers.
* Also not guaranteed: tamper-proofing. A role that owns the table can disable the append-only
  trigger, and a superuser can bypass user triggers entirely. `tests/db/test_append_only.py` records
  that boundary instead of pretending it away.

**The crop reference, and the honest size of it.** `crop_artifact_id` holds the id of the
`evidence_artifacts` row for the image the model was given, so "what did it see" resolves to
content-addressed bytes with a SHA-256 beside them. Two limits: the column carries **no foreign
key** — `alembic/versions/0005_run_records.py` leaves it a bare `uuid` because evidence artifacts
landed in a later migration — so the database does not guarantee the id names anything, and
`crop_for` returns `None` rather than raising when it does not. And an evidence artifact must belong
to a candidate or a canonical observation (`evidence_artifact_owner`), so a call that produced
neither has nothing to hang its crop from and is recorded with `crop_artifact_id=None`. For those
rows the record proves the cost, the identity and the outcome, but not the exact bytes.

**Linking a candidate to the call that produced it.** The link runs through the crop:
invocation → `crop_artifact_id` → `evidence_artifacts.candidate_id` → candidate, and back again.
`candidate_id_for` and `invocations_for_candidate` walk it in each direction. Read the relation
literally — *this call was given that candidate's crop*. Where the adapter writes the candidate, its
crop and the invocation in one transaction, that is the same thing as "the call that produced it";
where a call is given an earlier candidate's crop and produces a different one, it is not, and no
column in the schema currently tells the two apart. A direct
`observation_candidates.model_invocation_id` would; it does not exist, and the note on issue #251
asks for a ruling rather than inventing it here.

**No arithmetic on money happens here.** Costs are stored exactly as the caller computed them and
are summed, when they are summed at all, in integer micros.

Source: backend proposal §6.3, §10.1 `model_invocations`, and issue #251 ·
Design: `docs/DESIGN_AI.md` §4.5 · Verification: `tests/extraction/models/test_invocations.py`
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from importlib import import_module
from time import monotonic_ns
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.evidence import EvidenceArtifact
from app.models.runs import (
    ExtractionRun,
    ModelInvocation,
    ModelInvocationOutcome,
    TaskRun,
    WorkflowRun,
)

__all__ = [
    "BedrockConverseInvocationRecorder",
    "candidate_id_for",
    "crop_for",
    "invocations_for_candidate",
    "record",
]

REFUSAL_STOP_REASONS = frozenset({"content_filtered", "guardrail_intervened"})


class _AsRecord(Protocol):
    def as_record(self) -> dict[str, object]:
        """Return JSON-safe context data."""


class _InvocationRecordLike(Protocol):
    @property
    def extraction_run_id(self) -> UUID: ...

    @property
    def model_id(self) -> str: ...

    @property
    def prompt_id(self) -> str: ...

    @property
    def template_id(self) -> str: ...

    @property
    def crop_artifact_id(self) -> UUID | None: ...

    @property
    def node_invocation_key(self) -> str | None: ...

    @property
    def candidate_id(self) -> UUID | None: ...

    @property
    def assembled_context(self) -> _AsRecord | None: ...

    @property
    def bound_pt(self) -> Decimal | None: ...

    @property
    def input_tokens(self) -> int: ...

    @property
    def output_tokens(self) -> int: ...

    @property
    def cost_micros(self) -> int: ...

    @property
    def latency_ms(self) -> int: ...

    @property
    def outcome(self) -> str: ...


def _invocation_record_type() -> Any:
    """Load the validated call shape only when a caller is about to persist one."""
    return import_module("extraction.models.invocations").InvocationRecord


def record(session: Session, invocation: _InvocationRecordLike) -> ModelInvocation:
    """Write one model call — successful or not — and return the stored row.

    Takes a validated `InvocationRecord` rather than loose keyword arguments, so there is no route
    that persists a call whose cost was never an exact integer. Issue #251's sketched interface took
    the fields directly; the field set and the meaning of every one of them are unchanged, but the
    validation now happens somewhere it cannot be bypassed.

    The row is flushed before returning. Without that, a row the database refuses — a blank model id,
    a negative cost, an outcome outside the closed set, an extraction run that does not exist — would
    raise at a later commit, somewhere the caller cannot tell which write caused it.

    Args:
        session: the caller's session. This function never commits. Writing the invocation in the
            same transaction as the candidate it explains is what keeps the two consistent — either
            both rows land or neither does.
        invocation: the complete record of the call, including the ones that failed.

    Returns:
        The persisted invocation, with its id and `created_at` populated.

    Raises:
        sqlalchemy.exc.IntegrityError: if the database refuses the row.
    """
    stored = ModelInvocation(
        extraction_run_id=invocation.extraction_run_id,
        model_id=invocation.model_id,
        prompt_id=invocation.prompt_id,
        template_id=invocation.template_id,
        crop_artifact_id=invocation.crop_artifact_id,
        node_invocation_key=invocation.node_invocation_key,
        candidate_id=invocation.candidate_id,
        assembled_context=(
            None
            if invocation.assembled_context is None
            else invocation.assembled_context.as_record()
        ),
        bound_pt=invocation.bound_pt,
        input_tokens=invocation.input_tokens,
        output_tokens=invocation.output_tokens,
        cost_micros=invocation.cost_micros,
        latency_ms=invocation.latency_ms,
        outcome=invocation.outcome,
    )
    session.add(stored)
    session.flush()
    return stored


def _latest_extraction_run_id(session: Session, package_revision_id: UUID) -> UUID:
    """Return the newest existing run anchor for package-scoped presentation calls."""
    statement = (
        select(ExtractionRun.id)
        .join(TaskRun, ExtractionRun.task_run_id == TaskRun.id)
        .join(WorkflowRun, TaskRun.workflow_run_id == WorkflowRun.id)
        .where(WorkflowRun.package_revision_id == package_revision_id)
        .order_by(ExtractionRun.created_at.desc(), ExtractionRun.id.desc())
        .limit(1)
    )
    result = session.execute(statement).scalar_one_or_none()
    if result is None:
        raise ValueError(
            f"no extraction run for package revision {package_revision_id}: "
            "a model invocation needs the run chain for package usage attribution"
        )
    return result


def _milliseconds_since(started_ns: int) -> int:
    return max(0, (monotonic_ns() - started_ns) // 1_000_000)


def _usage(response: Mapping[str, Any] | None) -> tuple[int, int]:
    if response is None:
        return 0, 0
    usage = response.get("usage")
    if not isinstance(usage, Mapping):
        return 0, 0
    input_tokens = usage.get("inputTokens", 0)
    output_tokens = usage.get("outputTokens", 0)
    if isinstance(input_tokens, bool) or not isinstance(input_tokens, int):
        input_tokens = 0
    if isinstance(output_tokens, bool) or not isinstance(output_tokens, int):
        output_tokens = 0
    return max(0, input_tokens), max(0, output_tokens)


def _error_code(error: BaseException) -> str | None:
    response = getattr(error, "response", None)
    if not isinstance(response, Mapping):
        return None
    details = response.get("Error")
    if not isinstance(details, Mapping):
        return None
    code = details.get("Code")
    return code if isinstance(code, str) else None


def _is_timeout(error: BaseException) -> bool:
    return (
        isinstance(error, TimeoutError)
        or error.__class__.__name__
        in {
            "ConnectTimeoutError",
            "ReadTimeoutError",
        }
        or _error_code(error) == "ModelTimeoutException"
    )


def _stop_reason(response: Mapping[str, Any] | None) -> str | None:
    if response is None:
        return None
    value = response.get("stopReason")
    return value if isinstance(value, str) else None


def _outcome(response: Mapping[str, Any] | None, error: BaseException | None) -> str:
    if _stop_reason(response) in REFUSAL_STOP_REASONS:
        return ModelInvocationOutcome.REFUSED.value
    if error is None:
        return ModelInvocationOutcome.OK.value
    if _is_timeout(error):
        return ModelInvocationOutcome.TIMEOUT.value
    if response is not None:
        return ModelInvocationOutcome.REJECTED.value
    return ModelInvocationOutcome.FAILED.value


@dataclass(frozen=True, slots=True)
class BedrockConverseInvocationRecorder:
    """Persist one Bedrock converse call against an existing package run chain.

    Bedrock returns token usage, not invoice cost. Until a caller supplies an exact rate card, the
    only honest money value is zero; the model call and tokens are still counted by package ceilings.
    """

    session: Session
    package_revision_id: UUID

    def record(
        self,
        *,
        model_id: str,
        prompt_id: str,
        template_id: str,
        started_ns: int,
        response: Mapping[str, Any] | None,
        error: BaseException | None,
    ) -> ModelInvocation:
        outcome = _outcome(response, error)
        input_tokens, output_tokens = _usage(response)
        if outcome in {ModelInvocationOutcome.REFUSED.value, ModelInvocationOutcome.TIMEOUT.value}:
            output_tokens = 0
        return record(
            self.session,
            _invocation_record_type()(
                extraction_run_id=_latest_extraction_run_id(self.session, self.package_revision_id),
                model_id=model_id,
                prompt_id=prompt_id,
                template_id=template_id,
                crop_artifact_id=None,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_micros=0,
                latency_ms=_milliseconds_since(started_ns),
                outcome=outcome,
            ),
        )


def crop_for(session: Session, invocation: ModelInvocation) -> EvidenceArtifact | None:
    """Return the evidence artifact this call was given, or `None` if there is none to return.

    `None` covers two different situations and cannot distinguish them, because the schema does not:
    the call recorded no crop, or it recorded an id that resolves to no row. The column has no
    foreign key, so the second is possible, and returning `None` says so rather than raising an
    exception that would suggest the database had promised otherwise.

    The artifact carries `storage_key` and `sha256`, so the caller can fetch the bytes and use
    `EvidenceArtifact.content_matches` to confirm they are the same bytes the model saw.
    """
    if invocation.crop_artifact_id is None:
        return None
    return session.get(EvidenceArtifact, invocation.crop_artifact_id)


def candidate_id_for(session: Session, invocation: ModelInvocation) -> UUID | None:
    """Return the candidate whose crop this call was given, or `None`.

    `None` means the call recorded no crop, the crop id resolves to no artifact, or the artifact
    belongs to a canonical observation rather than to a candidate.
    """
    artifact = crop_for(session, invocation)
    return None if artifact is None else artifact.candidate_id


def invocations_for_candidate(session: Session, candidate_id: UUID) -> tuple[ModelInvocation, ...]:
    """Return every recorded call that was given one of this candidate's crops, oldest first.

    Failed and rejected calls are included, and that is the useful part: a candidate that took four
    attempts to read cost four calls, and a query returning only the successful one would make the
    expensive candidates look like the cheapest in the package.

    Ordered by `created_at` then `id`, so two calls written in the same transaction — which share a
    timestamp only if the clock is coarse — still come back in a stable order rather than an
    arbitrary one.
    """
    artifact_ids = select(EvidenceArtifact.id).where(EvidenceArtifact.candidate_id == candidate_id)
    statement = (
        select(ModelInvocation)
        .where(ModelInvocation.crop_artifact_id.in_(artifact_ids))
        .order_by(ModelInvocation.created_at, ModelInvocation.id)
    )
    return tuple(session.scalars(statement))
