"""The slot answer: marks, never numbers (Step 4).

The AI answers each slot by naming numbered marks on the two pictures: the vendor's marks that
answer it (`shop_mark_ids`), the vendor's second group where the slot holds the vendor against
itself or a GV rule (`shop_against_mark_ids`: a string's printed overall, the run under a top), and
the architect's marks (`arch_mark_ids`). A list is allowed on every side (a vendor's three cabinets
and two fillers against the architect's one overall); code adds it. Or it says the slot is not
shown, with one reason from a fixed set. It also says what the dimension measures, from a fixed
vocabulary (`slots.MEASURES`), so code can tell a width from a depth.

**No typed number can reach code.** The reply shape (`schema`) holds no number field at all: every
field is a closed list of words or of this call's own mark ids. The parser (`parse`) refuses any
field the shape does not name and any value that is not one of those words, so a typed "36" is
refused even if a model ignored the shape.

**The marks are shuffled per call** (`MarkMap`): mark numbers are dealt in an order drawn from the
call's own seed, and the map is kept, so a model's habit of picking low numbers or the first mark
listed cannot line up with the drawing's own order. `MarkMap.label_of` and `MarkMap.mark_of` take a
mark to its label and back.

Pure: no I/O, no AI call. Verification: tests/review/assistant/test_slot_answers.py.
"""

from __future__ import annotations

import random
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from app.review.assistant.slots import MEASURES, Slot

__all__ = [
    "ENDS",
    "NOT_SHOWN",
    "Answer",
    "AnswerRefused",
    "MarkMap",
    "MarkRef",
    "SlotReply",
    "parse",
    "schema",
]

type Side = Literal["vendor", "architect"]

#: Why a slot is not answered, from a fixed set (the AI picks one).
NOT_SHOWN: Final[tuple[str, ...]] = (
    "not_on_vendor_view",
    "not_on_architect_view",
    "printed_without_a_mark",
    "on_another_sheet",
    "not_this_kind_of_item",
    "cannot_tell",
)
#: What one end of a top meets (only the countertop's length slot reads it).
ENDS: Final[tuple[str, ...]] = ("wall", "free", "not_shown", "not_asked")
_MARK: Final = re.compile(r"^M(\d{1,4})$")
_ANSWER_FIELDS: Final = frozenset(
    {"what_it_measures", "shop_mark_ids", "shop_against_mark_ids", "arch_mark_ids", "ends"}
)
_SLOT_FIELDS: Final = frozenset({"slot", "status", "not_shown_reason", "answers"})
#: The most answers one slot may give on one item (a run's parts), and marks in one list.
MAX_ANSWERS: Final = 16
MAX_MARKS: Final = 24


class AnswerRefused(ValueError):
    """The reply does not follow the slot shape; the message says what is wrong."""


@dataclass(frozen=True, slots=True)
class MarkRef:
    """What one mark stands for: a label code read on one side's drawing."""

    mark: str
    side: Side
    file: str
    page: int
    label_id: str
    label: Mapping[str, Any] = field(compare=False, hash=False)


class MarkMap:
    """This call's marks: each code-read label of the two views gets a mark id (`M1`...), dealt in
    a shuffled order drawn from `seed`, and the map is kept both ways."""

    def __init__(
        self,
        labels: Sequence[tuple[Side, str, int, Mapping[str, Any]]],
        seed: str,
    ) -> None:
        """`labels`: (side, file, page, label) for every label the pictures show."""
        unique: list[tuple[Side, str, int, Mapping[str, Any]]] = []
        seen: set[tuple[str, int, str]] = set()
        for side, file, page, label in labels:
            key = (file, int(page), str(label.get("id")))
            if key not in seen:
                seen.add(key)
                unique.append((side, file, int(page), label))
        numbers = list(range(1, len(unique) + 1))
        random.Random(seed).shuffle(numbers)
        self._by_mark: dict[str, MarkRef] = {}
        self._by_label: dict[tuple[str, int, str], str] = {}
        for number, (side, file, page, label) in zip(numbers, unique, strict=True):
            mark = f"M{number}"
            self._by_mark[mark] = MarkRef(mark, side, file, page, str(label.get("id")), label)
            self._by_label[(file, page, str(label.get("id")))] = mark

    def __len__(self) -> int:
        return len(self._by_mark)

    def marks(self, side: Side | None = None) -> list[str]:
        """The mark ids, in number order (only `side`'s when given)."""
        found = [m for m, ref in self._by_mark.items() if side is None or ref.side == side]
        return sorted(found, key=lambda mark: int(mark[1:]))

    def refs(self, side: Side | None = None) -> list[MarkRef]:
        return [self._by_mark[mark] for mark in self.marks(side)]

    def label_of(self, mark: str) -> MarkRef:
        """The label a mark stands for. Raises `KeyError` for a mark this call never dealt."""
        return self._by_mark[mark]

    def mark_of(self, file: str, page: int, label_id: str) -> str | None:
        """The mark dealt to a label, else `None`."""
        return self._by_label.get((file, int(page), str(label_id)))

    def as_dict(self) -> dict[str, list[Any]]:
        """For the run's log: mark id -> [side, file, page, label id]."""
        return {
            mark: [ref.side, ref.file, ref.page, ref.label_id]
            for mark, ref in self._by_mark.items()
        }


@dataclass(frozen=True, slots=True)
class Answer:
    """One answer to one slot on one item, as marks code then reads."""

    measures: str
    shop: tuple[MarkRef, ...]
    against: tuple[MarkRef, ...] = ()
    architect: tuple[MarkRef, ...] = ()
    ends: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SlotReply:
    """One slot's reply on one item: answered (with its answers), or not shown (with why)."""

    key: str
    status: Literal["answered", "not_shown"]
    reason: str | None = None
    answers: tuple[Answer, ...] = ()
    refused: tuple[str, ...] = ()
    """Answers code refused (an unknown mark, a mark of the wrong side), each with why."""


def _marks_field(marks: Sequence[str], words: str) -> dict[str, Any]:
    items: dict[str, Any] = {"type": "string"}
    if marks:
        items["enum"] = list(marks)
    return {
        "type": "array",
        "items": items,
        "maxItems": MAX_MARKS if marks else 0,
        "description": words,
    }


def schema(asked: Sequence[Slot], marks: MarkMap) -> dict[str, Any]:
    """The reply shape for one call: every field a closed list of words or of this call's mark
    ids (no number field anywhere)."""
    vendor, architect = marks.marks("vendor"), marks.marks("architect")
    end = {"type": "string", "enum": list(ENDS)}
    answer = {
        "type": "object",
        "additionalProperties": False,
        "required": sorted(_ANSWER_FIELDS),
        "properties": {
            "what_it_measures": {"type": "string", "enum": list(MEASURES)},
            "shop_mark_ids": _marks_field(vendor, "The vendor's marks that answer it"),
            "shop_against_mark_ids": _marks_field(
                vendor, "The vendor's second group the slot asks for, else none"
            ),
            "arch_mark_ids": _marks_field(architect, "The architect's marks, else none"),
            "ends": {
                "type": "object",
                "additionalProperties": False,
                "required": ["left", "right"],
                "properties": {"left": end, "right": end},
            },
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["slots"],
        "properties": {
            "slots": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": sorted(_SLOT_FIELDS),
                    "properties": {
                        "slot": {"type": "string", "enum": [one.key for one in asked]},
                        "status": {"type": "string", "enum": ["answered", "not_shown"]},
                        "not_shown_reason": {"type": "string", "enum": [*NOT_SHOWN, "none"]},
                        "answers": {"type": "array", "items": answer, "maxItems": MAX_ANSWERS},
                    },
                },
            }
        },
    }


def _word(value: object, allowed: Sequence[str], where: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise AnswerRefused(f"{where}: {value!r} is not one of the allowed words")
    return value


def _refs(value: object, marks: MarkMap, side: Side, where: str) -> tuple[MarkRef, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise AnswerRefused(f"{where}: must be a list of mark ids")
    out: list[MarkRef] = []
    for item in value[:MAX_MARKS]:
        if not isinstance(item, str) or _MARK.match(item) is None:
            raise AnswerRefused(f"{where}: {item!r} is not a mark id (a typed value is refused)")
        try:
            ref = marks.label_of(item)
        except KeyError:
            raise AnswerRefused(f"{where}: {item} is not a mark on these pictures") from None
        if ref.side != side:
            raise AnswerRefused(f"{where}: {item} is on the {ref.side}'s picture, not the {side}'s")
        if ref not in out:
            out.append(ref)
    return tuple(out)


def _answer(raw: object, marks: MarkMap, slot: Slot) -> Answer:
    if not isinstance(raw, Mapping):
        raise AnswerRefused("an answer must be an object")
    unknown = set(raw) - _ANSWER_FIELDS
    if unknown:
        raise AnswerRefused(f"unknown field {min(unknown)!r} (typed values are refused)")
    measures = _word(raw.get("what_it_measures"), slot.measures, "what_it_measures")
    ends_raw = raw.get("ends") or {}
    if not isinstance(ends_raw, Mapping) or set(ends_raw) - {"left", "right"}:
        raise AnswerRefused("ends: only left and right")
    ends = {name: _word(ends_raw.get(name, "not_asked"), ENDS, f"ends.{name}") for name in ends_raw}
    shop = _refs(raw.get("shop_mark_ids"), marks, "vendor", "shop_mark_ids")
    if not shop:
        raise AnswerRefused("an answer names at least one vendor mark")
    return Answer(
        measures=measures,
        shop=shop,
        against=_refs(raw.get("shop_against_mark_ids"), marks, "vendor", "shop_against_mark_ids"),
        architect=_refs(raw.get("arch_mark_ids"), marks, "architect", "arch_mark_ids"),
        ends=ends,
    )


def parse(reply: Mapping[str, Any], asked: Sequence[Slot], marks: MarkMap) -> list[SlotReply]:
    """The reply as slot answers. A slot the reply leaves out is not in the list (the coverage
    ledger counts it as not asked); a malformed slot entry raises `AnswerRefused`; a malformed
    *answer* is refused on its own and kept in `refused` with why, so one bad mark never drops the
    slot's other answers."""
    if not isinstance(reply, Mapping) or set(reply) - {"slots"}:
        raise AnswerRefused("the reply must be an object with only `slots`")
    entries = reply.get("slots")
    if not isinstance(entries, list):
        raise AnswerRefused("slots must be a list")
    by_key = {one.key: one for one in asked}
    out: dict[str, SlotReply] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise AnswerRefused("each slot must be an object")
        unknown = set(entry) - _SLOT_FIELDS
        if unknown:
            raise AnswerRefused(f"unknown field {min(unknown)!r} (typed values are refused)")
        key = _word(entry.get("slot"), list(by_key), "slot")
        status = _word(entry.get("status"), ("answered", "not_shown"), "status")
        if key in out:
            continue  # the first reply for a slot stands
        if status == "not_shown":
            reason = _word(entry.get("not_shown_reason"), NOT_SHOWN, "not_shown_reason")
            out[key] = SlotReply(key, "not_shown", reason)
            continue
        raw_answers = entry.get("answers")
        if not isinstance(raw_answers, list):
            raise AnswerRefused("answers must be a list")
        answers: list[Answer] = []
        refused: list[str] = []
        for raw in raw_answers[:MAX_ANSWERS]:
            try:
                answers.append(_answer(raw, marks, by_key[key]))
            except AnswerRefused as why:
                refused.append(str(why))
        if not by_key[key].many:
            answers = answers[:1]
        if answers:
            out[key] = SlotReply(key, "answered", None, tuple(answers), tuple(refused))
        else:
            out[key] = SlotReply(key, "not_shown", "cannot_tell", (), tuple(refused))
    return list(out.values())
