"""One assistant question, step by step, as the events the panel streams (#1128).

1. `stage records`, then the review's records are read.
2. A request to decide is refused with a fixed answer (`intent`), no model call.
3. With the assistant off (or no key): a records-only answer when code can answer the question,
   else a plain "off" answer.
4. Otherwise `stage model`, one call; the call is recorded where Usage reads it (with its cost, or
   an unknown cost when no price is stated) and committed; `stage guard`, then the guarded answer,
   or the records-only fallback when the reply was malformed or failed the guard.

No spending cap for now (Anant, 2026-10-10: "first our things should work, then optimization
later"); every call is still recorded on the Usage page.

A provider failure is an `error` event in plain English (busy, unavailable), never the provider's
words. Logs carry ids, lengths and codes only, never the question or the answer.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
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
from app.review.assistant.placeholders import placeholder_guide
from app.review.assistant.records import ReviewSnapshot, prompt_records
from app.review.assistant.records_only import (
    NO_ANSWER_IN_RECORDS,
    answer_for_question,
    fallback_answer,
    glossary_answer,
    judging_answer,
)
from app.runs.invocations import BedrockConverseInvocationRecorder

__all__ = [
    "DISABLED_TEXT",
    "AssistantRuntime",
    "CallLedger",
    "SessionLedger",
    "stream_answer",
]

_log = logging.getLogger(__name__)

DISABLED_TEXT: Final = (
    "The review assistant is not switched on here, so I can only answer the suggested questions, "
    "from the records."
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


def _stage(stage: str) -> Event:
    return "stage", StageEvent(id=stage, label=STAGE_LABELS[stage])  # type: ignore[arg-type]


def _refusal(snapshot: ReviewSnapshot, request: AssistantRequest) -> Draft:
    """The fixed refusal, with a button to the item the request was about when one is known."""
    page = page_mentioned(request.question)
    focus = request.focus
    if page is None and focus is not None:
        page = focus.page_number
    actions: list[tuple[str, str]] = []
    if page is not None:
        # A page the records do not have gets no button: never one to an unrelated item.
        waiting = next((item for item in snapshot.on_page(page) if item.needs_you), None)
        if waiting is not None:
            actions.append(("open_queue_item", waiting.id))
        elif page in snapshot.pages():
            actions.append(("open_page", f"P{page}"))
        return Draft(text=REFUSAL_TEXT, actions=tuple(actions))
    if focus is not None and focus.record_id is not None:
        record = snapshot.by_record_id(focus.record_id)
        if record is not None:
            actions.append(("open_queue_item", record.id))
    if not actions and snapshot.readiness.needing_you:
        actions.append(("open_queue_item", snapshot.readiness.needing_you[0]))
    return Draft(text=REFUSAL_TEXT, actions=tuple(actions))


def _checked_or_plain(draft: Draft, snapshot: ReviewSnapshot) -> Draft:
    """A records-only draft that passes the guard, or the plain statement if (by a bug) it does not."""
    try:
        check(draft, snapshot, by_model=False)
    except Exception as rejected:  # noqa: BLE001 - a bug here must still give an answer
        _log.warning("review assistant records-only answer failed the guard: %s", rejected)
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
    return () if record is None else (record.id,)


def stream_answer(
    request: AssistantRequest,
    *,
    load_snapshot: Callable[[], ReviewSnapshot],
    runtime: AssistantRuntime,
    ledger: CallLedger,
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
    built = build_request(
        model_id=model.model_id,
        records_json=prompt_records(snapshot),
        placeholders=placeholder_guide(snapshot),
        question=question,
        history=history,
        focus=request.focus,
        focus_ids=_focus_ids(snapshot, request),
    )
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
            _log.info("review assistant call refused unserved: %s", error.status, extra=log)
            yield "error", _ACCOUNT if error.status == 402 else _BUSY
        else:
            _log.info("review assistant call failed: %s", type(error).__name__, extra=log)
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
    except (MalformedAnswer, GuardRejected) as dropped:
        _log.info("review assistant answer dropped: %s", dropped, extra=log)
    except Exception:  # noqa: BLE001 - anything after the call falls back to the records
        _log.exception("review assistant answer could not be checked", extra=log)
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
