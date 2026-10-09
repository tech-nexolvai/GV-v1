"""Placeholders: how every fact in an assistant answer is written by code, never by the model (#1128).

An answer's text is a template. Wherever it states a fact it writes a placeholder, and code fills
in the record's own display text:

- a record's facts: `{C1.page}` → "page 4", `{C1.outcome}` → "Needs correction",
  `{C1.printed}` → 'printed overall 84 1/2"', `{C1.piece.2}` → 'piece 2: 36"', `{F1.reason}`,
  `{P3.no_countertop}` … (`record_fields` lists exactly what each record holds);
- the package's counts and sign-off: `{count.needs_you}` → "3 items need your decision",
  `{signoff.status}` → "Sign-off is blocked: 3 items need your decision."

Numbers carry their role in the rendered words ("printed overall …", "needed overall …"), so a
value can never be put in the wrong role. After the last placeholder of each record in a sentence
code inserts that record's citation marker `[[n]]`, and the citations are the records the
placeholders name, in order: a citation always matches the fact it follows.

An unknown placeholder (an id the records do not have, or a field that record does not hold) is
`UnknownPlaceholder`.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final

from app.review.assistant.records import CountertopRecord, FindingRecord, ReviewSnapshot

__all__ = [
    "OUTCOME_PHRASES",
    "PLACEHOLDER",
    "Rendered",
    "Slot",
    "UnknownPlaceholder",
    "group_for",
    "outcome_phrase",
    "record_fields",
    "render",
    "slots",
    "split_sentences",
    "value_of",
]

#: `{C1.page}`, `{C1.piece.2}`, `{count.needs_you}`, `{signoff.status}`.
PLACEHOLDER: Final = re.compile(r"\{([A-Za-z]+[0-9]*)\.([a-z_]+)(?:\.([0-9]{1,3}))?\}")
_RECORD_KEY: Final = re.compile(r"^[CFP][0-9]+$")
_SENTENCE_BREAK: Final = re.compile(r"(?<=[.!?])\s+|\n+")

COUNT_FIELDS: Final = (
    "needs_you",
    "fail",
    "pass",
    "review",
    "waiting",
    "no_countertop_pages",
    "rows_not_checked",
    "countertops",
    "checks",
)


class UnknownPlaceholder(ValueError):
    """A placeholder naming a record or a field the records do not hold."""


@dataclass(frozen=True, slots=True)
class Slot:
    key: str
    field: str
    index: int | None
    start: int
    end: int

    @property
    def is_record(self) -> bool:
        return _RECORD_KEY.match(self.key) is not None


@dataclass(frozen=True, slots=True)
class Rendered:
    text: str
    citations: tuple[str, ...]
    groups: tuple[str, ...]


def slots(template: str) -> list[Slot]:
    return [
        Slot(
            key=match.group(1),
            field=match.group(2),
            index=None if match.group(3) is None else int(match.group(3)),
            start=match.start(),
            end=match.end(),
        )
        for match in PLACEHOLDER.finditer(template)
    ]


def split_sentences(template: str) -> list[str]:
    """Sentences and list lines of a template (placeholders hold no sentence break)."""
    return [part for part in (piece.strip() for piece in _SENTENCE_BREAK.split(template)) if part]


#: What `{Cn.outcome}` fills in: a verb phrase, so "The countertop on page 4 {C1.outcome}" reads
#: "… needs correction". The citation chip after it shows the badge word.
OUTCOME_PHRASES: Final[Mapping[str | None, str]] = {
    "PASS": "looks right",
    "FAIL": "needs correction",
    "REVIEW_REQUIRED": "needs your decision",
    "NOT_FOUND": "is waiting on a value",
    "NO_APPLICABLE_RULE": "has no rule that applies",
    None: "was not checked",
}


def outcome_phrase(outcome: str | None) -> str:
    return OUTCOME_PHRASES.get(outcome, "was not checked")


def _plural(count: int, one: str, many: str, none: str) -> str:
    if count == 0:
        return none
    return f"{count} {one if count == 1 else many}"


def _count(snapshot: ReviewSnapshot, field: str) -> str | None:
    records = snapshot.records()
    by_outcome = {
        name: sum(item.outcome == name for item in records)
        for name in ("FAIL", "PASS", "REVIEW_REQUIRED", "NOT_FOUND")
    }
    phrases: Mapping[str, Callable[[], str]] = {
        "needs_you": lambda: _plural(
            len(snapshot.readiness.needing_you),
            "item needs your decision",
            "items need your decision",
            "no item needs your decision",
        ),
        "fail": lambda: _plural(
            by_outcome["FAIL"],
            "result needs correction",
            "results need correction",
            "no result needs correction",
        ),
        "pass": lambda: _plural(
            by_outcome["PASS"], "result looks right", "results look right", "no result looks right"
        ),
        "review": lambda: _plural(
            by_outcome["REVIEW_REQUIRED"],
            "result was held for your decision",
            "results were held for your decision",
            "no result was held for your decision",
        ),
        "waiting": lambda: _plural(
            by_outcome["NOT_FOUND"],
            "result is waiting on a value",
            "results are waiting on a value",
            "no result is waiting on a value",
        ),
        "no_countertop_pages": lambda: _plural(
            len(snapshot.pages_without_countertop),
            "page has no countertop",
            "pages have no countertop",
            "no page is listed as having no countertop",
        ),
        "rows_not_checked": lambda: _plural(
            len(snapshot.rows_not_checked),
            "second countertop was not checked",
            "second countertops were not checked",
            "no second countertop is listed",
        ),
        "countertops": lambda: _plural(
            len(snapshot.countertops), "countertop", "countertops", "no countertop"
        ),
        "checks": lambda: _plural(
            len(snapshot.other_checks), "other check", "other checks", "no other check"
        ),
    }
    phrase = phrases.get(field)
    return None if phrase is None else phrase()


def _signoff(snapshot: ReviewSnapshot) -> str:
    readiness = snapshot.readiness
    if readiness.can_sign_off:
        return "You can sign off"
    if readiness.needing_you:
        return f"Sign-off is blocked: {_count(snapshot, 'needs_you')}"
    return (readiness.reason or "Sign-off is not possible yet").rstrip(".")


def _pages(pages: tuple[int, ...]) -> str | None:
    if not pages:
        return None
    joined = ", ".join(str(page) for page in pages)
    return f"page {joined}" if len(pages) == 1 else f"pages {joined}"


def _decision(item: CountertopRecord | FindingRecord) -> str | None:
    if item.decision is None:
        return None
    carried = " (carried over from the earlier run)" if item.decision.carried_over else ""
    note = f", note: {item.decision.note}" if item.decision.note else ""
    return f"Decision: {item.decision.words}{carried}{note}"


def _countertop_value(item: CountertopRecord, field: str, index: int | None) -> str | None:
    if field == "piece":
        piece = next((piece for piece in item.pieces if piece.number == index), None)
        if piece is None:
            return None
        return f"piece {piece.number}: {piece.value.display if piece.value else 'not read'}"
    if index is not None:
        return None
    rule = item.rule
    architect = item.architect
    values: Mapping[str, Callable[[], str | None]] = {
        "page": lambda: f"page {item.page_number}",
        "label": lambda: item.label,
        "outcome": lambda: outcome_phrase(item.outcome),
        "printed": lambda: (
            None if item.printed is None else f"printed overall {item.printed.display}"
        ),
        "needed": lambda: None if item.needed is None else f"needed overall {item.needed.display}",
        "difference": lambda: (
            None if item.difference is None else f"difference {item.difference.display}"
        ),
        "field_cut": lambda: (
            None
            if item.field_cut_per_end is None
            else f"field cut {item.field_cut_per_end.display} per end"
            + ("" if not item.field_cut_count else f" at {item.field_cut_count} ends")
        ),
        "pieces": lambda: (
            None
            if not item.pieces
            else "pieces "
            + ", ".join(
                f"{piece.number}: {piece.value.display if piece.value else 'not read'}"
                for piece in item.pieces
            )
        ),
        "walls": lambda: (
            None if item.wall is None else f"walls: {item.wall} (from {item.wall_source})"
        ),
        "check": lambda: None if rule is None else rule.name,
        "tolerance": lambda: (
            None if rule is None or not rule.tolerance else f"tolerance {rule.tolerance}"
        ),
        "comparison": lambda: (
            None if rule is None or not rule.comparison else f"compared: {rule.comparison}"
        ),
        "reason": lambda: None if rule is None else rule.reason,
        "hold_reason": lambda: (
            None if item.hold_reason is None else f"Held because: {item.hold_reason}"
        ),
        "drawn_length_note": lambda: item.drawn_length_note,
        "architect": lambda: (
            None
            if architect is None
            else (
                f"The architect check {outcome_phrase(architect.outcome)}"
                + (f" ({architect.reason.rstrip('.')})" if architect.reason else "")
                if architect.outcome_label is not None
                else architect.not_compared_reason
            )
        ),
        "decision": lambda: _decision(item),
        "needs_you": lambda: (
            "It needs your decision in the review queue" if item.needs_you else None
        ),
    }
    getter = values.get(field)
    return None if getter is None else getter()


def _finding_value(item: FindingRecord, field: str, index: int | None) -> str | None:
    if index is not None:
        return None
    values: Mapping[str, Callable[[], str | None]] = {
        "page": lambda: _pages(item.pages),
        "check": lambda: item.check_name,
        "outcome": lambda: outcome_phrase(item.outcome),
        "reason": lambda: item.reason,
        "comparison": lambda: None if not item.comparison else f"compared: {item.comparison}",
        "tolerance": lambda: None if not item.tolerance else f"tolerance {item.tolerance}",
        "decision": lambda: _decision(item),
        "needs_you": lambda: (
            "It needs your decision in the review queue" if item.needs_you else None
        ),
    }
    getter = values.get(field)
    return None if getter is None else getter()


def _page_value(snapshot: ReviewSnapshot, page: int, field: str, index: int | None) -> str | None:
    if index is not None or page not in snapshot.pages():
        return None
    if field == "page":
        return f"page {page}"
    if field == "no_countertop":
        note = next((n for n in snapshot.pages_without_countertop if n.page_number == page), None)
        return None if note is None else f"No countertop on page {page}: {note.reason}"
    if field == "second_row":
        note = next((n for n in snapshot.rows_not_checked if n.page_number == page), None)
        return (
            None
            if note is None
            else f"Second countertop on page {page}, not checked: {note.reason}"
        )
    return None


def value_of(snapshot: ReviewSnapshot, slot: Slot) -> str:
    """The display text a placeholder stands for, or `UnknownPlaceholder`."""
    value: str | None = None
    if slot.key == "count":
        value = None if slot.index is not None else _count(snapshot, slot.field)
    elif slot.key == "signoff":
        value = _signoff(snapshot) if slot.field == "status" and slot.index is None else None
    elif slot.key.startswith("P") and _RECORD_KEY.match(slot.key):
        value = _page_value(snapshot, int(slot.key[1:]), slot.field, slot.index)
    elif _RECORD_KEY.match(slot.key):
        record = snapshot.record(slot.key)
        if isinstance(record, CountertopRecord):
            value = _countertop_value(record, slot.field, slot.index)
        elif isinstance(record, FindingRecord):
            value = _finding_value(record, slot.field, slot.index)
    if value is None or not value.strip():
        raise UnknownPlaceholder(f"{slot.key}.{slot.field}")
    return value.strip().rstrip(".")


_COUNTERTOP_FIELDS: Final = (
    "page",
    "label",
    "outcome",
    "printed",
    "needed",
    "difference",
    "field_cut",
    "pieces",
    "walls",
    "check",
    "tolerance",
    "comparison",
    "reason",
    "hold_reason",
    "drawn_length_note",
    "architect",
    "decision",
    "needs_you",
)
_FINDING_FIELDS: Final = (
    "page",
    "check",
    "outcome",
    "reason",
    "comparison",
    "tolerance",
    "decision",
    "needs_you",
)


def record_fields(snapshot: ReviewSnapshot) -> dict[str, list[str]]:
    """Every placeholder the records can fill, by key: what the prompt offers the model."""
    fields: dict[str, list[str]] = {}

    def offer(key: str, names: tuple[str, ...]) -> None:
        held = []
        for name in names:
            try:
                value_of(snapshot, Slot(key, name, None, 0, 0))
            except UnknownPlaceholder:
                continue
            held.append(name)
        if held:
            fields[key] = held

    for item in snapshot.countertops:
        offer(item.id, _COUNTERTOP_FIELDS)
        fields.setdefault(item.id, []).extend(f"piece.{piece.number}" for piece in item.pieces)
    for finding in snapshot.other_checks:
        offer(finding.id, _FINDING_FIELDS)
    for page in sorted(snapshot.pages()):
        offer(f"P{page}", ("page", "no_countertop", "second_row"))
    fields["count"] = list(COUNT_FIELDS)
    fields["signoff"] = ["status"]
    return fields


def group_for(slot: Slot) -> str | None:
    """The evidence group a package-level placeholder shows."""
    if slot.key == "signoff" or (slot.key == "count" and slot.field == "needs_you"):
        return "blockers"
    if slot.key == "count" and slot.field in ("no_countertop_pages", "rows_not_checked"):
        return slot.field
    if slot.key.startswith("P") and slot.field == "no_countertop":
        return "no_countertop_pages"
    if slot.key.startswith("P") and slot.field == "second_row":
        return "rows_not_checked"
    return None


def _capitalised(sentence: str) -> str:
    """A sentence or list line that starts with a filled-in value starts with a capital."""
    for index, character in enumerate(sentence):
        if character in " -":
            continue
        if character.isalpha():
            return sentence[:index] + character.upper() + sentence[index + 1 :]
        return sentence
    return sentence


def render(template: str, snapshot: ReviewSnapshot) -> Rendered:
    """Fill every placeholder and put each record's marker after its last fact in a sentence."""
    found = slots(template)
    citations: list[str] = []
    for slot in found:
        if slot.is_record and slot.key not in citations:
            citations.append(slot.key)
    groups: list[str] = []
    for slot in found:
        group = group_for(slot)
        if group is not None and group not in groups:
            groups.append(group)

    pieces: list[str] = []
    for part in re.split(r"((?<=[.!?])\s+|\n+)", template):
        if not part or _SENTENCE_BREAK.fullmatch(part):
            pieces.append(part)
            continue
        in_part = slots(part)
        last = {slot.key: position for position, slot in enumerate(in_part) if slot.is_record}
        cursor = 0
        out: list[str] = []
        for position, slot in enumerate(in_part):
            out.append(part[cursor : slot.start])
            out.append(value_of(snapshot, slot))
            if slot.is_record and last[slot.key] == position:
                out.append(f" [[{citations.index(slot.key)}]]")
            cursor = slot.end
        out.append(part[cursor:])
        pieces.append(
            _capitalised("".join(out)) if part.lstrip("- ").startswith("{") else "".join(out)
        )
    return Rendered(text="".join(pieces), citations=tuple(citations), groups=tuple(groups))
