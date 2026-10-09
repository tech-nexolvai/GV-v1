"""Turning a checked `Draft` into the published `AnswerEvent` (#1128).

Short ids become the screen's real ids; citation labels, action labels, sources and suggestions are
written here by code, never by the model, so no model text reaches the panel outside `text` (which
the guard has checked).
"""

from __future__ import annotations

from typing import Final, Literal

from app.review.assistant.contract import (
    Action,
    AnswerEvent,
    Citation,
    CountertopEvidence,
    Draft,
    Evidence,
    GroupEvidence,
    OpenPageAction,
    OpenQueueItemAction,
)
from app.review.assistant.placeholders import render
from app.review.assistant.records import CountertopRecord, ReviewSnapshot
from app.review.assistant.starters import starters

__all__ = ["publish", "suggestions_for"]

MAX_ACTIONS: Final = 3
MAX_SUGGESTIONS: Final = 3

type _Group = Literal["blockers", "no_countertop_pages", "rows_not_checked"]
_GROUP_SOURCES: Final[dict[_Group, str]] = {
    "blockers": "Sign-off readiness: what still needs a decision",
    "no_countertop_pages": "Pages with no countertop",
    "rows_not_checked": "Countertop rows not checked",
}

type Mode = Literal["llm", "records_only", "refused", "disabled"]


def suggestions_for(snapshot: ReviewSnapshot, question: str) -> tuple[str, ...]:
    """Up to three follow-ups: the starters, without the question just asked."""
    asked = question.strip().casefold()
    return tuple(item for item in starters(snapshot) if item.casefold() != asked)[:MAX_SUGGESTIONS]


def _citation(snapshot: ReviewSnapshot, short_id: str) -> tuple[Citation, str]:
    if short_id.startswith("P"):
        page = int(short_id[1:])
        return (
            Citation(kind="page", page_number=page, record_id=None, label=f"Page {page}"),
            f"Records for page {page}",
        )
    record = snapshot.record(short_id)
    if record is None:  # the guard refuses this before publishing; kept total for safety
        raise ValueError("cannot publish a citation to a record the snapshot does not have")
    if isinstance(record, CountertopRecord):
        return (
            Citation(
                kind="countertop",
                page_number=record.page_number,
                record_id=record.record_id,
                label=f"Page {record.page_number} · {record.label}",
            ),
            f"Countertop result, page {record.page_number} ({record.label})",
        )
    page_number = record.pages[0] if record.pages else None
    where = "" if page_number is None else f"Page {page_number} · "
    return (
        Citation(
            kind="finding",
            page_number=page_number,
            record_id=record.record_id,
            label=f"{where}{record.check_name}",
        ),
        f"Check result: {record.check_name}",
    )


def publish(
    draft: Draft,
    snapshot: ReviewSnapshot,
    *,
    mode: Mode,
    question: str,
    checked: bool,
    model_id: str | None = None,
) -> AnswerEvent:
    """The panel's answer for a template the guard accepted: code fills every placeholder, puts
    each record's marker after its facts and lists the records named as the citations."""
    rendered = render(draft.text, snapshot)
    citations: list[Citation] = []
    sources: list[str] = []
    for short_id in rendered.citations:
        citation, source = _citation(snapshot, short_id)
        citations.append(citation)
        sources.append(source)

    evidence: list[Evidence] = []
    for item in dict.fromkeys((*draft.evidence, *rendered.groups)):
        if item.startswith("C"):
            record = snapshot.record(item)
            if record is not None:
                evidence.append(CountertopEvidence(kind="countertop", record_id=record.record_id))
            continue
        if item == "no_countertop_pages" and not snapshot.pages_without_countertop:
            continue
        if item == "rows_not_checked" and not snapshot.rows_not_checked:
            continue
        for group, source in _GROUP_SOURCES.items():
            if item == group:
                evidence.append(GroupEvidence(kind=group))
                sources.append(source)

    actions: list[Action] = []
    for kind, target in dict.fromkeys(draft.actions):
        if kind == "open_page" and target.startswith("P"):
            page = int(target[1:])
            actions.append(
                OpenPageAction(kind="open_page", page_number=page, label=f"Open page {page}")
            )
        elif kind == "open_queue_item":
            record = snapshot.record(target)
            if record is not None:
                where = (
                    f"page {record.page_number}"
                    if isinstance(record, CountertopRecord)
                    else record.check_name
                )
                actions.append(
                    OpenQueueItemAction(
                        kind="open_queue_item",
                        record_id=record.record_id,
                        label=f"Open {where} in the queue",
                    )
                )

    return AnswerEvent(
        text=rendered.text,
        citations=tuple(citations),
        evidence=tuple(evidence),
        actions=tuple(actions[:MAX_ACTIONS]),
        suggestions=suggestions_for(snapshot, question),
        checked=checked,
        mode=mode,
        model_id=model_id,
        sources=tuple(dict.fromkeys(sources)),
    )
