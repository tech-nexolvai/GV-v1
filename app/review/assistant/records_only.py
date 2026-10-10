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
from fractions import Fraction
from typing import Final

from app.review.assistant.contract import Draft, Focus
from app.review.assistant.placeholders import Slot, UnknownPlaceholder, value_of
from app.review.assistant.records import CountertopRecord, FindingRecord, ReviewSnapshot

__all__ = [
    "COULD_NOT_CHECK",
    "GLOSSARY",
    "NOTHING_ON_THAT_PAGE",
    "NOTHING_TO_DECIDE_THERE",
    "NO_ANSWER_IN_RECORDS",
    "YOUR_DECISION",
    "answer_for_question",
    "asks_about_deciding",
    "blockers_answer",
    "details_answer",
    "fallback_answer",
    "glossary_answer",
    "is_ranking_question",
    "judging_answer",
    "needs_you_answer",
    "ranked",
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
_DETAIL_WORDS: Final = re.compile(
    r"\bwalls?\b|\bwhat (?:was|were) read\b|\breadings?\b|\bpieces?\b|\bfield cut|"
    r"\bvalues?\b|\bmeasure|\bnumbers\b|\bdimensions?\b"
)
_FIX_WORDS: Final = re.compile(
    r"\bfix|\bhow do i (?:correct|solve|sort)|\bwhat (?:do|should) i tell"
)

#: Plain-word answers to "what does … mean?": the app's own vocabulary, no record facts.
GLOSSARY: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (
        re.compile(r"\bheld\b|\bhold\b|on hold"),
        (
            "Held means the app stopped before checking a countertop because it could not read it "
            "safely, for example when the readers picked different countertop lines. Nothing is "
            "checked; the countertop waits in the queue for you to look at it on the drawing."
        ),
    ),
    (
        re.compile(r"needs? correction"),
        (
            "Needs correction means the vendor's printed dimension and the dimension the rulebook "
            "calls for, from the pieces and field cuts the vendor drew, are not the same. You decide "
            "in the queue what goes back to the vendor."
        ),
    ),
    (
        re.compile(r"needs? (?:your|my) decision|review required"),
        (
            "Needs your decision means the app could not settle the check by itself, so it waits "
            "for you in the queue. Nothing about it was judged."
        ),
    ),
    (
        re.compile(r"waiting on a value|not found"),
        (
            "Waiting on a value means a number the check needs is missing, so the check could not "
            "run. Entering the value lets it run."
        ),
    ),
    (
        re.compile(r"looks? right"),
        (
            "Looks right means the vendor's printed dimension and the dimension the rulebook calls "
            "for are exactly the same."
        ),
    ),
    (
        re.compile(r"field cut"),
        (
            "A field cut is extra stone left at an end that meets a wall, so the installers can trim "
            "it to the real wall on site. The rulebook adds it to the pieces at each wall end."
        ),
    ),
    (
        re.compile(r"\bexception|\baccept|\breject|\bdismiss|\bapprov|\bconfirm"),
        (
            "In the queue you confirm a result, correct a value that was read wrongly, accept a result "
            "as an exception with a note, or dismiss it. Each is your own decision and is recorded "
            "with your name; sign-off approves the whole review. The assistant cannot do any of "
            "these."
        ),
    ),
    (
        re.compile(r"\bdifference\b|\bgap\b"),
        (
            "The difference is the vendor's printed overall minus the overall the rulebook calls for "
            "from the pieces and field cuts. A minus sign means the printed overall is the smaller "
            "of the two."
        ),
    ),
    (
        re.compile(r"\bfiller"),
        (
            "A filler is a narrow panel between cabinets or at a wall that takes up the leftover "
            "space. It counts as a piece under the countertop."
        ),
    ),
    (
        re.compile(r"sign[ -]?off"),
        (
            "Sign-off is your approval of the whole review. It is possible once every finding has a "
            "valid decision from you; the panel shows what still stands in the way."
        ),
    ),
    (
        re.compile(r"\boverall\b"),
        (
            "The overall is the full length of the countertop the vendor printed on the drawing; the "
            "rulebook compares it with the pieces underneath plus the field cuts."
        ),
    ),
    (
        re.compile(r"carried over"),
        (
            "Carried over means you decided this result before the checks ran again, nothing it rests "
            "on changed, so your decision still stands."
        ),
    ),
)
_GLOSSARY_QUESTION: Final = re.compile(
    r"\bwhat (?:does|do) .*\bmean\b|\bmeaning of\b|\bwhat(?: is| are|'s) (?:a|an|the)\b|"
    r"\bwhat would (?:that|it|this) do\b|\bdefine\b|\bexplain the term\b|\bwhat happens when\b"
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


#: The label code gives a countertop row when the drawing names none: it adds nothing to "page N".
_GENERIC_LABEL: Final = re.compile(r"^countertop row on page \d+$", re.IGNORECASE)


def _subject(snapshot: ReviewSnapshot, item: CountertopRecord | FindingRecord) -> str:
    """The record's subject: its page, and its label only when it tells two countertops apart."""
    key = item.id
    if isinstance(item, CountertopRecord):
        others = [
            other
            for other in snapshot.countertops
            if other.page_number == item.page_number and other.id != key
        ]
        if others and not _GENERIC_LABEL.match(item.label.strip()):
            return f"The countertop on {{{key}.page}} ({{{key}.label}})"
        return f"The countertop on {{{key}.page}}"
    where = f" on {{{key}.page}}" if _has(snapshot, key, "page") else ""
    return f"The {{{key}.check}} check{where}"


def _own_open(item: CountertopRecord | FindingRecord) -> bool:
    """Whether the record's own result (not its architect check) still waits for the reviewer."""
    return item.decision is None and item.outcome != "PASS"


def _architect_open(item: CountertopRecord | FindingRecord) -> bool:
    """A countertop whose own result is settled (decided, or looks right) but whose architect
    check still needs the reviewer: the architect check is what needs them."""
    return (
        isinstance(item, CountertopRecord)
        and item.architect is not None
        and item.architect.needs_you
        and not _own_open(item)
    )


def _open_outcome(item: CountertopRecord | FindingRecord) -> str | None:
    """The outcome that makes the record need the reviewer: its own, or its architect check's."""
    if _architect_open(item):
        assert isinstance(item, CountertopRecord) and item.architect is not None
        return item.architect.outcome
    return item.outcome


def _why(snapshot: ReviewSnapshot, item: CountertopRecord | FindingRecord) -> str | None:
    """The one field that says why a result is what it is: the architect check when that is what
    still needs the reviewer, else the hold, else the rule's reason."""
    key = item.id
    if _architect_open(item) and _has(snapshot, key, "architect"):
        return f"{{{key}.architect}}"
    if isinstance(item, CountertopRecord):
        if _has(snapshot, key, "hold_reason"):
            return f"{{{key}.hold_reason}}"
        if item.outcome not in ("PASS", None) and _has(snapshot, key, "reason"):
            return f"{{{key}.reason}}"
        return None
    if item.outcome != "PASS" and _has(snapshot, key, "reason"):
        return f"{{{key}.reason}}"
    return None


def _record_lines(
    snapshot: ReviewSnapshot,
    item: CountertopRecord | FindingRecord,
    extra: Sequence[str] = (),
) -> str:
    """One sentence: the subject and its outcome, then why. The evidence card under the
    answer carries the values, so the text never repeats them; `extra` adds a named field only
    when the question asks for it (the walls, the architect check)."""
    key = item.id
    parts = [f"{_subject(snapshot, item)} {{{key}.outcome}}"]
    if extra:
        parts.extend(f"{{{key}.{field}}}" for field in extra if _has(snapshot, key, field))
    else:
        why = _why(snapshot, item)
        if why is not None:
            parts.append(why)
    # One sentence, so it names its subject once and carries one citation.
    return "; ".join(parts) + "."


def _explain(
    snapshot: ReviewSnapshot,
    items: Sequence[CountertopRecord | FindingRecord],
    extra: Sequence[str] = (),
) -> Draft:
    paragraphs = [_record_lines(snapshot, item, extra) for item in items[:MAX_EXPLAINED]]
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


def _waiting(snapshot: ReviewSnapshot) -> list[CountertopRecord | FindingRecord]:
    return [
        record
        for short_id in snapshot.readiness.needing_you
        if (record := snapshot.record(short_id)) is not None
    ]


def blockers_answer(snapshot: ReviewSnapshot) -> Draft:
    """What still stands between this package and sign-off, in sign-off readiness's own numbers.

    The text is the sign-off status (and, when some countertops have no check result, their
    count, which readiness does not wait for); the `blockers` evidence lists every open item, so
    the text never repeats the list.
    """
    if not snapshot.checks_have_run:
        return Draft(text=NOTHING_HAS_RUN, evidence=("blockers",))
    waiting = _waiting(snapshot)
    blocking = [item for item in waiting if item.outcome is not None]
    unchecked = [item for item in waiting if item.outcome is None]
    sentences = ["{signoff.status}."]
    if unchecked:
        sentences.append("{count.not_checked}.")
        if snapshot.readiness.can_sign_off:
            sentences.append(UNCHECKED_DO_NOT_BLOCK)
    first = (blocking if not snapshot.readiness.can_sign_off else []) + unchecked
    return Draft(
        text=" ".join(sentences),
        evidence=("blockers",),
        actions=(() if not first else (("open_queue_item", first[0].id),)),
    )


#: Said first when a free answer to the question was refused and code answers instead.
COULD_NOT_CHECK: Final = "I couldn't check a free answer to that; here is what needs you."
#: Said when sign-off is possible although some countertops have no check result.
UNCHECKED_DO_NOT_BLOCK: Final = "Countertops with no check result do not hold up sign-off."
#: Questions that ask code to rank or prioritise what needs the reviewer: answered by code.
#: Deliberately narrow: "which page is first?", "worst-case tolerance", "next to the sink",
#: "the priority of the sink check" are questions about the records, not a ranking.
RANKING: Final = re.compile(
    r"\bmost (?:worrying|worrisome|concerning|serious|urgent|critical|problematic)\b|"
    r"\bworst\b(?![- ]case)|\bbiggest (?:problems?|issues?|risks?|concerns?|gaps?)\b|"
    r"\bprioriti[sz]|\bwhere (?:do|should|can|must) (?:i|we) (?:start|begin)\b|"
    r"\b(?:which|what)\b[^?.]{0,30}\b(?:should|must|can|do) (?:i|we)\b[^?.]{0,40}\b"
    r"(?:first|next)\b|\bin (?:what|which) order\b"
)
#: A ranking question about deciding ("what should I approve first?") also gets the sentence
#: that the decision is the reviewer's: the start item is never a recommendation.
_DECIDING: Final = re.compile(
    r"\bapprov|\baccept|\bdismiss|\breject|\bexception|\bsign off|\bpass\b|\bok to\b|"
    r"\bokay to\b|\bconfirm"
)


def is_ranking_question(question: str) -> bool:
    """Whether the question asks which results matter most or come first (and names no page)."""
    folded = _folded(question)
    return RANKING.search(folded) is not None and _PAGE.search(folded) is None


def asks_about_deciding(question: str) -> bool:
    """Whether the question is about approving, accepting, dismissing or signing off."""
    return _DECIDING.search(_folded(question)) is not None


def _difference_size(item: CountertopRecord | FindingRecord) -> Fraction:
    if isinstance(item, CountertopRecord) and item.difference and item.difference.exact:
        numerator, _, denominator = item.difference.exact.partition("/")
        try:
            return abs(Fraction(int(numerator), int(denominator or "1")))
        except (ValueError, ZeroDivisionError):
            return Fraction(-1)
    return Fraction(-1)


_RANK_ORDER: Final = {"FAIL": 0, "REVIEW_REQUIRED": 1, "NOT_FOUND": 2, "NO_APPLICABLE_RULE": 3}
_SEVERITY_ORDER: Final = {"critical": 0, "major": 1, "minor": 2}


def _rank_key(item: CountertopRecord | FindingRecord) -> tuple[int, int, Fraction, int, int]:
    pages = (item.page_number,) if isinstance(item, CountertopRecord) else item.pages
    size = Fraction(-1) if _architect_open(item) else _difference_size(item)
    return (
        _RANK_ORDER.get(_open_outcome(item) or "", 4),
        _SEVERITY_ORDER.get((item.severity or "").casefold(), 3),
        -size,
        0 if pages else 1,
        pages[0] if pages else 0,
    )


def ranked(snapshot: ReviewSnapshot) -> list[CountertopRecord | FindingRecord]:
    """What needs the reviewer, in code's order (it judges nothing):

    1. by the outcome that needs the reviewer (its own, or its architect check's when only that
       is open): needs correction, then held, then waiting on a value, then no check result;
    2. within an outcome, by the rulebook's severity (critical, major, minor, unknown);
    3. then by the size of the difference, largest first (a result without one comes after);
    4. then results on a page before page-less ones, by page.
    So an other check's failure ranks with the countertop failures of the same severity, after
    those with a difference, never after every countertop failure by default.
    """
    return sorted(_waiting(snapshot), key=_rank_key)


def needs_you_answer(
    snapshot: ReviewSnapshot, *, lead: str | None = None, about_deciding: bool = False
) -> Draft:
    """The sign-off status and one sentence naming where to start, in code's order.

    The `blockers` evidence lists everything that needs the reviewer, so the text names only the
    first item (with the reason it comes first) and its countertop card; it never repeats the
    list. The answer to "which look most worrying?" and "where do I start?", and the default when
    a free answer could not be checked (`lead` says so).
    """
    sentences = [lead] if lead else []
    if not snapshot.checks_have_run:
        return Draft(text=" ".join([*sentences, NOTHING_HAS_RUN]), evidence=("blockers",))
    order = ranked(snapshot)
    sentences.append("{signoff.status}.")
    if not order:
        if about_deciding:
            sentences.append(YOUR_DECISION)
        return Draft(text=" ".join(sentences), evidence=("blockers",))
    first = order[0]
    subject = _subject(snapshot, first)
    start = f"Start with {subject[:1].lower()}{subject[1:]}: it {{{first.id}.outcome}}"
    why = _why(snapshot, first)
    sentences.append(f"{start}; {why}." if why else f"{start}.")
    if snapshot.readiness.can_sign_off and all(item.outcome is None for item in order):
        sentences.append(UNCHECKED_DO_NOT_BLOCK)
    if about_deciding:
        sentences.append(YOUR_DECISION)
    card = (first.id,) if first.id.startswith("C") else ()
    return Draft(
        text=" ".join(sentences),
        evidence=(*card, "blockers"),
        actions=(("open_queue_item", first.id),),
    )


def _notes_answer(snapshot: ReviewSnapshot, *, no_countertop: bool) -> Draft:
    """One sentence with the count; the evidence group lists the pages and the readers' reasons."""
    notes = snapshot.pages_without_countertop if no_countertop else snapshot.rows_not_checked
    group = "no_countertop_pages" if no_countertop else "rows_not_checked"
    return Draft(
        text=f"{{count.{group}}}.",
        evidence=(group,),
        actions=(() if not notes else (("open_page", f"P{notes[0].page_number}"),)),
    )


def _page_notes(snapshot: ReviewSnapshot, page: int) -> tuple[list[str], list[str]]:
    """Short sentences for a page's notes, and the evidence groups that hold their reasons."""
    sentences: list[str] = []
    groups: list[str] = []
    if _has(snapshot, f"P{page}", "no_countertop"):
        sentences.append(f"{{P{page}.page}} has no countertop.")
        groups.append("no_countertop_pages")
    if _has(snapshot, f"P{page}", "second_row"):
        sentences.append(f"{{P{page}.page}} also has a second countertop that was not checked.")
        groups.append("rows_not_checked")
    return sentences, groups


def _page_answer(
    snapshot: ReviewSnapshot, page: int, *, want: str, extra: Sequence[str] = ()
) -> Draft:
    records = list(snapshot.on_page(page))
    notes, groups = _page_notes(snapshot, page)
    if not records and not notes:
        return Draft(text=NOTHING_ON_THAT_PAGE)
    if want == "fail":
        chosen = [item for item in records if item.outcome == "FAIL"] or records
    elif want == "need":
        chosen = [item for item in records if item.needs_you] or records
    else:
        chosen = records
    draft = _explain(snapshot, chosen, extra) if chosen else Draft(text="")
    if not notes:
        return draft
    return Draft(
        text="\n\n".join(part for part in (draft.text, " ".join(notes)) if part),
        evidence=(*draft.evidence, *groups),
        actions=draft.actions or (("open_page", f"P{page}"),),
    )


_FIX_NOTE: Final = (
    "The app does not change the drawing; the vendor redraws it. Open the item in the queue to "
    "see it on the drawing and record what goes back to the vendor."
)


def details_answer(snapshot: ReviewSnapshot, page: int) -> Draft:
    """What was read on a page: the outcome and the walls in text; the evidence card shows the
    printed overall, the pieces, the field cut and the needed overall."""
    return _page_answer(snapshot, page, want="any", extra=("walls",))


def _fix_answer(snapshot: ReviewSnapshot, page: int) -> Draft:
    draft = _page_answer(snapshot, page, want="fail")
    if draft.text == NOTHING_ON_THAT_PAGE:
        return draft
    return Draft(
        text=f"{draft.text}\n\n{_FIX_NOTE}", evidence=draft.evidence, actions=draft.actions
    )


def glossary_answer(question: str) -> Draft | None:
    """The fixed explanation of an app term the question asks about, or `None`."""
    folded = _folded(question)
    if not _GLOSSARY_QUESTION.search(folded) and not folded.endswith(("mean", "mean?")):
        return None
    if _PAGE.search(folded):
        return None
    for pattern, text in GLOSSARY:
        if pattern.search(folded):
            return Draft(text=text)
    return None


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
    why a page failed or needs the reviewer), what was read on a page (walls, pieces, field cut),
    how to fix a page, what an app term means, and a question about one page or the focus record.
    """
    glossary = glossary_answer(question)
    if glossary is not None:
        return glossary
    folded = _folded(question)
    page_match = _PAGE.search(folded)
    page = int(page_match.group(1)) if page_match else None
    if (
        page is None
        and focus is not None
        and focus.page_number is not None
        and (_DETAIL_WORDS.search(folded) or _FIX_WORDS.search(folded))
    ):
        page = focus.page_number
    if _NO_COUNTERTOP.search(folded):
        return _notes_answer(snapshot, no_countertop=True)
    if page is None and _NOT_CHECKED.search(folded):
        return _notes_answer(snapshot, no_countertop=False)
    if page is None and RANKING.search(folded):
        return needs_you_answer(snapshot)
    if page is None and _BLOCKERS.search(folded):
        return blockers_answer(snapshot)
    if page is not None:
        if _FIX_WORDS.search(folded):
            return _fix_answer(snapshot, page)
        if _DETAIL_WORDS.search(folded):
            return details_answer(snapshot, page)
        if "architect" in folded:
            return _page_answer(snapshot, page, want="any", extra=("architect",))
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
    """For "should page 4 pass?", "is page 4 fine?": the records, and whose call it is.

    The assistant never judges, so this is code, not a model. A named page: its results and any
    second countertop not checked there (or that it has no countertop, or that the records have
    nothing on it). No page: the focus record, else what sign-off readiness says and what is
    still open. The queue button points only at a record on that page that needs the reviewer.
    """
    if page is None and focus is not None:
        record = None if focus.record_id is None else snapshot.by_record_id(focus.record_id)
        if record is not None:
            return Draft(
                text=f"{_subject(snapshot, record)} {{{record.id}.outcome}}. {YOUR_DECISION}",
                evidence=(record.id,) if record.id.startswith("C") else (),
                actions=((("open_queue_item", record.id),) if record.needs_you else ()),
            )
        page = focus.page_number
    if page is None:
        blockers = blockers_answer(snapshot)
        return Draft(
            text=f"{blockers.text} {YOUR_DECISION}",
            evidence=blockers.evidence,
            actions=blockers.actions,
        )
    records = list(snapshot.on_page(page))
    notes, groups = _page_notes(snapshot, page)
    if not records and not notes:
        return Draft(text=NOTHING_ON_THAT_PAGE)
    sentences = [
        f"{_subject(snapshot, item)} {{{item.id}.outcome}}." for item in records[:MAX_EXPLAINED]
    ]
    sentences.extend(notes)
    sentences.append(YOUR_DECISION if records else NOTHING_TO_DECIDE_THERE)
    waiting = next((item for item in records if item.needs_you), None)
    return Draft(
        text=" ".join(sentences),
        evidence=(
            *(item.id for item in records[:MAX_EXPLAINED] if item.id.startswith("C")),
            *groups,
        ),
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
    """What replaces a refused or failed model answer: never a dead end.

    The question's own code answer when there is one (a page or the focus record, details, a
    fix, a term, what blocks sign-off); otherwise what needs the reviewer, failures first, after
    one line saying the free answer could not be checked. `cited` (the records the refused answer
    named) is not trusted to choose the answer.
    """
    del cited
    return (
        answer_for_question(snapshot, question, focus)
        or _focus_answer(snapshot, focus)
        or needs_you_answer(snapshot, lead=COULD_NOT_CHECK)
    )
