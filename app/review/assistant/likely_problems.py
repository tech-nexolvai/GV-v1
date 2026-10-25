"""Likely problems: what the whole-drawing look found that code could not prove (Anant, 2026-10-23).

The whole-drawing look (`whole_look`) compares each vendor drawing with its paired architect drawing
the way an experienced reviewer looks at two sheets side by side: missing or extra pieces, different
counts, different overall sizes, a different layout. The AI never decides red. Each of its claims is:

- **proved by code** and then a normal item: code read both named labels at their spots and code's
  own geometry (or pairing) proves they end at the same places, so the item takes the usual red /
  green path (the verifier, the devil's advocate, the trust rules). Only what code proves is ever
  clouded;
- else a **Likely problem** (`proof.proof_group` "likely_problem"): shown dashed on the screen, never
  solid red; on the letter and the register it reads "Please check and confirm: ...", with its spot,
  never as an error. A likely problem is never clouded, whatever a check says, unless GV's reviewer
  agrees with it.

A claim that names a label code cannot find where the AI says, or that takes GV's note, the brand
prototype or the vendor's own earlier round as the architect, is dropped by `whole_look` (the run's
log says why) and never reaches the reviewer.

**The data the screen reads** (`proof.proof_of`): `proof.group == "likely_problem"`,
`proof.label == "Likely problem"`, `proof.reason` (the AI's reason in one line), and
`proof.likely_problem`:

    {"kind": "missing_piece" | "extra_piece" | "count" | "size" | "layout",
     "words": "Please check and confirm: <what differs>",
     "reason": "<the AI's reason, one line>",
     "sure": "sure" | "likely",
     "by": "whole_drawing_look"}

The spots are the finding's own `vendor_box` / `arch_box` (with their pictures), as for any item.

Pure code: no I/O, no AI call. Verification: `tests/review/assistant/test_whole_look.py`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

__all__ = [
    "COUNT_WORDS",
    "FIELD",
    "GROUP",
    "GROUP_WORDS",
    "HEADING",
    "KINDS",
    "LETTER_LEAD",
    "SHORT_WORDS",
    "likely",
    "likely_only",
    "needs_code_ends",
    "payload",
    "please_check",
]

#: The proof group's name.
GROUP: Final = "likely_problem"
#: The finding's field the whole-drawing look writes (`whole_look.mark`).
FIELD: Final = "whole_look"
#: The group as every screen and file names it.
GROUP_WORDS: Final = "Likely problem: please check and confirm"
#: Short, for a status cell or a tile.
SHORT_WORDS: Final = "Likely problem"
#: After a count in a summary line: "3 likely problems (please check and confirm)".
COUNT_WORDS: Final = "likely problems (please check and confirm)"
#: The letter's and the drawing's notes heading.
HEADING: Final = "Please check and confirm"
#: The letter's words before each item.
LETTER_LEAD: Final = "Please check and confirm"
#: What a claim can be about.
KINDS: Final = ("missing_piece", "extra_piece", "count", "size", "layout")
#: How a claim joined the check: code proved the pair it names (the normal path), it joined an item
#: a helper had already listed, or it is a likely problem only (never clouded).
PROOFS: Final = ("labels", "joined", "likely_only")


def _mark(finding: Mapping[str, Any]) -> Mapping[str, Any] | None:
    mark = finding.get(FIELD)
    return mark if isinstance(mark, Mapping) else None


def needs_code_ends(finding: Mapping[str, Any]) -> bool:
    """An item the whole-drawing look recorded itself: red only when code's own geometry (or code's
    own pairing) proves both dimensions end at the same places, never on agreeing AI picks alone."""
    mark = _mark(finding)
    return mark is not None and mark.get("proof") in ("labels", "likely_only")


def likely_only(finding: Mapping[str, Any]) -> bool:
    """A claim code could not tie to two labels measuring the same thing (a count, a missing piece,
    a layout, a size with one label): never clouded, whatever a check says."""
    mark = _mark(finding)
    return mark is not None and mark.get("proof") == "likely_only"


#: The claim rules' record (`claim_rules.FIELD`): "not_compared" holds a claim back.
_HELD_BY: Final = "claim_rule"


def likely(finding: Mapping[str, Any]) -> bool:
    """The whole-drawing look found it and it is still open (a difference, or not compared): a
    Likely problem unless code proved it (clouded) or found it Right, or code's claim rules hold
    it back as Not compared (a count code could not add up across the item's sheets)."""
    held = finding.get(_HELD_BY)
    if isinstance(held, Mapping) and held.get("outcome") == "not_compared":
        return False
    return _mark(finding) is not None and finding.get("status") in ("different", "check")


def please_check(finding: Mapping[str, Any]) -> str:
    """ "Please check and confirm: the architect shows 2 units; the vendor shows 1 unit." """
    mark = _mark(finding) or {}
    words = str(mark.get("words") or "").strip()
    if words:
        return words
    item = " ".join(str(finding.get("item") or "this item").split())
    return (
        f"{LETTER_LEAD}: {item}: the architect shows {finding.get('architect')}; the vendor "
        f"shows {finding.get('vendor')}."
    )


def payload(finding: Mapping[str, Any]) -> dict[str, Any] | None:
    """The screen's `proof.likely_problem` (see the module), else `None`."""
    mark = _mark(finding)
    if mark is None:
        return None
    kind = str(mark.get("kind") or "size")
    sure = str(mark.get("sure") or "likely")
    return {
        "kind": kind if kind in KINDS else "size",
        "words": please_check(finding),
        "reason": " ".join(str(mark.get("why") or "").split()) or None,
        "sure": sure if sure in ("sure", "likely") else "likely",
        "by": "whole_drawing_look",
    }
