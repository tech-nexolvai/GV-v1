"""Placeholders: how every fact in an assistant answer is written by code, never by the model (#1128).

An answer's text is a template. Wherever it states a fact it writes a placeholder, and code fills
in the record's own text. Every fill has a grammatical kind, shown to the model with an example:

- **name** (a subject): `{C1.page}` → "page 4", `{C1.label}`, `{F1.check}`;
- **verb phrase**: `{C1.outcome}` → "needs correction", "looks right", "was held for your decision;
  you confirmed it" — the record's state as the screen shows it, its standing decision included;
- **noun phrase** (a value with its role): `{C1.printed}` → 'the printed overall, 84 1/2"',
  `{C1.needed}`, `{C1.difference}`, `{C1.piece.2}` → 'piece 2, 36"', `{C1.walls}` …;
- **sentence**: `{C1.reason}`, `{C1.hold_reason}`, `{C1.decision}`, `{C1.needs_you}`,
  `{P3.no_countertop}`, `{P9.second_row}`;
- **clause**: `{count.needs_you}` → "3 findings still need your decision", `{signoff.status}` →
  "sign-off is blocked: 3 findings still need your decision". Counts come from the uncapped,
  decision-aware totals and the sign-off readiness numbers, exactly as the screens show them.

Code also: puts each record's citation marker `[[n]]` after its last fact in a sentence (the
citations are the records the placeholders name, in order); starts a sentence that states a
record's facts without naming it with "On page N," so every fact sentence carries its subject;
capitalises a fill that starts a sentence and lower-cases one that does not.

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
    "COUNT_FIELDS",
    "KINDS",
    "OUTCOME_PHRASES",
    "PLACEHOLDER",
    "Rendered",
    "Slot",
    "UnknownPlaceholder",
    "group_for",
    "outcome_phrase",
    "placeholder_guide",
    "record_fields",
    "render",
    "slots",
    "split_sentences",
    "value_of",
]

#: `{C1.page}`, `{C1.piece.2}`, `{count.needs_you}`, `{signoff.status}`.
PLACEHOLDER: Final = re.compile(r"\{([A-Za-z]+[0-9]*)\.([a-z_]+)(?:\.([0-9]{1,3}))?\}")
_RECORD_KEY: Final = re.compile(r"^[CFP][0-9]+$")
#: A sentence ends at . or ? before a capital letter or a placeholder, or at a line break.
SENTENCE_BREAK: Final = re.compile(r"(?<=[.?!])\s+(?=[A-Z{])|\n+")

COUNT_FIELDS: Final = (
    "needs_you",
    "fail",
    "pass",
    "review",
    "waiting",
    "not_checked",
    "no_countertop_pages",
    "rows_not_checked",
    "countertops",
    "checks",
)

#: The grammatical kind of every field, as the prompt shows it.
KINDS: Final[Mapping[str, str]] = {
    "page": "name",
    "label": "name",
    "check": "name",
    "outcome": "verb phrase",
    "printed": "noun phrase",
    "needed": "noun phrase",
    "difference": "noun phrase",
    "field_cut": "noun phrase",
    "pieces": "noun phrase",
    "piece": "noun phrase",
    "walls": "noun phrase",
    "tolerance": "noun phrase",
    "comparison": "noun phrase",
    "reason": "sentence",
    "hold_reason": "sentence",
    "drawn_length_note": "sentence",
    "architect": "sentence",
    "decision": "sentence",
    "needs_you": "sentence",
    "no_countertop": "sentence",
    "second_row": "sentence",
    "status": "clause",
}
_NAMES: Final = frozenset({"page", "label", "check"})


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

    @property
    def name(self) -> str:
        return f"{self.key}.{self.field}" + ("" if self.index is None else f".{self.index}")


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
    return [part for part in (piece.strip() for piece in SENTENCE_BREAK.split(template)) if part]


#: What `{Cn.outcome}` fills in for an undecided result: a verb phrase, so "The countertop on
#: page 4 {C1.outcome}" reads "… needs correction".
OUTCOME_PHRASES: Final[Mapping[str | None, str]] = {
    "PASS": "looks right",
    "FAIL": "needs correction",
    "REVIEW_REQUIRED": "needs your decision",
    "NOT_FOUND": "is waiting on a value",
    "NO_APPLICABLE_RULE": "has no rule that applies",
    None: "was not checked",
}
#: The same outcome once a decision stands on it.
_DECIDED_PHRASES: Final[Mapping[str | None, str]] = {
    "REVIEW_REQUIRED": "was held for your decision",
    "NOT_FOUND": "was waiting on a value",
}
_DECISION_PHRASES: Final[Mapping[str, str]] = {
    "confirm": "you confirmed it",
    "except": "you accepted it as an exception",
    "dismiss": "you dismissed it",
    "correct": "you corrected a value, so the checks need to run again",
}


def outcome_phrase(
    outcome: str | None, action: str | None = None, *, carried_over: bool = False
) -> str:
    """The result as the screen shows it: the recorded outcome and the decision standing on it."""
    if action is None:
        return OUTCOME_PHRASES.get(outcome, "was not checked")
    if outcome == "REVIEW_REQUIRED" and action == "confirm":
        phrase = "was confirmed by you"
    else:
        base = _DECIDED_PHRASES.get(outcome) or OUTCOME_PHRASES.get(outcome, "was not checked")
        phrase = f"{base}; {_DECISION_PHRASES.get(action, f'you recorded {action}')}"
    return phrase + (" (carried over from the earlier run)" if carried_over else "")


def _record_outcome(item: CountertopRecord | FindingRecord) -> str:
    decision = item.decision
    return outcome_phrase(
        item.outcome,
        None if decision is None else decision.action,
        carried_over=decision is not None and decision.carried_over,
    )


def _plural(count: int, one: str, many: str, none: str) -> str:
    if count == 0:
        return none
    return f"{count} {one if count == 1 else many}"


_COUNT_WORDS: Final[Mapping[str, tuple[str, str, str]]] = {
    "needs_you": (
        "finding still needs your decision",
        "findings still need your decision",
        "no finding needs your decision",
    ),
    "fail": (
        "result needs correction",
        "results need correction",
        "no result needs correction",
    ),
    "pass": ("result looks right", "results look right", "no result looks right"),
    "review": (
        "held result still needs your decision",
        "held results still need your decision",
        "no held result needs your decision",
    ),
    "waiting": (
        "result is waiting on a value",
        "results are waiting on a value",
        "no result is waiting on a value",
    ),
    "not_checked": (
        "countertop was not checked",
        "countertops were not checked",
        "every countertop listed has a check result",
    ),
    "no_countertop_pages": (
        "page has no countertop",
        "pages have no countertop",
        "no page is listed as having no countertop",
    ),
    "rows_not_checked": (
        "second countertop was not checked",
        "second countertops were not checked",
        "no second countertop is listed",
    ),
    "countertops": ("countertop", "countertops", "no countertop"),
    "checks": ("other check", "other checks", "no other check"),
}


def _count(snapshot: ReviewSnapshot, field: str) -> str | None:
    words = _COUNT_WORDS.get(field)
    if words is None:
        return None
    return _plural(snapshot.totals.get(field, 0), *words)


def _lower_first(text: str) -> str:
    return text[:1].lower() + text[1:] if text[1:2].islower() or text[1:2] == " " else text


def _signoff(snapshot: ReviewSnapshot) -> str:
    """Exactly what sign-off readiness says: the screen's numbers, never a second count."""
    readiness = snapshot.readiness
    if readiness.can_sign_off:
        return "you can sign off"
    if readiness.blocking_findings:
        return f"sign-off is blocked: {_count(snapshot, 'needs_you')}"
    return _lower_first((readiness.reason or "sign-off is not possible yet").rstrip("."))


def _pages(pages: tuple[int, ...]) -> str | None:
    if not pages:
        return None
    joined = ", ".join(str(page) for page in pages)
    return f"page {joined}" if len(pages) == 1 else f"pages {joined}"


def _decision(item: CountertopRecord | FindingRecord) -> str | None:
    if item.decision is None:
        return None
    carried = " (carried over from the earlier run)" if item.decision.carried_over else ""
    note = f", with the note “{item.decision.note}”" if item.decision.note else ""
    return f"the decision on record: {item.decision.words}{carried}{note}"


def _quoted(text: str) -> str:
    text = text.rstrip(".")
    return f"“{text[:1].upper()}{text[1:]}”"


def _countertop_value(item: CountertopRecord, field: str, index: int | None) -> str | None:
    if field == "piece":
        piece = next((piece for piece in item.pieces if piece.number == index), None)
        if piece is None:
            return None
        return f"piece {piece.number}, {piece.value.display if piece.value else 'not read'}"
    if index is not None:
        return None
    rule = item.rule
    architect = item.architect

    def pieces() -> str | None:
        if not item.pieces:
            return None
        values = [piece.value.display if piece.value else "not read" for piece in item.pieces]
        listed = values[0] if len(values) == 1 else ", ".join(values[:-1]) + " and " + values[-1]
        return f"the pieces, {listed}"

    values: Mapping[str, Callable[[], str | None]] = {
        "page": lambda: f"page {item.page_number}",
        "label": lambda: item.label,
        "outcome": lambda: _record_outcome(item),
        "printed": lambda: (
            None if item.printed is None else f"the printed overall, {item.printed.display}"
        ),
        "needed": lambda: (
            None if item.needed is None else f"the needed overall, {item.needed.display}"
        ),
        "difference": lambda: (
            None if item.difference is None else f"the difference, {item.difference.display}"
        ),
        "field_cut": lambda: (
            None
            if item.field_cut_per_end is None
            else f"the field cut, {item.field_cut_per_end.display} per end"
            + ("" if not item.field_cut_count else f" at {item.field_cut_count} ends")
        ),
        "pieces": pieces,
        "walls": lambda: (
            None if item.wall is None else f"the walls, {item.wall} (from {item.wall_source})"
        ),
        "check": lambda: None if rule is None else rule.name,
        "tolerance": lambda: (
            None if rule is None or not rule.tolerance else f"the tolerance, {rule.tolerance}"
        ),
        "comparison": lambda: (
            None if rule is None or not rule.comparison else f"the comparison, {rule.comparison}"
        ),
        "reason": lambda: None if rule is None else rule.reason,
        "hold_reason": lambda: (
            None
            if item.hold_reason is None
            else f"it is held because {_lower_first(item.hold_reason)}"
        ),
        "drawn_length_note": lambda: item.drawn_length_note,
        "architect": lambda: (
            None
            if architect is None
            else (
                f"the architect check {outcome_phrase(architect.outcome)}"
                + (f" ({architect.reason.rstrip('.')})" if architect.reason else "")
                if architect.outcome is not None
                else architect.not_compared_reason
            )
        ),
        "decision": lambda: _decision(item),
        "needs_you": lambda: (
            "it needs your decision in the review queue" if item.needs_you else None
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
        "outcome": lambda: _record_outcome(item),
        "reason": lambda: item.reason,
        "comparison": lambda: None if not item.comparison else f"the comparison, {item.comparison}",
        "tolerance": lambda: None if not item.tolerance else f"the tolerance, {item.tolerance}",
        "decision": lambda: _decision(item),
        "needs_you": lambda: (
            "it needs your decision in the review queue" if item.needs_you else None
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
        return (
            None
            if note is None
            else f"page {page} has no countertop (the readers said: {_quoted(note.reason)})"
        )
    if field == "second_row":
        note = next((n for n in snapshot.rows_not_checked if n.page_number == page), None)
        return (
            None
            if note is None
            else f"page {page} has a second countertop that was not checked (the readers said: "
            f"{_quoted(note.reason)})"
        )
    return None


def value_of(snapshot: ReviewSnapshot, slot: Slot) -> str:
    """The text a placeholder stands for, or `UnknownPlaceholder`."""
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
        raise UnknownPlaceholder(slot.name)
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


def _held(snapshot: ReviewSnapshot, key: str, field: str, index: int | None = None) -> bool:
    try:
        value_of(snapshot, Slot(key, field, index, 0, 0))
    except UnknownPlaceholder:
        return False
    return True


def record_fields(snapshot: ReviewSnapshot) -> dict[str, list[str]]:
    """Every placeholder the records can fill, by key: what the prompt offers the model."""
    fields: dict[str, list[str]] = {}
    for item in snapshot.countertops:
        fields[item.id] = [name for name in _COUNTERTOP_FIELDS if _held(snapshot, item.id, name)]
        fields[item.id].extend(f"piece.{piece.number}" for piece in item.pieces)
    for finding in snapshot.other_checks:
        fields[finding.id] = [name for name in _FINDING_FIELDS if _held(snapshot, finding.id, name)]
    for page in sorted(snapshot.pages()):
        held = [
            name
            for name in ("page", "no_countertop", "second_row")
            if _held(snapshot, f"P{page}", name)
        ]
        if held:
            fields[f"P{page}"] = held
    fields["count"] = list(COUNT_FIELDS)
    fields["signoff"] = ["status"]
    return fields


def _kind(field: str) -> str:
    if field.startswith("piece."):
        return "noun phrase"
    return KINDS.get(field, "clause")


def placeholder_guide(snapshot: ReviewSnapshot) -> list[str]:
    """One line per placeholder: its kind and the text it fills in here (for the prompt)."""
    lines: list[str] = []
    for key, names in record_fields(snapshot).items():
        for name in names:
            field, _, index = name.partition(".")
            slot = Slot(key, field, int(index) if index else None, 0, 0)
            lines.append(f"{{{key}.{name}}} ({_kind(name)}): {value_of(snapshot, slot)}")
    return lines


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


#: A sentence opening with one of these continues the sentence before it; it never gets a subject
#: of its own put in front ("On page 5, Also, …"). The guard refuses one about another record.
CONNECTOR: Final = re.compile(
    r"^(?:-\s+)?(?:also|then|so|and|but|however|next|finally|additionally|plus|still|meanwhile|"
    r"besides|furthermore|moreover|lastly|again|too|otherwise|therefore|thus|instead)\b",
    re.IGNORECASE,
)


def _only_queue_fill(part: str, outcome_keys: set[str]) -> bool:
    """A sentence whose only fact is "it needs your decision in the review queue" for a record
    whose outcome the answer already states: it would say the outcome twice."""
    in_part = [slot for slot in slots(part) if slot.is_record]
    return bool(in_part) and all(
        slot.field == "needs_you" and slot.key in outcome_keys for slot in in_part
    )


def _with_subject(part: str, header_keys: set[str], snapshot: ReviewSnapshot) -> str:
    """A sentence stating one record's facts without naming it starts "On page N," (code's)."""
    in_part = slots(part)
    keys = {slot.key for slot in in_part if slot.is_record and slot.key[0] in "CF"}
    if len(keys) != 1:
        return part
    key = next(iter(keys))
    named = any(slot.key == key and slot.field in ("page", "label") for slot in in_part)
    if named or key in header_keys or not _held(snapshot, key, "page"):
        return part
    if any(slot.key == key and slot.field == "outcome" for slot in in_part):
        return part  # an outcome needs its subject written; the guard refuses it otherwise
    if CONNECTOR.match(part):
        return part  # continues the sentence before, which names the same record (guard)
    prefix = "- " if part.startswith("- ") else ""
    return f"{prefix}On {{{key}.page}}, {part[len(prefix) :]}"


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

    outcome_keys = {slot.key for slot in found if slot.field == "outcome"}
    pieces: list[str] = []
    header_keys: set[str] = set()
    for part in re.split(r"((?<=[.?!])\s+(?=[A-Z{])|\n+)", template):
        if not part or SENTENCE_BREAK.fullmatch(part):
            pieces.append(part)
            continue
        if _only_queue_fill(part, outcome_keys):
            continue
        if not part.startswith("- "):
            header_keys = (
                {slot.key for slot in slots(part) if slot.is_record}
                if part.rstrip().endswith(":")
                else set()
            )
            part = _with_subject(part, set(), snapshot)
        else:
            part = _with_subject(part, header_keys, snapshot)
        in_part = slots(part)
        last = {slot.key: position for position, slot in enumerate(in_part) if slot.is_record}
        cursor = 0
        out: list[str] = []
        for position, slot in enumerate(in_part):
            before = part[cursor : slot.start]
            out.append(before)
            value = value_of(snapshot, slot)
            at_start = not part[: slot.start].strip(" -")
            if not at_start and slot.field not in _NAMES:
                value = _lower_first(value)
            out.append(value)
            if slot.is_record and last[slot.key] == position:
                out.append(f" [[{citations.index(slot.key)}]]")
            cursor = slot.end
        out.append(part[cursor:])
        text = "".join(out)
        pieces.append(_capitalised(text) if part.lstrip("- ").startswith("{") else text)
    text = "".join(pieces)
    # A dropped sentence leaves its separator behind: tidy the spaces it leaves, keep paragraphs.
    text = re.sub(r"[ \t]+(\n|$)", r"\1", text)
    text = re.sub(r"(\n[ \t]*){3,}", "\n\n", text).strip()
    return Rendered(text=text, citations=tuple(citations), groups=tuple(groups))
