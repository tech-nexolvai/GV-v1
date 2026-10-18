"""The amber "Vendor to confirm" question: one plain question to the vendor per undecided item.

The owner wants no human in the loop (2026-10-18). An item code could not settle (a Wrong whose
proof is missing, a "Your decision" item the resolver kept) used to sit in a grey list that said
"check before sending": someone had to look. Now, after the full check's escalation
(`escalation.py`) has tried everything it can within the fixed price, such an item becomes an amber
**Vendor to confirm** item: the cloud drawing puts an amber marker and a note "Confirm: <question>"
on the vendor's sheet (never a cloud: red stays for proven Wrongs) and lists the question in the
comment pages; round 2 then says whether the vendor answered it (`round_check`).

The question is written by code from the finding's own fields, by why it is undecided (the
resolver's `keep_reason`, #1567, or for a Wrong the missing pairing proof), and follows one
writing rule (tested):

- **plain and specific**: it names the item, cites the page and the values on both sides;
- **never accuses**: no "wrong", "error", "mistake" or "incorrect": the vendor is asked to confirm,
  not told it failed;
- **never says "AI"** (nor "helper", "model", GV's internal words such as "markup" or "yellow box").

An item with nothing to ask (an area that simply has no vendor sheet, an item with no values on
either side) gets no question: it is listed as "Not applicable / no information".

Pure code: no I/O, no AI call.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from app.review.assistant.chat_trust import inch_words
from app.review.assistant.finding_checks import to_inches

__all__ = [
    "BANNED",
    "PREFIX",
    "count_words",
    "has_question",
    "question_of",
    "writes_well",
]

#: The note on the sheet and the comment pages start with this.
PREFIX: Final = "Confirm: "
#: Words a question to the vendor never uses: it does not accuse and never names the AI or GV's
#: own working words.
BANNED: Final = re.compile(
    r"\b(?:AI|A\.I\.|wrong|errors?|mistakes?|incorrect|helpers?|LLM|markup|yellow[- ]box|"
    r"GV'?s? (?:cloud|box|note|ink)|check before sending|scaled? by eye)\b",
    re.IGNORECASE,
)
#: Bits of a side's words that are the AI's working notes, not what the drawing prints.
_NOISE: Final = re.compile(
    r"\([^)]*(?:\bAI\b|helper|markup|yellow|GV'?s|scal|about|approx|unclear|cut off|not fully|"
    r"tiny|uncertain|legible|partly|round \d|rev \d)[^)]*\)|\b(?:about|approx\w*|roughly)\s+|"
    r"^\s*(?:brand prototype|prototype|vendor|architect)(?: shows)?\s*:?\s+(?=\S)|"
    r"\s*\(?\b(?:ST|CT|SS)?-?\S*\)?\s*(?:STONE TOP )?by GV\b",
    re.IGNORECASE,
)
_SIZE: Final = re.compile(
    r"\d+'\s*-?\s*\d+(?:\s+\d+/\d+)?\s*\"?|\d+(?:\s+\d+/\d+)?\s*(?:\"|”|in\b)?"
)
_LONGEST: Final = 70
#: Sources with nothing on the architect's side to compare: a page with no vendor sheet and the
#: like (code's completeness list).
_NOTHING_TO_ASK: Final = frozenset({"completeness"})
#: Words for a count of drawn parts: a layout question names the counts when code can read them
#: from both sides' words.
_COUNT: Final = re.compile(
    r"\b(\d+|no|one|two|three|four|five|six|seven|eight|nine|ten)\s+"
    r"(?:[\w.]+\s+){0,2}?"
    r"(lockers?|doors?|drawers?|bins?|shelves|shelf|bays?|cubbies|cubby|panels?|pull-?outs?)\b",
    re.IGNORECASE,
)
_NUMBER_WORDS: Final[Mapping[str, int]] = {
    "no": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}
#: "the drawings' own" words for a spans enum (`full_check_advocate.SPANS`).
_SPAN_WORDS: Final[Mapping[str, str]] = {
    "top_of_counter": "the top of the counter",
    "underside_of_counter": "the underside of the counter",
    "top_of_splash": "the top of the splash",
    "floor": "the floor",
    "wall_face": "the wall",
    "cabinet_side": "the cabinet side",
    "cabinet_end": "the cabinet end",
    "centre_line": "a centre line",
    "box_above_unit": "the box above the unit",
    "blocking": "the blocking",
}


# ---- words --------------------------------------------------------------------------------------


def _flat(text: object) -> str:
    return " ".join(str(text or "").split())


def _short(text: object, limit: int = _LONGEST) -> str:
    """One side's words, cleaned of working notes, at most `limit` characters, cut at a word."""
    words = _flat(_NOISE.sub("", _flat(text)))
    # The first clause: a side's words often go on with notes ("…; round 2 changed it").
    words = re.split(r"\s*[;\n]\s*", words)[0].strip(" ,.")
    if len(words) <= limit:
        return words
    cut = words[:limit].rsplit(" ", 1)[0].rstrip(",;:(")
    return f"{cut}…"


def _thing(item: object) -> str:
    """The item inside a sentence: "the bussing station overall width" (its capitals kept for a
    name: "the ADA vanity", "the Connection Centre counter")."""
    flat = _flat(_NOISE.sub("", _flat(item))) or "item"
    flat = re.sub(r"\s*\((?:rev|round)[^)]*\)", "", flat, flags=re.IGNORECASE)
    words = flat.split(" ")
    second = words[1] if len(words) > 1 else ""
    if words[0][1:] == words[0][1:].lower() and not second[:1].isupper():
        flat = flat[:1].lower() + flat[1:]
    return f"the {flat}"


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


_MM_IN: Final = re.compile(r"^(\d{2,5})\s*\[([^\]]+)\]")
_IN_MM: Final = re.compile(r"^(\d+(?:\s+\d+/\d+)?)\s*[(\[](\d{2,5})[)\]]")


def _value(finding: Mapping[str, Any], side: str) -> str:
    """A side's value as printed: feet and inches with code's inches (`3'-1" (37")`), millimetres
    and inches as inches (`16"` for "407 [16]"), else the size the words start with, else the
    side's own short words."""
    inches = _decimal((finding.get("inches") or {}).get(side))
    words = _short(finding.get(side), 48)
    first = _SIZE.match(words)
    if inches is not None:
        shown = inch_words(inches)
        if first is not None and "'" in first.group(0):
            return f"{first.group(0).strip()} ({shown})"
        return shown
    paired = _MM_IN.match(words)
    if paired is not None:
        return f'{paired.group(2).strip()}"'
    paired = _IN_MM.match(words)
    if paired is not None and _mm_agrees(paired.group(1), paired.group(2)):
        return f'{paired.group(1)}"'
    if first is not None and first.group(0).strip() and _whole(words, first.group(0)):
        return first.group(0).strip()
    return words


def _mm_agrees(inches: str, mm: str) -> bool:
    """`24 (610)`: the bracketed millimetres are the same size, so the plain number is inches."""
    value = to_inches(f'{inches}"')
    return value is not None and abs(value * Decimal("25.4") - Decimal(mm)) <= Decimal(3)


def _is_size(text: str) -> bool:
    found = _SIZE.match(text)
    return found is not None and ('"' in found.group(0) or "'" in found.group(0))


def _whole(words: str, size: str) -> bool:
    """The words are just a size, or a size and a word or two about it ("191\" overall")."""
    rest = words[len(size) :].strip()
    return ('"' in size or "'" in size) and len(rest.split()) <= 2


def _page(finding: Mapping[str, Any]) -> int | None:
    page = finding.get("page")
    return int(page) if isinstance(page, int) else None


def _arch_page(finding: Mapping[str, Any]) -> int | None:
    box = finding.get("arch_box")
    if isinstance(box, Mapping) and isinstance(box.get("page"), int):
        return int(box["page"])
    page = finding.get("vs_page")
    return int(page) if isinstance(page, int) else None


def _on(page: int | None, own: int | None = None) -> str:
    if page is None:
        return ""
    return " on the same sheet" if page == own else f" on page {page}"


def count_words(text: object) -> dict[str, int]:
    """Counts of drawn parts a side's words state ("4 lockers", "no lockers drawn"), by part."""
    found: dict[str, int] = {}
    for number, part in _COUNT.findall(_flat(text)):
        key = part.lower().rstrip("s").replace("shelve", "shelf").replace("cubbie", "cubby")
        value = _NUMBER_WORDS.get(number.lower())
        found.setdefault(key, value if value is not None else int(number))
    return found


def writes_well(question: str) -> bool:
    """The writing rule: never accuses, never names the AI or GV's working words, and is about
    something the vendor can find (it cites a page)."""
    return bool(question) and not BANNED.search(question) and "page" in question.lower()


def _clean(question: str) -> str:
    text = _flat(question)
    return text[:1].upper() + text[1:] if text else text


# ---- the question -------------------------------------------------------------------------------


def _advocate_spans(finding: Mapping[str, Any]) -> tuple[str, str] | None:
    """Where the devil's advocate saw each dimension's ends, when they differ (`pairing`)."""
    pairing = finding.get("pairing")
    reply = pairing.get("advocate") if isinstance(pairing, Mapping) else None
    if not isinstance(reply, Mapping):
        return None
    vendor, architect = reply.get("vendor_spans"), reply.get("architect_spans")
    if not isinstance(vendor, Mapping) or not isinstance(architect, Mapping) or vendor == architect:
        return None

    def ends(spans: Mapping[str, Any]) -> str:
        start = _SPAN_WORDS.get(str(spans.get("from")), "")
        end = _SPAN_WORDS.get(str(spans.get("to")), "")
        return f"from {start} to {end}" if start and end else ""

    a, v = ends(architect), ends(vendor)
    return (a, v) if a and v else None


def _sum_question(finding: Mapping[str, Any]) -> str | None:
    """The vendor's own string: its parts against its printed overall."""
    page = _page(finding)
    summed = _decimal(finding.get("parts_sum"))
    total = _short(finding.get("printed_total"), 30)
    if summed is None or not total:
        return None
    return (
        f"On page {page} the parts of {_thing(finding.get('item'))} add up to "
        f"{inch_words(summed)}, and the overall prints {total}. Confirm which size is intended."
    )


def _wrong_question(finding: Mapping[str, Any]) -> str:
    """A difference code read but could not prove (no pairing proof, or AI readings only)."""
    page = _page(finding)
    thing = _thing(finding.get("item"))
    vendor = _value(finding, "vendor")
    source = finding.get("source")
    if source == "vendor_arithmetic":
        summed = _sum_question(finding)
        if summed is not None:
            return summed
    reference = _value(finding, "architect")
    where = _on(_arch_page(finding), page)
    if not vendor:
        return f"Print {thing} on page {page}: the architect's drawing{where} gives {reference}."
    if source == "brand_prototype":
        return (
            f"Page {page} shows {vendor} for {thing}; the brand prototype{where} shows "
            f"{reference}. Confirm with the designer which governs."
        )
    if source == "rules":
        return (
            f"Page {page} shows {vendor} for {thing}; GV's standard gives {reference}. Confirm "
            "the size you will build."
        )
    spans = _advocate_spans(finding)
    if spans is not None:
        return (
            f"Page {page} shows {vendor} for {thing} ({spans[1]}); the architect's drawing{where} "
            f"prints {reference} ({spans[0]}). Confirm the size and what it measures."
        )
    which = "size" if _is_size(vendor) and _is_size(reference) else "one"
    return (
        f"Page {page} shows {vendor} for {thing}; the architect's drawing{where} prints "
        f"{reference}. Confirm which {which} is intended."
    )


def _decision_question(finding: Mapping[str, Any]) -> str | None:
    """A "Your decision" item the resolver kept, by its reason (#1567)."""
    page = _page(finding)
    thing = _thing(finding.get("item"))
    reason = str(finding.get("keep_reason") or "other")
    vendor = _value(finding, "vendor")
    architect = _value(finding, "architect")
    arch_page = _arch_page(finding)
    where = _on(arch_page, page)
    if reason == "architect_prints_nothing":
        estimate = _code_estimate(finding)
        about = f" (the architect's drawing scales about {estimate})" if estimate else ""
        lead = _SIZE.match(vendor)
        if _is_size(vendor) and lead is not None:
            size = f"{lead.group(0).strip()} on page {page}"
        else:
            size = f"the size on page {page} ({vendor})"
        return (
            f"The architect prints no size for {thing}{_on(arch_page)}. Confirm {size} fits the "
            f"space on site{about}."
        )
    if reason == "measures_something_else":
        return (
            f"Confirm what {thing} measures: the architect prints {architect}{where}, page {page} "
            f"shows {_short(finding.get('vendor'), 60)}. Confirm both run between the same "
            "points, and the finished size."
        )
    if reason == "which_governs":
        return (
            f"The brand prototype shows {architect}{where}; page {page} shows {vendor} for "
            f"{thing}. Confirm with the designer which governs."
        )
    if reason == "unreadable":
        return f"Value illegible on page {page}: reprint {thing}."
    if reason == "needs_job_value":
        return (
            f"Print the size of {thing} on page {page}: the drawings do not give one to build "
            "or check it from."
        )
    if reason == "vendor_disagrees":
        summed = _sum_question(finding)
        if summed is not None:
            return summed
        return (
            f"Page {page} prints more than one size for {thing} ({vendor}). Confirm which one "
            "is intended."
        )
    if reason == "layout":
        a_counts, v_counts = count_words(finding.get("architect")), count_words(
            finding.get("vendor")
        )
        differ = [
            part for part in a_counts if part in v_counts and a_counts[part] != v_counts[part]
        ] + [part for part in a_counts if part not in v_counts and _missing(finding, part)]
        if differ:
            part = differ[0]
            shows = v_counts.get(part, 0)
            return (
                f"The architect shows {a_counts[part]} {_plural(part, a_counts[part])}{where} for "
                f"{thing}; page {page} shows {shows or 'none'}. Confirm the layout."
            )
        return (
            f"Confirm {thing} on page {page} follows the architect's drawing{where}: the "
            f"architect shows {_short(finding.get('architect'), 60)}; page {page} shows "
            f"{_short(finding.get('vendor'), 60)}."
        )
    if not architect and not vendor:
        return None
    return (
        f"Confirm {thing} on page {page}: page {page} shows {vendor or 'nothing'}; the architect's "
        f"drawing{where} shows {architect or 'nothing'}."
    )


def _missing(finding: Mapping[str, Any], part: str) -> bool:
    """The vendor's words say the part is not drawn ("no lockers drawn")."""
    return bool(
        re.search(
            rf"\bno {part}s?\b|\b{part}s? (?:not|missing)",
            _flat(finding.get("vendor")),
            re.IGNORECASE,
        )
    )


def _plural(part: str, count: int) -> str:
    if count == 1:
        return part
    return {"shelf": "shelves", "cubby": "cubbies"}.get(part, f"{part}s")


def _code_estimate(finding: Mapping[str, Any]) -> str | None:
    """The architect's drawn size, only when code measured it (`measure_on_drawing`), never the
    AI's own scaling."""
    help_ = finding.get("decision_help")
    estimate = help_.get("estimate") if isinstance(help_, Mapping) else None
    if not isinstance(estimate, Mapping):
        return None
    method = str(estimate.get("method") or "").lower()
    if "measure_on_drawing" not in method and "measured by code" not in method:
        return None
    value = _decimal(estimate.get("value_in"))
    return None if value is None else inch_words(value)


def _helped(finding: Mapping[str, Any]) -> str | None:
    """The finding's own confirm line (#1545), when it already reads as a clean question to the
    vendor with a page."""
    help_ = finding.get("decision_help")
    confirm = _flat(help_.get("confirm")) if isinstance(help_, Mapping) else ""
    if not confirm or not confirm.endswith("?") or len(confirm) > 220:
        return None
    if re.search(r"\b(?:reviewer|GV|you decide|your decision)\b", confirm, re.IGNORECASE):
        return None
    return confirm if writes_well(confirm) else None


def has_question(finding: Mapping[str, Any]) -> bool:
    """Whether the item has anything to ask the vendor."""
    return question_of(finding) is not None


def question_of(finding: Mapping[str, Any]) -> str | None:
    """The one question to the vendor for an undecided item (without `PREFIX`), or `None` when
    there is nothing to ask (see the module). Every question passes `writes_well`; one that would
    not (a word from the finding's own text) falls back to the plain form."""
    if str(finding.get("source") or "") in _NOTHING_TO_ASK:
        return None
    if not _flat(finding.get("vendor")) and not _flat(finding.get("architect")):
        return None
    helped = _helped(finding)
    if helped is not None:
        return _clean(helped)
    status = finding.get("status")
    tags = finding.get("missing_tags")
    asked: str | None
    if isinstance(tags, list) and tags and finding.get("keep_reason") != "layout":
        # Code read both drawings' tags (the escalation's notes step): name the ones left out.
        named = ", ".join(str(tag) for tag in tags[:4])
        asked = (
            f"The architect tags {named}{_on(_arch_page(finding), _page(finding))} for "
            f"{_thing(finding.get('item'))}; the vendor's drawing on page {_page(finding)} does "
            f"not print {'it' if len(tags) == 1 else 'them'}. Confirm the material and finish."
        )
    elif status == "different":
        asked = _wrong_question(finding)
    else:
        asked = _decision_question(finding)
        if asked is not None and isinstance(tags, list) and tags:
            # A layout with tags too: the tags code found left out, after the layout question.
            asked += (
                f" The architect's tag{'s' if len(tags) > 1 else ''} "
                f"{', '.join(str(tag) for tag in tags[:4])} {'are' if len(tags) > 1 else 'is'} "
                f"not printed on page {_page(finding)}."
            )
    if asked is None:
        return None
    asked = _clean(asked)
    if writes_well(asked):
        return asked
    page = _page(finding)
    plain = _clean(
        f"Confirm {_thing(finding.get('item'))} on page {page}: print the size you will build."
    )
    return plain if writes_well(plain) else f"Confirm the item on page {page}."
