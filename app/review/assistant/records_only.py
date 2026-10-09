"""Answers written by code from the records alone (#1128): no model, every fact a placeholder.

Used for the starter-type questions when the assistant is off, as the fallback when a model answer
is dropped, for a focus record, and for a request to judge. Each answer is a template whose facts
are placeholders (`{C1.outcome}`, `{count.needs_you}`, `{signoff.status}`), filled and cited by the
same code as a model answer, and checked by the same guard (placeholders, one record per sentence,
no digit outside a placeholder). Only the fixed connecting words are code's own.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Final

from app.review.assistant.contract import Draft, Focus
from app.review.assistant.placeholders import Slot, UnknownPlaceholder, value_of
from app.review.assistant.records import CountertopRecord, FindingRecord, ReviewSnapshot

__all__ = [
    "NOTHING_ON_THAT_PAGE",
    "NOTHING_TO_DECIDE_THERE",
    "NO_ANSWER_IN_RECORDS",
    "YOUR_DECISION",
    "answer_for_question",
    "blockers_answer",
    "fallback_answer",
    "judging_answer",
    "records_answer",
]

#: How many records one records-only answer lists before pointing to the queue for the rest.
MAX_LISTED: Final = 10
#: How many records the fallback explains in full.
MAX_EXPLAINED: Final = 3

NOTHING_ON_THAT_PAGE: Final = "This review's records have nothing about that page."
NO_ANSWER_IN_RECORDS: Final = (
    "I couldn't check an answer to that against this review's records, so I've left it out. "
    "Try one of the suggested questions, or open the page on the drawing."
)
NOTHING_HAS_RUN: Final = (
    "No checks have run on this package yet, so there are no results to explain."
)

_PAGE: Final = re.compile(r"\b(?:page|sheet|pg|p)\.?\s*#?\s*(\d+)\b")
_BLOCKERS: Final = re.compile(
    r"before (?:i |we )?(?:can )?sign off|left (?:to do|before|to decide)|(?:what|anything)(?:'s| is)? "
    r"(?:still )?left|block(?:s|ing|ed)?\b|still (?:need|open)|remaining|what needs (?:me|my|you|a decision)"
    r"|(?:my|the) queue|can (?:i|we) sign off|ready (?:to|for) sign off"
)
_NO_COUNTERTOP: Final = re.compile(r"no countertop|without (?:a |any )?countertop|no stone")
_NOT_CHECKED: Final = re.compile(
    r"not (?:been )?checked|unchecked|was ?n[o']?t checked|second countertop|skipped"
)
_FAIL_WORDS: Final = re.compile(r"\bfail|wrong|correction|incorrect|off by|mismatch")
_NEED_WORDS: Final = re.compile(
    r"need(?:s)? (?:me|you|my|a decision)|held|hold|decide|decision|review"
)


def _folded(question: str) -> str:
    folded = question.casefold().replace("’", "'")
    folded = re.sub(r"sign[\s-]?off", "sign off", folded)
    return re.sub(r"\s+", " ", folded).strip()


def _has(snapshot: ReviewSnapshot, key: str, field: str) -> bool:
    try:
        value_of(snapshot, Slot(key, field, None, 0, 0))
    except UnknownPlaceholder:
        return False
    return True


def _record_lines(snapshot: ReviewSnapshot, item: CountertopRecord | FindingRecord) -> list[str]:
    """One sentence per fact the record holds, each a placeholder of that record."""
    key = item.id

    def has(field: str) -> bool:
        return _has(snapshot, key, field)

    if isinstance(item, CountertopRecord):
        lines = [f"The countertop on {{{key}.page}} ({{{key}.label}}) {{{key}.outcome}}."]
        values = [
            f"{{{key}.{field}}}" for field in ("printed", "needed", "difference") if has(field)
        ]
        if values:
            lines.append("; ".join(values) + ".")
        if has("tolerance"):
            lines.append(f"{{{key}.check}}, {{{key}.tolerance}}.")
        if (
            has("reason")
            and item.outcome != "PASS"
            and item.rule is not None
            and item.rule.reason != item.hold_reason
        ):
            lines.append(f"{{{key}.reason}}.")
        fields = ("hold_reason", "drawn_length_note", "architect", "decision", "needs_you")
    else:
        where = f" on {{{key}.page}}" if has("page") else ""
        lines = [f"The {{{key}.check}} check{where} {{{key}.outcome}}."]
        fields = ("reason", "comparison", "tolerance", "decision", "needs_you")
    lines.extend(f"{{{key}.{field}}}." for field in fields if has(field))
    return lines


def _explain(snapshot: ReviewSnapshot, items: Sequence[CountertopRecord | FindingRecord]) -> Draft:
    paragraphs = [" ".join(_record_lines(snapshot, item)) for item in items[:MAX_EXPLAINED]]
    if len(items) > MAX_EXPLAINED:
        paragraphs.append("The rest are listed in the queue.")
    return Draft(
        text="\n\n".join(paragraphs),
        evidence=tuple(item.id for item in items[:MAX_EXPLAINED] if item.id.startswith("C")),
        actions=_actions_for(items),
    )


def _actions_for(items: Sequence[CountertopRecord | FindingRecord]) -> tuple[tuple[str, str], ...]:
    actions: list[tuple[str, str]] = []
    first = items[0] if items else None
    if isinstance(first, CountertopRecord):
        actions.append(("open_page", f"P{first.page_number}"))
    elif isinstance(first, FindingRecord) and first.pages:
        actions.append(("open_page", f"P{first.pages[0]}"))
    waiting = next((item for item in items if item.needs_you), None)
    if waiting is not None:
        actions.append(("open_queue_item", waiting.id))
    return tuple(actions)


def _list_line(snapshot: ReviewSnapshot, item: CountertopRecord | FindingRecord) -> str:
    key = item.id
    if isinstance(item, CountertopRecord):
        return f"- The countertop on {{{key}.page}} ({{{key}.label}}) {{{key}.outcome}}"
    where = f" on {{{key}.page}}" if _has(snapshot, key, "page") else ""
    return f"- The {{{key}.check}} check{where} {{{key}.outcome}}"


def _waiting(snapshot: ReviewSnapshot) -> list[CountertopRecord | FindingRecord]:
    return [
        record
        for short_id in snapshot.readiness.needing_you
        if (record := snapshot.record(short_id)) is not None
    ]


def blockers_answer(snapshot: ReviewSnapshot) -> Draft:
    """What still stands between this package and sign-off."""
    if not snapshot.checks_have_run:
        return Draft(text=NOTHING_HAS_RUN, evidence=("blockers",))
    waiting = _waiting(snapshot)
    lines = ["{signoff.status}."]
    if waiting:
        lines.append("These still need your decision:")
        lines.extend(_list_line(snapshot, item) for item in waiting[:MAX_LISTED])
        if len(waiting) > MAX_LISTED:
            lines.append("The rest are listed in the queue.")
    return Draft(
        text="\n".join(lines),
        evidence=("blockers",),
        actions=(() if not waiting else (("open_queue_item", waiting[0].id),)),
    )


def _notes_answer(snapshot: ReviewSnapshot, *, no_countertop: bool) -> Draft:
    notes = snapshot.pages_without_countertop if no_countertop else snapshot.rows_not_checked
    group = "no_countertop_pages" if no_countertop else "rows_not_checked"
    field = "no_countertop" if no_countertop else "second_row"
    if not notes:
        return Draft(text=f"{{count.{group}}}.", evidence=(group,))
    lines = [f"{{count.{group}}}:"]
    lines.extend(f"- {{P{note.page_number}.{field}}}" for note in notes[:MAX_LISTED])
    if len(notes) > MAX_LISTED:
        lines.append("The rest are listed under the countertop results.")
    return Draft(
        text="\n".join(lines),
        evidence=(group,),
        actions=(("open_page", f"P{notes[0].page_number}"),),
    )


def _page_notes(snapshot: ReviewSnapshot, page: int) -> list[str]:
    return [
        f"{{P{page}.{field}}}."
        for field in ("no_countertop", "second_row")
        if _has(snapshot, f"P{page}", field)
    ]


def _page_answer(snapshot: ReviewSnapshot, page: int, *, want: str) -> Draft:
    records = list(snapshot.on_page(page))
    notes = _page_notes(snapshot, page)
    if not records and not notes:
        return Draft(text=NOTHING_ON_THAT_PAGE)
    if want == "fail":
        chosen = [item for item in records if item.outcome == "FAIL"] or records
    elif want == "need":
        chosen = [item for item in records if item.needs_you] or records
    else:
        chosen = records
    draft = _explain(snapshot, chosen) if chosen else Draft(text="")
    if not notes:
        return draft
    return Draft(
        text="\n\n".join(part for part in (draft.text, " ".join(notes)) if part),
        evidence=draft.evidence,
        actions=draft.actions or (("open_page", f"P{page}"),),
    )


def records_answer(snapshot: ReviewSnapshot, short_ids: Sequence[str]) -> Draft | None:
    """Explain these records (unknown ids ignored); `None` when none of them exists."""
    items = [
        record
        for short_id in dict.fromkeys(short_ids)
        if (record := snapshot.record(short_id)) is not None
    ]
    return _explain(snapshot, items) if items else None


def _focus_answer(snapshot: ReviewSnapshot, focus: Focus | None) -> Draft | None:
    if focus is None:
        return None
    if focus.record_id is not None:
        record = snapshot.by_record_id(focus.record_id)
        if record is not None:
            return _explain(snapshot, [record])
    if focus.page_number is not None:
        return _page_answer(snapshot, focus.page_number, want="any")
    return None


def answer_for_question(
    snapshot: ReviewSnapshot, question: str, focus: Focus | None = None
) -> Draft | None:
    """A records-only answer when the question is one code can answer; otherwise `None`.

    Covers the starter questions (what blocks sign-off, pages with no countertop, rows not checked,
    why a page failed or needs the reviewer) and a question about one page or the focus record.
    """
    folded = _folded(question)
    page_match = _PAGE.search(folded)
    page = int(page_match.group(1)) if page_match else None
    if _NO_COUNTERTOP.search(folded):
        return _notes_answer(snapshot, no_countertop=True)
    if page is None and _NOT_CHECKED.search(folded):
        return _notes_answer(snapshot, no_countertop=False)
    if page is None and _BLOCKERS.search(folded):
        return blockers_answer(snapshot)
    if page is not None:
        want = (
            "fail"
            if _FAIL_WORDS.search(folded)
            else ("need" if _NEED_WORDS.search(folded) else "any")
        )
        return _page_answer(snapshot, page, want=want)
    return _focus_answer(snapshot, focus)


#: Said after the recorded outcome when the reviewer asks whether to approve or pass something.
YOUR_DECISION: Final = (
    "Whether to approve it is your decision, not mine: record it in the review queue."
)
NOTHING_TO_DECIDE_THERE: Final = "There is no countertop result on that page to decide."


def judging_answer(snapshot: ReviewSnapshot, page: int | None, focus: Focus | None = None) -> Draft:
    """For "should page 4 pass?", "what should I approve first?": the records, and whose call it is.

    The assistant never judges, so this is code, not a model. A named page: its results and any
    second countertop not checked there (or that it has no countertop, or that the records have
    nothing on it). No page: the focus record, else everything that still needs the reviewer.
    The queue button points only at a record on that page that needs the reviewer.
    """
    if page is None and focus is not None:
        if focus.record_id is not None and snapshot.by_record_id(focus.record_id) is not None:
            record = snapshot.by_record_id(focus.record_id)
            assert record is not None
            return Draft(
                text="\n".join(
                    [_list_line(snapshot, record).removeprefix("- ") + ".", YOUR_DECISION]
                ),
                actions=((("open_queue_item", record.id),) if record.needs_you else ()),
            )
        page = focus.page_number
    if page is None:
        blockers = blockers_answer(snapshot)
        return Draft(
            text=f"{blockers.text}\n{YOUR_DECISION}",
            evidence=blockers.evidence,
            actions=blockers.actions,
        )
    records = list(snapshot.on_page(page))
    notes = _page_notes(snapshot, page)
    if not records and not notes:
        return Draft(text=NOTHING_ON_THAT_PAGE)
    lines = [_list_line(snapshot, item).removeprefix("- ") + "." for item in records[:MAX_LISTED]]
    lines.extend(notes)
    lines.append(YOUR_DECISION if records else NOTHING_TO_DECIDE_THERE)
    waiting = next((item for item in records if item.needs_you), None)
    return Draft(
        text="\n".join(lines),
        actions=(
            (("open_queue_item", waiting.id),)
            if waiting is not None
            else (("open_page", f"P{page}"),)
        ),
    )


def fallback_answer(
    snapshot: ReviewSnapshot,
    question: str,
    *,
    cited: Sequence[str] = (),
    focus: Focus | None = None,
) -> Draft:
    """What replaces a dropped model answer: the records it named, else the focus, else the
    question's records-only answer, else a plain statement that nothing could be checked."""
    return (
        records_answer(snapshot, [short_id for short_id in cited if short_id[:1] in "CF"])
        or _focus_answer(snapshot, focus)
        or answer_for_question(snapshot, question, focus)
        or Draft(text=NO_ANSWER_IN_RECORDS)
    )
