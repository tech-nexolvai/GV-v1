"""Writing and reading the record of every model call — the paid ones that worked and the ones that did not.

Two questions have to stay answerable long after a package has shipped: *what did this cost, and
why*, and *what did the model actually see when it produced this reading*. `model_invocations` is
where both answers live, so this module's whole job is to make writing a row easy and forgetting one
hard.

**Failures are recorded, and that is the point of the module rather than a detail of it.** A call
that timed out, was refused, or produced output the validator rejected still consumed tokens, still
took wall-clock time, and still explains a gap in the extracted evidence. Recording only successes
would turn the table into a summary of the work that went well, which is the one shape a cost record
must never have: F5's per-package ceiling would then be computed from an under-count, and it would
fail to trigger precisely on the packages that burned the most money going nowhere.

**Cost is an integer number of micros, and this module refuses anything else.** That refusal is not
decoration over a database check — it is the only thing standing between the ledger and silent
rounding. `cost_micros` is a PostgreSQL `integer`; handing the driver `1.6` inserts `2`, and handing
it `Decimal("2.4")` inserts `2`. No error, no warning, and the number that a ceiling later reads is
not the number anybody computed. Money in binary floating point is how a cost ceiling quietly stops
being true, so the type is checked at the door where the caller can still be told which argument was
wrong. `tests/extraction/models/test_invocations.py` demonstrates the rounding rather than taking it
on trust.

**Which layer guarantees what.** Worth stating exactly, because "every call is recorded" is easy to
over-claim:

* The **database** refuses a row with a blank model, prompt or template identifier, a negative token
  count, a negative cost, a negative latency, or an outcome outside the closed set in
  `ModelInvocationOutcome`. It refuses an `extraction_run_id` naming no run.
  `alembic/versions/0013_append_only.py` additionally refuses every `UPDATE` and `DELETE`, so a row
  written here cannot later be edited or removed by ordinary means.
* This **module** adds the integer-type check described above, refuses to commit, and flushes so a
  rejected row raises at the call that wrote it rather than at some later commit elsewhere in the
  request.
* **Nothing at any layer makes a call impossible to omit.** `record` cannot be called by code that
  does not call it. Completeness is a property of the adapter that wraps the model client — it must
  write the record on the error path as well as the success path — and this module can only make
  that easy and give the failure paths a first-class vocabulary. The claim "every call is recorded"
  is a claim about the caller, not a guarantee this file delivers.
* Also not guaranteed: tamper-proofing. A role that owns the table can disable the append-only
  trigger, and a superuser can bypass user triggers entirely. `tests/db/test_append_only.py`
  records that boundary instead of pretending it away.

**The crop reference, and the honest size of it.** `crop_artifact_id` holds the id of the
`evidence_artifacts` row for the image the model was given, so "what did it see" resolves to
content-addressed bytes with a SHA-256 beside them. Two limits: the column carries **no foreign
key**, so the database does not guarantee the id names anything — `crop_for` returns `None` rather
than raising when it does not; and an evidence artifact must belong to a candidate or a canonical
observation, so a call that produced neither has nothing to hang its crop from and is written with
`crop_artifact_id=None`. For those rows the record proves the cost, the identity and the outcome,
but not the exact bytes. The argument is keyword-only with no default so that "there was no crop" is
something a caller states rather than something it forgets.

**Linking a candidate to the call that produced it.** The link runs through the crop:
invocation → `crop_artifact_id` → `evidence_artifacts.candidate_id` → candidate, and back again.
`candidate_id_for` and `invocations_for_candidate` walk it in each direction. Read the relation
literally — *this call was given that candidate's crop*. Where the adapter writes the candidate, its
crop and the invocation in one transaction, that is the same thing as "the call that produced it";
where a call is given an earlier candidate's crop and produces a different one, it is not, and no
column in the schema currently tells the two apart. See the note in issue #251: a direct
`observation_candidates.model_invocation_id` would, and it does not exist.

**No arithmetic on money happens here.** Costs are stored exactly as the caller computed them and
are summed, when they are summed at all, by the database in integer micros.

Source: backend proposal §6.3, §10.1 `model_invocations`, and issue #251 ·
Design: `docs/DESIGN_AI.md` §4.5 · Verification: `tests/extraction/models/test_invocations.py`
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.evidence import EvidenceArtifact
from app.models.runs import ModelInvocation, ModelInvocationOutcome

__all__ = [
    "candidate_id_for",
    "crop_for",
    "invocations_for_candidate",
    "record",
]


def _exact_count(name: str, value: int) -> int:
    """Return `value` if it is a plain integer, else say which argument was wrong and why.

    `bool` is excluded deliberately even though it is a subclass of `int`: `True` reaching a token
    count means a caller passed a flag where a number belongs, and storing `1` would hide it.

    `float` and `Decimal` are excluded because the column is a PostgreSQL `integer` and the driver
    rounds silently on the way in. A caller holding a fractional cost has done floating-point
    arithmetic on money somewhere upstream, and the place to find that out is here rather than in a
    ceiling that fails to fire six weeks later.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(
            f"{name} must be a plain int, not {type(value).__name__}. "
            "This column is a PostgreSQL integer: a float or Decimal is rounded on insert with no "
            "error, so a cost that was never exact would be recorded as though it were."
        )
    return value


def record(
    session: Session,
    *,
    extraction_run_id: UUID,
    model_id: str,
    prompt_id: str,
    template_id: str,
    crop_artifact_id: UUID | None,
    input_tokens: int,
    output_tokens: int,
    cost_micros: int,
    latency_ms: int,
    outcome: ModelInvocationOutcome,
) -> ModelInvocation:
    """Write one model call — successful or not — and return the stored row.

    Call this on every path out of the model client, including the ones that raise. An invocation
    that errored, timed out or was refused is not an absence of work: it is spent money and a hole
    in the evidence, and both are things somebody will need to account for.

    The row is flushed before returning, so a row the database refuses — a blank model id, a
    negative cost, an outcome outside the closed set, an extraction run that does not exist — raises
    at this call rather than at a later commit where the cause is no longer obvious. Nothing here
    re-checks what the database checks; two copies of the same rule drift, and the copy that drifted
    is the one nobody was watching.

    Args:
        session: the caller's session. This function never commits. Writing the invocation in the
            same transaction as the candidate it explains is what keeps the two consistent — either
            both rows land or neither does.
        extraction_run_id: the version-pinned extractor run this call belongs to. Required by the
            table, and the reason a cost total can be attributed to a package at all.
        model_id: the exact model identifier, version included. "Which model said this" is not
            answerable from a family name once the family has moved on.
        prompt_id: the identifier of the prompt this call used.
        template_id: the identifier of the template the prompt was rendered from.
        crop_artifact_id: the `evidence_artifacts` row for the image the model was given, or `None`
            when there was no crop to reference. See the module docstring for what the `None` case
            costs you.
        input_tokens: tokens sent, as counted by the provider.
        output_tokens: tokens returned, as counted by the provider. Zero is the normal value for a
            refusal or a timeout and is recorded as zero rather than left out.
        cost_micros: the cost of this call in millionths of a currency unit, as a plain `int`.
        latency_ms: wall-clock duration in whole milliseconds.
        outcome: how the call ended, from the closed `ModelInvocationOutcome` set. Passing anything
            outside it is rejected by the database `CHECK`, at this call, by name.

    Returns:
        The persisted invocation, with its id and `created_at` populated.

    Raises:
        TypeError: if a token count, cost or latency is not a plain `int`.
        sqlalchemy.exc.IntegrityError: if the database refuses the row.
    """
    # Checked before anything is built or added to the session, so a caller that rounded its cost
    # somewhere upstream is told which argument was wrong and leaves no half-built row behind.
    exact_input_tokens = _exact_count("input_tokens", input_tokens)
    exact_output_tokens = _exact_count("output_tokens", output_tokens)
    exact_cost_micros = _exact_count("cost_micros", cost_micros)
    exact_latency_ms = _exact_count("latency_ms", latency_ms)

    invocation = ModelInvocation(
        extraction_run_id=extraction_run_id,
        model_id=model_id,
        prompt_id=prompt_id,
        template_id=template_id,
        crop_artifact_id=crop_artifact_id,
        input_tokens=exact_input_tokens,
        output_tokens=exact_output_tokens,
        cost_micros=exact_cost_micros,
        latency_ms=exact_latency_ms,
        outcome=outcome,
    )
    session.add(invocation)
    session.flush()
    return invocation


def crop_for(session: Session, invocation: ModelInvocation) -> EvidenceArtifact | None:
    """Return the evidence artifact this call was given, or `None` if there is none to return.

    `None` covers two different situations and cannot distinguish them, because the schema does not:
    the call recorded no crop, or it recorded an id that resolves to no row. The column has no
    foreign key, so the second is possible and returning `None` says so rather than raising an
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
    attempts to read cost four calls, and a query that returned only the successful one would make
    the expensive candidates look cheap.

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
