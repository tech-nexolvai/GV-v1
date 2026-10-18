"""The one guard on words that leave GV for the vendor (#1622): no internal phrase ever reaches the
comment letter, the issue register, the cloud drawing's notes or the summary.

GV Review's own working words are for the reviewer: "see GV Review's record", "recorded check",
record ids (`air:<id>`, `ai-review:p3:…`), field names (`rule_check_id`), "AI", the lead and its
helpers, "check it before sending". `vendor_text` cleans what can be cleaned (a record id inside an
item's name) and refuses the rest (`None`, or the caller's fallback), so an internal phrase can never
be printed by accident.

`rule_requirement` turns a GV rule check's own comparison (`verdict/operations`: `|38.25 - 40.5| =
2.25 > 0 in`, `30 < 34 in`, …) into the plain requirement with its computed value ("40 1/2"", "at
least 34"", "between 1" and 2""), or `None` when the rule states none: then the item is not a
numbered comment to the vendor (`proof.stated_requirement`).

Pure code: no I/O, no AI call.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Final

__all__ = ["inches_first", "internal", "item_name", "rule_requirement", "vendor_text"]

#: Internal phrases, in any case.
_PHRASES: Final = re.compile(
    r"GV Review'?s record|recorded (?:check|difference|result)s?|rule_check_id|record_id|"
    r"\bai[-_ ]review\b|\bhelpers?\b|\bthe lead\b|\blead (?:model|checks?|checked|verified)\b|"
    r"devil'?s advocate|check (?:it |them |these )?before sending|AI reading|\bwitness(?:es)?\b",
    re.IGNORECASE,
)
#: "AI" as a word, as written (so "air" or a file name like "AI_Set" never trips it).
_AI: Final = re.compile(r"(?<![\w-])AI(?![\w-])")
#: Record ids that may sit inside an item's name; removed, the rest kept.
_RECORD_ID: Final = re.compile(
    r"\b(?:air|ai-review):[\w:.-]+|\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_SEE_RECORD: Final = re.compile(r"\(?\s*see GV Review'?s record\s*\)?", re.IGNORECASE)
#: A check's id in the run's records ("rule check C18"): the words stay, the id goes.
_CHECK_ID: Final = re.compile(r"\b(check)\s+[CF]\d+\b", re.IGNORECASE)


def internal(text: object) -> bool:
    """Whether the words hold an internal phrase or id."""
    words = str(text or "")
    return bool(
        _PHRASES.search(words)
        or _AI.search(words)
        or _RECORD_ID.search(words)
        or _CHECK_ID.search(words)
    )


#: A metric drawing's value with its inches in brackets, as printed: `1219 [48]`, `2007 [79 1/32]`.
_MM_INCHES: Final = re.compile(
    r"(?<![\w./\[])(\d{2,5}(?:\.\d+)?)\s*\[\s*(\d+(?:\s+\d+/\d+)?|\d+/\d+)\s*\](?![\"\w])"
)


def inches_first(text: object) -> str:
    """Every millimetre value printed with its inches in brackets, written inches first as the
    vendor's reviewer reads them: `1219 [48]` -> `48" (1219 mm)` (#1634, letter and register)."""
    return _MM_INCHES.sub(lambda m: f'{m.group(2)}" ({m.group(1)} mm)', str(text or ""))


def vendor_text(text: object, fallback: str | None = None) -> str | None:
    """The words as the vendor may read them: record ids and "see GV Review's record" taken out,
    millimetres written after the inches (`inches_first`), spaces tidied; `fallback` when what is
    left is empty or still internal."""
    words = " ".join(inches_first(text).split())
    if not internal(words):
        return words or fallback
    cleaned = _CHECK_ID.sub(r"\1", _SEE_RECORD.sub(" ", _RECORD_ID.sub(" ", words)))
    cleaned = " ".join(cleaned.split()).strip(" :;,·-")
    if cleaned and not internal(cleaned):
        return cleaned
    return fallback


#: An item named only by a record id (`air:<uuid>`, `ai-review:p3:...`, a bare uuid).
_ID_ONLY: Final = re.compile(
    r"^(?:(?:air|ai-review)(?::[\w.-]+)+|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$",
    re.IGNORECASE,
)
#: Code's own sentences appended to a reason (a pairing's proof): never an item's name.
_CODE_SENTENCE: Final = re.compile(r"^(?:Code |The architect prints|Both print|Vendor's parts)")
_NAME_CHARS: Final = 90


def _first_clause(text: object) -> str | None:
    """A reason's first sentence (cut at `.`, `;` or `:` after a few words), capitalised."""
    words = " ".join(str(text or "").split())
    if not words or _CODE_SENTENCE.match(words):
        return None
    cut = re.split(r"[.;](?=\s|$)", words, maxsplit=1)[0].strip(" ,:-")
    if not cut:
        return None
    cut = cut[:1].upper() + cut[1:]
    return cut if len(cut) <= _NAME_CHARS else cut[: _NAME_CHARS - 1].rstrip() + "\u2026"


def item_name(finding: object) -> str | None:
    """A finding's name as the vendor reads it, never a record id: its item; else the first
    clause of its reason ("Shelf depth, front to back"); else the vendor's printed dimension code
    read ("Dimension 48\" (1219 mm)"). `None` when there are no words to name it by."""
    if not isinstance(finding, dict):
        return None
    item = " ".join(str(finding.get("item") or "").split())
    if item and not _ID_ONLY.match(item):
        named = vendor_text(item)
        if named:
            return named
    why = _first_clause(finding.get("why"))
    if why:
        named = vendor_text(why)
        if named:
            return named
    dims = finding.get("dims")
    label = dims.get("vendor") if isinstance(dims, dict) else None
    if isinstance(label, dict) and label.get("text"):
        return vendor_text(f"Dimension {label['text']}")
    return None


def inch_words(value: Decimal) -> str:
    """Inches as a reviewer writes them: `80 1/8"`, `3/4"`, `96"` (to the nearest 1/16; the same
    words as `chat_trust.inch_words`, kept here so this guard imports nothing of the chat)."""
    sixteenths = int((abs(value) * 16).to_integral_value())
    whole, rest = divmod(sixteenths, 16)
    if not rest:
        return f'{whole}"'
    numerator, denominator = rest, 16
    while numerator % 2 == 0:
        numerator //= 2
        denominator //= 2
    return f'{whole} {numerator}/{denominator}"' if whole else f'{numerator}/{denominator}"'


def _number(text: str) -> Decimal | None:
    try:
        return Decimal(Fraction(text).numerator) / Decimal(Fraction(text).denominator)
    except (ValueError, ZeroDivisionError, InvalidOperation):
        return None


_NUM: Final = r"(-?\d+(?:\.\d+)?(?:/\d+)?)"
_ABS: Final = re.compile(rf"^\|\s*{_NUM}\s*-\s*{_NUM}\s*\|\s*=\s*{_NUM}\s*>\s*{_NUM}\s*in\b")
_MIN: Final = re.compile(rf"^{_NUM}\s*<\s*{_NUM}\s*in\b")
_MAX: Final = re.compile(rf"^{_NUM}\s*>\s*{_NUM}\s*in\b")
_BETWEEN: Final = re.compile(rf"^{_NUM}\s*<=\s*{_NUM}\s*<=\s*{_NUM}\s*in\s+is\s+False\b")


def rule_requirement(comparison: object) -> str | None:
    """A failed GV rule check's requirement, in plain words with its computed value: `40 1/2"`
    (± its tolerance when there is one), `at least 34"`, `at most 2"`, `between 1" and 2"`;
    `None` when the comparison states no requirement in inches."""
    text = " ".join(str(comparison or "").split())
    found = _ABS.match(text)
    if found:
        expected, tolerance = _number(found.group(2)), _number(found.group(4))
        if expected is None:
            return None
        words = inch_words(expected)
        if tolerance:
            words += f" (± {inch_words(tolerance)})"
        return words
    found = _MIN.match(text)
    if found and (bound := _number(found.group(2))) is not None:
        return f"at least {inch_words(bound)}"
    found = _MAX.match(text)
    if found and (bound := _number(found.group(2))) is not None:
        return f"at most {inch_words(bound)}"
    found = _BETWEEN.match(text)
    if found:
        low, high = _number(found.group(1)), _number(found.group(3))
        if low is not None and high is not None:
            return f"between {inch_words(low)} and {inch_words(high)}"
    return None
