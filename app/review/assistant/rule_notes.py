"""A size the architect specifies that breaks a GV rule, printed the same by the vendor (#1839).

The owner's decision (2026-10-21): such an item is a **decided outcome**, not "Not compared". Code
read both values at their spots, the vendor prints the architect's own value, and the architect's
value breaks a GV rule in force (GV's filler range, GV's sink front offset, any GV rule with an
architect side the vendor follows). The architect governs, so the vendor's drawing is never red and
there is no "should be"; what is left is GV's own question to the architect. The item goes in its
own group, **"Breaks a GV rule (architect's size): ask the architect"** (`proof.proof_group`
"rule_note"), carrying the rule (its name, the settings in force and where they come from) and a
ready question for the architect:

    Architect: the filler width is specified at 5/8" (Pantry); GV's standard is 1"-2".
    Please confirm the size.

**What makes one** (`rule_note_of`; anything unsure stays Not compared):

- the item is a GV rule's note on the architect's size (`keep_reason` "architect_outside_gv_rule",
  status `check`): `filler_range` (the architect sizes the filler outside GV's range) or
  `sink_offset` (the architect prints another front offset than GV's standard);
- code read both values from the drawings' own text at their spots (`dims.vendor` and
  `dims.architect`, route `text`), and they are the same size (a sixteenth: the drawings' step);
- the rule's own record says code proved it and the vendor follows the architect (the check's
  `gv_standard`: `proved`, `follows_architect`), or, on an item stored before #1839 that has no
  such record, its own words name the rule and its settings ("outside GV's 1"-2" filler range",
  "architect 5" vs GV standard 4" (sink front offset)").

Pure code: no I/O, no AI call. `classification` classifies such an item as `rule_note`; `proof`
groups it; the screen, the summary, the cover, the letter and the register all read it from there.

Verification: `tests/review/assistant/test_rule_notes.py` (made-up values).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Any, Final

__all__ = [
    "COUNT_WORDS",
    "GROUP",
    "GROUP_WORDS",
    "HEADING",
    "KEEP_REASON",
    "SHORT_WORDS",
    "RuleNote",
    "origin_of",
    "rule_note_of",
]

#: The proof group's name.
GROUP: Final = "rule_note"
#: The group as every screen and file names it (the owner's words).
GROUP_WORDS: Final = "Breaks a GV rule (architect's size): ask the architect"
#: Short, for a status cell or a tile.
SHORT_WORDS: Final = "Ask the architect (GV rule)"
#: After a count in a summary line: "2 break a GV rule (ask the architect)".
COUNT_WORDS: Final = "break a GV rule (ask the architect)"
#: The heading of their section in the letter, the register, the report and the notes pages.
HEADING: Final = "Questions for the architect (GV rules)"
#: The keep reason the GV rule checks give such an item (`filler_range`, `sink_offset`).
KEEP_REASON: Final = "architect_outside_gv_rule"
#: Two printed values closer than this are the same (a sixteenth: the drawings' own step).
_TOLERANCE: Final = Fraction(1, 16)
#: Where the settings come from, as the question says it.
_ORIGIN_WORDS: Final[Mapping[str, str]] = {
    "GV standard": "GV's standard",
    "project value": "this project's GV value",
    "drawing set value": "GV's value for this drawing set",
}
#: The words of an item stored before #1839 (no `gv_standard` record): the filler range check's
#: and the sink front offset check's own notes.
_FILLER_WORDS: Final = re.compile(r"outside GV's (?P<value>\S+?) filler range")
_SINK_WORDS: Final = re.compile(
    r"\bvs (?P<origin>GV standard|project value|drawing set value) (?P<value>\S+) "
    r"\((?P<name>[^)]+)\)"
)


@dataclass(frozen=True, slots=True)
class RuleNote:
    """The GV rule an architect's size breaks, and GV's question to the architect."""

    name: str
    """The rule's subject: "filler width", "sink front offset"."""
    settings: str
    """The settings in force as printed: `1"-2"`, `4"`."""
    origin: str
    """"GV standard", "project value" or "drawing set value"."""
    architect: str
    """The architect's size, which the vendor prints too: `5/8"`."""
    rule_id: str | None = None
    """The rulebook's id when the check names one (CT-SINK-OFFSET-FRONT-001)."""
    place: str | None = None
    """Where, for the question: the area and the architect's page ("Pantry, architect's page 1")."""

    @property
    def standard(self) -> str:
        """ "GV's standard is 1"-2"" (or the project's value)."""
        return f"{_ORIGIN_WORDS.get(self.origin, self.origin)} is {self.settings}"

    @property
    def why(self) -> str:
        """One plain line: what the architect specifies, the rule, and that the vendor follows."""
        return (
            f"the architect specifies {self.architect} for the {self.name}; {self.standard}; the "
            "vendor prints the architect's size, so nothing is asked of the vendor"
        )

    @property
    def question(self) -> str:
        """The ready question for the architect (an RFI line)."""
        where = f" ({self.place})" if self.place else ""
        return (
            f"Architect: the {self.name} is specified at {self.architect}{where}; "
            f"{self.standard}. Please confirm the size."
        )

    def as_dict(self) -> dict[str, Any]:
        """The proof's `rule_note` block (`chat_contract.ChatFindingRuleNoteOut`)."""
        return {
            "name": self.name,
            "rule_id": self.rule_id,
            "settings": self.settings,
            "origin": self.origin,
            "architect": self.architect,
            "standard": self.standard,
            "question": self.question,
        }


def origin_of(where: object) -> str:
    """`rules.md`'s "Where it comes from" words in short: the project's value or sink card, a value
    typed for the drawing set, else GV's own standard (the same words `sink_offset` uses)."""
    words = " ".join(str(where or "").split()).lower()
    if words.startswith(("this project's", "the project")):
        return "project value"
    if words.startswith("typed for this drawing set"):
        return "drawing set value"
    return "GV standard"


def _fraction(value: object) -> Fraction | None:
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        return Fraction(Decimal(str(value)))
    except (InvalidOperation, ValueError, ArithmeticError):
        return None


def _inches(value: Fraction) -> str:
    """Exact inches as a drawing writes them: `5/8"`, `4"`, `1 1/2"`."""
    whole, rest = divmod(value.numerator, value.denominator)
    if rest == 0:
        return f'{whole}"'
    part = f"{rest}/{value.denominator}"
    return f'{whole} {part}"' if whole else f'{part}"'


def _printed(label: object) -> Fraction | None:
    """A side's value when code read it from the drawing's own text at its spot, else `None`."""
    if not isinstance(label, Mapping) or not label.get("id"):
        return None
    if str(label.get("route") or "text") != "text":
        return None  # drawn ink read by OCR is never this proof
    return _fraction(label.get("inches"))


def _place(finding: Mapping[str, Any], architect: Mapping[str, Any]) -> str | None:
    """The area (the item's own "Area: ..." words) and the architect's page, for the question."""
    item = " ".join(str(finding.get("item") or "").split())
    area = item.split(":", 1)[0].strip() if ":" in item else ""
    page = architect.get("page")
    parts = [area] if area else []
    if isinstance(page, int) and not isinstance(page, bool):
        parts.append(f"architect's page {page}")
    return ", ".join(parts) or None


def _from_words(finding: Mapping[str, Any]) -> tuple[str, str, str, str | None] | None:
    """(name, settings, origin, rule id) from an older item's own words, else `None`."""
    words = " ".join(f"{finding.get('gv_note') or ''} {finding.get('why') or ''}".split())
    made_by = finding.get("created_by")
    if made_by == "filler_range":
        found = _FILLER_WORDS.search(words)
        if found is not None:
            return ("filler width", found.group("value"), "GV standard", None)
        return None
    if made_by == "sink_offset":
        found = _SINK_WORDS.search(words)
        if found is not None:
            return (found.group("name"), found.group("value"), found.group("origin"), None)
    return None


def rule_note_of(finding: Mapping[str, Any]) -> RuleNote | None:
    """The GV rule the architect's size breaks, when the item is one (see the module), else
    `None` (it stays Not compared)."""
    if finding.get("keep_reason") != KEEP_REASON or finding.get("status") != "check":
        return None
    dims = finding.get("dims")
    if not isinstance(dims, Mapping):
        return None
    vendor_label, architect_label = dims.get("vendor"), dims.get("architect")
    if not isinstance(architect_label, Mapping):
        return None
    vendor, architect = _printed(vendor_label), _printed(architect_label)
    if vendor is None or architect is None or abs(vendor - architect) > _TOLERANCE:
        return None  # not both read at their spots, or the vendor does not follow the architect
    standard = finding.get("gv_standard")
    if isinstance(standard, Mapping):
        if standard.get("proved") is False or standard.get("follows_architect") is False:
            return None
        name, settings = standard.get("name"), standard.get("value")
        if not name or not settings:
            return None
        found: tuple[str, str, str, str | None] = (
            str(name),
            str(settings),
            str(standard.get("origin") or "GV standard"),
            str(standard["rule"]) if standard.get("rule") else None,
        )
    else:
        words = _from_words(finding)
        if words is None:
            return None
        found = words
    name, settings, origin, rule_id = found
    if origin not in _ORIGIN_WORDS:
        origin = "GV standard"  # the contract's three words only
    return RuleNote(
        name=name,
        settings=settings,
        origin=origin,
        architect=_inches(architect),
        rule_id=rule_id,
        place=_place(finding, architect_label),
    )
