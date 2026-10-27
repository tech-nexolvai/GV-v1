"""What each item of a check is, by the architect-first rules (Anant, 2026-10-18): R1-R10.

**The principle.** The architect's drawings and the contract documents are the source of truth.
GV's reviewer compares the vendor's shop drawing against the architect's intent: did the vendor
miss anything, change the design or use the wrong material? Shop drawings are not contract
documents and cannot vary them (AIA A201); printed dimensions govern over scaled ones, larger-scale
and detail drawings over general ones, a later revision over an earlier one. Nobody at GV is asked
to decide anything: an item is **Wrong** (red, proved), **Right**, a **rule note** (the architect's
size breaks a GV rule: GV asks the architect, #1839), or **Not compared** (with the reason and what
would settle it, listed in the separate "Not compared" PDF and the summary line).

The rules, in the order they are applied:

- **R1, the architect's set governs.** Wherever the architect gives information, it governs: a
  difference from the brand prototype alone is never a question, it is listed as information
  (`prototype`), and code never proves it. The brand prototype is the reference (Anant,
  2026-10-20, as GV's reviewer does: "we don't have ID set elevation, we have taken reference from
  the brand prototype") when the architect gives **no elevation** of the item: code marks such a
  finding `reference: "brand_prototype"` from the page map (no architect drawing of its area,
  `plan_check.architect_in_area`), and it is then judged like an architect comparison, its words
  saying "reference: brand prototype" (`PROTOTYPE_REFERENCE_WORDS`). The prototype also governs
  where the architect's drawing points to it ("per brand standard").
- **R1 and R4, the reference is the architect's** (Anant, 2026-10-22: "if it is not matched with
  our rules or the architect's drawing, then it is wrong, no matter how many rounds it took"). An
  item compared only with the vendor's own drawing (its round 1, or another vendor drawing:
  `round_reference.round_only`) is never Right, whatever the values: it is Not compared
  (`round_only`) unless code compared it with the architect instead (`round_reference`), and the
  round match is shown as information only ("Unchanged since round 1"). A "same" against the brand
  prototype where the prototype is not the reference, or a "vendor" value on the brand
  prototype's drawing, is never Right either (`prototype_only`).
- **R2, printed dimensions only.** A value scaled off a drawing never decides anything; it may
  appear as context ("the drawing scales about 58\\"").
- **R3, the architect's drawings disagree** (code's reading ledger: the architect's own views):
  the governing view decides when code knows it; else Not compared, both values and pages named.
- **R4, the vendor differs from the architect** (a size, a count, a position; a missing item or
  tag; a different material; something added): Wrong when proved (`proof.code_proved`: code read
  both values and proved they measure the same thing, or code searched the vendor's sheets for the
  architect's tag); otherwise Not compared, with why it could not be proved.
- **R5, the vendor's own contradictions** (a string that does not add up, one span printed twice):
  Wrong when code proved it. A string is proved only when code verified its geometry (the parts
  touch end to end and end on the overall's extension lines, `proof.string_verified`); a string an
  AI grouped is Not compared, "Not reviewed: string not verified on the drawing" (#1639).
  A string code's lines cannot prove complete (a filler note at an end, a part left out) is Not
  compared, "the string could not be proved complete" (#1802).
- **The architect guard, for every rule** (#1802, `architect_conflict`): a vendor value that
  equals the architect's printed value for the same item is never Wrong, and a "should be" that
  differs from the architect's printed value for the same item is never proposed: Not compared.
- **R6, GV's rule** (from GV's rulebook) broken: Wrong.
- **A rule note** (#1839, the owner 2026-10-21): the architect specifies a size that breaks a GV
  rule and the vendor prints that same size, both read by code at their spots (`rule_notes`): a
  DECIDED outcome of its own, "Breaks a GV rule (architect's size): ask the architect", never red
  and never Not compared; it carries the rule and GV's ready question for the architect.
- **R7, the architect gives no information** (no size, no layout, no drawing of the area), or only
  parts where the vendor prints an overall: a total the architect never prints is never Wrong
  (`no_overall`, code's pairs: "architect's parts add to 110\"; vendor shows 107 3/4\"").
- **R8, different reference points** (the underside against the top of the counter): not directly
  comparable.
- **R9, the vendor's value is unreadable** after the whole escalation ladder: code looked at its
  spot and could not read it. Never said of words (#1859): a note or model in words is "a note in
  words" (code compares sizes and tags, not notes), a tag is compared by the tag reader
  (`tag_reader`: "tags differ", or the tag not confirmed at its spot), and words code cannot read
  because they are drawn as pen strokes are "words drawn as strokes". A vendor value that is GV's
  own sticker over the vendor's ink (`gv_stickers`) is never the vendor's: "GV's sticker", with
  what code reads of the vendor's own value under it. A value with no spot on the drawing (an older stored record that
  carries a value but no box or label, which `spot_locate` could not place) was never looked at:
  it is Not compared as `no_spot` ("the value has no spot on the drawing"), never "Illegible"
  (#1738).
- **R10, a job value or GV setting is missing.** Also an open item GV's own rule sets rather than
  the architect's drawing (the countertop's depth = cabinet depth + overhang; the sink cut-out's
  size from the sink's inside size) whose job values nobody entered (`job_value_rules`): the rule
  and the values to enter, never "different reference points".

The escalation ladder (`escalation.py`, then the roster's one deeper look) runs BEFORE an item
falls to Not compared. Pure code: no I/O, no AI call. The full check, the resolver's leftovers and
chat answers all classify through `classify`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Any, Final, Literal

from app.review.assistant import (
    dim_ends,
    drafting_notes,
    job_value_rules,
    right_proof,
    row_places,
)
from app.review.assistant.finding_checks import to_inches, wrong_part
from app.review.assistant.ledger import format_inches
from app.review.assistant.round_picks import pick_words
from app.review.assistant.round_reference import (
    ROUND_MATCH,
    earlier_round,
    match_words,
    round_only,
)
from app.review.assistant.rule_notes import RuleNote, rule_note_of, views_note_of
from app.review.assistant.spot_locate import VENDOR_SEARCH, search_words, spot_words

__all__ = [
    "GV_STICKER",
    "PROTOTYPE_REFERENCE",
    "PROTOTYPE_REFERENCE_WORDS",
    "READER_ERROR",
    "READER_ERROR_WORDS",
    "REASONS",
    "REASON_WORDS",
    "SAME_ITEM",
    "TRADE_WORDS",
    "Classified",
    "architect_conflict",
    "classify",
    "differing_values",
    "not_compared_line",
    "points_to_prototype",
    "prototype_governs",
    "prototype_reference",
    "rule_without_requirement",
    "same_item_conflict",
]

type Outcome = Literal["wrong", "right", "not_compared", "rule_note"]
type Reason = Literal[
    "architect_disagrees",
    "nothing_to_compare",
    "different_reference",
    "unreadable",
    "no_spot",
    "ai_unsure",
    "missing_setting",
    "prototype",
    "not_proved",
    "not_seen",
    "field_verify",
    "finish_not_comparable",
    "no_sheet",
    "no_overall",
    "string_not_verified",
    "architect_outside_gv_rule",
    "outside_gv_rule",
    "round_only",
    "prototype_only",
    "internal",
    "gv_sticker",
    "tags_differ",
    "tag_unconfirmed",
    "words",
    "stroke_words",
]

#: The Not-compared reasons, in the order the list and the summary give them.
REASONS: Final[tuple[Reason, ...]] = (
    "field_verify",
    "nothing_to_compare",
    "no_overall",
    "string_not_verified",
    "different_reference",
    "gv_sticker",
    "unreadable",
    "stroke_words",
    "no_spot",
    "ai_unsure",
    "tags_differ",
    "tag_unconfirmed",
    "words",
    "architect_disagrees",
    "missing_setting",
    "architect_outside_gv_rule",
    "outside_gv_rule",
    "not_proved",
    "not_seen",
    "finish_not_comparable",
    "no_sheet",
    "round_only",
    "prototype",
    "prototype_only",
    "internal",
)
#: Each reason as the Not-compared list's heading and the summary line say it.
REASON_WORDS: Final[Mapping[str, str]] = {
    "nothing_to_compare": "architect gives no size or information",
    "different_reference": "not directly comparable (different reference points)",
    "unreadable": "vendor's value could not be read",
    "no_spot": "the value has no spot on the drawing",
    # The list goes to the vendor: no "AI" in it (`vendor_text.internal`); GV's screen keeps the
    # reading's own words ("the AI was unsure and no printed value was found").
    "ai_unsure": "not settled from the drawings (no printed architect value found)",
    "architect_disagrees": "architect's drawings disagree",
    "missing_setting": "a job value or GV setting is missing",
    "not_proved": "difference not proved by code",
    "not_seen": "not looked at on both drawings",
    "prototype": "brand prototype differs (reference only)",
    "field_verify": "the drawing says verify in field",
    "finish_not_comparable": "finish codes not comparable (the finish check)",
    "no_sheet": "no sheet of the area or round to compare",
    "no_overall": "architect gives only parts, no overall",
    "string_not_verified": "vendor's string not verified on the drawing",
    "architect_outside_gv_rule": "the architect's size is outside a GV rule (GV note)",
    "outside_gv_rule": "outside GV's usual range (GV note)",
    "round_only": "compared only with the vendor's earlier round, not the architect",
    "prototype_only": "matches the brand prototype only (reference only)",
    "internal": "read by the AI only, never at its spot (GV's reviewer only)",
    "gv_sticker": "GV's sticker covers the vendor's value",
    "tags_differ": "tags differ (confirm the vendor's code maps to the architect's)",
    "tag_unconfirmed": "tag not confirmed by code against the architect's",
    "words": "a note in words, not a size",
    "stroke_words": "words drawn as strokes (code cannot read them yet)",
}
#: The trade's own words for each reason (industry review, topic 5), shown next to ours on the
#: Not-compared list, the workbook and the screen.
TRADE_WORDS: Final[Mapping[str, str]] = {
    "field_verify": "Field verify",
    "nothing_to_compare": "Not reviewed: not shown on the architect's drawings",
    "different_reference": "Not reviewed: not directly comparable",
    "unreadable": "Illegible",
    "no_spot": "Not reviewed: no spot on the drawing",
    "ai_unsure": "Not reviewed: not settled from the drawings",
    "architect_disagrees": "RFI to the architect",
    "missing_setting": "Not reviewed: project value not set",
    "not_proved": "Not reviewed: not proved by code",
    "not_seen": "Not reviewed: not looked at on both drawings",
    "prototype": "Not reviewed: brand prototype (reference only)",
    "finish_not_comparable": "Not reviewed: finish codes not comparable",
    "no_sheet": "Not reviewed: no sheet to compare",
    "no_overall": "Not reviewed: architect gives no overall",
    "string_not_verified": "Not reviewed: string not verified on the drawing",
    "architect_outside_gv_rule": "Not reviewed: per architect (outside GV's rule)",
    "outside_gv_rule": "Not reviewed: outside GV's usual range (GV note)",
    "round_only": "Not reviewed: compared with the vendor's earlier round only",
    "prototype_only": "Not reviewed: brand prototype (reference only)",
    "internal": "Internal: read but not recorded at its spot",
    "gv_sticker": "Not reviewed: under GV's sticker",
    "tags_differ": "Not reviewed: confirm the tag",
    "tag_unconfirmed": "Not reviewed: tag not confirmed",
    "words": "Not reviewed: note in words",
    "stroke_words": "Illegible: words drawn as strokes",
}
#: The rule each reason comes from.
_RULES: Final[Mapping[str, str]] = {
    "nothing_to_compare": "R7",
    "different_reference": "R8",
    "unreadable": "R9",
    "no_spot": "R4",
    "ai_unsure": "R4",
    "architect_disagrees": "R3",
    "missing_setting": "R10",
    "not_proved": "R4",
    "not_seen": "R4",
    "prototype": "R1",
    "field_verify": "R7",
    "finish_not_comparable": "R4",
    "no_sheet": "R7",
    "no_overall": "R7",
    "string_not_verified": "R5",
    "architect_outside_gv_rule": "R1",
    "outside_gv_rule": "R6",
    "round_only": "R4",
    "prototype_only": "R1",
    "internal": "R9",
    "gv_sticker": "R9",
    "tags_differ": "R4",
    "tag_unconfirmed": "R4",
    "words": "R4",
    "stroke_words": "R9",
}
#: The architect's drawing points to the brand standard: the prototype is then its intent (R1).
_POINTS_TO_PROTOTYPE: Final = re.compile(
    r"\b(?:per|see|refer to|as)\s+(?:the\s+)?(?:brand\s+(?:standard|prototype)s?|prototype)\b",
    re.IGNORECASE,
)
#: "Verify in field" on the architect's side.
_VIF: Final = re.compile(r"\bV\.?I\.?F\b|verify in field|field verify", re.IGNORECASE)
#: The vendor's side says the item is not there ("not shown on the vendor", "missing", "none").
VENDOR_ABSENT: Final = re.compile(
    r"^\s*(?:not (?:shown|drawn|on|found|in)\b|missing\b|absent\b|none\b|no\b|-+\s*$)",
    re.IGNORECASE,
)


#: The vendor's side says the item is not there, anywhere in its words ("outlet not shown on the
#: vendor", "no outlet drawn", "not on the vendor's sheets"), #1671.
_ABSENT_WORDS: Final = re.compile(
    r"\bnot\s+(?:shown|drawn|dimensioned|indicated|included|provided|found|seen|noted|located)\b"
    r"|\bnot\s+on\s+(?:the\s+)?vendor|\b(?:missing|absent|omitted)\b"
    r"|\bno\s+(?:[\w/-]+\s+){0,2}(?:shown|drawn|dimension(?:ed)?|indicated|noted?)\b",
    re.IGNORECASE,
)
#: Words that say the vendor's value could not be read: no absence.
_UNREAD_WORDS: Final = re.compile(
    r"\b(?:unread\w*|illegible|unclear|blurr\w*|covered|cut\s+off)\b"
    r"|\bnot\s+(?:read|legible|readable|clear)\b|\bcan(?:no|')?t\s+(?:be\s+)?read\b"
    r"|\bunder\s+(?:gv|the|a)\b[^.;]*\bmark",
    re.IGNORECASE,
)


def vendor_absent(text: object) -> bool:
    """Whether the vendor's side says the item is not there: starting so (`VENDOR_ABSENT`), or
    saying so anywhere with no number of its own and no words that it could not be read."""
    words = _flat(text)
    if VENDOR_ABSENT.match(words):
        return True
    return (
        bool(_ABSENT_WORDS.search(words))
        and not _UNREAD_WORDS.search(words)
        and not re.search(r"\d", words)
    )


def missing_on_vendor(finding: Mapping[str, Any]) -> bool:
    """The architect shows something and the vendor's side says it is not there."""
    architect = _flat(finding.get("architect"))
    return (
        vendor_absent(finding.get("vendor"))
        and bool(architect)
        and not (VENDOR_ABSENT.match(architect))
    )


def _missing(finding: Mapping[str, Any]) -> Classified:
    """The architect shows it and the vendor's drawing does not, by the readings, but code did not
    prove the vendor's sheets leave it out (#1648: code does not recognise every vendor's symbol
    for an outlet, so a symbol it does not find may still be drawn). When code searched the
    vendor's drawings of the area for the architect's printed value (`escalation`'s
    `vendor_search`), the reason says what it found."""
    shows = f"the architect shows it ({_short(finding.get('architect'), 90)})"
    searched = search_words(finding.get(VENDOR_SEARCH))
    if searched is not None:
        search = finding.get(VENDOR_SEARCH) or {}
        found = search.get("found")
        said = "" if found == "none" else "the AI says the vendor's drawing does not show it, but "
        # Why code did not make it a missing millwork item (`missing_items.judge`, #1847).
        why_not = " ".join(str(search.get("not_red") or "").split())
        searched += f"; not a red: {why_not}" if why_not else ""
        return _not(
            "not_proved",
            f"{shows}; {said}{searched}",
            "the vendor to show {thing} as the architect draws it, or to note that it is by others",
        )
    return _not(
        "not_proved",
        f"{shows}; the vendor's drawing "
        "does not, by the AI's reading; code could not confirm the vendor's sheets leave it out",
        "the vendor to show {thing} as the architect draws it, or to note that it is by others",
    )


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
    """What would settle it (Not compared only); for a rule note, the question for the
    architect."""
    note: RuleNote | None = None
    """A rule note (#1839): the GV rule the architect's size breaks."""


def _flat(text: object) -> str:
    return " ".join(str(text or "").split())


def _short(text: object, limit: int = 70) -> str:
    words = _flat(text)
    return words if len(words) <= limit else words[: limit - 1].rstrip() + "…"


def points_to_prototype(words: str) -> bool:
    """The architect's own words point to the brand standard (R1)."""
    return bool(_POINTS_TO_PROTOTYPE.search(words))


#: A finding's `reference` when code found no architect elevation of its item and the brand
#: prototype is the reference (R1, Anant 2026-10-20). Set and cleared by code only.
PROTOTYPE_REFERENCE: Final = "brand_prototype"
#: How the proof, the letter, the register and the Not-compared list name it.
PROTOTYPE_REFERENCE_WORDS: Final = "reference: brand prototype"


def prototype_reference(finding: Mapping[str, Any]) -> bool:
    """Code marked the finding: the architect gives no elevation of the item, so the brand
    prototype is its reference (R1)."""
    return (
        finding.get("source") == "brand_prototype"
        and finding.get("reference") == PROTOTYPE_REFERENCE
    )


def prototype_governs(finding: Mapping[str, Any]) -> bool:
    """Whether a finding compared with the brand prototype is judged like an architect comparison
    (R1): the architect gives no elevation of the item (`prototype_reference`), or its drawing
    points to the brand standard. Any other prototype comparison is reference only: the architect
    governs wherever it gives information."""
    if finding.get("source") != "brand_prototype":
        return False
    return prototype_reference(finding) or points_to_prototype(_flat(finding.get("why")))


def _page(finding: Mapping[str, Any]) -> object:
    return finding.get("page")


#: A finding one of whose numbers a code reader failed to read (it raised, #1850): the reason.
READER_ERROR: Final = "reader_error"
READER_ERROR_WORDS: Final = "the number could not be read (reader error)"
#: A finding whose vendor value is one of GV's stickers (`gv_stickers`, set by `escalation`): its
#: page, box, text, and the vendor's own value code reads under it (`under`, or `None`, `how`).
GV_STICKER: Final = "gv_sticker"
#: What the tag reader read and decided (`tag_reader.TAG_READ`, set by `escalation`).
_TAG_READ: Final = "tag_read"
#: The copies of one item folded into this card by the last step (`one_card`): `copies`, and
#: `conflict` when they pair one vendor number with different architect numbers.
SAME_ITEM: Final = "same_item"


def same_item_conflict(finding: Mapping[str, Any]) -> list[str] | None:
    """The architect values the copies of this item pair its one vendor number with, when they
    differ (`one_card`): the card is then Not compared, never Wrong or Right. Else `None`."""
    folded = finding.get(SAME_ITEM)
    values = folded.get("conflict") if isinstance(folded, Mapping) else None
    return [str(v) for v in values] if isinstance(values, list) and values else None


def _sticker(finding: Mapping[str, Any]) -> Classified | None:
    """A vendor value that is GV's sticker (#1859): never the vendor's."""
    sticker = finding.get(GV_STICKER)
    if not isinstance(sticker, Mapping):
        return None
    page = sticker.get("page") or finding.get("page")
    said = _short(sticker.get("text") or finding.get("vendor"), 40)
    under = _short(sticker.get("under"), 40)
    why = (
        f"the value read ({said}) is GV's sticker on page {page}, not the vendor's: under GV's "
        f"sticker the vendor prints {under}"
        if under
        else f"the value read ({said}) is GV's sticker on page {page}, not the vendor's; code has "
        f"not read the vendor's own value under it yet ({_short(sticker.get('how'), 90)})"
    )
    return _not(
        "gv_sticker",
        why,
        f"the vendor's own value under GV's sticker on page {page}, read by code (or on the next "
        "round)",
    )


def _tag_read(finding: Mapping[str, Any]) -> Classified | None:
    """The tag reader's Not compared (#1859), with its own reason and words."""
    read = finding.get(_TAG_READ)
    if not isinstance(read, Mapping) or read.get("outcome") != "not_compared":
        return None
    reason = read.get("reason")
    if reason not in _RULES:
        return None
    return _not(
        reason,  # type: ignore[arg-type]
        _short(read.get("why"), 240) or REASON_WORDS[str(reason)],
        _short(read.get("settle"), 200) or "the item compared by code at both spots",
    )


def _words_item(finding: Mapping[str, Any]) -> Classified | None:
    """A vendor side in words, not a size, that would be "Illegible": a note in words (#1859)."""
    vendor = _flat(finding.get("vendor"))
    if not re.search(r"[A-Za-z]{2,}", vendor) or _UNREAD_WORDS.search(vendor):
        return None
    checks = finding.get("checks") if isinstance(finding.get("checks"), Mapping) else {}
    if (checks or {}).get("printed_vendor") != "not_a_number" and _ONE_SIZE.search(vendor):
        return None
    return _not(
        "words",
        f'a note in words, not a size: vendor "{_short(vendor, 60)}", architect '
        f'"{_short(finding.get("architect"), 60)}"; code compares sizes, and tags by the tag reader',
        "a person reads the two notes (or a tag code can compare)",
    )


def _not(
    reason: Reason, why: str, settle: str, finding: Mapping[str, Any] | None = None
) -> Classified:
    del finding
    return Classified("not_compared", _RULES[reason], reason, why, settle)


def _measured(finding: Mapping[str, Any]) -> str:
    """What code measured by the reference's own scale (`scale_measure`), else the drawing's
    scaled words (`_scaled_about`)."""
    measured = finding.get("measured_by_scale")
    if isinstance(measured, Mapping) and measured.get("text"):
        return f" (measured by scale: {measured['text']}; scaled, not printed)"
    return _scaled_about(finding)


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


def _has_spot(finding: Mapping[str, Any], side: str) -> bool:
    """Whether a side has a spot on the drawing: a box, or a numbered label it was built from."""
    dims = finding.get("dims")
    label = dims.get(side) if isinstance(dims, Mapping) else None
    return bool(finding.get("vendor_box" if side == "vendor" else "arch_box")) or bool(label)


def _unread_detail(finding: Mapping[str, Any]) -> str:
    """Why `read_number` (#1746) could not read the vendor's value, as `: <reason>`, or nothing
    when it did not run: its reason is the Not-compared reason (cut off in the file, the readers
    disagree, ...)."""
    checks = finding.get("checks")
    verify = checks.get("verify_vendor") if isinstance(checks, Mapping) else None
    if isinstance(verify, Mapping) and verify.get("read_number") and verify.get("reason"):
        return f": {_short(verify.get('reason'), 140)}"
    return ""


#: The comparisons whose `inches.architect` is the other drawing's printed value (#1802).
_ARCHITECT_SOURCES: Final = frozenset({"architect", "brand_prototype", "gv_review"})


def _number(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def architect_conflict(finding: Mapping[str, Any]) -> str | None:
    """The global guard against a false red (#1802), for every rule: why the item may not be red,
    or `None`.

    - The vendor's value EQUALS the architect's for the same pair (a comparison with the
      architect's printed value, or the architect's printed overall code found for the same run,
      `architect_printed`): nothing to correct.
    - The "should be" differs from the architect's printed value for the same item: when the
      architect prints it (`architect_printed`, code's search of the architect's runs), a sum of
      the vendor's parts that disagrees with it is not what the vendor should print (one of the
      parts may be the wrong number)."""
    inches = finding.get("inches") if isinstance(finding.get("inches"), Mapping) else {}
    vendor = _number(inches.get("vendor"))
    if (
        finding.get("source") in _ARCHITECT_SOURCES
        and vendor is not None
        and vendor == _number(inches.get("architect"))
    ):
        return "the vendor prints the architect's own value"
    printed = {
        value
        for value in (_number(v) for v in finding.get("architect_printed") or ())
        if value is not None
    }
    if not printed:
        return None
    if vendor is not None and vendor in printed:
        return "the vendor prints the architect's own value for the same run"
    settled = finding.get("settled")
    if isinstance(settled, Mapping) and settled.get("method") == "gv_limits":
        return None  # GV's limits are a range, not the architect's size
    part = wrong_part(finding)
    should = (
        part[1]
        if part is not None
        else (
            _number(finding.get("parts_sum"))
            if finding.get("source") == "vendor_arithmetic"
            else _number(inches.get("architect"))
        )
    )
    if should is None or printed != {should}:
        shown = ", ".join(f'{_trim(v)}"' for v in sorted(printed))
        return f"the architect prints {shown} for the same run, not the value it should be"
    return None


#: Two compared values this close are the same printed size (a sixteenth, the drawings' step).
SAME_SIZE: Final = Decimal(1) / Decimal(16)
#: A bare millimetre number paired with inches is the same size within a millimetre.
_MM_PER_INCH: Final = Decimal("25.4")


def _compared_inches(finding: Mapping[str, Any]) -> tuple[Decimal, Decimal] | None:
    """The two numbers a "same" item compared (vendor, reference), in inches, as the rule that
    compared them took them; `None` when they are not both plain numbers. GV's formulas compare the
    vendor's value with their own expected value (a chain's sum, the run plus its allowances), and
    a field cut code allowed is taken off the vendor's value first."""
    held = finding.get("inches")
    inches: Mapping[str, Any] = held if isinstance(held, Mapping) else {}
    vendor, reference = _number(inches.get("vendor")), _number(inches.get("architect"))
    if vendor is None or reference is None:
        return None
    formula = finding.get("formula")
    if isinstance(formula, Mapping) and formula.get("outcome") == "same":
        expected = formula.get("expected")
        value = to_inches(str(expected)) if isinstance(expected, str) and expected.strip() else None
        return None if value is None else (vendor, value)
    cut = finding.get("field_cut")
    if isinstance(cut, Mapping) and cut.get("outcome") == "allowed":
        per_end = to_inches(str(cut.get("per_end") or ""))
        count = cut.get("field_cuts")
        if per_end is None or isinstance(count, bool) or not isinstance(count, int):
            return None
        return (vendor - per_end * count, reference)
    return (vendor, reference)


def differing_values(finding: Mapping[str, Any]) -> tuple[str, str] | None:
    """A Right's two compared values when they are plain numbers that differ (2026-10-27: the AI
    paired a top depth with a cabinet width and called them the same), as a drawing writes
    them; `None` when they are the same within a sixteenth, when either is not a number (a tag, a
    model, words), or when the item is not a comparison of two drawings' sizes (GV's limits are a
    range, a vendor's own sum compares its parts). The value compared is the one the rule took
    (`_compared_inches`); a bare millimetre number against inches is the same size within 1 mm."""
    if finding.get("source") not in _ARCHITECT_SOURCES | {"cross_view"}:
        return None
    settled = finding.get("settled")
    if isinstance(settled, Mapping) and settled.get("method") == "gv_limits":
        return None
    found = _compared_inches(finding)
    if found is None:
        return None
    vendor, reference = found
    if abs(vendor - reference) <= SAME_SIZE:
        return None
    if any(abs(a * _MM_PER_INCH - b) <= 1 for a, b in ((vendor, reference), (reference, vendor))):
        return None  # one side is the same size printed in millimetres only
    return (_inch_text(vendor), _inch_text(reference))


def _inch_text(value: Decimal) -> str:
    return format_inches(Fraction(value).limit_denominator(16))


def _trim(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _unproved(finding: Mapping[str, Any]) -> Classified:
    """A difference (status different) code could not prove: the closest reason."""
    page = _page(finding)
    conflict = architect_conflict(finding)
    if conflict is not None:
        return _not(
            "not_proved",
            f"{conflict}; which number is wrong is not proved",
            "the architect's printed value for {thing}",
        )
    reads = finding.get("string_reads")
    if (
        finding.get("source") == "vendor_arithmetic"
        and isinstance(reads, Mapping)
        and reads.get("outcome") == "gap"
        and finding.get("string_geometry") not in ("verified", "two_reads")
    ):
        return _not(
            "string_not_verified",
            _short(reads.get("why"), 240),
            "code's check that the parts run end to end between the overall's extension lines",
        )
    if (
        finding.get("source") == "vendor_arithmetic"
        and finding.get("created_by") != "consistency"
        and finding.get("string_geometry") not in ("verified", "two_reads")
    ):
        parts = [str(p) for p in finding.get("parts") or []]
        summed = " + ".join(parts) if parts else "the parts"
        return _not(
            "string_not_verified",
            f"parts {summed} = {_short(finding.get('architect'), 30)}; printed "
            f"{_short(finding.get('printed_total') or finding.get('vendor'), 30)}; code could not "
            "verify on the drawing that these parts are one string",
            "code's check that the parts run end to end between the overall's extension lines",
        )
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
    if missing_on_vendor(finding):
        return _missing(finding)
    if not _vendor_read(finding) and not _has_spot(finding, "vendor"):
        # A value with no spot was never looked for on the drawing (#1738): said so, never
        # "Illegible" (`spot_locate` found no single label that prints it).
        return _not(
            "no_spot",
            f"the vendor's value {_short(finding.get('vendor'), 30)}: "
            f"{spot_words(finding, 'vendor')} (page {page})",
            f"the label the vendor's value is printed at on page {page}",
        )
    if not _vendor_read(finding):
        return _words_item(finding) or _not(
            "unreadable",
            f"could not read the vendor's value on page {page}{_unread_detail(finding)}",
            f"a legible value on page {page} (or on the next round)",
        )
    critic = finding.get("critic")
    if (
        isinstance(critic, Mapping)
        and critic.get("model") == "devil's advocate"
        and str(critic.get("note") or "").startswith("Not verified within the fixed price")
    ):
        # The money ran out before its proof check (#1641): said so, never silently grey.
        return _not(
            "not_proved",
            "not verified within the fixed price: code read the values, but the check that both "
            "measure the same thing was not reached",
            "the same check in a full check with money left for it",
        )
    stopped = _advocate_stop(finding)
    if stopped is not None:
        return stopped
    ruled = _job_rule(finding)
    if ruled is not None:
        return ruled
    if _read_by_code(finding):
        return _not(
            "different_reference",
            "code read both values but could not prove both dimensions run between the same points",
            "a dimension of {thing} on both drawings between the same two points",
        )
    return _not(
        "not_proved",
        "the difference rests on readings code could not check",
        "a printed value at both spots, or code's own pairing of the two dimensions",
    )


#: The devil's advocate's code checks (`full_check_advocate.code_checks`, the roster's label and
#: pairing checks) whose failure means the two values were never compared as one dimension, in the
#: order the advocate makes them (its note names the first that failed), with what would settle
#: each.
_ADVOCATE_STOPS: Final[tuple[tuple[str, str], ...]] = (
    ("labels", "a code reading of the value at a numbered label on the drawing"),
    ("ink", "the vendor's own value clear of GV's markup (or on the next round)"),
    ("annotation", "the vendor's own value clear of GV's markup (or on the next round)"),
    ("roles", "the architect's own printed value for {thing} (the reference is another drawing)"),
    ("round", "the same item on the vendor's newest round"),
    ("units", "a second reading of the value (its millimetres and inches disagree)"),
    ("slip", "a second reading of the value"),
    ("code_pair", "which of the architect's dimensions the vendor's value matches"),
)


def _advocate_stop(finding: Mapping[str, Any]) -> Classified | None:
    """A difference the devil's advocate stopped at one of its code checks (the value is GV's
    markup, the reference is on the vendor's own or the brand's drawing, the value has no label
    code read, an older round, ...): Not compared with that check's own words, never "different
    reference points", which says the two dimensions were compared and run between different
    points (accuracy step 1: of the full-price Set 3 run's 42 "different reference points", most
    had stopped at such a check). A width against a height (`axis`) and an architect dimension
    that may run to a bulkhead (`trade`) are different points, so they stay as they are."""
    critic = finding.get("critic")
    if not isinstance(critic, Mapping) or critic.get("model") != "devil's advocate":
        return None
    answers = critic.get("answers")
    if not isinstance(answers, Mapping) or critic.get("result") == "passed":
        return None
    failed = {str(name) for name, outcome in answers.items() if outcome == "fail"}
    if failed & {"axis", "trade"}:
        return None
    for name, settle in _ADVOCATE_STOPS:
        if name in failed:
            note = _flat(critic.get("note")).removeprefix("Not proved:").strip().rstrip(".")
            words = note or f"code's {name} check did not pass"
            return _not("not_proved", f"code's check stopped it: {_short(words, 160)}", settle)
    return None


def _check_item(finding: Mapping[str, Any]) -> Classified:
    """An item the reading could not decide (status check), by why (the resolver's reason)."""
    page = _page(finding)
    reason = str(finding.get("keep_reason") or "")
    named = str(finding.get("check_reason") or "")
    if reason == "architect_outside_gv_rule":
        # The vendor follows the architect, whose size is outside a GV rule (`filler_range`,
        # `sink_offset`): the architect governs, so never red. When code proved both values at
        # their spots it is DECIDED (#1839, the owner 2026-10-21): a rule note, GV's question to
        # the architect; else a GV note on the Not-compared list (Anant's rule, 2026-10-18).
        note = rule_note_of(finding)
        if note is not None:
            return Classified(
                "rule_note", "R1", "architect_outside_gv_rule", note.why, note.question, note
            )
        return _not(
            "architect_outside_gv_rule",
            _short(finding.get("gv_note") or finding.get("why"), 120)
            or "the architect's size is outside a GV rule",
            "GV's own call on the architect's size (an RFI to the architect if it matters)",
        )
    if reason == "architect_disagrees":
        # The architect's own views print this span differently (the simple check's guard,
        # 2026-10-23): never red against the vendor; GV asks the architect (its own item).
        return _not(
            "architect_disagrees",
            _short(finding.get("why"), 160) or "the architect's drawings disagree",
            "the architect's answer on which drawing is right, or a revised drawing",
        )
    if reason == "outside_gv_rule":
        # A size outside GV's usual range with no architect size for it (`filler_range`): GV's
        # practice, not a difference; a GV note, never red (the orchestrator, 2026-10-20).
        return _not(
            "outside_gv_rule",
            _short(finding.get("gv_note") or finding.get("why"), 120) or "outside GV's usual range",
            "GV's own call on the size (the vendor may keep it)",
        )
    if finding.get("source") == "cross_view":
        side = finding.get("reference_side")
        note = views_note_of(finding) if side == "architect" else None
        if note is not None:
            # The architect's own drawings disagree and code read both sizes at their spots
            # (2026-10-23): DECIDED, GV's question to the architect; the governing view is
            # the reference. Never red against the vendor.
            return Classified(
                "rule_note", "R3", "architect_disagrees", note.why, note.question, note
            )
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
    which_governs = reason == "which_governs" or named == "which_governs"
    if (which_governs and not prototype_reference(finding)) or (
        finding.get("source") == "brand_prototype" and not prototype_governs(finding)
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
    if missing_on_vendor(finding) and finding.get("source") != "completeness":
        # Missing on the vendor's drawing: said so, whatever the reading's own reason was.
        return _missing(finding)
    splash = finding.get("splash_views")
    if isinstance(splash, Mapping) and splash.get("why") and not finding.get("measured_by_scale"):
        # The splash step looked on the item's other views (`splash_views`, 2026-10-23): its
        # reason names each view it looked at.
        unlike = splash.get("outcome") == "not_like_for_like"
        return _not(
            "different_reference" if unlike else "nothing_to_compare",
            _short(splash.get("why"), 300),
            "the architect's printed splash size for {thing} (its section or elevation)",
        )
    ruled = _job_rule(finding)
    if ruled is not None:
        return ruled
    if reason in ("architect_prints_nothing",) or named == "pairing_not_certain":
        why = f"architect gives no size or information for it{_measured(finding)}"
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
        if not _has_spot(finding, "vendor"):
            return _not(
                "no_spot",
                f"the vendor's value: the value has no spot on the drawing (page {page})",
                f"the label the vendor's value is printed at on page {page}",
            )
        return _words_item(finding) or _not(
            "unreadable",
            f"could not read the vendor's value on page {page}{_unread_detail(finding)}",
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
    unpaired = _unpaired(finding)
    if unpaired is not None:
        return unpaired
    architect = _flat(finding.get("architect"))
    if (
        VENDOR_ABSENT.match(architect)
        and not vendor_absent(finding.get("vendor"))
        and not _UNREAD_WORDS.search(architect)
        and not _ARCHITECT_UNREAD.search(architect)
    ):
        # The reading says the architect's drawing does not show it (R7): nothing was compared.
        # Never when it says the architect's value could not be read ("no tag legible").
        return _not(
            "nothing_to_compare",
            f"architect gives no size or information for it (the reading: "
            f'"{_short(finding.get("architect"), 60)}"){_measured(finding)}',
            "the architect's size or note for {thing}",
        )
    return _not(
        "not_proved",
        _short(finding.get("why"), 120) or "the drawings do not settle it",
        "a printed value at both spots",
    )


#: The reading says no architect page was agreed for the item ("no agreed architect drawing").
_ARCHITECT_UNPAIRED: Final = re.compile(r"\bno\s+agreed\b|\bpair(?:ed|ing)?\b", re.IGNORECASE)
#: The reading says the architect's words could not be read ("no tag legible"): not an absence.
_ARCHITECT_UNREAD: Final = re.compile(r"\b(?:legible|readable|read)\b", re.IGNORECASE)


def _job_rule(finding: Mapping[str, Any]) -> Classified | None:
    """An item GV's own rule sets (the countertop's depth, the sink cut-out's size), whose job
    values nobody entered (`job_value_rules`, named by the escalation): a job value is missing,
    with the rule and the values to enter, never "different reference points". Not compared
    either way: never red, never Right."""
    record = finding.get(job_value_rules.FIELD)
    if not isinstance(record, Mapping) or not record.get("missing"):
        return None
    if finding.get("keep_reason") in ("unreadable", "needs_job_value") or not _vendor_read(finding):
        return (
            None  # the vendor's own value is the problem: entering job values would not settle it
        )
    why, settle = job_value_rules.words(record)
    return _not("missing_setting", why, settle)


def _unpaired(finding: Mapping[str, Any]) -> Classified | None:
    """An item on a vendor page no architect page is paired with (`reference_page`: pairing
    unsure, or no architect drawing of it in the upload): nothing was compared, so its reason is
    the missing pair, never "difference not proved by code" (R7)."""
    record = finding.get("reference_page")
    architect = _flat(finding.get("architect"))
    unpaired = (
        isinstance(record, Mapping)
        and record.get("page") is None
        and not record.get("pair")
        and str(record.get("why") or "").startswith("No architect page is paired")
    )
    # An older item with no reference page: the reading's own words say no page was agreed.
    said_so = not isinstance(record, Mapping) and bool(
        VENDOR_ABSENT.match(architect) and _ARCHITECT_UNPAIRED.search(architect)
    )
    if not (unpaired or said_so):
        return None
    said = _short(finding.get("architect"), 60)
    return _not(
        "no_sheet",
        f"no architect page is paired with vendor page {_page(finding)}, so nothing was compared"
        + (f' (the reading: "{said}")' if said else ""),
        f"the architect page that shows {{thing}} (a person confirms the pair for page "
        f"{_page(finding)})",
    )


def classify(finding: Mapping[str, Any], *, proved: bool) -> Classified:
    """Where `finding` stands under R1-R10. `proved`: code proved it Wrong (`proof.code_proved`,
    which applies D1 for a pairing). The reviewer's decisions are laid over this by `proof`. A
    finding whose reference is the brand prototype (`prototype_reference`) says so in its reason."""
    found = _with_thing(_classify(finding, proved=proved), finding)
    if found.outcome == "not_compared" and found.why and prototype_reference(finding):
        found = Classified(
            found.outcome,
            found.rule,
            found.reason,
            f"{found.why} ({PROTOTYPE_REFERENCE_WORDS}: the architect gives no elevation of it)",
            found.settle,
        )
    return found


def rule_without_requirement(finding: Mapping[str, Any]) -> Classified:
    """A GV rule result that states no requirement the vendor can act on (#1622, R6): Not
    compared, because the value GV's rule requires is not set for it. The Not-compared list and
    the issue register both say this (#1766)."""
    found = _not(
        "missing_setting",
        "GV's rule found a difference, but the value it requires is not set, so there is "
        "nothing the vendor can act on",
        "the value GV's rule requires for {thing}",
    )
    return _with_thing(
        Classified(found.outcome, "R6", found.reason, found.why, found.settle), finding
    )


def _with_thing(found: Classified, finding: Mapping[str, Any]) -> Classified:
    """`found` with "{thing}" in what would settle it named by the item."""
    if found.settle is None or "{thing}" not in found.settle:
        return found
    item = _flat(finding.get("item")) or "this item"
    thing = f"the {item[:1].lower()}{item[1:]}" if item[1:2].islower() else f"the {item}"
    return Classified(
        found.outcome,
        found.rule,
        found.reason,
        found.why,
        found.settle.replace("{thing}", thing),
        found.note,
    )


#: A vendor side that says the item is not there.
_NOT_ON_VENDOR: Final = re.compile(
    r"^\s*(?:not (?:shown|drawn|on|found|in)\b|missing\b|absent\b|none\b)", re.IGNORECASE
)


def _code_disagrees(finding: Mapping[str, Any]) -> str | None:
    """Why code's own checks contradict an item, in plain words; `None` when none does (an AI
    reading that disagrees is not code's)."""
    checks = finding.get("checks")
    if not isinstance(checks, Mapping):
        return None
    if checks.get("arithmetic") == "does_not_add_up":
        return "code's sum of the parts does not add up to the printed overall"
    if checks.get("rule") == "disagrees":
        return "GV's recorded check disagrees"
    if checks.get("ledger") == "disagrees":
        return "code's reading ledger disagrees"
    if checks.get("units") == "disagrees":
        return "its millimetres and inches disagree"
    for side, key in (("architect", "verify_architect"), ("vendor", "verify_vendor")):
        read = checks.get(key)
        # Only a side that is one printed size is read against its spot (a description of
        # fronts and legs is not).
        if (
            isinstance(read, Mapping)
            and read.get("result") == "mismatch"
            and _ONE_SIZE.fullmatch(" ".join(str(finding.get(side) or "").split()))
        ):
            return f"the drawing prints {read.get('read') or 'another value'} at its spot"
    return None


#: A side that is one printed size: 28", 3' - 1", 711 [28], 33 1/4".
_ONE_SIZE: Final = re.compile(r"[0-9][0-9 /'\-\".\[\]]*")


def internal(finding: Mapping[str, Any]) -> bool:
    """An item for GV's reviewer only, never the vendor's (#1652): a difference a helper read but
    never recorded at its spots (`salvaged`, the full check's UNRECORDED lines), or the reading
    ledger's "one drawing shows it, the other does not" item, whose absence code never searched
    for. Neither is ever red, and neither is in the letter, the register, the Not-compared list
    or the cloud drawing; the screen lists it in grey."""
    if finding.get("salvaged") is True:
        return True
    if finding.get("created_by") == "ledger" and finding.get("source") == "completeness":
        return True
    # An AI's "the vendor does not show it" with no spot on the architect's drawing either: code
    # never saw what is claimed missing, so nothing can be shown to the vendor (#1654).
    dims = finding.get("dims") if isinstance(finding.get("dims"), Mapping) else {}
    return (
        str(finding.get("created_by") or "").startswith("helper")
        and _NOT_ON_VENDOR.match(" ".join(str(finding.get("vendor") or "").split())) is not None
        and not finding.get("arch_box")
        and not (dims or {}).get("architect")
    )


def _reference_problem(finding: Mapping[str, Any]) -> Classified | None:
    """An item whose reference is not the architect's (Anant, 2026-10-22), or `None`: compared
    only with the vendor's own drawing (`round_only`, whatever its status: never Right, never
    red), or called the same as the brand prototype where the prototype is not the reference, or
    with its "vendor" value on the prototype's drawing (`prototype_only`)."""
    dims = finding.get("dims") if isinstance(finding.get("dims"), Mapping) else {}
    vendor, architect = (dims or {}).get("vendor"), (dims or {}).get("architect")
    if (
        finding.get("status") == "same"
        and isinstance(vendor, Mapping)
        and vendor.get("side") == "brand"
    ):
        return _not(
            "prototype_only",
            "the value called the vendor's is on the brand prototype's drawing: the vendor's own "
            "drawing was not compared with the architect's",
            "the vendor's value of {thing} against the architect's",
        )
    if round_only(finding):
        match = finding.get(ROUND_MATCH)
        label = architect
        page = (
            match.get("page")
            if isinstance(match, Mapping)
            else (label.get("page") if isinstance(label, Mapping) else finding.get("vs_page"))
        )
        said = f"{match_words(match)}: " if isinstance(match, Mapping) else ""
        where = f" on page {page}" if page is not None else ""
        earlier = match.get("earlier_item") if isinstance(match, Mapping) else None
        why = _short(match.get("earlier_reason"), 120) if isinstance(match, Mapping) else ""
        tail = (
            f"; the earlier round's own comparison with the architect ({earlier}) is not "
            f"proved either: {why or 'not compared'}"
            if earlier
            else "; code found no architect value of this dimension to compare it with"
        )
        picked = pick_words(match) if isinstance(match, Mapping) and not earlier else None
        if picked:
            # The cheap pick for the leftovers (#1861): what it said, or that it was not asked.
            tail += f", and {picked}"
        if isinstance(match, Mapping) and not earlier_round(match):
            # One round has no earlier round (#1866): the vendor's other drawing of the same
            # round, said as such; still never Right, never red (`round_only`).
            return _not(
                "not_seen",
                f"{said}compared only with the vendor's own other drawing{where} (the set has "
                f"one round), never with the architect's{tail}",
                "the architect's value of {thing}",
            )
        return _not(
            "round_only",
            f"{said}compared only with the vendor's own drawing{where}, never with the "
            f"architect's{tail}",
            "the architect's value of {thing}",
        )
    if finding.get("status") != "same":
        return None
    on_prototype = finding.get("source") == "brand_prototype" or (
        isinstance(architect, Mapping) and architect.get("side") == "brand"
    )
    if on_prototype and not prototype_governs(finding):
        return _not(
            "prototype_only",
            "matches the brand prototype, which is reference only: the architect's drawing "
            "governs and was not compared",
            "the architect's value of {thing}",
        )
    return None


def _classify(finding: Mapping[str, Any], *, proved: bool) -> Classified:
    if internal(finding):
        if finding.get("salvaged") is not True and str(finding.get("created_by") or "").startswith(
            "helper"
        ):
            why = (
                "an AI reading says the vendor does not show it, with no spot on the architect's "
                "drawing; code never saw it; kept for GV's reviewer only"
            )
        elif finding.get("salvaged") is True:
            why = (
                "a helper read it but did not record it at its spots, so code never read it; "
                "kept for GV's reviewer only"
            )
        else:
            why = (
                "an AI reading lists it on one drawing only and code did not search the other "
                "drawing for it; kept for GV's reviewer only"
            )
        return _not("internal", why, "both values read at their spots on the drawings")
    conflict = same_item_conflict(finding)
    if conflict is not None:
        # The same vendor number paired with different architect numbers (`one_card`): which
        # one it measures is not proved, so it is neither Wrong nor Right.
        return _not(
            "not_proved",
            f"the vendor's {_short(finding.get('vendor'), 30)} was compared with "
            f"{len(conflict)} different architect sizes ({', '.join(conflict)}); code did not "
            "prove which one it measures",
            "the architect's dimension that measures the same thing as {thing}",
        )
    # GV's sticker is never the vendor's value, whatever it was compared with (#1859).
    sticker = _sticker(finding)
    if sticker is not None:
        return sticker
    reference = _reference_problem(finding)
    if reference is not None:
        return reference
    status = finding.get("status")
    unlike = dim_ends.unlike_reason(finding)
    if unlike is not None and status in ("same", "different", "check"):
        # Like for like only (fix 3, 2026-10-25): code read what both dimensions measure from and
        # to, and they are not the same kind of points; never Right, never Wrong.
        return _not(
            "different_reference",
            unlike,
            "the architect's dimension between the same two points as {thing}",
        )
    soft = drafting_notes.softened(finding)
    if soft is not None:
        # The reference marks its size V.I.F. or +/-, or the difference is within the half-inch
        # drafting convention to a wall's face (final plan #9): never red, never a Likely problem.
        return _not(
            "field_verify",
            soft,
            "the vendor's field-measured value for {thing}, shown on the drawing",
        )
    if finding.get(READER_ERROR) and status in ("different", "same"):
        # One of its numbers could not be read because a code reader raised (#1850): never
        # Wrong or Right on what was not read; the check itself stays complete.
        return _not(
            "unreadable",
            READER_ERROR_WORDS,
            "both values read at their spots on the drawings",
        )
    # The tag reader's own reason stands (#1859).
    held = _tag_read(finding)
    if held is not None:
        return held
    rule = finding.get("claim_rule")
    if (
        not proved
        and isinstance(rule, Mapping)
        and rule.get("outcome") == "not_compared"
        and rule.get("why")
    ):
        # Code's claim rules could not settle it (`claim_rules`: a unit count over the item's
        # regular and reversed sheets): their reason, never a Likely problem or "not proved".
        return _not(
            "not_proved",
            _short(rule.get("why"), 300),
            str(rule.get("needs") or "")
            or "the unit list of every sheet of {thing} (its regular and reversed sheets)",
        )
    source = str(finding.get("source") or "")
    parts = finding.get("architect_parts")
    if (
        status in ("different", "check")
        and isinstance(parts, Mapping)
        and not (status == "different" and parts.get("chain_proved"))
    ):
        return _not(
            "no_overall",
            f"architect's parts add to {parts.get('total')}; vendor shows {parts.get('vendor')}",
            "the architect's printed overall for {thing}",
        )
    if status == "different" and source == "brand_prototype" and not prototype_governs(finding):
        # R1: the architect governs wherever it gives information; never Wrong by the prototype.
        return _not(
            "prototype",
            "the brand prototype differs; the architect's drawing governs (the prototype is "
            "reference only)",
            "nothing, unless the architect's drawing points to the brand standard",
        )
    if status == "different" and proved:
        if source in ("vendor_arithmetic", "cross_view"):
            return Classified("wrong", "R5")
        if source == "brand_prototype":
            return Classified("wrong", "R1")  # no architect elevation: the prototype governs
        return Classified("wrong", "R6" if source == "rules" else "R4")
    if status == "same":
        against = _code_disagrees(finding)
        if against is not None:
            # Right needs proof too (#1656): an item called the same that code's own check
            # contradicts (the paid run of 2026-10-19: "30 + 18 + 18" called the same as 72")
            # is never a Right sent to the vendor.
            return _not(
                "not_proved",
                f"called the same, but {against}",
                "both values read at their spots, and code's check agreeing",
            )
        differ = differing_values(finding)
        if differ is not None:
            # Right needs the two numbers to be the same (2026-10-27): an AI that pairs a top
            # depth with a cabinet width and calls them the same is never a green tick.
            return _not(
                "not_proved",
                f"the two values differ ({differ[0]} vs {differ[1]}); not the same thing",
                "the architect's dimension that measures the same thing as {thing}",
            )
        elsewhere = row_places.elsewhere_reason(finding)
        if elsewhere is not None:
            # A piece of a row of sizes is Right only against the reference piece that starts
            # where it does (2026-10-27): the same value further along the row is another piece.
            return _not(
                "not_proved",
                _short(elsewhere, 300),
                "the reference's size that starts where the vendor's does along {thing}",
            )
        apart = right_proof.different(finding)
        if apart is not None:
            # Code's geometry shows the two sizes end at other places (2026-10-27): two equal
            # numbers between different points are a tick by luck, never a Right.
            return _not(
                "different_reference",
                _short(f"the two sizes do not measure the same thing: {apart}", 300),
                "the reference's dimension that runs between the same points as {thing}",
            )
        if finding.get("compared") is False:
            return _not(
                "not_seen",
                "not looked at on both drawings, so not counted as right",
                "a look at both drawings of {thing}",
            )
        return Classified("right", "R4")
    if status == "different":
        return _unproved(finding)
    return _check_item(finding)


def not_compared_line(counts: Mapping[str, int], unopened: int = 0) -> str:
    """The summary's one line: "12 items not compared (architect gives no size or information: 9,
    …). See the Not-compared list." (or that everything could be compared). #1717: the screen's
    words, one meaning each. `unopened`: content pages nobody opened (#1805: a stopped check with
    13 of 17 pages unopened said "Every item could be compared"); then it never says that."""
    total = sum(counts.values())
    pages = "1 page" if unopened == 1 else f"{unopened} pages"
    if not total:
        if unopened > 0:
            return f"{pages} not opened: their items were not compared."
        return "Every item could be compared."
    parts = [f"{REASON_WORDS[r]}: {counts[r]}" for r in REASONS if counts.get(r)]
    items = "1 item" if total == 1 else f"{total} items"
    opened = f" {pages} not opened." if unopened > 0 else ""
    return f"{items} not compared ({'; '.join(parts)}).{opened} See the Not-compared list."
