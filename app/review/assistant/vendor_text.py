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

__all__ = ["internal", "rule_requirement", "vendor_text"]

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


def internal(text: object) -> bool:
    """Whether the words hold an internal phrase or id."""
    words = str(text or "")
    return bool(_PHRASES.search(words) or _AI.search(words) or _RECORD_ID.search(words))


def vendor_text(text: object, fallback: str | None = None) -> str | None:
    """The words as the vendor may read them: record ids and "see GV Review's record" taken out,
    spaces tidied; `fallback` when what is left is empty or still internal."""
    words = " ".join(str(text or "").split())
    if not internal(words):
        return words or fallback
    cleaned = _SEE_RECORD.sub(" ", _RECORD_ID.sub(" ", words))
    cleaned = " ".join(cleaned.split()).strip(" :;,·-")
    if cleaned and not internal(cleaned):
        return cleaned
    return fallback


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
