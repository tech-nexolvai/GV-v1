"""Read depths again: the depth and sink reader on a drawing set that was already read (#1308).

**Why.** The depth and sink reader (#1290, `GV_DEPTH_READER`) runs inside the match stage of a
set's first reading. A set read before the reader existed, or before it was switched on, could
never get depth readings: a new reading request finds the match stage already done, and "Read the
unread pages" reads the vendor file again and never reaches the reader.

**What a person asks for.** "Read depths again" on the package's current revision. The worker
(`DatabaseStages.read_depths_again`) runs only Bala's countertop suggestions (code, no cost) and the
depth and sink reader, then queues the checks again. It never reads the set again.

**Everything here is the action's plumbing that the API needs too**, so it imports no extraction
code (`tests/api/test_no_heavy_work.py`): the outbox work's name, the plain refusals, the refusal
both the API and the worker ask before anything is read, the estimate a person sees before starting,
and what became of the last request.

* **The same safeguards as the reader's first run.** Only with `GV_DEPTH_READER` and the Claude
  readers on, with stored answers reused (`GV_READER_REUSE_ANSWERS`): an identical question takes
  its stored answer at no cost. The spend cap is the reader's own: the Claude readers' ceiling, held
  under what the drawing set's AI budget has left (`request_cap_usd`); what the set has left is
  said too (`set_budget_left`, #1328). Every call is recorded through the call ledger under the
  reader's own extraction run, and the status counts a question as paid when the request made its
  call, as reused only when the answer was paid for before (`_spent_by_request`, #1328).
* **A run of its own, per request.** The reading is recorded under the match stage's task run, as a
  run whose `config_hash` ends with `;again=<the request's outbox id>` (`again_hash`). A redelivered
  request finds its run and reads nothing again; a new request is a new run, newest wins, and the
  earlier readings stay as they are (append-only). A person's decisions on an earlier reading carry
  over to an identical reading, and a person's link always wins (`workflow/depth_evidence.py`,
  `workflow/depth_reader.person_link`).
* **Never silent.** A request refused by the worker raises `DepthsAgainRefused` with a sentence for
  a person; the outbox parks it with that sentence (`app.lifecycle.side_states.plain_stop_reason`)
  and never redelivers it. A reading that failed or was stopped by the spend cap is recorded as such
  and `last_request` says so.

Verification: `tests/workflow/test_read_depths_again.py`, `tests/api/test_read_depths_again.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Final, Literal
from uuid import UUID

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session, aliased

from app.lifecycle.closed import CLOSED_STATES
from app.models import (
    DrawingItem,
    DrawingView,
    OutboxEntry,
    Package,
    PackageRevision,
    PackageState,
    PartConfirmation,
    PartDecision,
    PartProposal,
    PartProposalRow,
)
from app.models.evidence import ObservationCandidate
from app.models.package import package_product
from app.models.runs import (
    REUSED_FROM_KEY,
    ExtractionRun,
    ModelInvocation,
    ModelInvocationOutcome,
    TaskRun,
)
from rules.semantic_types import ProductType
from vocabulary.part_kinds import PartKind
from workflow.ai_budget import AiBudget
from workflow.bala_items import BALA_SOURCE
from workflow.depth_evidence import RUN_FAILED, depth_runs
from workflow.set_spend import depth_cap_usd, revision_spend, set_budget_left_usd
from workflow.slot_row_scope import (
    budget_limit,
    latest_slot_reader_run,
    slot_rows_and_unchosen_pages,
)

__all__ = [
    "AGAIN_MARK",
    "ALREADY_ASKED",
    "LEDGER_KEY_MARK",
    "NEEDS_CLAUDE",
    "NEEDS_REUSE",
    "READ_DEPTHS_AGAIN_WORKFLOW",
    "SWITCHED_OFF",
    "DepthsAgain",
    "DepthsAgainEstimate",
    "DepthsAgainRefused",
    "SpendLimits",
    "again_hash",
    "countertops_by_drawing",
    "estimate",
    "last_request",
    "refusal",
    "request_cap_usd",
    "request_ledger_tag",
    "request_waiting",
    "set_budget_left",
]

#: The outbox work "Read depths again" enqueues: `DatabaseStages.read_depths_again`.
READ_DEPTHS_AGAIN_WORKFLOW: Final = "read_depths_again"

#: How a depth reading made for one request names it in its `config_hash`.
AGAIN_MARK: Final = ";again="

SWITCHED_OFF: Final = (
    "The depth and sink reader is switched off (GV_DEPTH_READER), so depths are not read again. "
    "Ask an admin to switch it on."
)
NEEDS_CLAUDE: Final = (
    "The depth and sink reader is read by the two Claude readers, and they are switched off "
    "(GV_CLAUDE_READER_ENABLED). Ask an admin to switch them on."
)
NEEDS_REUSE: Final = (
    "Reading depths again needs stored answers reused (GV_READER_REUSE_ANSWERS): without them "
    "every question would be asked and paid for again. Ask an admin to switch it on."
)
CLOSED: Final = "This review is signed off or closed, so nothing on it is read again."
BUSY: Final = (
    "This drawing set is being read or checked right now. Read depths again once the "
    "reading or the checks have finished."
)
NOT_COUNTERTOPS: Final = (
    "The countertop checks are not in scope for this drawing set, so no depth is read."
)
NO_COUNTERTOP: Final = (
    "Bala suggested no countertop on this drawing set, so there is no countertop to read depths "
    "for."
)
ALREADY_ASKED: Final = (
    "Depths have already been asked to be read again on this drawing set. Wait for that to finish."
)
UNPRICED: Final = (
    "A call on this drawing set had no price, so no more calls can be made: an admin must fix "
    "the model rates first."
)
BUDGET_USED_UP: Final = (
    "This drawing set's AI budget is used up, and some questions have no stored answer, so "
    "reading depths again would stop before it finished. Ask an admin to raise the AI budget for "
    "a drawing set first."
)
#: The reason on each page of a request's reading that stopped part way while a complete earlier
#: reading is on record: the reading is kept, marked failed, and the earlier one is used (#1308).
STOPPED_KEEPS_EARLIER: Final = (
    "the depth reading stopped before it finished ({why}), so the earlier complete reading is "
    "still used"
)
#: How many times the outbox may try a request before it no longer blocks a new one (#1308
#: review): every failure the worker can name is parked at once, so this is only for one it
#: cannot, such as a failure to commit.
MAX_TRIES: Final = 3

#: Where a reading has handed the revision to a person, so depths may be read again.
READ_STATES: Final = frozenset({PackageState.AWAITING_REVIEW, PackageState.NEEDS_INPUT})

#: The questions one countertop takes, from the phase 5 readings design (§8): its view's kind,
#: the roles of its dimensions, and one to four numbers (a depth only, or a depth and a sink), each
#: asked of both readers. An estimate only: views code does not cut apart take none, and every
#: other view on the page takes a kind question.
QUESTIONS_PER_COUNTERTOP: Final = (2 * 3, 2 * 6)


@dataclass(frozen=True, slots=True)
class SpendLimits:
    """The settings the depth reader's spend cap is made from (`workflow.set_spend.depth_cap_usd`):
    the page-scaled budget, a fixed amount stated for a set, and the Claude readers' ceiling."""

    budget: AiBudget
    fixed_usd: Decimal | None
    claude_ceiling_usd: Decimal | None


def request_cap_usd(session: Session, revision_id: UUID, limits: SpendLimits) -> Decimal:
    """The most one "Read depths again" request may spend on this set, in dollars, rounded down to
    the cent: the worker's own cap (the Claude readers' ceiling, held under what the set's AI budget
    has left). It is a limit per request, not the money left: after a run that spent $0.29 under a
    $1.50 ceiling it is $1.50 again (#1328), so it is never shown as "left"."""
    cap = depth_cap_usd(
        session,
        revision_id,
        budget=limits.budget,
        fixed_usd=limits.fixed_usd,
        claude_ceiling_usd=limits.claude_ceiling_usd,
    )
    return cap.quantize(Decimal("0.01"), rounding=ROUND_DOWN)


def set_budget_left(session: Session, revision_id: UUID, limits: SpendLimits) -> Decimal:
    """What this drawing set's whole AI budget has left, in dollars, rounded down to the cent
    (#1328): the same budget and spend the worker's cap is made from (`workflow.set_spend`)."""
    left = set_budget_left_usd(
        session, revision_id, budget=limits.budget, fixed_usd=limits.fixed_usd
    )
    return left.quantize(Decimal("0.01"), rounding=ROUND_DOWN)


class DepthsAgainRefused(Exception):
    """The worker refuses a request, with a sentence for a person (`plain_reason`).

    The outbox parks such work at once with that sentence (`plain_stop_reason`), never
    redelivering it: a refusal no retry changes, said where the person asks for the status.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.plain_reason = reason


#: What every call-ledger run's task key holds after its stage's key
#: (`workflow.reading_watch.ledger_key_prefix`, which this module may not import: it is heavy).
LEDGER_KEY_MARK: Final = ":reader-calls:"


def request_ledger_tag(request: str) -> str:
    """What a request's call-ledger runs carry in their key, after `LEDGER_KEY_MARK` (#1328): the
    calls it paid for are found by its id (`_request_calls`)."""
    return f"again={request}:"


def again_hash(base_hash: str, request: str) -> str:
    """The `config_hash` of the depth reading made for one request (its outbox id)."""
    return f"{base_hash}{AGAIN_MARK}{request}"


def refusal(
    session: Session,
    revision: PackageRevision,
    *,
    switched_on: bool,
    claude_readers: bool,
    reuse_answers: bool,
    limits: SpendLimits,
) -> str | None:
    """Why depths may not be read again on this revision now, or `None` when they may.

    Asked by the API before the work is queued and by the worker before anything is read, so a
    setting changed in between is still honoured. **Nothing is asked that could only stop part
    way** (#1308 review): a set whose calls were not all priced, or whose budget is used up while
    a question has no stored answer, is refused before anything is read or paid.
    """
    if not switched_on:
        return SWITCHED_OFF
    if not claude_readers:
        return NEEDS_CLAUDE
    if not reuse_answers:
        return NEEDS_REUSE
    if revision.state in CLOSED_STATES:
        return CLOSED
    if PackageState(revision.state) not in READ_STATES:
        return BUSY
    product = package_product(session.get_one(Package, revision.package_id))
    if product not in (None, ProductType.COUNTERTOP):
        return NOT_COUNTERTOPS
    _spent, unpriced = revision_spend(session, revision.id)
    if unpriced or budget_limit(session, revision.id) == "unpriced":
        return UNPRICED
    if request_cap_usd(session, revision.id, limits) <= 0:
        shown = estimate(session, revision.id)
        if shown.questions_high > shown.stored_answers:
            return BUDGET_USED_UP
    return None


def countertops_by_drawing(session: Session, revision_id: UUID) -> dict[UUID, tuple[UUID, ...]]:
    """Each drawing view's countertops, by Bala's countertop suggestion.

    A suggestion counts when it is Bala's for the newest slot-reader run and nobody withdrew it, or
    when it is behind a live countertop item (an older reading's, after a re-read). Each counts once.

    Moved word for word from `workflow/depth_reader.py` (#1308), which still names it, so the API
    can count what the reader would read without importing the reader.
    """
    later = aliased(PartConfirmation)
    current = (
        select(PartConfirmation.decision)
        .where(
            PartConfirmation.part_proposal_id == PartProposal.id,
            ~exists().where(later.supersedes_id == PartConfirmation.id),
        )
        .order_by(PartConfirmation.created_at.desc())
        .limit(1)
        .scalar_subquery()
    )
    held: dict[UUID, set[UUID]] = {}
    newest = latest_slot_reader_run(session, revision_id)
    if newest is not None:
        for proposal_id, view_id, decision in session.execute(
            select(PartProposal.id, PartProposal.drawing_view_id, current)
            .join(PartProposalRow, PartProposalRow.part_proposal_id == PartProposal.id)
            .where(
                PartProposalRow.extraction_run_id == newest,
                PartProposalRow.slot_index.is_(None),
                PartProposal.kind == PartKind.COUNTERTOP.value,
                PartProposal.source == BALA_SOURCE,
            )
        ):
            if decision != PartDecision.WITHDRAWN.value:
                held.setdefault(view_id, set()).add(proposal_id)
    from workflow.item_plan import live_countertops

    live = live_countertops(session, revision_id, follow_rows=False)
    if live:
        rows = session.execute(
            select(PartConfirmation.part_proposal_id, DrawingItem.drawing_view_id)
            .join(DrawingItem, DrawingItem.id == PartConfirmation.drawing_item_id)
            .where(
                PartConfirmation.drawing_item_id.in_([item.item_id for item in live]),
                PartConfirmation.decision == PartDecision.CONFIRMED.value,
                ~exists().where(later.supersedes_id == PartConfirmation.id),
            )
        ).all()
        for proposal_id, view_id in rows:
            held.setdefault(view_id, set()).add(proposal_id)
    return {view: tuple(sorted(found, key=str)) for view, found in held.items()}


@dataclass(frozen=True, slots=True)
class DepthsAgainEstimate:
    """What a person sees before starting: what would be read, and about how many questions."""

    pages: int
    """Pages holding a countertop: the pages the reader would read."""
    countertops: int
    questions_low: int
    questions_high: int
    """About how many questions would be asked (each reader asked is one question)."""
    stored_answers: int
    """Of those, how many already have an accepted stored answer, reused at no cost."""
    basis: str
    """How the estimate was made, in plain words."""


def _asked(session: Session, run_id: UUID) -> tuple[int, int]:
    """The questions a depth reading asked (each reader once) and how many were answered."""
    asked, answered = session.execute(
        select(
            func.count(),
            func.count().filter(
                ModelInvocation.outcome == ModelInvocationOutcome.OK.value,
                ModelInvocation.private_raw_response.is_not(None),
            ),
        ).where(
            ModelInvocation.extraction_run_id == run_id,
            func.coalesce(ModelInvocation.reader_attempt_number, 1) == 1,
        )
    ).one()
    return int(asked or 0), int(answered or 0)


def _refused(session: Session, run_id: UUID) -> set[str]:
    """The refusals that stopped a depth reading part way: the budget, or the provider account."""
    return set(
        session.scalars(
            select(ModelInvocation.outcome).where(
                ModelInvocation.extraction_run_id == run_id,
                ModelInvocation.outcome.in_(
                    (
                        ModelInvocationOutcome.BUDGET_REFUSED.value,
                        ModelInvocationOutcome.ACCOUNT_REFUSED.value,
                    )
                ),
            )
        )
    )


def _failed_reason(session: Session, run_id: UUID) -> str | None:
    """Why a depth reading is marked failed, or `None` when it is not."""
    reason = session.scalars(
        select(ObservationCandidate.review_reason)
        .where(
            ObservationCandidate.extraction_run_id == run_id,
            ObservationCandidate.ambiguity_flags.contains([RUN_FAILED]),
        )
        .limit(1)
    ).first()
    if reason is None:
        return None
    return reason or "the depth reading failed"


def estimate(session: Session, revision_id: UUID) -> DepthsAgainEstimate:
    """About how many questions reading depths again would ask, and how many cost nothing.

    After a complete earlier depth reading of the set (not failed, not stopped part way): the
    questions it asked, and how many of them have an accepted stored answer (an identical question
    reuses it at no cost). Before any: at least a range per countertop
    (`QUESTIONS_PER_COUNTERTOP`), from Bala's countertops, or from the countertop rows read where
    Bala has not suggested any yet (Bala runs first).
    """
    by_drawing = countertops_by_drawing(session, revision_id)
    countertops = len({proposal for found in by_drawing.values() for proposal in found})
    pages = len(
        set(
            session.scalars(
                select(DrawingView.page_id).where(DrawingView.id.in_(list(by_drawing)))
            ).all()
        )
    )
    from_rows = False
    if not countertops:
        rows, _unchosen = slot_rows_and_unchosen_pages(session, revision_id)
        countertops = len(rows)
        pages = len({row.page_number for row in rows})
        from_rows = True
    complete = next(
        (
            run
            for run in depth_runs(session, revision_id)
            if _failed_reason(session, run.id) is None and not _refused(session, run.id)
        ),
        None,
    )
    if complete is not None:
        asked, answered = _asked(session, complete.id)
        return DepthsAgainEstimate(
            pages=pages,
            countertops=countertops,
            questions_low=asked,
            questions_high=asked,
            stored_answers=answered,
            basis=(
                f"The last complete depth reading of this set asked {asked} questions; {answered} "
                "of them have a stored answer, reused at no cost. Only a new or changed question "
                "is paid for."
            ),
        )
    low, high = QUESTIONS_PER_COUNTERTOP
    basis = (
        f"At least {low} to {high} questions for each countertop: what its view shows, what its "
        "dimensions are, and one to four numbers, each asked of both readers. Every other view on "
        "these pages is also asked what it shows."
    )
    if from_rows:
        basis += " Bala suggests the countertops first, from the countertop rows already read."
    return DepthsAgainEstimate(
        pages=pages,
        countertops=countertops,
        questions_low=low * countertops,
        questions_high=high * countertops,
        stored_answers=0,
        basis=basis,
    )


@dataclass(frozen=True, slots=True)
class _Spent:
    """What one request's depth reading asked, and what it paid for (#1328)."""

    asked: int
    """Questions asked (each reader once; a retry of the same question is not another)."""
    reused: int
    """Of those, answered by an answer paid for before the request: no cost to it."""
    paid: int
    """Of those, asked of a model during the request: paid for."""
    paid_micros: int
    """What the request's calls cost, retries included, in millionths of a dollar."""
    not_sent: int
    """Of those, never sent: refused by the budget or the provider account before any answer."""


#: A reader row that records an attempt never sent: refused by the budget or the provider account,
#: with no answer and no cost (`workflow.stages._CAUSED_FAILURES`). Neither reused nor paid for.
_NOT_SENT: Final = frozenset(
    {ModelInvocationOutcome.BUDGET_REFUSED.value, ModelInvocationOutcome.ACCOUNT_REFUSED.value}
)


def _request_calls(session: Session, request: str) -> dict[UUID, int | None]:
    """The calls a request paid for, by id, with their cost: the rows of the call-ledger runs whose
    key carries the request's tag (`request_ledger_tag`), across every try of the request."""
    return {
        row_id: cost
        for row_id, cost in session.execute(
            select(ModelInvocation.id, ModelInvocation.cost_micros)
            .join(ExtractionRun, ExtractionRun.id == ModelInvocation.extraction_run_id)
            .join(TaskRun, TaskRun.id == ExtractionRun.task_run_id)
            .where(
                TaskRun.idempotency_key.contains(
                    f"{LEDGER_KEY_MARK}{request_ledger_tag(request)}", autoescape=True
                )
            )
        )
    }


def _spent_by_request(session: Session, run_id: UUID, *, request: str) -> _Spent:
    """Split a request's questions into reused, paid and not asked, by the call ledger (#1328).

    A row of the run that names another row (`reused_from`) is either a stored answer reused or
    the $0 twin of a call the request made itself: the call ledger writes each attempt the moment
    it returns, and the stage then writes a twin naming it (`workflow/reading_watch.py`). Both have
    the same shape, so the row named tells them apart, **by the request's id, never by time**: a
    call in a ledger run tagged with this request (`request_ledger_tag`; an earlier try of the same
    request included, since the request paid for it) was paid for by the request; any other is a
    stored answer, reused at no cost, even one another reading paid for while this request waited.
    A row naming nothing is a paid call the stage wrote itself. An attempt the budget or the
    provider account refused before it was sent is "not asked", whether it is a twin or not. Each
    call is counted once, and a retry of a question (attempt 2 or later) is no extra question,
    though its cost counts.

    A request read before this tag existed has no tagged ledger run, so its new calls count as
    reused; asking again counts them rightly (every answer is then a stored one, at no cost).
    """
    request_calls = _request_calls(session, request)
    rows = session.execute(
        select(
            func.coalesce(ModelInvocation.reader_attempt_number, 1),
            ModelInvocation.reader_question_packet,
            ModelInvocation.cost_micros,
            ModelInvocation.outcome,
        )
        .where(ModelInvocation.extraction_run_id == run_id)
        .order_by(ModelInvocation.created_at, ModelInvocation.id)
    ).all()
    asked = reused = paid = paid_micros = not_sent = 0
    counted_calls: set[UUID] = set()
    for attempt, packet, cost, outcome in rows:
        first = attempt == 1
        asked += first
        if outcome in _NOT_SENT:
            not_sent += first
            continue
        if not (isinstance(packet, dict) and REUSED_FROM_KEY in packet):
            paid += first
            paid_micros += cost or 0
            continue
        try:
            target: UUID | None = UUID(str(packet[REUSED_FROM_KEY]))
        except ValueError:
            target = None
        if target is None or target not in request_calls or target in counted_calls:
            reused += first
            continue
        counted_calls.add(target)
        paid += first
        paid_micros += request_calls[target] or 0
    return _Spent(asked=asked, reused=reused, paid=paid, paid_micros=paid_micros, not_sent=not_sent)


def _dollars(amount_micros: int) -> str:
    """Micros as dollars to the cent; a paid amount under a cent says so rather than "$0.00"."""
    if 0 < amount_micros < 5_000:
        return "less than $0.01"
    shown = (Decimal(amount_micros) / 1_000_000).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"${shown}"


def _counted(spent: _Spent) -> str:
    """The plain count a request's status gives: questions, reused at no cost, and paid for."""
    counted = (
        f"{spent.asked} {'question' if spent.asked == 1 else 'questions'}, "
        f"{spent.reused} answered from stored answers at no cost"
    )
    if spent.paid or spent.paid_micros:
        counted += (
            f", {spent.paid} asked of the AI readers and paid for ({_dollars(spent.paid_micros)})"
        )
    if spent.not_sent:
        counted += f", {spent.not_sent} not asked"
    return counted


type _State = Literal["waiting", "not_read", "read", "stopped", "failed"]


@dataclass(frozen=True, slots=True)
class DepthsAgain:
    """What became of the newest "Read depths again" request of a revision."""

    request_id: UUID
    asked_at: datetime
    state: _State
    sentence: str


def request_waiting(session: Session, revision_id: UUID) -> bool:
    """Whether a request for this revision is queued and not yet handed over or parked: one at a
    time, so a second is refused (`ALREADY_ASKED`). A request tried `MAX_TRIES` times without
    finishing no longer blocks a new one."""
    return (
        session.scalar(
            select(OutboxEntry.id)
            .where(
                OutboxEntry.workflow == READ_DEPTHS_AGAIN_WORKFLOW,
                OutboxEntry.dispatched_at.is_(None),
                OutboxEntry.abandoned_at.is_(None),
                OutboxEntry.attempts < MAX_TRIES,
                OutboxEntry.payload["package_revision_id"].astext == str(revision_id),
            )
            .limit(1)
        )
        is not None
    )


def last_request(session: Session, revision_id: UUID) -> DepthsAgain | None:
    """The newest request's outcome, from the records it left: its outbox row and its reading."""
    entry = session.scalars(
        select(OutboxEntry)
        .where(
            OutboxEntry.workflow == READ_DEPTHS_AGAIN_WORKFLOW,
            OutboxEntry.payload["package_revision_id"].astext == str(revision_id),
        )
        .order_by(OutboxEntry.created_at.desc(), OutboxEntry.id.desc())
        .limit(1)
    ).first()
    if entry is None:
        return None

    def told(state: _State, sentence: str) -> DepthsAgain:
        return DepthsAgain(entry.id, entry.created_at, state, sentence)

    if entry.abandoned_at is not None:
        return told("not_read", entry.abandoned_reason or "The request was not run.")
    run = next(
        (
            run
            for run in depth_runs(session, revision_id)
            if run.config_hash.endswith(f"{AGAIN_MARK}{entry.id}")
        ),
        None,
    )
    if run is None:
        if entry.dispatched_at is not None:
            return told("not_read", "No depth reading was recorded for this request.")
        if entry.attempts >= MAX_TRIES:
            return told(
                "not_read",
                f"Tried {entry.attempts} times without finishing. Ask again to try once more; "
                "answers already paid for are reused at no cost.",
            )
        if entry.attempts:
            return told(
                "waiting",
                f"Tried {entry.attempts} times without finishing; it is tried again.",
            )
        return told("waiting", "Waiting to start, or reading now.")
    failed = _failed_reason(session, run.id)
    refused = _refused(session, run.id)
    counted = _counted(_spent_by_request(session, run.id, request=str(entry.id)))
    kept = (
        " The earlier complete reading is still used."
        if failed is not None
        else " The values not read are held, each with this reason."
    )
    if ModelInvocationOutcome.ACCOUNT_REFUSED.value in refused:
        return told(
            "stopped", f"Stopped: the AI provider account refused the calls ({counted}).{kept}"
        )
    if ModelInvocationOutcome.BUDGET_REFUSED.value in refused:
        return told(
            "stopped", f"Stopped: this drawing set's AI budget was reached ({counted}).{kept}"
        )
    if failed is not None:
        return told("failed", failed)
    return told("read", f"Depths read again: {counted}.")
