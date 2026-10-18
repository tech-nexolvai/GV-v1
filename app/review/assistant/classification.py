"""What each item of a check is, by the architect-first rules (Anant, 2026-10-18): R1-R10.

**The principle.** The architect's drawings and the contract documents are the source of truth.
GV's reviewer compares the vendor's shop drawing against the architect's intent: did the vendor
miss anything, change the design or use the wrong material? Shop drawings are not contract
documents and cannot vary them (AIA A201); printed dimensions govern over scaled ones, larger-scale
and detail drawings over general ones, a later revision over an earlier one. Nobody at GV is asked
to decide anything: an item is **Wrong** (red, proved), **Right**, or **Not compared** (with the
reason and what would settle it, listed in the separate "Not compared" PDF and the summary line).

The rules, in the order they are applied:

- **R1, the architect's set governs.** The brand prototype is reference only, unless the
  architect's drawing points to it ("per brand standard"); a difference from the prototype alone is
  never a question: it is listed as information (`prototype`).
- **R2, printed dimensions only.** A value scaled off a drawing never decides anything; it may
  appear as context ("the drawing scales about 58\\"").
- **R3, the architect's drawings disagree** (code's reading ledger: the architect's own views):
  the governing view decides when code knows it; else Not compared, both values and pages named.
- **R4, the vendor differs from the architect** (a size, a count, a position; a missing item or
  tag; a different material; something added): Wrong when proved (`proof.code_proved`: code read
  both values and proved they measure the same thing, or code searched the vendor's sheets for the
  architect's tag); otherwise Not compared, with why it could not be proved.
- **R5, the vendor's own contradictions** (a string that does not add up, one span printed twice):
  Wrong when code proved it.
- **R6, GV's rule** (Raj's) broken: Wrong.
- **R7, the architect gives no information** (no size, no layout, no drawing of the area).
- **R8, different reference points** (the underside against the top of the counter): not directly
  comparable.
- **R9, the vendor's value is unreadable** after the whole escalation ladder.
- **R10, a job value or GV setting is missing.**

The escalation ladder (`escalation.py`, then the roster's one deeper look) runs BEFORE an item
falls to Not compared. Pure code: no I/O, no AI call. The full check, the resolver's leftovers and
chat answers all classify through `classify`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal

__all__ = [
    "REASONS",
    "REASON_WORDS",
    "TRADE_WORDS",
    "Classified",
    "classify",
    "not_compared_line",
    "points_to_prototype",
]

type Outcome = Literal["wrong", "right", "not_compared"]
type Reason = Literal[
    "architect_disagrees",
    "nothing_to_compare",
    "different_reference",
    "unreadable",
    "missing_setting",
    "prototype",
    "not_proved",
    "not_seen",
    "field_verify",
]

#: The Not-compared reasons, in the order the list and the summary give them.
REASONS: Final[tuple[Reason, ...]] = (
    "field_verify",
    "nothing_to_compare",
    "different_reference",
    "unreadable",
    "architect_disagrees",
    "missing_setting",
    "not_proved",
    "not_seen",
    "prototype",
)
#: Each reason as the Not-compared list's heading and the summary line say it.
REASON_WORDS: Final[Mapping[str, str]] = {
    "nothing_to_compare": "architect gives no size or information",
    "different_reference": "not directly comparable (different reference points)",
    "unreadable": "vendor's value could not be read",
    "architect_disagrees": "architect's drawings disagree",
    "missing_setting": "a job value or GV setting is missing",
    "not_proved": "difference not proved by code",
    "not_seen": "not looked at on both drawings",
    "prototype": "brand prototype differs (reference only)",
    "field_verify": "the drawing says verify in field",
}
#: The trade's own words for each reason (industry review, topic 5), shown next to ours on the
#: Not-compared list, the workbook and the screen.
TRADE_WORDS: Final[Mapping[str, str]] = {
    "field_verify": "Field verify",
    "nothing_to_compare": "Not reviewed: not shown on the architect's drawings",
    "different_reference": "Not reviewed: not directly comparable",
    "unreadable": "Illegible",
    "architect_disagrees": "RFI to the architect",
    "missing_setting": "Not reviewed: project value not set",
    "not_proved": "Not reviewed: not proved by code",
    "not_seen": "Not reviewed: not looked at on both drawings",
    "prototype": "Not reviewed: brand prototype (reference only)",
}
#: The rule each reason comes from.
_RULES: Final[Mapping[str, str]] = {
    "nothing_to_compare": "R7",
    "different_reference": "R8",
    "unreadable": "R9",
    "architect_disagrees": "R3",
    "missing_setting": "R10",
    "not_proved": "R4",
    "not_seen": "R4",
    "prototype": "R1",
    "field_verify": "R7",
}
#: The architect's drawing points to the brand standard: the prototype is then its intent (R1).
_POINTS_TO_PROTOTYPE: Final = re.compile(
    r"\b(?:per|see|refer to|as)\s+(?:the\s+)?(?:brand\s+(?:standard|prototype)s?|prototype)\b",
    re.IGNORECASE,
)
#: "Verify in field" on the architect's side.
_VIF: Final = re.compile(r"\bV\.?I\.?F\b|verify in field|field verify", re.IGNORECASE)
#: Words of a scaled value (R2): context only.
_SCALES: Final = re.compile(r"scales? (?:about|approx\w*|~)?\s*([0-9][0-9 /'\-\"]*)", re.IGNORECASE)
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


@dataclass(frozen=True, slots=True)
class Classified:
    """Where an item stands: its outcome, the rule, and for Not compared why and what settles it."""

    outcome: Outcome
    rule: str
    reason: Reason | None = None
    why: str | None = None
    """Why it could not be compared, in one plain line (Not compared only)."""

    @property
    def label(self) -> str | None:
        """The trade's words for the reason ("Field verify", "RFI to the architect", …)."""
        return TRADE_WORDS.get(self.reason) if self.reason else None

    settle: str | None = None
    """What would settle it (Not compared only)."""


def _flat(text: object) -> str:
    return " ".join(str(text or "").split())


def _short(text: object, limit: int = 70) -> str:
    words = _flat(text)
    return words if len(words) <= limit else words[: limit - 1].rstrip() + "…"


def points_to_prototype(words: str) -> bool:
    """The architect's own words point to the brand standard (R1)."""
    return bool(_POINTS_TO_PROTOTYPE.search(words))


def _page(finding: Mapping[str, Any]) -> object:
    return finding.get("page")


def _not(
    reason: Reason, why: str, settle: str, finding: Mapping[str, Any] | None = None
) -> Classified:
    del finding
    return Classified("not_compared", _RULES[reason], reason, why, settle)


def _scaled_about(finding: Mapping[str, Any]) -> str:
    found = _SCALES.search(_flat(finding.get("architect")) + " " + _flat(finding.get("why")))
    return (
        f" (the architect's drawing scales about {found.group(1).strip()}: context only)"
        if (found)
        else ""
    )


def _spans(finding: Mapping[str, Any]) -> tuple[str, str] | None:
    pairing = finding.get("pairing")
    reply = pairing.get("advocate") if isinstance(pairing, Mapping) else None
    if not isinstance(reply, Mapping):
        return None
    vendor, architect = reply.get("vendor_spans"), reply.get("architect_spans")
    if not isinstance(vendor, Mapping) or not isinstance(architect, Mapping) or vendor == architect:
        return None
    v = _SPAN_WORDS.get(str(vendor.get("to")), "")
    a = _SPAN_WORDS.get(str(architect.get("to")), "")
    return (v, a) if v and a else None


def _read_by_code(finding: Mapping[str, Any]) -> bool:
    checks = finding.get("checks") or {}
    found = finding.get("witnesses")
    if isinstance(found, list) and ("printed" in found or "ocr_ink" in found):
        return True
    return checks.get("printed_vendor") == "match" and checks.get("printed_architect") == "match"


def _vendor_read(finding: Mapping[str, Any]) -> bool:
    checks = finding.get("checks") or {}
    verify = checks.get("verify_vendor")
    return checks.get("printed_vendor") == "match" or (
        isinstance(verify, Mapping) and verify.get("result") == "match"
    )


def _unproved(finding: Mapping[str, Any]) -> Classified:
    """A difference (status different) code could not prove: the closest reason."""
    page = _page(finding)
    if finding.get("check_reason") == "scaled" or _SCALES.search(_flat(finding.get("architect"))):
        return _not(
            "nothing_to_compare",
            f"the architect prints no size for it; a scaled value never decides{_scaled_about(finding)}",
            "the architect's printed size for {thing}",
        )
    spans = _spans(finding)
    if spans is not None:
        return _not(
            "different_reference",
            f"vendor dimensions to {spans[0]}, architect to {spans[1]}",
            "the printed offset between the two points (for example the top's thickness)",
        )
    if not _vendor_read(finding):
        return _not(
            "unreadable",
            f"could not read the vendor's value on page {page}",
            f"a legible value on page {page} (or on the next round)",
        )
    if _read_by_code(finding):
        return _not(
            "different_reference",
            "code read both values but could not prove both dimensions run between the same "
            "points",
            "a dimension of {thing} on both drawings between the same two points",
        )
    return _not(
        "not_proved",
        "the difference rests on readings code could not check",
        "a printed value at both spots, or code's own pairing of the two dimensions",
    )


def _check_item(finding: Mapping[str, Any]) -> Classified:
    """An item the reading could not decide (status check), by why (the resolver's reason)."""
    page = _page(finding)
    reason = str(finding.get("keep_reason") or "")
    named = str(finding.get("check_reason") or "")
    if finding.get("source") == "cross_view":
        side = finding.get("reference_side")
        if side == "architect":
            return _not(
                "architect_disagrees",
                f"architect's drawings disagree: {_short(finding.get('architect'), 40)} vs "
                f"{_short(finding.get('vendor'), 40)}"
                + (
                    f" (pages {finding.get('vs_page')} and {page})"
                    if finding.get("vs_page")
                    else ""
                ),
                "the architect's answer on which drawing governs, or a revised drawing",
            )
        return _not(
            "not_proved",
            "the vendor's own drawings print different values; code could not prove they are "
            "the same span",
            "the vendor's corrected drawing, or one dimension that governs",
        )
    if (
        reason == "which_governs"
        or named == "which_governs"
        or finding.get("source") == ("brand_prototype")
    ):
        return _not(
            "prototype",
            "the brand prototype differs; the architect's drawing governs (the prototype is "
            "reference only)",
            "nothing, unless the architect's drawing points to the brand standard",
        )
    if named == "site_verify" or _VIF.search(_flat(finding.get("architect"))):
        return _not(
            "field_verify",
            "the architect's drawing says verify in field (VIF)",
            "the vendor's field-measured value for {thing}, shown on the drawing",
        )
    if reason in ("architect_prints_nothing",) or named == "pairing_not_certain":
        why = f"architect gives no size or information for it{_scaled_about(finding)}"
        return _not("nothing_to_compare", why, "the architect's size or note for {thing}")
    if reason == "measures_something_else" or named == "different_extent":
        return _not(
            "different_reference",
            "the two dimensions run between different points",
            "the printed offset between the two points (for example the top's thickness)",
        )
    if named == "scaled":
        return _not(
            "nothing_to_compare",
            f"only a scaled value; printed dimensions only{_scaled_about(finding)}",
            "the architect's printed size for {thing}",
        )
    if reason == "unreadable":
        return _not(
            "unreadable",
            f"could not read the vendor's value on page {page}",
            f"a legible value on page {page} (or on the next round)",
        )
    if reason == "needs_job_value":
        return _not(
            "missing_setting",
            f"a job value or GV setting is missing: {_short(finding.get('why'), 90) or 'not named'}",
            "that job value or GV setting for {thing}",
        )
    if reason == "vendor_disagrees" or finding.get("source") == "vendor_arithmetic":
        return _not(
            "not_proved",
            "the vendor's own numbers disagree, but code could not prove they are one string",
            "the vendor's corrected string, or code reading every part",
        )
    if reason == "layout":
        return _not(
            "not_proved",
            "the layout differs by the readings, but code could not prove the counts",
            "a count code can read (a printed schedule, tag or note)",
        )
    if finding.get("source") == "completeness":
        return _not(
            "nothing_to_compare",
            "one side shows the item and the other never does, by the readings",
            "{thing} on both drawings",
        )
    return _not(
        "not_proved",
        _short(finding.get("why"), 120) or "the drawings do not settle it",
        "a printed value at both spots",
    )


def classify(finding: Mapping[str, Any], *, proved: bool) -> Classified:
    """Where `finding` stands under R1-R10. `proved`: code proved it Wrong (`proof.code_proved`,
    which applies D1 for a pairing). The reviewer's decisions are laid over this by `proof`."""
    found = _classify(finding, proved=proved)
    if found.settle is None or "{thing}" not in found.settle:
        return found
    item = _flat(finding.get("item")) or "this item"
    thing = f"the {item[:1].lower()}{item[1:]}" if item[1:2].islower() else f"the {item}"
    return Classified(
        found.outcome, found.rule, found.reason, found.why, found.settle.replace("{thing}", thing)
    )


def _classify(finding: Mapping[str, Any], *, proved: bool) -> Classified:
    status = finding.get("status")
    source = str(finding.get("source") or "")
    if status == "different" and proved:
        if source in ("vendor_arithmetic", "cross_view"):
            return Classified("wrong", "R5")
        return Classified("wrong", "R6" if source == "rules" else "R4")
    if status == "same":
        if finding.get("compared") is False:
            return _not(
                "not_seen",
                "not looked at on both drawings, so not counted as right",
                "a look at both drawings of {thing}",
            )
        return Classified("right", "R4")
    if status == "different":
        if source == "brand_prototype" and not points_to_prototype(_flat(finding.get("why"))):
            return _not(
                "prototype",
                "the brand prototype differs; the architect's drawing governs (the prototype is "
                "reference only)",
                "nothing, unless the architect's drawing points to the brand standard",
            )
        return _unproved(finding)
    return _check_item(finding)


def not_compared_line(counts: Mapping[str, int]) -> str:
    """The summary's one line: "12 items had nothing to compare (architect gives no size or
    information: 9, …). See the Not-compared list." (or that everything could be compared)."""
    total = sum(counts.values())
    if not total:
        return "Every item could be compared."
    parts = [f"{REASON_WORDS[r]}: {counts[r]}" for r in REASONS if counts.get(r)]
    items = "1 item" if total == 1 else f"{total} items"
    return f"{items} had nothing to compare ({'; '.join(parts)}). See the Not-compared list."
