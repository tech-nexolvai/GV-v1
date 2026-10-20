"""Nothing reaches the vendor that code cannot find on the drawing (#1650).

A full check's items go to the vendor: red clouds, the Not-compared PDF and workbook, the comment
letter. An item whose values code cannot find printed, or read at a spot on the drawing, cannot be
shown to the vendor or checked by anyone: on the paid run of 2026-10-18 the scorer called three such
items fabricated (a GV rule result whose architect side read "see GV Review's record", and two
completeness copies whose vendor side read "shown", with no spot and no picture).

At a full check's finish (after the resolver and the escalation, before the counts and the files)
code drops each such item, with its reason, as an internal note (the answer keeps it as a dropped
block; a recorded difference is dismissed with that reason). An item is kept when each side that
states something is backed:

- the side has its spot (a box: what was read there has its picture), or a code label (`dims`);
- or it says the thing is not there ("not shown", "none"): an absence has no spot;
- or it is a size code finds printed on that page, on that side's drawing (`dims.json`'s labels;
  a page whose labels are not known yet is never held against it);
- or it is the vendor's value of a difference between two drawings whose architect side has its
  spot or label: the item stays listed (Not compared: "no spot", with the read-number tool's
  reason), never red, so a found difference is not lost because the page scan missed the
  vendor's number (round 1, 2026-10-20);
- or code made it: a GV rule result that states its requirement (GV's rule engine read the vendor's
  value itself), the vendor's own sum (a string's should-be), or a code witness that located the
  values (printed at both spots, the drawn-ink reading, a finish code read from the text, code's
  sum). A rule result with no stated requirement ("see GV Review's record") is never sent.

Pure code: no PDF here; the labels come from the caller (`dim_index.DimLabels.on_page`).
Verification: `tests/review/assistant/test_spot_guard.py` (made-up findings).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from decimal import Decimal
from typing import Any, Final

from app.review.assistant.finding_checks import claim_value, to_inches

__all__ = ["MAX_NOTE", "Labels", "dropped", "unbacked"]

#: A page's labels: (file or None for the main upload, page) -> labels, or None when unknown.
type Labels = Callable[[str | None, int], Sequence[Mapping[str, Any]] | None]

#: A side that says the thing is not there ("not shown on the vendor", "not found", "none").
_ABSENT: Final = re.compile(
    r"^\s*(?:not (?:shown|drawn|on|found|in)\b|missing\b|absent\b|none\b|no\b|n/?a\b|-+\s*$)",
    re.IGNORECASE,
)
#: Code witnesses that located the item's values on the drawing.
_LOCATED: Final = frozenset({"printed", "ocr_ink", "text", "arithmetic"})
#: Statuses that reach the vendor's files (Wrong and Not compared).
_SHOWN: Final = frozenset({"different", "check"})
#: The longest note a dropped finding keeps (the finding book's limit for a reason).
MAX_NOTE: Final = 300
#: A printed size and a stated one agree within this (inches).
_SAME_IN: Final = Decimal("0.03125")
_SIDES: Final = {"architect": ("architect", "brand"), "vendor": ("vendor",)}


def _flat(value: object) -> str:
    return " ".join(str(value or "").split())


def _size(text: str) -> Decimal | None:
    """The size a side states (its first dimension), or None when it states none."""
    value = to_inches(text)
    if value is not None:
        return value
    first = claim_value(text)
    return to_inches(first) if first else None


def _printed(
    labels: Labels, file: str | None, pages: Iterable[int], sides: Sequence[str], size: Decimal
) -> bool | None:
    """Whether a label on one of `pages` (that side's drawing) prints `size`; None when no page's
    labels are known (code could not look, so it never drops on that)."""
    known = False
    for page in pages:
        found = labels(file, page)
        if found is None:
            continue
        known = True
        for label in found:
            if str(label.get("side") or "unknown") not in (*sides, "unknown"):
                continue
            try:
                printed = Decimal(str(label.get("inches")))
            except (ArithmeticError, ValueError):
                continue
            if abs(printed - size) <= _SAME_IN:
                return True
    return False if known else None


def _box_file(finding: Mapping[str, Any]) -> str | None:
    for key in ("vendor_box", "arch_box"):
        box = finding.get(key)
        if isinstance(box, Mapping) and box.get("file"):
            return str(box["file"])
    return None


def _note(finding: Mapping[str, Any], why: str) -> str:
    """The internal note: the item and both values (kept for GV's reviewer), and why."""
    what = (
        f"{_flat(finding.get('item'))[:80]} (architect {_flat(finding.get('architect'))[:40]}; "
        f"vendor {_flat(finding.get('vendor'))[:40]})"
    )
    note = f"Internal note, not for the vendor: {what}. {why}"
    return note if len(note) <= MAX_NOTE else note[: MAX_NOTE - 1].rstrip() + "…"


#: Findings that compare a vendor value with another drawing's (the AI found the difference).
_BETWEEN_DRAWINGS: Final = frozenset({"architect", "gv_review", "brand_prototype"})


def _found_difference(finding: Mapping[str, Any]) -> bool:
    """A difference between two drawings whose architect side is backed (its spot, its label, or
    a value printed on its page): the item is real; only the vendor's spot is unknown."""
    if finding.get("source") not in _BETWEEN_DRAWINGS:
        return False
    dims = finding.get("dims") if isinstance(finding.get("dims"), Mapping) else {}
    return bool(finding.get("arch_box") or (dims or {}).get("architect"))


def listed_only(finding: Mapping[str, Any]) -> bool:
    """Whether `unbacked` keeps `finding` only to be listed (#1808): a difference between two
    drawings whose vendor size has no spot (no box, no label) and no code witness that located
    it. Such an item is never red (`proof.code_proved`)."""
    if not _found_difference(finding):
        return False
    dims = finding.get("dims") if isinstance(finding.get("dims"), Mapping) else {}
    if finding.get("vendor_box") or (dims or {}).get("vendor"):
        return False
    witnesses = finding.get("witnesses")
    found = {str(w) for w in witnesses} if isinstance(witnesses, list) else set()
    if _LOCATED & found:
        return False
    return _size(_flat(finding.get("vendor"))) is not None


def unbacked(finding: Mapping[str, Any], labels: Labels) -> str | None:
    """Why `finding` cannot be shown to the vendor (a plain line), or None when it can."""
    if finding.get("status") not in _SHOWN:
        return None
    witnesses = finding.get("witnesses")
    found = {str(w) for w in witnesses} if isinstance(witnesses, list) else set()
    if _LOCATED & found:
        return None
    if "rule" in found and _size(_flat(finding.get("architect"))) is not None:
        # GV's rule engine read the vendor's value itself and states the requirement: a rule
        # result with a requirement is code's, spot or not (one without is never sent, #1622).
        return None
    dims = finding.get("dims") if isinstance(finding.get("dims"), Mapping) else {}
    page = finding.get("page")
    vs_page = finding.get("vs_page")
    for side, box_key in (("architect", "arch_box"), ("vendor", "vendor_box")):
        text = _flat(finding.get(side))
        if finding.get(box_key) or (dims or {}).get(side):
            continue
        if not text or _ABSENT.match(text):
            continue
        size = _size(text)
        made_by_code = finding.get("source") in ("rules", "vendor_arithmetic")
        if side == "architect" and made_by_code and size is not None:
            continue  # GV's rule requirement, or code's sum of the vendor's parts
        if size is not None:
            where = (vs_page, page) if side == "architect" else (page,)
            pages = [p for p in where if isinstance(p, int)]
            if _printed(labels, _box_file(finding), pages, _SIDES[side], size) is not False:
                continue
            if side == "vendor" and _found_difference(finding):
                # A difference found between two drawings whose vendor value code cannot find
                # (pen strokes the page scan missed): kept, listed as Not compared with the
                # read-number tool's reason, never red (no spot, no code reading), never dropped.
                continue
            return _note(
                finding,
                f"Code could not find the {side}'s value printed on page "
                f"{pages[0] if pages else '?'}, and the item gives no spot on the drawing.",
            )
        return _note(
            finding,
            f"The {side}'s side is not a value code can find on the drawing, and the item gives "
            "no spot on it.",
        )
    return None


def dropped(findings: Iterable[Mapping[str, Any]], labels: Labels) -> list[tuple[str, str]]:
    """(finding id, reason) for every finding `unbacked` keeps from the vendor."""
    out: list[tuple[str, str]] = []
    for finding in findings:
        reason = unbacked(finding, labels)
        if reason is not None and finding.get("id"):
            out.append((str(finding["id"]), reason))
    return out
