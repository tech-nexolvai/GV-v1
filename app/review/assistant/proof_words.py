"""One plain sentence per red mark (#1804): the proof card, the comment letter and the issue
register all say the same thing in the same words.

The owner (2026-10-20): "the PDF and the draft must PROVE every mark properly and make it very
clear, simple and visible, explainable in the output". So every clouded item gets, made here once:

- **the sentence**: `Vendor shows 98" for the overall width of the run; the architect shows
  97 1/2". Change it to 97 1/2" (1/2" over).` (`sentence`, and `sentence_runs` for the coloured
  print: the vendor's value red, the should-be green);
- **the sum written out** when arithmetic is the proof: `2" + 86 1/2" + 2" = 90 1/2", not the
  88 1/2" printed` (`sum_line`), or the rule's own working (`worked_line`);
- **the reference**: "Architect drawing, page 28", "GV rule", "Reference: brand prototype (the
  architect gives no elevation of it)", "The vendor's own parts", "Decided by GV's reviewer: <note>"
  (`reference_words`);
- **how it was checked**, in the two words the vendor may read: "Proved by code: <plain witness
  words>" or "Decided by GV's reviewer" (`checked_words`). Internal trust levels, model names, the
  check's machinery ("blind pick", "advocate", "lead") and the reviewer's login never appear; every
  line goes through the one guard (`vendor_text`).

The reviewer's decision (#1803) is the finding's `decision`: `decision` (`agree` /
`not_a_problem`), `note`, `should_be` (with an agree only: the value the reviewer gave), `by`, `at`.
An agreed item's green value is the reviewer's `should_be` when given (`proof.should_be_value`).

Pure code: no PDF, no I/O, no AI call.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Any, Final, Literal

from app.review.assistant import classification, same_item_pages
from app.review.assistant import proof as rule
from app.review.assistant.finding_checks import wrong_part
from app.review.assistant.vendor_text import inch_words, item_name, vendor_text

__all__ = [
    "REVIEWER_WORDS",
    "Role",
    "Run",
    "checked_words",
    "difference_words",
    "either_string",
    "inches_of",
    "missing",
    "reference_kind",
    "reference_words",
    "reviewer_agreed",
    "sentence",
    "sentence_runs",
    "should_text",
    "sum_line",
    "worked_line",
]

type Role = Literal["plain", "vendor", "should"]
type Run = tuple[str, Role]
"""One piece of the sentence: its words and how it is printed (plain, the vendor's red value, the
green should-be)."""

#: How the vendor's files name a decision of GV's reviewer (never the login, #1803).
REVIEWER_WORDS: Final = "Decided by GV's reviewer"
_PROVED: Final = "Proved by code"
#: Each code witness in the vendor's words (`proof._PROOF_WORDS` in plain language).
_WITNESS_WORDS: Final[Mapping[str, str]] = {
    "printed": "both numbers are printed on the drawings and were read from the drawings' text",
    "ocr_ink": "both numbers were read from the drawn lines by two separate readers",
    "rule": "GV's rule check fails it",
    "arithmetic": "the vendor's own parts were added up",
    "ledger": "the vendor's other views of it agree",
    "units": "the millimetres and inches printed together agree",
    "text": "the finish codes were read from the drawings' own text, on every vendor sheet of "
    "the area",
}
_PAIRED_BY_CODE: Final = "both dimensions run between the same two end points"
_PAIRED_BY_CHECK: Final = "a second, separate check matched the same two dimensions and both ends"
#: A vendor value that says the item is not drawn at all.
_MISSING: Final = re.compile(
    r"\bnot (?:on|shown|drawn|found|in)\b|\bmissing\b|\bleft out\b|\bomitted\b|^none$",
    re.IGNORECASE,
)
#: Working words GV keeps to itself inside a rule's explanation: "(rules.md, ...)".
_WORKING_NOTE: Final = re.compile(r"\s*\((?:[^()]*\brules\.md\b[^()]*)\)")
_RANGE: Final = re.compile(
    r"^(?:between\s+)?(?P<low>[\d\s/'\"-]+?\")\s*(?:to|and|-)\s*(?P<high>[\d\s/'\"-]+?\")$",
    re.IGNORECASE,
)
_BOUND: Final = re.compile(r"^at (?P<side>least|most)\s+(?P<value>.+)$", re.IGNORECASE)
_FEET_INCHES: Final = re.compile(
    r"^\s*(?:(?P<feet>\d+)\s*'\s*-?\s*)?(?P<whole>\d+(?:\.\d+)?)?\s*(?P<frac>\d+/\d+)?\s*\"?\s*$"
)


# ---- numbers ------------------------------------------------------------------------------------


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def inches_of(text: object) -> Decimal | None:
    """A printed size in inches: `97 1/2"`, `98"`, `3'-1"`, `1' - 2"`, `3/4"`, `37`; else `None`
    (a sentence, a range, a code)."""
    words = " ".join(str(text or "").split()).replace("″", '"').replace("′", "'")
    found = _FEET_INCHES.match(words)
    if not words or found is None or not any(found.group(k) for k in ("feet", "whole", "frac")):
        return None
    total = Fraction(0)
    if found.group("feet"):
        total += 12 * int(found.group("feet"))
    if found.group("whole"):
        total += Fraction(found.group("whole"))
    if found.group("frac"):
        numerator, denominator = (int(part) for part in found.group("frac").split("/"))
        if denominator == 0:
            return None
        total += Fraction(numerator, denominator)
    return Decimal(total.numerator) / Decimal(total.denominator)


def _vendor_inches(finding: Mapping[str, Any]) -> Decimal | None:
    part = wrong_part(finding)
    if part is not None:
        return inches_of(part[0])
    if finding.get("source") == "vendor_arithmetic" and finding.get("printed_total"):
        return inches_of(finding.get("printed_total"))
    found = _decimal((finding.get("inches") or {}).get("vendor"))
    return found if found is not None else inches_of(rule.vendor_value(finding))


def _should_inches(finding: Mapping[str, Any]) -> Decimal | None:
    given = _reviewer_value(finding)
    if given is not None:
        return inches_of(given)
    part = wrong_part(finding)
    if part is not None:
        return part[1]
    if finding.get("source") == "vendor_arithmetic":
        summed = _decimal(finding.get("parts_sum"))
        if summed is not None:
            return summed
    found = _decimal((finding.get("inches") or {}).get("architect"))
    return found if found is not None else inches_of(rule.should_be_value(finding))


def _range(text: str) -> tuple[Decimal | None, Decimal | None] | None:
    """A requirement as its low and high bounds (`1" to 2"`, `between 1" and 2"`, `at least 34"`),
    else `None`."""
    words = " ".join(text.split())
    found = _RANGE.match(words)
    if found:
        low, high = inches_of(found.group("low")), inches_of(found.group("high"))
        if low is not None and high is not None:
            return low, high
    found = _BOUND.match(words)
    if found:
        value = inches_of(found.group("value"))
        if value is not None:
            return (value, None) if found.group("side").lower() == "least" else (None, value)
    return None


def difference_words(finding: Mapping[str, Any]) -> str | None:
    """By how much the vendor's value is off: `1/2" over`, `2" short` (`3" over` a rule's upper
    bound); `None` when either side is not one size, or when two of the vendor's pages disagree
    and which one is right is not known (`same_item_pages`)."""
    if reference_kind(finding) == "pages":
        return None
    vendor = _vendor_inches(finding)
    if vendor is None:
        return None
    should = _should_inches(finding)
    if should is None:
        bounds = _range(should_text(finding))
        if bounds is None:
            return None
        low, high = bounds
        if high is not None and vendor > high:
            return f"{inch_words(vendor - high)} over"
        if low is not None and vendor < low:
            return f"{inch_words(low - vendor)} short"
        return None
    if vendor == should:
        return None
    gap = inch_words(abs(vendor - should))
    return f"{gap} over" if vendor > should else f"{gap} short"


# ---- who said what ------------------------------------------------------------------------------


def _decision(finding: Mapping[str, Any]) -> Mapping[str, Any]:
    decision = finding.get("decision")
    return decision if isinstance(decision, Mapping) else {}


def reviewer_agreed(finding: Mapping[str, Any]) -> bool:
    """GV's reviewer said it is a mistake ("It's a mistake: add a red mark", #1803)."""
    return _decision(finding).get("decision") == "agree"


def _reviewer_value(finding: Mapping[str, Any]) -> str | None:
    if not reviewer_agreed(finding):
        return None
    given = " ".join(str(_decision(finding).get("should_be") or "").split())
    return vendor_text(given) if given else None


def _reviewer_note(finding: Mapping[str, Any]) -> str | None:
    note = " ".join(str(_decision(finding).get("note") or "").split())
    return vendor_text(note) if note else None


type Kind = Literal["reviewer", "prototype", "rule", "sum", "span", "pages", "architect"]


def reference_kind(finding: Mapping[str, Any]) -> Kind:
    """What the should-be comes from: GV's reviewer (an agreed item with their own value, or no
    code proof), the brand prototype, GV's rule, the vendor's own parts or other span, else the
    architect."""
    if _reviewer_value(finding) is not None and rule.governs(finding) == rule.REVIEWER:
        # Only a value code does not know is the reviewer's (#1803): the architect's value kept
        # as it was pre-filled stays the architect's, decided by the reviewer (`checked_words`).
        return "reviewer"
    own = rule.vendor_own(finding)
    if own == "sum":
        return "sum"
    if own == "span":
        return "span"
    if own == "pages":
        return "pages"
    if finding.get("source") == "rules" or rule.gv_limits(finding) is not None:
        return "rule"
    if finding.get("source") == "brand_prototype":
        return "prototype"
    return "architect"


def _where(finding: Mapping[str, Any]) -> str:
    """Where the reference is printed: "page 28", "arch.pdf page 1", "on the same sheet", ""."""
    box = finding.get("arch_box")
    page = box.get("page") if isinstance(box, Mapping) and box.get("page") else None
    page = page or finding.get("vs_page")
    separate = rule.architect_file(finding)
    if separate is not None and page:
        return f"{separate} page {page}"
    if page and page != finding.get("page"):
        return f"page {page}"
    if isinstance(box, Mapping):
        return "on the same sheet"
    return ""


def reference_words(finding: Mapping[str, Any], *, note: bool = True) -> str:
    """The reference in one line: "Architect drawing, page 28", "Reference: brand prototype (the
    architect gives no elevation of it), page 40", "GV rule", "The vendor's own parts", "Decided by
    GV's reviewer: <their note>"."""
    kind = reference_kind(finding)
    where = _where(finding)
    if kind == "reviewer":
        said = _reviewer_note(finding) if note else None
        return f"{REVIEWER_WORDS}: {said}" if said else REVIEWER_WORDS
    if kind == "sum":
        return "The vendor's own parts (their sum)"
    if kind == "span":
        return "The vendor's other dimension of the same span" + (f", {where}" if where else "")
    if kind == "pages":
        return "The vendor's own other drawing of the same item" + (f", {where}" if where else "")
    if kind == "rule":
        return "GV filler rule" if rule.gv_limits(finding) is not None else "GV rule"
    if kind == "prototype":
        head = (
            "Reference: brand prototype (the architect gives no elevation of it)"
            if classification.prototype_reference(finding)
            else "Brand prototype"
        )
        return f"{head}, {where}" if where and where != "on the same sheet" else head
    if where == "on the same sheet":
        return "Architect drawing, on the same sheet"
    return f"Architect drawing, {where}" if where else "Architect drawing"


def _who(finding: Mapping[str, Any]) -> str:
    """The sentence's reference: "the architect shows", "GV's rule requires", …"""
    return {
        "reviewer": "GV's reviewer requires",
        "prototype": "the brand prototype shows",
        "rule": (
            "GV's filler rule requires"
            if rule.gv_limits(finding) is not None
            else "GV's rule requires"
        ),
        "span": "the vendor's other dimension of the same span shows",
        "pages": "the vendor's other drawing of the same item shows",
        "sum": "its own parts add up to",
        "architect": "the architect shows",
    }[reference_kind(finding)]


# ---- the sentence -------------------------------------------------------------------------------


def _item_phrase(finding: Mapping[str, Any]) -> str:
    """ " for the overall width of the run", or "" when the item's name does not read in a
    sentence (long, or a heading with a colon): the card's title names it then."""
    name = item_name(dict(finding)) or ""
    if not name or len(name) > 60 or ":" in name:
        return ""
    if len(name) > 1 and name[0].isupper() and name[1].islower():
        name = name[0].lower() + name[1:]
    if not re.match(r"(?:the|a|an|each|every|both)\b", name, re.IGNORECASE):
        name = f"the {name}"
    return f" for {name}"


def should_text(finding: Mapping[str, Any]) -> str:
    """The green should-be value, as the vendor may read it (the reviewer's value first). A vendor
    string whose drawing does not name the wrong number names both ways to fix it
    (`either_string`): `40 5/8" overall, or parts adding up to 40 1/2"`."""
    # `should_be_value` holds the reviewer's value (#1803), as code writes it when it is the same.
    value = vendor_text(rule.should_be_value(finding), "") or ""
    if value and either_string(finding):
        printed = vendor_text(rule.vendor_value(finding), "") or ""
        if printed:
            return f"{value} overall, or parts adding up to {printed}"
    return value


def either_string(finding: Mapping[str, Any]) -> bool:
    """A vendor string that does not add up where nothing says which number is wrong: no part the
    drawing names (`wrong_part`) and no value GV's reviewer gave. The vendor may correct the parts
    or the overall; code never guesses which (GV's rule: exact match, flag everything, so
    even a 1/8" miss is red, 2026-10-23)."""
    return (
        rule.vendor_own(finding) == "sum"
        and wrong_part(finding) is None
        and _reviewer_value(finding) is None
    )


def missing(vendor: str) -> bool:
    """The vendor's value says the item is not on its sheets at all."""
    return not vendor or bool(_MISSING.search(vendor))


def _printed_too(finding: Mapping[str, Any], value: str) -> str:
    """The architect's value as printed when it reads differently from the should-be ("1' - 2"" for
    14"), else ""."""
    if reference_kind(finding) not in ("architect", "prototype", "span"):
        return ""
    printed = vendor_text(" ".join(str(finding.get("architect") or "").split()), "") or ""
    if (
        not printed
        or printed == value
        or len(printed) > 24
        or not any(c.isdigit() for c in printed)
    ):
        return ""
    return printed if inches_of(printed) is not None else ""


def _action(should: str) -> str:
    bounds = _range(should)
    if bounds is None:
        return f"Change it to {should}"
    low, high = bounds
    if low is not None and high is not None:
        return f"Change it so it is between {inch_words(low)} and {inch_words(high)}"
    return f"Change it so it is {should}"


def _sum_gap(gap: str) -> str:
    """ "2" short" (the printed total against its parts) as "2" more than the total"."""
    amount, _space, side = gap.rpartition(" ")
    return f"{amount} {'more' if side == 'short' else 'less'} than the total"


def sentence_runs(finding: Mapping[str, Any]) -> list[Run]:
    """The item's one sentence in pieces: the vendor's value (`vendor`), the should-be (`should`)
    and the words round them (`plain`)."""
    vendor = vendor_text(rule.vendor_value(finding), "") or ""
    should = should_text(finding)
    gap = difference_words(finding)
    item = _item_phrase(finding)
    kind = reference_kind(finding)
    if kind == "sum" and wrong_part(finding) is not None:
        # The drawing names the wrong part (#1887): the other parts and the overall are right.
        overall = vendor_text(str(finding.get("printed_total") or ""), "") or ""
        where = f" of{item.removeprefix(' for')}" if item else ""
        return [
            ("Vendor prints ", "plain"),
            (vendor, "vendor"),
            (
                (
                    f" as one part{where}, but the parts must add up to the {overall} overall and "
                    "the drawing shows this part is the wrong number. Change it to "
                ),
                "plain",
            ),
            (should, "should"),
            (f" ({gap})." if gap else ".", "plain"),
        ]
    if kind == "sum":
        # The parts' sum (the green value alone); the drawing does not say which number is wrong,
        # so the sentence names both ways to correct it.
        should = vendor_text(rule.should_be_value(finding), "") or ""
        runs: list[Run] = [
            ("Vendor prints ", "plain"),
            (vendor, "vendor"),
            (f"{item or ' for the total'}, but its own parts add up to ", "plain"),
            (should, "should"),
            (f" ({_sum_gap(gap)})." if gap else ".", "plain"),
            (" Correct the total to ", "plain"),
            (should, "should"),
            (", or the parts so they add up to ", "plain"),
            (vendor, "vendor"),
            (".", "plain"),
        ]
        return runs
    if kind == "pages":
        # Two of the vendor's pages print the same item in two sizes: code never picks one.
        pages = same_item_pages.pages_of(finding)
        if len(pages) == 2:
            (first_page, first), (second_page, second) = pages
            return [
                ("Vendor prints ", "plain"),
                (second, "vendor"),
                (f"{item} on page {second_page} but ", "plain"),
                (first, "should"),
                (
                    (
                        f" on page {first_page}: the same item must be the same size on every "
                        "drawing. Correct one of them so both pages agree."
                    ),
                    "plain",
                ),
            ]
    if missing(vendor):
        return [
            ("The vendor's sheets do not show it; ", "plain"),
            (_who(finding), "plain"),
            (" ", "plain"),
            (should, "should"),
            (f". Add {should}.", "plain"),
        ]
    printed = _printed_too(finding, should)
    runs = [
        ("Vendor shows ", "plain"),
        (vendor, "vendor"),
        (f"{item}; {_who(finding)} ", "plain"),
        (should, "should"),
        (f" (printed {printed})" if printed else "", "plain"),
        (f". {_action(should)}", "plain"),
        (f" ({gap}).", "plain") if gap else (".", "plain"),
    ]
    return [(words, role) for words, role in runs if words]


def sentence(finding: Mapping[str, Any]) -> str:
    """The item's one sentence, as the letter, the register and the proof card print it."""
    words = "".join(text for text, _role in sentence_runs(finding))
    return vendor_text(words, "") or ""


def sum_line(finding: Mapping[str, Any]) -> str | None:
    """A sum the proof rests on, written out: `2" + 86 1/2" + 2" = 90 1/2", not the 88 1/2"
    printed`; `None` when the proof is not the vendor's own sum."""
    if reference_kind(finding) != "sum":
        return None
    parts = [vendor_text(str(part), "") or "" for part in finding.get("parts") or []]
    parts = [part for part in parts if part]
    wrong = wrong_part(finding)
    if wrong is not None:
        # The overall less the other parts: `17 1/4" - 2 5/8" - 3" = 11 5/8", not the 14" printed`.
        part = vendor_text(wrong[0], "") or ""
        overall = vendor_text(str(finding.get("printed_total") or ""), "") or ""
        if not part or part not in parts or not overall:
            return None
        rest = list(parts)
        rest.remove(part)
        return f"{' - '.join([overall, *rest])} = {inch_words(wrong[1])}, not the {part} printed"
    total = vendor_text(rule.should_be_value(finding), "") or ""
    printed = vendor_text(rule.vendor_value(finding), "") or ""
    if not parts or not total:
        return None
    return f"{' + '.join(parts)} = {total}, not the {printed} printed"


def worked_line(finding: Mapping[str, Any]) -> str | None:
    """A rule's own working when GV's rule settled the item (the filler limits, the cabinet
    run fit): `door cabinet 1 should be 15" = (75" - 3" - 24" - 3") / 3`; GV's file names left
    out. `None` otherwise."""
    settled = finding.get("settled")
    if not isinstance(settled, Mapping) or not settled.get("how"):
        return None
    how = _WORKING_NOTE.sub("", " ".join(str(settled["how"]).split())).strip()
    how = vendor_text(how, "") or ""
    return how.rstrip(".") or None


def checked_words(finding: Mapping[str, Any]) -> str:
    """How the item was checked, in the vendor's two words: "Decided by GV's reviewer" (with
    nothing code proved), else "Proved by code: <plain words of each code witness>"."""
    if reference_kind(finding) == "reviewer":
        # The reference line names the reviewer and their note; this says no drawing proves it.
        return REVIEWER_WORDS
    if reviewer_agreed(finding) and not rule.code_proved(finding):
        note = _reviewer_note(finding)
        decided = f"{REVIEWER_WORDS}: {note}" if note else REVIEWER_WORDS
        return vendor_text(decided, REVIEWER_WORDS) or REVIEWER_WORDS
    parts: list[str] = []
    witnesses = rule.witnesses_of(finding)
    for name in witnesses:
        if name in _WITNESS_WORDS:
            parts.append(_WITNESS_WORDS[name])
    if rule.needs_pairing(finding) and rule.pairing_proved(finding):
        verdict = finding.get("critic")
        answers = verdict.get("answers") if isinstance(verdict, Mapping) else None
        by_code = finding.get("paired_by") == "code" or (
            isinstance(answers, Mapping) and answers.get("pairing") == "code"
        )
        parts.append(_PAIRED_BY_CODE if by_code else _PAIRED_BY_CHECK)
    if rule.string_verified(finding) and reference_kind(finding) == "sum":
        parts.append("the parts touch end to end and end on the total's own lines")
    if classification.prototype_reference(finding):
        parts.append(
            "the architect gives no elevation of it, so the brand prototype is the reference"
        )
    if reviewer_agreed(finding):
        parts.append("GV's reviewer agrees")
    words = "; ".join(dict.fromkeys(parts))
    line = f"{_PROVED}: {words}" if words else _PROVED
    return vendor_text(line, _PROVED) or _PROVED
