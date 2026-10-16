"""One assistant question, step by step, as the events the panel streams (#1128).

1. `stage records`, then the review's records are read.
2. A request to decide is refused with a fixed answer (`intent`), no model call.
3. With the assistant off (or no key): a records-only answer when code can answer the question,
   else a plain "off" answer.
4. Otherwise `stage model`, one call; the call is recorded where Usage reads it (with its cost, or
   an unknown cost when no price is stated) and committed; `stage guard`, then the guarded answer,
   or the records-only fallback when the reply was malformed or failed the guard.

**Looking at the page (#1431).** A question about a page (named in the question, the page in
focus, or the focused record's page) also sends that page's architect and vendor pictures and its
AI page review rows (`page_pictures.py`). Every question also carries the package's AI page review
summary (per page, how many rows of each result) when the source has one.

**Chat-assistant style (owner, 2026-10-16).** The model may read numbers off the pictures, count
and compare in its own words; the guard keeps only the hard rules (no action claimed or advised,
no pass or fail of its own, a recorded result quoted through its placeholder).

**Cost (#1431).** Every call is recorded on the Usage page. With a budget (`AssistantBudget`), a
question whose estimated cost is over the per-question cap is asked without its pictures, and
when the package's assistant spend plus the estimate would pass the package budget the model is
not asked: the records answer if they can, else a plain "budget used" answer. A call with no
stated price cannot be estimated, so it is asked and recorded with an unknown cost, as before.

A provider failure is an `error` event in plain English (busy, unavailable), never the provider's
words. Logs carry ids, lengths and codes only, never the question or the answer.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from decimal import Decimal
from time import monotonic_ns
from typing import Any, Final, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.review.assistant.answers import publish
from app.review.assistant.contract import (
    STAGE_LABELS,
    AnswerEvent,
    AssistantRequest,
    Draft,
    ErrorEvent,
    StageEvent,
)
from app.review.assistant.guard import GuardRejected, check, placeholder_keys
from app.review.assistant.intent import (
    REFUSAL_TEXT,
    is_decision_request,
    is_judging_question,
    page_mentioned,
)
from app.review.assistant.model import (
    PROMPT_ID,
    TEMPLATE_ID,
    AssistantModel,
    MalformedAnswer,
    ModelRefused,
    build_request,
    parse_answer,
)
from app.review.assistant.page_pictures import PageLook, PagePictureSource
from app.review.assistant.placeholders import placeholder_guide
from app.review.assistant.records import (
    FindingRecord,
    ReviewItemRecord,
    ReviewSnapshot,
    prompt_records,
)
from app.review.assistant.records_only import (
    NO_ANSWER_IN_RECORDS,
    answer_for_question,
    asks_about_deciding,
    fallback_answer,
    glossary_answer,
    is_ranking_question,
    judging_answer,
    needs_you_answer,
)
from app.runs.invocations import BedrockConverseInvocationRecorder
from app.runs.rates import ModelRates, call_cost_micros

__all__ = [
    "BUDGET_TEXT",
    "DISABLED_TEXT",
    "AssistantBudget",
    "AssistantRuntime",
    "CallLedger",
    "SessionLedger",
    "assistant_spent_micros",
    "stream_answer",
]

_log = logging.getLogger(__name__)

DISABLED_TEXT: Final = (
    "The review assistant is not switched on here, so I can only answer the suggested questions, "
    "from the records."
)
BUDGET_TEXT: Final = (
    "This package has used its assistant budget, so Claude Sonnet was not asked. The suggested "
    "questions still work from the records."
)
_BUSY: Final = ErrorEvent(
    code="model_busy",
    message="Claude Sonnet is busy right now and did not answer. Please ask again in a minute.",
)
_UNAVAILABLE: Final = ErrorEvent(
    code="model_unavailable",
    message=(
        "Claude Sonnet could not be reached or did not answer in time. Please ask again; the "
        "suggested questions work from the records."
    ),
)
_ACCOUNT: Final = ErrorEvent(
    code="model_account",
    message="The AI service account needs attention, so Claude Sonnet did not answer. "
    "Tell your admin.",
)
_RECORDS_UNAVAILABLE: Final = ErrorEvent(
    code="records_unavailable",
    message="This review's records could not be read. Reload the page and ask again.",
)

type Event = tuple[str, BaseModel]


class CallLedger(Protocol):
    """Where a model call is recorded: the Usage page's rows."""

    def record(
        self,
        *,
        model_id: str,
        started_ns: int,
        response: Mapping[str, Any] | None,
        error: BaseException | None,
    ) -> None: ...

    def before_call(self) -> None:
        """End any open read transaction before the (slow) model call."""


@dataclass(frozen=True, slots=True)
class SessionLedger:
    """The request's session: the call row is written and committed at once, so the Usage page
    sees it even though the stream goes on. Its cost comes from `GV_MODEL_RATES_FILE`; a model
    with no stated price is recorded with an unknown cost (`NULL`), which Usage counts as
    unpriced, never as free (`app/runs/rates.py`)."""

    session: Session
    package_revision_id: UUID

    def before_call(self) -> None:
        # The records are read and nothing is written yet: ending the transaction releases its
        # connection-held snapshot and locks while the model is asked (seconds, not milliseconds).
        self.session.rollback()

    def record(
        self,
        *,
        model_id: str,
        started_ns: int,
        response: Mapping[str, Any] | None,
        error: BaseException | None,
    ) -> None:
        try:
            BedrockConverseInvocationRecorder(self.session, self.package_revision_id).record(
                model_id=model_id,
                prompt_id=PROMPT_ID,
                template_id=TEMPLATE_ID,
                started_ns=started_ns,
                response=response,
                error=error,
            )
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise


@dataclass(frozen=True, slots=True)
class AssistantRuntime:
    """What a deployment configured. `model` is `None` when the assistant is off or has no key."""

    model: AssistantModel | None
    max_history_turns: int
    page_pictures: bool = False
    """Whether a question about a page sends that page's pictures (#1431)."""
    budget_usd: Decimal | None = None
    """The assistant's budget per package revision; `None` is no budget (tests)."""
    question_cap_usd: Decimal | None = None


@dataclass(frozen=True, slots=True)
class AssistantBudget:
    """The package's assistant budget and the cap on one question, in millionths of a dollar."""

    package_cap_micros: int
    question_cap_micros: int
    spent_micros: Callable[[], int]
    """What this package's assistant calls have cost so far, from the recorded calls."""
    rates: ModelRates | None


#: Tokens for the API's own framing of a request, on top of what it carries.
_REQUEST_OVERHEAD_TOKENS: Final = 500
_TILE_PX: Final = 28


def _estimate_micros(
    request: Mapping[str, Any], look: PageLook | None, model_id: str, rates: ModelRates | None
) -> int | None:
    """An upper bound on one call's cost: one token per text byte, each picture's tile count, and
    the whole output allowance; `None` when the model has no stated price."""
    tokens = _REQUEST_OVERHEAD_TOKENS
    for block in request.get("system") or ():
        tokens += len(str(block.get("text", "")).encode("utf-8"))
    for message in request.get("messages") or ():
        for block in message.get("content") or ():
            if isinstance(block.get("text"), str):
                tokens += len(block["text"].encode("utf-8"))
    for picture in () if look is None else look.pictures:
        tokens += -(-picture.width_px // _TILE_PX) * -(-picture.height_px // _TILE_PX)
    output = int(request.get("inferenceConfig", {}).get("maxTokens", 0))
    return call_cost_micros(rates, model_id, tokens, output)


def assistant_spent_micros(session: Session, package_revision_id: UUID) -> int:
    """What the assistant's recorded calls on this revision cost (unpriced calls count as zero)."""
    from sqlalchemy import func, select

    from app.models import ModelInvocation

    return int(
        session.scalar(
            select(func.coalesce(func.sum(ModelInvocation.cost_micros), 0)).where(
                ModelInvocation.package_revision_id == package_revision_id,
                ModelInvocation.prompt_id.like("review-assistant-%"),
            )
        )
        or 0
    )


def _asked_page(snapshot: ReviewSnapshot, request: AssistantRequest) -> int | None:
    """The page a question is about: named in it, else in focus, else the focused record's."""
    named = page_mentioned(request.question)
    if named is not None:
        return named
    focus = request.focus
    if focus is None:
        return None
    if focus.page_number is not None:
        return focus.page_number
    if focus.record_id is None:
        return None
    record = snapshot.by_record_id(focus.record_id)
    if isinstance(record, FindingRecord):
        # A finding names its pages; one page is looked at only when it names exactly one.
        return record.pages[0] if len(record.pages) == 1 else None
    if record is not None:
        return record.page_number
    entry = snapshot.review_item_by_key(focus.record_id)
    return None if entry is None else entry.page_number


def _stage(stage: str) -> Event:
    return "stage", StageEvent(id=stage, label=STAGE_LABELS[stage])  # type: ignore[arg-type]


#: A review-list entry named by its short id in a question ("confirm R2").
_REVIEW_ID_MENTIONED: Final = re.compile(r"\b([Rr][0-9]+)\b")


#: Words that say which group of the review list a request is about.
_GROUP_WORDS: Final = (
    ("failure", re.compile(r"\b(?:failure|failures|failed|fail|agree)\b", re.IGNORECASE)),
    ("not_checked", re.compile(r"\b(?:not checked|settle|didn'?t run)\b", re.IGNORECASE)),
)


def _open_entry(
    snapshot: ReviewSnapshot, question: str, page: int | None
) -> ReviewItemRecord | None:
    """The open review-list entry a refused request is about: on its page when it names one, and
    in the group it names ("agree with the failure" opens the failure), else the first one."""
    entries = [
        item
        for item in snapshot.review_items
        if item.needs_you and (page is None or item.page_number == page)
    ]
    for group, words in _GROUP_WORDS:
        if words.search(question):
            named = next((item for item in entries if item.group == group), None)
            if named is not None:
                return named
    return entries[0] if entries else None


def _refusal(snapshot: ReviewSnapshot, request: AssistantRequest) -> Draft:
    """The fixed refusal, with a button to the item the request was about when one is known."""
    named = _REVIEW_ID_MENTIONED.search(request.question)
    entry = None if named is None else snapshot.review_item(named.group(1).upper())
    if entry is not None and entry.needs_you:
        # "Confirm R2": the button goes to that entry of the review list.
        return Draft(text=REFUSAL_TEXT, actions=(("open_review_item", entry.id),))
    page = page_mentioned(request.question)
    focus = request.focus
    if page is None and focus is not None:
        page = focus.page_number
    actions: list[tuple[str, str]] = []
    if page is not None:
        # A page the records do not have gets no button: never one to an unrelated item.
        waiting = next((item for item in snapshot.on_page(page) if item.needs_you), None)
        listed = _open_entry(snapshot, request.question, page)
        if waiting is not None:
            actions.append(("open_queue_item", waiting.id))
        if listed is not None:
            actions.append(("open_review_item", listed.id))
        if not actions and page in snapshot.pages():
            actions.append(("open_page", f"P{page}"))
        return Draft(text=REFUSAL_TEXT, actions=tuple(actions))
    if focus is not None and focus.record_id is not None:
        record = snapshot.by_record_id(focus.record_id)
        entry = snapshot.review_item_by_key(focus.record_id)
        if record is not None:
            actions.append(("open_queue_item", record.id))
        elif entry is not None and entry.needs_you:
            actions.append(("open_review_item", entry.id))
    first_open = _open_entry(snapshot, request.question, None)
    if not actions and first_open is not None:
        # With the review list read, the button goes to the first entry that waits (#1354).
        actions.append(("open_review_item", first_open.id))
    if not actions and snapshot.readiness.needing_you:
        actions.append(("open_queue_item", snapshot.readiness.needing_you[0]))
    return Draft(text=REFUSAL_TEXT, actions=tuple(actions))


def _checked_or_plain(draft: Draft, snapshot: ReviewSnapshot) -> Draft:
    """A records-only draft that passes the guard, or the plain statement if (by a bug) it does not."""
    try:
        check(draft, snapshot, by_model=False)
    except Exception as rejected:  # noqa: BLE001 - a bug here must still give an answer
        reason = str(rejected) if isinstance(rejected, GuardRejected) else type(rejected).__name__
        _log.warning("review assistant records-only answer failed the guard: %s", reason)
        return Draft(text=NO_ANSWER_IN_RECORDS)
    return draft


def _publish_safely(
    draft: Draft,
    snapshot: ReviewSnapshot,
    *,
    mode: Literal["records_only", "refused", "disabled"],
    question: str,
) -> AnswerEvent:
    """Publish a code-written draft; if that fails (a bug), publish the plain statement instead."""
    checked = mode == "records_only"
    try:
        return publish(draft, snapshot, mode=mode, question=question, checked=checked)
    except Exception:  # noqa: BLE001 - the stream must end with an answer, never silently
        _log.exception("review assistant could not publish a records-only answer")
        return publish(
            Draft(text=NO_ANSWER_IN_RECORDS), snapshot, mode=mode, question=question, checked=False
        )


def _focus_ids(snapshot: ReviewSnapshot, request: AssistantRequest) -> tuple[str, ...]:
    if request.focus is None or request.focus.record_id is None:
        return ()
    record = snapshot.by_record_id(request.focus.record_id)
    if record is not None:
        return (record.id,)
    entry = snapshot.review_item_by_key(request.focus.record_id)
    return () if entry is None else (entry.id,)


def stream_answer(
    request: AssistantRequest,
    *,
    load_snapshot: Callable[[], ReviewSnapshot],
    runtime: AssistantRuntime,
    ledger: CallLedger,
    pictures: PagePictureSource | None = None,
    budget: AssistantBudget | None = None,
) -> Iterator[Event]:
    """The events for one question, in the order the steps happen."""
    question = request.question
    log = {"question_chars": len(question), "history_turns": len(request.history)}
    yield _stage("records")
    try:
        snapshot = load_snapshot()
    except Exception:  # noqa: BLE001 - reported as a plain event; the stream has started
        _log.exception("review assistant could not read the records", extra=log)
        yield "error", _RECORDS_UNAVAILABLE
        return

    if is_decision_request(question):
        _log.info("review assistant refused a decision request", extra=log)
        yield (
            "answer",
            _publish_safely(
                _refusal(snapshot, request), snapshot, mode="refused", question=question
            ),
        )
        return

    glossary = glossary_answer(question)
    if glossary is not None:
        # What an app word means: a fixed explanation in code, no model and no record facts.
        yield _stage("guard")
        yield (
            "answer",
            _publish_safely(
                _checked_or_plain(glossary, snapshot),
                snapshot,
                mode="records_only",
                question=question,
            ),
        )
        return

    if is_ranking_question(question):
        # "Which look most worrying?": code's order from the records, never a model's judgement.
        # A ranking question about deciding (or one that also asks to judge) says whose decision
        # it is, so the item named first is never a recommendation to approve it.
        _log.info("review assistant answered a ranking question from the records", extra=log)
        deciding = is_judging_question(question) or asks_about_deciding(question)
        yield _stage("guard")
        yield (
            "answer",
            _publish_safely(
                _checked_or_plain(needs_you_answer(snapshot, about_deciding=deciding), snapshot),
                snapshot,
                mode="records_only",
                question=question,
            ),
        )
        return

    if is_judging_question(question):
        # Asked to judge: the recorded outcome and whose decision it is, never a model's view.
        _log.info("review assistant answered a request to judge from the records", extra=log)
        yield _stage("guard")
        judged = judging_answer(snapshot, page_mentioned(question), request.focus)
        yield (
            "answer",
            _publish_safely(
                _checked_or_plain(judged, snapshot),
                snapshot,
                mode="records_only",
                question=question,
            ),
        )
        return

    model = runtime.model
    if model is None:
        draft = answer_for_question(snapshot, question, request.focus)
        if draft is None:
            yield (
                "answer",
                _publish_safely(
                    Draft(text=DISABLED_TEXT), snapshot, mode="disabled", question=question
                ),
            )
            return
        yield _stage("guard")
        yield (
            "answer",
            _publish_safely(
                _checked_or_plain(draft, snapshot), snapshot, mode="records_only", question=question
            ),
        )
        return

    # Only the reviewer's own earlier questions go back to the model: text a client says the
    # assistant wrote is not trusted, so it never reaches the prompt.
    asked = tuple(turn for turn in request.history if turn.role == "user")
    history = asked[-runtime.max_history_turns :] if runtime.max_history_turns else ()
    look: PageLook | None = None
    page = _asked_page(snapshot, request)
    if pictures is not None and page is not None:
        try:
            look = pictures.for_page(page)
        except Exception as failed:  # noqa: BLE001 - no pictures never stops the answer
            _log.warning(
                "review assistant could not make the page pictures: %s",
                type(failed).__name__,
                extra=log,
            )

    overview: tuple[Mapping[str, Any], ...] = ()
    summarise = getattr(pictures, "ai_review_overview", None)
    if callable(summarise):
        try:
            overview = tuple(summarise())
        except Exception as failed:  # noqa: BLE001 - no summary never stops the answer
            _log.warning(
                "review assistant could not read the AI page review summary: %s",
                type(failed).__name__,
                extra=log,
            )

    def _built(shown: PageLook | None) -> dict[str, Any]:
        return build_request(
            model_id=model.model_id,
            records_json=prompt_records(snapshot),
            placeholders=placeholder_guide(snapshot),
            question=question,
            history=history,
            focus=request.focus,
            focus_ids=_focus_ids(snapshot, request),
            look=shown,
            overview=overview,
        )

    built = _built(look)
    if budget is not None:
        estimate = _estimate_micros(built, look, model.model_id, budget.rates)
        if estimate is not None and estimate > budget.question_cap_micros and look is not None:
            # Over the cap with the pictures: asked about the records (and AI review rows) only.
            look = PageLook(
                page_number=look.page_number, pictures=(), ai_review_rows=look.ai_review_rows
            )
            built = _built(look)
            estimate = _estimate_micros(built, look, model.model_id, budget.rates)
        try:
            spent = budget.spent_micros()
        except Exception:  # noqa: BLE001 - an unknown spend stops the call rather than overspending
            _log.exception("review assistant could not read its spend", extra=log)
            spent = budget.package_cap_micros
        if estimate is not None and (
            estimate > budget.question_cap_micros or spent + estimate > budget.package_cap_micros
        ):
            _log.info("review assistant budget stop", extra=log)
            draft = answer_for_question(snapshot, question, request.focus)
            yield _stage("guard")
            yield (
                "answer",
                _publish_safely(
                    (
                        _checked_or_plain(draft, snapshot)
                        if draft is not None
                        else Draft(text=BUDGET_TEXT)
                    ),
                    snapshot,
                    mode="records_only" if draft is not None else "disabled",
                    question=question,
                ),
            )
            return
    looked_at = look.page_number if look is not None and look.pictures else None
    # Nothing is held open in the database while the model thinks.
    try:
        ledger.before_call()
    except Exception:  # noqa: BLE001 - only ends a read transaction; never stops the answer
        _log.exception("review assistant could not end the read transaction", extra=log)
    yield _stage("model")
    started_ns = monotonic_ns()
    response: Mapping[str, Any] | None = None
    error: BaseException | None = None
    try:
        response = model.answer(built)
    except Exception as raised:  # noqa: BLE001 - every provider failure becomes a plain event
        error = raised
    try:
        ledger.record(
            model_id=model.model_id, started_ns=started_ns, response=response, error=error
        )
    except Exception:  # noqa: BLE001 - the answer still goes out; the failure is logged
        _log.exception("review assistant call could not be recorded", extra=log)

    if error is not None:
        if isinstance(error, ModelRefused):
            _log.warning("review assistant call refused unserved: %s", error.status, extra=log)
            yield "error", _ACCOUNT if error.status == 402 else _BUSY
        else:
            _log.warning("review assistant call failed: %s", type(error).__name__, extra=log)
            yield "error", _UNAVAILABLE
        return

    yield _stage("guard")
    proposed: Draft | None = None
    try:
        assert response is not None
        proposed = parse_answer(response)
        check(proposed, snapshot)
        answer = publish(
            proposed, snapshot, mode="llm", question=question, checked=True, model_id=model.model_id
        )
        if looked_at is not None:
            answer = answer.model_copy(update={"looked_at_page": looked_at})
    except (MalformedAnswer, GuardRejected) as dropped:
        # The reason code only (never the question or the answer), at a level a local dev
        # server shows, so a developer can see why an answer fell back.
        _log.warning("review assistant answer dropped: %s", dropped, extra=log)
    except Exception as failed:  # noqa: BLE001 - anything after the call falls back to the records
        # The exception's type only: its message could quote the answer.
        _log.warning(
            "review assistant answer could not be checked: %s", type(failed).__name__, extra=log
        )
    else:
        yield "answer", answer
        return
    fallback = fallback_answer(
        snapshot,
        question,
        cited=() if proposed is None else placeholder_keys(proposed.text),
        focus=request.focus,
    )
    yield (
        "answer",
        _publish_safely(
            _checked_or_plain(fallback, snapshot), snapshot, mode="records_only", question=question
        ),
    )
