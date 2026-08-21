"""Retry and failure policy: what is retried, what is never retried, and what is never resolved (#216).

`docs/DESIGN_PLATFORM.md` §6.3 is a four-row table, and each row exists because the alternative is a
silent wrong answer. This module is that table as code.

| Situation | Policy |
|---|---|
| Malformed PDF | one repair attempt, recorded |
| OCR disagreement | **never** auto-resolved — mark `CONFLICTING` |
| Unknown unit | route to REVIEW REQUIRED (ADR-0001) |
| Transient failure | bounded retries with backoff; exhaustion is a visible outcome |

**Two of those four are decided elsewhere, and this module deliberately does not decide them again.**
`evidence/corroborate.py` decides `CONFLICTING` (F2, #120), and `evidence/gate.py` refuses an unknown unit
(ADR-0001). The issue's plan says "do not re-implement it here" of the unit case, and the same reasoning
covers the OCR case: a second module deciding evidence status is how a safety property stops being
enforceable — two places to change, and one of them gets missed. What lives here for those two rows is the
*statement* that they are never retried and never resolved, in a form a test can prove.

**Retrying is only ever for transient failures.** A `PERMANENT` failure class from
`app/lifecycle/side_states.py` is never retried, because repeating a `ValueError` produces the same
`ValueError` more slowly. And exhausting the budget is a recorded state change with the attempt count in
it, never a quiet stop — `AGENTS.md` §2.2, silence must not read as completion.

**The numbers are not this module's to invent.** OCR retries were already capped at 2 in
`docs/DESIGN_AI.md`, and this issue fixes PDF repair at exactly one. The backoff and the stage budget were
Anant's call; where they came from is recorded beside each one.

Source: backend proposal §9.2–§9.4; `AGENTS.md` §6 · Design: `docs/DESIGN_PLATFORM.md` §6.3 ·
Verification: `tests/workflow/test_retry_policy.py`
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Final, Literal, NamedTuple
from uuid import UUID

from sqlalchemy.orm import Session

from app.lifecycle.side_states import FailureClass, enter_failure
from app.models import PackageStateEvent
from evidence.candidate import ObservationCandidate
from verdict.operands import EvidenceStatus
from workflow.idempotency import CLAIMED, claim, stage_idempotency_key
from workflow.review import stage_order

__all__ = [
    "FAILURE_POLICY",
    "MAX_PDF_REPAIR_ATTEMPTS",
    "OCR_RULE",
    "PDF_REPAIR_RULE",
    "REPAIR_CAP_VERSION",
    "RETRY_POLICY",
    "STAGE_RULE",
    "UNKNOWN_TASK_RULE",
    "Policy",
    "RetryRule",
    "Situation",
    "claim_pdf_repair",
    "delay_for",
    "give_up",
    "on_ocr_disagreement",
    "rule_for",
    "should_retry",
    "total_delay_bound",
]


class Situation(StrEnum):
    """The four situations §6.3 names. Data, so the table can be read rather than reconstructed."""

    MALFORMED_PDF = "malformed_pdf"
    OCR_DISAGREEMENT = "ocr_disagreement"
    UNKNOWN_UNIT = "unknown_unit"
    TRANSIENT_FAILURE = "transient_failure"


class Policy(NamedTuple):
    """What happens in one situation, and what was rejected.

    `rejected_alternative` is not decoration. Every row of this table is a place where the obvious
    engineering instinct — retry it, pick the better reading, assume the unit — produces a confident wrong
    answer, and the reason has to travel with the rule or the next person re-adds the instinct.
    """

    what_happens: str
    rejected_alternative: str
    decided_by: str
    retried: bool


#: The §6.3 table. Reviewable in one place instead of scattered through branches.
FAILURE_POLICY: Final[Mapping[Situation, Policy]] = {
    Situation.MALFORMED_PDF: Policy(
        what_happens="One repair attempt, recorded on the task run. Never a second.",
        rejected_alternative=(
            "Repairing repeatedly. A file that needs two repairs is not a file we understand, and each "
            "repair rewrites the bytes every downstream fact was extracted from."
        ),
        decided_by="docs/DESIGN_PLATFORM.md §6.3",
        retried=False,
    ),
    Situation.OCR_DISAGREEMENT: Policy(
        what_happens=(
            "The observation becomes CONFLICTING and a check abstains. Decided by "
            "evidence/corroborate.py, not here."
        ),
        rejected_alternative=(
            "Preferring the higher-confidence reading. A disagreement about a number is exactly the case "
            "where guessing produces a confident wrong answer, so there is no winner to pick."
        ),
        decided_by="AGENTS.md §2.3; backend §6.2; evidence/corroborate.py",
        retried=False,
    ),
    Situation.UNKNOWN_UNIT: Policy(
        what_happens="Routed to REVIEW REQUIRED through evidence/gate.py. No unit is ever assumed.",
        rejected_alternative=(
            "Assuming millimetres because most drawings are metric. A wrong unit is a wrong number that "
            "passes every arithmetic check."
        ),
        decided_by="ADR-0001; evidence/gate.py",
        retried=False,
    ),
    Situation.TRANSIENT_FAILURE: Policy(
        what_happens=(
            "Bounded retries with jittered backoff. Exhaustion is a recorded transition to "
            "FAILED_RETRYABLE with the attempt count in the reason."
        ),
        rejected_alternative=(
            "Retrying without a bound. An unbounded retry against a dependency that is down looks like a "
            "slow package rather than a broken one, and nobody is told."
        ),
        decided_by="docs/DESIGN_PLATFORM.md §6.3; budgets chosen by Anant on #216",
        retried=True,
    ),
}


class RetryRule(NamedTuple):
    """One task type's budget.

    `max_attempts` counts the original try, so 1 means "run it once and never again".
    """

    max_attempts: int
    base_delay_s: float
    max_delay_s: float


#: The six pipeline stages. Three attempts — the original plus two — chosen by Anant on #216: enough to
#: ride out a worker restart or a dropped connection, few enough that a genuinely broken package surfaces
#: instead of churning. 2s base and a 120s cap for the same reason the concurrency defaults are low: on one
#: shared 8 GB VM, an immediate re-attempt competes with rendering, OCR and PostgreSQL for memory.
STAGE_RULE: Final = RetryRule(max_attempts=3, base_delay_s=2.0, max_delay_s=120.0)

#: OCR. Two attempts, and this number was **already decided** — `docs/DESIGN_AI.md` line 50, "OCR retries
#: ≤ 2, with versioned attempt records", which `memory.md` line 38 repeats. Not re-decided here.
OCR_RULE: Final = RetryRule(max_attempts=2, base_delay_s=2.0, max_delay_s=120.0)

#: PDF repair. Exactly one attempt, from this issue's own Scope, and enforced by the database rather than
#: by an `if` — see `claim_pdf_repair`. Zero delay because there is never a second attempt to delay.
PDF_REPAIR_RULE: Final = RetryRule(max_attempts=1, base_delay_s=0.0, max_delay_s=0.0)

#: What an unrecognised task type gets: one attempt, no retry.
#:
#: The safe direction, and the same choice `side_states.classify` already makes when it meets an exception
#: it has no entry for. A task type nobody wrote a budget for is a task type nobody thought about, and
#: giving it the generous default would mean a new task silently inherits retries its author never
#: considered. Failing visibly on the first attempt is the outcome that gets noticed and fixed.
UNKNOWN_TASK_RULE: Final = RetryRule(max_attempts=1, base_delay_s=0.0, max_delay_s=0.0)

#: The task type used when claiming a PDF repair, and the cap that goes with it.
PDF_REPAIR_TASK_TYPE: Final = "pdf_repair"
MAX_PDF_REPAIR_ATTEMPTS: Final = 1

#: The key version for the repair cap, and it is **deliberately not `ENGINE_VERSION`**.
#:
#: A stage's key includes the engine version on purpose: new code is a different task rather than a cache
#: hit, so the stage should run again (#215). Repair is the opposite, and putting the engine version in its
#: key was a real defect — bumping `ENGINE_VERSION` produced a different key, `claim` succeeded, and the
#: same file got repaired a second time. Verified before fixing: attempt 3 at engine 1.1.0 returned True
#: where it had to return False.
#:
#: The cap has to be absolute because each repair rewrites the bytes every downstream fact was extracted
#: from. "At most once per engine version" is not a cap on modifying a source document, it is a cap that
#: resets whenever this file changes — which is exactly when nobody is thinking about it.
#:
#: A constant, so the value cannot drift. Changing it re-opens one repair per document for every document
#: in the system, which is why it is not derived from anything.
REPAIR_CAP_VERSION: Final = "absolute"

#: task_type -> budget. The stage names come from `workflow/review.py` rather than being retyped, so a
#: stage added there cannot quietly end up with no policy: it would land on `UNKNOWN_TASK_RULE`, get one
#: attempt, and be noticed.
RETRY_POLICY: Final[Mapping[str, RetryRule]] = {
    **{stage: STAGE_RULE for stage in stage_order()},
    "ocr": OCR_RULE,
    PDF_REPAIR_TASK_TYPE: PDF_REPAIR_RULE,
}


def rule_for(task_type: str) -> RetryRule:
    """The budget for a task type, or the one-attempt rule if nobody wrote one."""
    return RETRY_POLICY.get(task_type, UNKNOWN_TASK_RULE)


def should_retry(task_type: str, *, attempt: int, failure_class: FailureClass) -> bool:
    """Whether to try again after `attempt` failed. `attempt` is 1-based.

    Two reasons to stop, and they are different reasons:

    - **The failure is `PERMANENT`.** Retrying a `ValueError` produces the same `ValueError` more slowly.
      `app/lifecycle/side_states.py` owns that classification, including its deliberate rule that an
      unclassified exception is permanent — so a failure this system does not recognise is not retried.
    - **The budget is spent.** Which is what `give_up` then records.
    """
    if attempt < 1:
        raise ValueError("attempt is 1-based: the first try is attempt 1")
    if failure_class is FailureClass.PERMANENT:
        return False
    return attempt < rule_for(task_type).max_attempts


def delay_for(task_type: str, *, attempt: int, jitter_fraction: float | None = None) -> float:
    """Seconds to wait after `attempt` before trying again. Exponential, capped, and jittered.

    **Equal jitter, not full jitter.** The delay lands in `[capped / 2, capped]`. Full jitter — a uniform
    draw from `[0, capped]` — can return almost zero, which throws away the backoff exactly when a
    struggling dependency most needs the gap. Halving the spread keeps the retries spread out *and* keeps
    the growth, and it makes the total provably bounded (`total_delay_bound`).

    Jitter at all because without it, every task that failed on the same dependency retries in lockstep
    and hits it again together — the failure recovers into a thundering herd.

    `jitter_fraction` is injectable so a test can pin the delay exactly; production leaves it None and
    gets a random draw.
    """
    if attempt < 1:
        raise ValueError("attempt is 1-based: the first try is attempt 1")
    rule = rule_for(task_type)
    if rule.base_delay_s <= 0:
        return 0.0

    # 2, 4, 8, 16 … then flat at the cap.
    uncapped = rule.base_delay_s * (2 ** (attempt - 1))
    capped = min(uncapped, rule.max_delay_s)

    fraction = random.random() if jitter_fraction is None else jitter_fraction
    if not 0.0 <= fraction <= 1.0:
        raise ValueError("jitter_fraction is a proportion between 0 and 1")
    half: float = capped / 2
    return float(half + fraction * half)


def total_delay_bound(task_type: str) -> float:
    """The most time a task can ever spend waiting between its attempts.

    Exists so "bounded" is a number somebody can check rather than an adjective. A budget of three
    attempts at 2s base waits at most 2s + 4s = 6s in total, because the last attempt is not followed by
    a wait.
    """
    rule = rule_for(task_type)
    return sum(
        delay_for(task_type, attempt=attempt, jitter_fraction=1.0)
        for attempt in range(1, rule.max_attempts)
    )


def give_up(
    session: Session,
    package_revision_id: UUID,
    *,
    task_type: str,
    attempts: int,
    actor: str,
) -> PackageStateEvent:
    """Record that a task ran out of retries. The exhaustion *is* the outcome.

    Lands in `FAILED_RETRYABLE`, chosen by Anant on #216: the cause was transient, so the package stays
    honestly labelled as something a retry may fix, and a person can retry it deliberately. Calling it
    permanent would tell a reviewer that a database blip cannot be recovered from, which is false.

    The reason names the task and the count, because "it failed" sends somebody to the logs while "gave up
    after 3 attempts at run_checks" does not.
    """
    if attempts < 1:
        raise ValueError("a task that never ran cannot have exhausted its retries")
    return enter_failure(
        session,
        package_revision_id,
        actor=actor,
        failure_class=FailureClass.RETRYABLE,
        reason=(
            f"gave up after {attempts} "
            f"{'attempt' if attempts == 1 else 'attempts'} at {task_type}"
        ),
    )


def claim_pdf_repair(
    session: Session,
    *,
    package_revision_id: UUID,
    document_id: UUID,
    workflow_run_id: UUID,
) -> bool:
    """Take the single permitted repair attempt for one document. True if it is yours.

    **The cap is enforced by the database, not by a counter this code trusts itself to increment.** The
    attempt claims an idempotency key built from the package revision and the document, and
    `task_runs.idempotency_key` is unique — so a second attempt, from a retried task or a second worker,
    finds the row already there and is refused. An `if attempts < 1` would be correct right up to the
    first time two workers ran it at once.

    Recorded either way: the claim leaves a `task_runs` row with `attempt` on it, which is what the issue
    means by the attempt being recorded, and it is visible to a reviewer asking whether the file was
    touched.

    **Once means once, not once per engine version.** The key is versioned by `REPAIR_CAP_VERSION` rather
    than `ENGINE_VERSION` — see that constant for the defect this fixes and how it was verified.
    """
    key = stage_idempotency_key(
        package_revision_id=package_revision_id,
        stage=f"{PDF_REPAIR_TASK_TYPE}:{document_id}",
        engine_version=REPAIR_CAP_VERSION,
    )
    taken = claim(
        session,
        key,
        workflow_run_id=workflow_run_id,
        task_type=PDF_REPAIR_TASK_TYPE,
        outcome=CLAIMED,
    )
    return taken.created


def on_ocr_disagreement(
    readings: Sequence[ObservationCandidate],
) -> Literal[EvidenceStatus.CONFLICTING]:
    """Disagreeing OCR readings produce `CONFLICTING`. There is deliberately no other branch.

    **This function cannot pick a winner, because it never looks at `readings`.** That is the whole design:
    the argument is here so the call site reads honestly, and the body ignores it. A test asserts by AST
    inspection that the parameter never appears in the body and that the only value this can return is the
    one constant — so an edit that added "prefer the higher-confidence reading" fails the suite instead of
    shipping.

    The real decision is `evidence/corroborate.py`'s, and it already refuses to use confidence: *"Confidence
    is deliberately absent from every decision."* This is the policy restated where the retry table can
    point at it, not a second decider. Nothing in the pipeline should call this to *set* a status; call it
    to ask what the policy is.

    Why it matters more than it looks: two readers disagreeing about a dimension is the one case where a
    plausible answer is available and wrong. Preferring either reader converts a caught problem into a
    confident wrong number, which is the exact failure this whole system exists to prevent.
    """
    return EvidenceStatus.CONFLICTING
