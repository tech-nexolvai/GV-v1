"""Code fills a slot answer and decides it (Step 4): marks in, facts and a verdict out.

The AI named marks (`slot_answers`); everything after that is code's:

1. **Each mark becomes its label**, code's own reading of the drawing (`dims.json`: its text, its
   inches, its box, its route). A mark the call never dealt was already refused by the parser.
2. **Each label is qualified or not** (`unqualified`). Decision B (2026-10-26): a red needs two
   different kinds of reading. A label qualifies when it is the PDF's own text whose unit is
   known (an inch or foot mark, or millimetres with an inch figure that agrees), or drawn ink read
   by OCR whose dimension line agrees with the drawing's scale. A label read only from a pasted
   picture, a scan's own text or one OCR spot, an equal part code worked out, a bare number whose
   unit is not printed, or a label whose millimetres and inches disagree does not qualify: it may
   still be compared, but it is **never red**.
3. **A list is added exactly** (`Fraction`), and is used for a red only when code's own lines prove
   it **contiguous** (`chain`): every part has its dimension line, all run the same way on one
   line, each part starting where the one before ends. Otherwise the sum is compared but the pair
   is unproved and never red. A string's printed overall must also start and end where its parts
   do (the rule: parts and overall share their outer extension lines).
4. **The maths**: the architect's size exactly; the vendor's own overall or second size exactly; GV's
   published rule through the isolated verdict engine (`slot_rules`); a job value exactly.
5. **Facts and the verdict**: each side becomes a `verdict_facts` fact (`Present` with its value,
   page, box and method; `Missing` with one of the review's reasons), the pair's comparability is
   decided (a vendor width is never held against an architect height; a string's overall that
   runs another way bars its red), and the six-row table gives the verdict. What bars a red is
   listed on the outcome (`red_barred`), and `proof` reads it: nothing listed there is ever
   clouded.

Pure: no I/O, no AI call (the rule engine reads the rulebook files once).
Verification: tests/review/assistant/test_slot_maths.py (made-up labels and values).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from itertools import pairwise
from typing import Any, Final

from app.review.assistant import slot_rules
from app.review.assistant.slot_answers import Answer, MarkRef
from app.review.assistant.slots import Slot
from app.review.assistant.verdict_facts import (
    Comparable,
    Fact,
    Missing,
    Present,
    Verdict,
    verdict,
)

__all__ = [
    "FIELD",
    "JOINT_PT",
    "ROW_PT",
    "SlotOutcome",
    "chain",
    "decide",
    "pair_of",
    "unqualified",
]

#: The finding's slot record (`decide`'s outcome, `SlotOutcome.record`).
FIELD: Final = "slot"
#: Two parts meet when one's line ends within this of where the next starts (points): the same
#: joint the advocate's string check allows (`full_check_advocate`).
JOINT_PT: Final = 1.0
#: Parts are on one line when their lines are within this of each other across (points).
ROW_PT: Final = 2.0
#: A string's overall shares its parts' outer extension lines within this (points).
ENDS_PT: Final = 1.5
#: The reason a red is barred, in plain words (`SlotOutcome.red_barred`).
NOT_CODE_READ: Final = "a value code did not read itself (two kinds of reading are needed)"
NOT_CONTIGUOUS: Final = "the parts are not proved to run end to end on one line"
OVERALL_ELSEWHERE: Final = "the overall is not proved to span the same parts"
#: The label origins that are not code's own reading of the drawing's text or ink.
_NOT_CODE: Final = frozenset({"picture", "scan", "ocr"})


def _fraction(value: object) -> Fraction | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Fraction(Decimal(str(value)))
    except (InvalidOperation, ValueError, ArithmeticError):
        return None


def _decimal(value: Fraction) -> Decimal:
    """An exact inch value as a Decimal (inch fractions are halves, quarters ... sixty-fourths, so
    the division ends)."""
    return Decimal(value.numerator) / Decimal(value.denominator)


def _inch_words(value: Fraction) -> str:
    whole, rest = divmod(value, 1)
    if not rest:
        return f'{whole}"'
    return f'{whole} {rest}"' if whole else f'{rest}"'


def unqualified(label: Mapping[str, Any]) -> str | None:
    """Why `label` is not a reading that may carry a red (decision B), else `None`."""
    if _fraction(label.get("inches")) is None:
        return "code has no inch value for it"
    if label.get("derived"):
        return "an equal part code worked out, not a printed value"
    origin = label.get("origin")
    if origin in _NOT_CODE:
        return f"read by OCR from {'a pasted picture' if origin == 'picture' else 'one spot'} only"
    if label.get("units") == "disagrees":
        return "its millimetres and inches disagree"
    text = str(label.get("text") or "")
    route = label.get("route")
    if route == "drawn" or origin == "read_number":
        if label.get("scale_ok") is not True:
            return "drawn ink whose line is not checked against the drawing's scale"
        return None
    if route != "text":
        return "not read from the drawing's own text"
    if '"' not in text and "'" not in text and label.get("units") != "agrees":
        return "a bare number: its unit is not printed"
    return None


def _line(label: Mapping[str, Any]) -> tuple[float, float, float] | None:
    """(start, end, across) of a label's dimension line along its own axis; `None` without one
    (or for a label not read from the drawing's own text or ink)."""
    line = label.get("dim_line")
    if not line or len(line) != 2 or label.get("origin") in _NOT_CODE:
        return None
    if label.get("orientation") not in ("h", "v"):
        return None
    along = 0 if label.get("orientation") == "h" else 1
    try:
        ends = sorted(float(point[along]) for point in line)
        across = float(line[0][1 - along])
    except (TypeError, ValueError, IndexError):
        return None
    return ends[0], ends[1], across


def chain(labels: Sequence[Mapping[str, Any]]) -> tuple[bool, tuple[float, float] | None, str]:
    """Whether code's lines prove `labels` one contiguous run: (proved, (start, end), why).
    One label is a run of one when it has its line."""
    lines = [_line(label) for label in labels]
    if any(line is None for line in lines):
        return False, None, "a part has no dimension line code found"
    ways = {label.get("orientation") for label in labels}
    if len(ways) != 1:
        return False, None, "the parts do not all run the same way"
    found = sorted(line for line in lines if line is not None)
    across = [line[2] for line in found]
    if max(across) - min(across) > ROW_PT:
        return False, None, "the parts are not on one line"
    for before, after in pairwise(found):
        if abs(after[0] - before[1]) > JOINT_PT:
            return False, None, "the parts leave a gap or overlap"
    return True, (found[0][0], found[-1][1]), ""


def _union(refs: Sequence[MarkRef]) -> tuple[float, float, float, float] | None:
    boxes = []
    for ref in refs:
        box = ref.label.get("box")
        if isinstance(box, Sequence) and len(box) == 4:
            boxes.append([float(v) for v in box])
    if not boxes:
        return None
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


@dataclass(slots=True)
class _Group:
    """One side's named marks, read by code."""

    refs: tuple[MarkRef, ...]
    total: Fraction | None
    barred: list[str] = field(default_factory=list)
    unread: list[str] = field(default_factory=list)
    span: tuple[float, float] | None = None

    @property
    def text(self) -> str:
        texts = [" ".join(str(ref.label.get("text") or "").split()) for ref in self.refs]
        if len(texts) == 1 or self.total is None:
            return " + ".join(texts)
        return f"{' + '.join(texts)} = {_inch_words(self.total)}"


def _group(refs: Sequence[MarkRef]) -> _Group:
    values = [_fraction(ref.label.get("inches")) for ref in refs]
    unread = [ref.mark for ref, value in zip(refs, values, strict=True) if value is None]
    total = None if unread or not refs else sum((v for v in values if v is not None), Fraction(0))
    group = _Group(tuple(refs), total, unread=unread)
    reasons = [why for why in (unqualified(ref.label) for ref in refs) if why is not None]
    if reasons:
        group.barred.append(NOT_CODE_READ)
    if len(refs) > 1:
        proved, span, _why = chain([ref.label for ref in refs])
        if not proved:
            group.barred.append(NOT_CONTIGUOUS)
        group.span = span
    elif refs:
        line = _line(refs[0].label)
        group.span = (line[0], line[1]) if line is not None else None
    return group


def _present(group: _Group, source: str) -> Fact:
    if group.total is None:
        return Missing("unreadable", "code has no inch value for a named mark")
    first = group.refs[0]
    return Present(
        value=group.text,
        unit="inches",
        inches=_decimal(group.total),
        page=first.page,
        box=_union(group.refs),
        source=source,
        method=str(first.label.get("route") or "text") if len(group.refs) == 1 else "sum",
        trust="unsure" if NOT_CODE_READ in group.barred else "code",
    )


@dataclass(frozen=True, slots=True)
class SlotOutcome:
    """One slot answer, decided by code."""

    slot: Slot
    answer: Answer
    vendor: Fact
    reference: Fact
    comparable: Comparable
    verdict: Verdict
    red_barred: tuple[str, ...]
    maths: Mapping[str, Any] | None
    """The maths record (`ai_maths.FIELD` shape): result, kind, words, read_from."""
    rule: Mapping[str, Any] | None = None

    @property
    def differs(self) -> bool:
        return self.verdict.differs

    def record(self) -> dict[str, Any]:
        """The finding's slot record (`FIELD`): what was asked, the marks, the facts, the row."""
        marks = {
            name: [
                {"mark": r.mark, "file": r.file, "page": r.page, "label": r.label_id} for r in refs
            ]
            for name, refs in (
                ("vendor", self.answer.shop),
                ("against", self.answer.against),
                ("architect", self.answer.architect),
            )
        }
        return {
            "key": self.slot.key,
            "measures": self.answer.measures,
            "maths": self.slot.maths,
            "compares": self.slot.compares,
            "marks": marks,
            "ends": dict(self.answer.ends),
            "row": self.verdict.row,
            "outcome": (
                "same"
                if self.verdict.same
                else "differs" if self.verdict.differs else "not_compared"
            ),
            "red_barred": list(self.red_barred),
            "facts": {
                "vendor": _fact_dict(self.vendor),
                "reference": _fact_dict(self.reference),
                "comparable": {
                    "ok": self.comparable.ok,
                    "reason": self.comparable.reason,
                    "why": self.comparable.why,
                    "proved": self.comparable.proved,
                },
            },
            **({"rule": dict(self.rule)} if self.rule else {}),
        }


def _fact_dict(fact: Fact) -> dict[str, Any]:
    if isinstance(fact, Missing):
        return {"found": False, "reason": fact.reason, "why": fact.why}
    return {
        "found": True,
        "value": fact.value,
        "unit": fact.unit,
        "inches": None if fact.inches is None else str(fact.inches),
        "page": fact.page,
        "box": None if fact.box is None else list(fact.box),
        "source": fact.source,
        "method": fact.method,
        "trust": fact.trust,
    }


def _fact_of(raw: Mapping[str, Any]) -> Fact:
    if not raw.get("found"):
        return Missing(str(raw.get("reason") or "nothing_to_compare"), str(raw.get("why") or ""))
    box = raw.get("box")
    return Present(
        value=str(raw.get("value") or ""),
        unit=raw.get("unit") or "none",
        inches=None if raw.get("inches") is None else Decimal(str(raw["inches"])),
        page=raw.get("page") if isinstance(raw.get("page"), int) else None,
        box=tuple(float(v) for v in box) if isinstance(box, list) and len(box) == 4 else None,  # type: ignore[arg-type]
        source=str(raw.get("source") or "unknown"),
        method=str(raw.get("method") or "unknown"),
        trust=raw.get("trust") or "unsure",
    )


def pair_of(record: Mapping[str, Any]) -> tuple[Fact, Fact, Comparable] | None:
    """A stored slot record's two facts and comparability (`verdict_adapter` reads them), else
    `None` when the record holds no facts."""
    facts = record.get("facts")
    if not isinstance(facts, Mapping):
        return None
    given = facts.get("comparable")
    pair: Mapping[str, Any] = given if isinstance(given, Mapping) else {}
    ok = bool(pair.get("ok"))
    comparable = (
        Comparable(True)
        if ok
        else Comparable(
            False,
            str(pair.get("reason") or "not_proved"),
            str(pair.get("why") or ""),
            proved=bool(pair.get("proved")),
        )
    )
    return (
        _fact_of(facts.get("vendor") or {}),
        _fact_of(facts.get("reference") or {}),
        comparable,
    )


def _orientations(refs: Sequence[MarkRef]) -> set[str]:
    return {
        str(r.label.get("orientation")) for r in refs if r.label.get("orientation") in ("h", "v")
    }


def _maths(result: str, kind: str, words: str) -> dict[str, Any]:
    return {"result": result, "kind": kind, "words": words[:600], "read_from": "slots"}


def _finish(
    slot: Slot,
    answer: Answer,
    vendor: Fact,
    reference: Fact,
    comparable: Comparable,
    differs: bool,
    barred: Sequence[str],
    maths: Mapping[str, Any] | None,
    rule: Mapping[str, Any] | None = None,
) -> SlotOutcome:
    found = verdict(vendor, reference, comparable, differs)
    unique = tuple(dict.fromkeys(barred))
    return SlotOutcome(slot, answer, vendor, reference, comparable, found, unique, maths, rule)


def decide(
    slot: Slot,
    answer: Answer,
    *,
    rules_md: str | None = None,
    when: datetime,
) -> SlotOutcome:
    """One answer, decided by code (see the module). `when` stamps any setting the rule engine is
    handed (a parameter records when it was set; this module reads no clock)."""
    shop = _group(answer.shop)
    if slot.maths == "architect":
        return _architect(slot, answer, shop)
    if slot.maths in ("string", "vendor_equal"):
        return _vendor_own(slot, answer, shop)
    if slot.maths == "job_value":
        return _job_value(slot, answer, shop, rules_md)
    return _rule(slot, answer, shop, rules_md, when)


def _architect(slot: Slot, answer: Answer, shop: _Group) -> SlotOutcome:
    vendor = _present(shop, "vendor")
    if not answer.architect:
        reference: Fact = Missing("nothing_to_compare", "no architect mark was named")
        arch = None
    else:
        arch = _group(answer.architect)
        reference = _present(arch, "architect")
    barred = [*shop.barred, *(arch.barred if arch else [])]
    comparable = Comparable(True)
    ways_v, ways_a = _orientations(answer.shop), _orientations(answer.architect)
    if len(ways_v) == 1 and len(ways_a) == 1 and ways_v != ways_a:
        comparable = Comparable(
            False, "different_reference", "one is a width, the other a height", proved=True
        )
    differs = bool(
        isinstance(vendor, Present)
        and isinstance(reference, Present)
        and vendor.inches != reference.inches
    )
    maths = None
    if shop.total is not None and arch is not None and arch.total is not None:
        words = (
            f"The vendor's {_inch_words(shop.total)} against the architect's "
            f"{_inch_words(arch.total)}"
            + (
                ": the same."
                if not differs
                else f": {_inch_words(abs(shop.total - arch.total))} apart."
            )
        )
        maths = _maths("differs" if differs else "same", "sizes", words)
    return _finish(slot, answer, vendor, reference, comparable, differs, barred, maths)


def _vendor_own(slot: Slot, answer: Answer, shop: _Group) -> SlotOutcome:
    """`string`: the overall (`against`) is the vendor's value and the parts' sum the reference;
    `vendor_equal`: the vendor's size against the vendor's own second size (a sum allowed)."""
    against = _group(answer.against)
    if slot.maths == "string":
        printed, parts = against, shop
    else:
        printed, parts = shop, against
    vendor = (
        _present(printed, "vendor") if printed.refs else Missing("no_overall", "no overall named")
    )
    reference: Fact = (
        _present(parts, "vendor_parts") if parts.refs else Missing("no_overall", "nothing named")
    )
    barred = [*shop.barred, *against.barred]
    if slot.maths == "string":
        if len(parts.refs) < 2:
            barred.append(NOT_CONTIGUOUS)
        elif (
            printed.span is None
            or parts.span is None
            or abs(printed.span[0] - parts.span[0]) > ENDS_PT
            or abs(printed.span[1] - parts.span[1]) > ENDS_PT
            or _orientations(printed.refs) != _orientations(parts.refs)
        ):
            barred.append(OVERALL_ELSEWHERE)
    differs = bool(
        isinstance(vendor, Present)
        and isinstance(reference, Present)
        and vendor.inches != reference.inches
    )
    maths = None
    if printed.total is not None and parts.total is not None and printed.refs and parts.refs:
        words = f"Code adds {parts.text}; the vendor prints {_inch_words(printed.total)}" + (
            ": the same."
            if not differs
            else f": {_inch_words(abs(printed.total - parts.total))} apart."
        )
        maths = _maths("differs" if differs else "same", "string", words)
    return _finish(slot, answer, vendor, reference, Comparable(True), differs, barred, maths)


def _job_value(slot: Slot, answer: Answer, shop: _Group, rules_md: str | None) -> SlotOutcome:
    from app.review.assistant.sink_offset import setting_from

    vendor = _present(shop, "vendor")
    setting = (
        setting_from(rules_md, slot.setting or "", slot.setting or "") if slot.setting else None
    )
    if setting is None:
        reference: Fact = Missing(
            "missing_setting", slot.missing_fact_when or "the job value is not entered"
        )
        return _finish(slot, answer, vendor, reference, Comparable(True), False, shop.barred, None)
    reference = Present(
        value=setting.text,
        unit="inches",
        inches=_decimal(setting.value),
        source="gv_rule",
        method="rule",
        trust="code",
    )
    differs = isinstance(vendor, Present) and vendor.inches != reference.inches
    maths = None
    if shop.total is not None:
        words = (
            f"The vendor prints {_inch_words(shop.total)}; the job's value is {setting.text}"
            + (": the same." if not differs else ": they differ.")
        )
        maths = _maths("differs" if differs else "same", "rule", words)
    return _finish(slot, answer, vendor, reference, Comparable(True), differs, shop.barred, maths)


#: Each published rule's input names for the slot's two groups of vendor marks.
_RULE_INPUTS: Final[Mapping[str, tuple[str, str | None]]] = {
    "CT-WIDTH-001": ("countertop_width", "piece_widths"),
    "CT-SINK-CUTOUT-WIDTH-001": ("cutout_width", None),
    "CT-SINK-CUTOUT-DEPTH-001": ("cutout_depth", None),
    "CT-SINK-OFFSET-FRONT-001": ("front_offset", None),
}


def _evidence(refs: Sequence[MarkRef]) -> str:
    return ",".join(f"{r.file}:p{r.page}:{r.label_id}" for r in refs)


def _rule(
    slot: Slot, answer: Answer, shop: _Group, rules_md: str | None, when: datetime
) -> SlotOutcome:
    vendor = _present(shop, "vendor")
    rule_id = slot.rule_id(answer.measures)
    inputs = _RULE_INPUTS.get(rule_id or "")
    if rule_id is None or inputs is None:
        reference: Fact = Missing("nothing_to_compare", "no published rule for this slot")
        return _finish(slot, answer, vendor, reference, Comparable(True), False, shop.barred, None)
    against = _group(answer.against)
    barred = [*shop.barred, *against.barred]
    values: dict[str, slot_rules.Value] = {}
    if shop.total is not None:
        values[inputs[0]] = slot_rules.Value(
            (shop.total,), _evidence(answer.shop), NOT_CODE_READ not in shop.barred
        )
    if inputs[1] is not None and against.refs:
        each = [_fraction(r.label.get("inches")) for r in against.refs]
        if all(v is not None for v in each):
            values[inputs[1]] = slot_rules.Value(
                tuple(v for v in each if v is not None),
                _evidence(answer.against),
                not against.barred,
                many=True,
            )
    decided = slot_rules.run_rule(rule_id, values, rules_md=rules_md, when=when, ends=answer.ends)
    rule = {
        "id": rule_id,
        "snapshot_id": decided.snapshot_id,
        "outcome": decided.outcome,
        "words": decided.words,
    }
    comparable = Comparable(True)
    if decided.result == "missing":
        reference = Missing(decided.missing or "nothing_to_compare", decided.words)
    else:
        reference = Present(
            value=f"GV's rule {rule_id}: {decided.words}",
            unit="words",
            source="gv_rule",
            method="rule",
            trust="code",
        )
        if decided.result == "review":
            comparable = Comparable(False, "not_proved", decided.words)
    differs = decided.result == "differs"
    maths = None
    if decided.result in ("same", "differs"):
        maths = _maths(decided.result, "rule", f"GV's rule {rule_id}: {decided.words}.")
    return _finish(slot, answer, vendor, reference, comparable, differs, barred, maths, rule)


# ---- the finding a decided answer becomes ----------------------------------------------------------

#: Who records a slot answer's finding (`chat_findings.FindingBook.report`).
CREATED_BY: Final = "slots"
#: The finding's source for each kind of maths (`chat_findings.SOURCES`).
_SOURCES: Final[Mapping[str, str]] = {
    "architect": "architect",
    "string": "vendor_arithmetic",
    "vendor_equal": "vendor_arithmetic",
    "rule": "rules",
    "job_value": "rules",
}


def _ref(ref: MarkRef) -> str:
    return f"p{ref.page}.{ref.label_id}"


def _box_of(refs: Sequence[MarkRef], file: str) -> dict[str, Any] | None:
    box = _union(refs)
    if box is None or not refs:
        return None
    return {
        "page": refs[0].page,
        "x0": box[0],
        "y0": box[1],
        "x1": box[2],
        "y1": box[3],
        "file": file,
    }


def finding_fields(
    outcome: SlotOutcome, category: str = "other"
) -> tuple[dict[str, Any], dict[str, Any]]:
    """(what `FindingBook.report` is given, what is laid on the recorded finding after it).

    The report names a single mark's label (`vendor_dim`, `architect_dim`), so the book fills that
    side from code's own label as every other label finding is filled; a sum is given as its
    words and the union of its labels' boxes. Laid on after: the slot record (`FIELD`) and the
    maths record (`ai_maths.FIELD`), which code alone wrote."""
    slot, answer = outcome.slot, outcome.answer
    if slot.maths == "string":
        printed, other = answer.against, answer.shop
    elif slot.maths == "vendor_equal":
        printed, other = answer.shop, answer.against
    else:
        printed, other = answer.shop, answer.architect
    first = printed[0] if printed else answer.shop[0]
    item = f"{slot.key.replace('_', ' ')}: {answer.measures.replace('_', ' ')}"
    raw: dict[str, Any] = {
        "page": first.page,
        "item": item[:150],
        "category": category,
        "status": "different" if outcome.differs else "check",
        "verified": False,
        "source": _SOURCES[slot.maths],
        "why": (outcome.maths or {}).get("words") or f"The fixed question: {slot.asks}",
    }
    if len(printed) == 1:
        raw["vendor_dim"] = _ref(printed[0])
    elif printed:
        raw["vendor"] = " + ".join(str(r.label.get("text") or "") for r in printed)[:200]
        raw["vendor_box"] = _box_of(printed, printed[0].file)
    reference = outcome.reference
    if slot.maths == "architect" and len(other) == 1:
        raw["architect_dim"] = _ref(other[0])
        if other[0].page != first.page or other[0].file != first.file:
            raw["vs_page"] = other[0].page
    else:
        raw["architect"] = (reference.value if isinstance(reference, Present) else "not given")[
            :200
        ]
        if other:
            raw["arch_box"] = _box_of(other, other[0].file)
            if slot.maths == "architect" and other[0].page != first.page:
                raw["vs_page"] = other[0].page
    if slot.maths == "string" and len(answer.shop) >= 2 and answer.against:
        raw["parts"] = [str(r.label.get("text") or "") for r in answer.shop]
        raw["printed_total"] = str(answer.against[0].label.get("text") or "")
    extra: dict[str, Any] = {FIELD: outcome.record()}
    if outcome.maths is not None:
        extra["maths"] = dict(outcome.maths)
    return raw, extra
