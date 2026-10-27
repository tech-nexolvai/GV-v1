"""The zero-click cloud drawing's rule: which items code clouds by itself, and its words (#1581).

The owner (2026-10-18): the full check ends with a cloud drawing that says exactly what the vendor
did wrong (in red) and what it should be (in green), with no human step. With nobody in the loop,
a false red cloud goes straight to the vendor, so **only what code proved is clouded**:

- **clouded**: a Wrong item (status `different`) with an independent code witness behind it
  (`witness.py`: printed at both spots, drawn ink read by two OCR engines, GV's rule check, code's
  sum, the reading ledger, millimetres and inches agreeing, finish codes read from the drawings'
  text), a trust level of Confirmed or Likely, and a governing value code knows (GV's rule or
  limits, code's sum, the architect's value read as one size, or the architect's finish code). Never a value measured off the drawing or taken from a scale note (the
  printed scales are wrong wherever GV resized a pasted drawing). A comparison of two drawn
  dimensions (vendor against architect or prototype) also needs a **pairing proof** that both
  numbers measure the same thing (`pairing_proved`: the full check's devil's advocate passed it,
  #1611, under the setting `assistant_pairing_proof`); GV's rule, GV's limits and the vendor's own
  sum need none. Red cloud at the vendor's value, green "Should be", and a proof entry.
- **not_compared** (#1619, Anant's architect-first rules R1-R10, `classification.py`): what code
  could not compare after the full check's escalation (`escalation.py`): a difference code could
  not prove (no pairing proof, a value code could not read), an item the architect gives no size
  for, different reference points, a missing job value, the brand prototype (reference only),
  the architect's own drawings disagreeing, a Right item not seen on both sides. Never a question
  to anyone: listed with its reason and what would settle it in the separate "Not compared" PDF,
  and counted in the summary's one line.
- **right**: a Right item that was compared: a short green list.
- **rule_note** (#1839, the owner 2026-10-21): the architect specifies a size that breaks a GV rule
  and the vendor prints that same size, both read by code at their spots (`rule_notes`): DECIDED,
  "Breaks a GV rule (architect's size): ask the architect". Never clouded, no "should be", not on
  the Not-compared list: the letter and the register carry GV's ready question for the architect.
- **likely_problem** (the owner 2026-10-23, `likely_problems`): what the whole-drawing look found
  (`whole_look`: a missing unit, another count or height, another layout) and code could not prove.
  Never clouded and never on the Not-compared list: shown dashed, and on the letter and the
  register "Please check and confirm: ...". One code proves takes the clouded path like any item.

The reviewer's Agree / Not a problem stay as optional overrides: an agreed item is clouded
whatever code proved (the reviewer is the witness), and an item marked not a problem is never
clouded (`dismissed`, for GV's screen; the vendor's files show it as Right, noted "(reviewer)",
#1766). Nobody has to click.

Pure code (no PDF, no I/O): the drawing (`proof_drawing.py`), the check report, the CSV and the
screen (each finding's `proof`, added on read) all group and count from here, so every count
agrees.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Literal

from app.review.assistant import (
    check_record,
    classification,
    dim_ends,
    drafting_notes,
    likely_problems,
    missing_items,
    reading_routes,
    rule_notes,
    same_item_pages,
    spot_guard,
    witness,
)
from app.review.assistant import full_check_advocate as advocate
from app.review.assistant.chat_trust import inch_words
from app.review.assistant.finding_checks import wrong_part
from app.review.assistant.round_reference import round_only
from app.review.assistant.vendor_text import vendor_text

__all__ = [
    "AI_ONLY_REASON",
    "GROUPS",
    "GROUP_WORDS",
    "KEEP_WORDS",
    "LEFT_CAPTIONS",
    "NO_REQUIREMENT_REASON",
    "PAIRING_REASON",
    "PROTOTYPE_PROOF",
    "READING_LEAD",
    "REVIEWER",
    "REVIEWER_WRONG",
    "RULE_LINE",
    "TWO_READS",
    "UNDECIDED",
    "ProofGroup",
    "architect_file",
    "blocks_with_proof",
    "classified",
    "code_proved",
    "could_not_compare_reason",
    "ends_unconfirmed",
    "ends_words",
    "governs",
    "group_words",
    "gv_limits",
    "held_back",
    "likely_words",
    "needs_pairing",
    "no_stated_requirement",
    "pairing_proved",
    "pick_disagrees",
    "pick_words",
    "please_check_words",
    "proof_counts",
    "proof_group",
    "proof_line",
    "proof_of",
    "proved_wrong",
    "reading_held",
    "reading_words",
    "reviewer_decided",
    "reviewer_should_be",
    "reviewer_value_refusal",
    "reviewer_wrong",
    "scaled",
    "should_be",
    "should_be_value",
    "stated_requirement",
    "string_verified",
    "unproved_reason",
    "vendor_facing",
    "vendor_own",
    "vendor_value",
    "witnesses_of",
]

type ProofGroup = Literal[
    "clouded", "right", "rule_note", "likely_problem", "not_compared", "dismissed"
]
GROUPS: Final[tuple[ProofGroup, ...]] = (
    "clouded",
    "right",
    "rule_note",
    "likely_problem",
    "not_compared",
    "dismissed",
)
#: The groups of an item still open after code's steps (the escalation, the lead review and the
#: look-again work on these): Not compared, and a Likely problem code may still prove.
UNDECIDED: Final = frozenset({"not_compared", "likely_problem"})
#: Each group as the drawing, the report, the CSV and the screen name it.
GROUP_WORDS: Final[Mapping[str, str]] = {
    "clouded": "Wrong, proved by code (clouded)",
    "right": "Right",
    "rule_note": rule_notes.GROUP_WORDS,
    "likely_problem": likely_problems.GROUP_WORDS,
    "not_compared": "Not compared",
    "dismissed": "Right (reviewer)",
}
#: A Wrong the reviewer made (#1803), as the report and the CSV name its group.
REVIEWER_CLOUDED_WORDS: Final = "Wrong, decided by GV's reviewer (clouded)"


def group_words(finding: Mapping[str, Any], group: str) -> str:
    """The group's words for this item (`GROUP_WORDS`); a Wrong the reviewer made says so."""
    if group == "clouded" and reviewer_decided(finding):
        return REVIEWER_CLOUDED_WORDS
    return GROUP_WORDS[group]


#: The rule, in one line, on the drawing's cover and the report.
RULE_LINE: Final = (
    "The architect's drawings govern. Only differences code proved are clouded in red (GV's rule, "
    "the vendor's own sum, a finish code the vendor leaves out or changes, or two dimensions code proved measure the "
    "same thing); what could not be compared is listed apart, with the reason."
)
#: Words saying a value was measured off the drawing or taken from a scale note, not printed.
_SCALED: Final = re.compile(
    r"\bscal(?:e|es|ed|ing)\b|\bmeasured (?:off|on) the drawing\b",
    re.IGNORECASE,
)
#: A vendor string two blind readings agree on (#1641, `string_verified`).
TWO_READS: Final = "two_reads"
#: Why a Wrong item code read right is not clouded: nothing proves both numbers measure the same.
PAIRING_REASON: Final = "Not clouded: code read both values, but what each measures is not proved."
#: Why a GV rule result is not a comment to the vendor (#1622): no requirement it can act on.
NO_REQUIREMENT_REASON: Final = "GV rule result without a stated requirement."
#: The proof line's words when the brand prototype is the reference (R1, Anant 2026-10-20).
PROTOTYPE_PROOF: Final = (
    f"{classification.PROTOTYPE_REFERENCE_WORDS} (the architect gives no elevation of it)"
)
#: Why a Wrong item only AI readings back is not clouded.
AI_ONLY_REASON: Final = "Not clouded: code could not read both values at their spots."
#: The full check's devil's advocate (`full_check_advocate`, #1611) signs its verdict so.
_ADVOCATE: Final = "devil's advocate"
#: Trust levels a clouded item may have (with a code witness).
_PROVED_LEVELS: Final = frozenset({"confirmed", "likely"})

#: Why code could not settle an item (`decision_resolver.keep_reason`), in one plain line.
KEEP_WORDS: Final[Mapping[str, str]] = {
    "architect_prints_nothing": "the architect prints no size here",
    "measures_something_else": "the two dimensions measure different things",
    "which_governs": "the ID set and the brand prototype disagree: which governs?",
    "unreadable": "code cannot read the vendor's value here",
    "needs_job_value": "it needs a job value GV has not given",
    "layout": "a layout to compare by eye (no single size)",
    "vendor_disagrees": "the vendor's own sizes disagree with each other",
    "other": "the drawings do not settle it",
}
#: `check_reason` (#1545) in plain words.
_CHECK_REASON_WORDS: Final[Mapping[str, str]] = {
    "different_extent": "the architect's dimension spans more than the unit",
    "which_governs": "the ID set and the brand prototype disagree: which governs?",
    "pairing_not_certain": "not sure the two drawings show the same item",
    "scaled": "one value is measured off the drawing, not printed",
    "site_verify": "the drawing says verify in field",
}
#: What each source is, named plainly in the green "Should be".
_SOURCE_WORDS: Final[Mapping[str, str]] = {
    "architect": "architect",
    "brand_prototype": "brand prototype",
    "rules": "GV rule",
    "cross_view": "the vendor's own other dimension of the same span",
    # A difference GV Review's page review recorded is the vendor against the architect.
    "gv_review": "architect",
    "vendor_arithmetic": "the vendor's own parts",
}
#: Each non-AI witness as the proof line says it.
_PROOF_WORDS: Final[Mapping[str, str]] = {
    "printed": "printed at both spots, read by code",
    "ocr_ink": "read at both spots from the drawn ink by two OCR engines",
    "rule": "GV's rule check fails it",
    "ledger": "code's reading ledger backs it (the string adds up, or other views agree)",
    "units": "the millimetres and inches printed together agree",
    "text": "code read the finish codes in the drawings' own text and searched every vendor "
    "sheet and round of the area",
}
#: The limits in the resolver's words for GV's limits (`decision_resolver._filler_limits`).
_LIMITS: Final = re.compile(r"limits are (.+?) \(")
#: A pairing proof in the proof line: code paired the two itself, or the advocate's checks.
_PAIRED_BY_CODE: Final = "code paired the two dimensions itself"
_PAIRED_BY_ADVOCATE: Final = (
    "a blind second pick chose the same architect dimension and code checked both ends"
)
#: The resolver's words (#1554): the vendor's value code read at its spot, the filler widths
#: GV's limits caught, and code's sum of the vendor's parts.
_HOW_VENDOR: Final = re.compile(r"\bvendor ((?:\d[\d/\s'-]*)?\d[\d/]*\")")
_HOW_FILLERS: Final = re.compile(r"\bvendor draws (.+?) fillers\b")
_HOW_SUM: Final = re.compile(r"\bparts to (.+?);")
_AI_WORDS: Final[Mapping[str, str]] = {
    "read": "a second reading agrees",
    "lead": "checked on the drawing",
    "critic": "a second, independent check passed it",
}


def _decided(finding: Mapping[str, Any]) -> str | None:
    decision = finding.get("decision")
    if isinstance(decision, Mapping) and decision.get("decision"):
        return str(decision["decision"])
    return None


def reviewer_should_be(finding: Mapping[str, Any]) -> str | None:
    """The value the reviewer said it should be with an Agree (#1803, "It's a mistake: add a red
    mark"), as the vendor may read it; `None` without one."""
    decision = finding.get("decision")
    if _decided(finding) != "agree" or not isinstance(decision, Mapping):
        return None
    return vendor_text(decision.get("should_be")) or None


def reviewer_value_refusal(finding: Mapping[str, Any], should_be: str | None) -> str | None:
    """Why the reviewer's "should be" cannot make a red mark (#1803), or `None`: it must hold a
    number, and it must not be the value the vendor already prints (a red mark that asks for what
    is already there would be a false red)."""
    value = " ".join((should_be or "").split())
    if not value:
        return None
    if not any(ch.isdigit() for ch in value):
        return 'The value it should be needs a number, like 34 1/2".'
    printed = [vendor_value(finding), str(finding.get("vendor") or "")]
    if any(_same_value(value) == _same_value(text) for text in printed if text):
        return "That is the value the vendor already shows. A red mark needs a different value."
    return None


def reviewer_decided(finding: Mapping[str, Any]) -> bool:
    """A Wrong the reviewer made (an Agree) that code did not prove by itself: its proof is the
    reviewer's decision (#1803)."""
    return _decided(finding) == "agree" and not (
        finding.get("status") == "different" and code_proved(finding)
    )


def _witnesses(finding: Mapping[str, Any]) -> list[str]:
    """The finding's non-AI witnesses: as stored, else worked out from its checks."""
    stored = finding.get("witnesses")
    if isinstance(stored, list):
        return [str(w) for w in stored]
    checks = finding.get("checks")
    if not isinstance(checks, Mapping):
        return []
    status = finding.get("status") if isinstance(finding.get("status"), str) else None
    return witness.witnesses(checks, status)


def scaled(finding: Mapping[str, Any]) -> bool:
    """A side measured off the drawing or taken from a scale note, not printed: never proof. The
    printed scale notes are wrong wherever GV resized a pasted drawing (tools research,
    2026-10-18), so only printed values and code's arithmetic count."""
    if finding.get("check_reason") == "scaled" or finding.get("measured_by_scale"):
        return True  # code measured the reference by its scale (`scale_measure`): never proof
    return any(_SCALED.search(str(finding.get(key) or "")) for key in ("architect", "vendor"))


def needs_pairing(finding: Mapping[str, Any]) -> bool:
    """Whether a red cloud needs proof that both numbers measure the same thing: the full check's
    own rule (`full_check_advocate.needs_pairing`, #1611: a Wrong between two drawings that code
    did not pair by itself, not GV's rule check). A filler GV's limits settled compares the
    vendor's width with GV's rule, not with a drawing: it needs none either."""
    if _gv_limits(finding) is not None:
        return False
    return bool(advocate.needs_pairing(finding))


def vendor_own(finding: Mapping[str, Any]) -> str | None:
    """`sum` when the proof is the vendor's own string (its parts against its printed total),
    `span` when the vendor prints the same span twice and code proved it by geometry (the
    consistency check: both dimension lines end on the same two extension lines), `pages` when the
    vendor prints the same item in two sizes on two pages and code read both (`same_item_pages`:
    which one is wrong is not known), else `None`: then there is no architect's side to show."""
    if finding.get("source") == "vendor_arithmetic":
        return "sum"
    if same_item_pages.marked(finding):
        return "pages"
    if (
        finding.get("source") == "cross_view"
        and finding.get("reference_side") == "vendor"
        and finding.get("created_by") == "consistency"
    ):
        return "span"
    return None


#: The proof page's left caption, by `vendor_own` (else the architect's or GV's rule).
LEFT_CAPTIONS: Final[Mapping[str | None, str]] = {
    "sum": "Vendor's own parts (green: their sum)",
    "span": "Vendor's other dimension of the same span (green: what governs)",
    "pages": "Vendor's other drawing of the same item (which one is right is not known)",
    None: "Architect / rule (green: what governs)",
}


def string_verified(finding: Mapping[str, Any]) -> bool:
    """A vendor string whose geometry code verified (#1639): the consistency check made it (its
    parts touch end to end and end on the overall's extension lines), or it confirmed a reading's
    string is that same chain (`string_geometry: verified`); or, under D1 (`ai_pair_plus_code`),
    two readings of the drawing agree on its parts and overall, the second blind to the first
    (`string_geometry: two_reads`, #1641: the full check sets it only in that mode). A string an AI grouped (the reading
    ledger's `record_string`, a helper's sum) is never red without it: a part from another chain
    makes a string that "does not add up" out of one that does."""
    return finding.get("created_by") == "consistency" or finding.get("string_geometry") in (
        "verified",
        TWO_READS,
    )


def _advocate(finding: Mapping[str, Any]) -> Mapping[str, Any] | None:
    critic = finding.get("critic")
    if isinstance(critic, Mapping) and critic.get("model") == _ADVOCATE:
        return critic
    return None


def pairing_proved(finding: Mapping[str, Any]) -> bool:
    """The full check proved the pairing (#1611, the one signal): code paired the two labels by
    itself (`paired_by: code`), or the devil's advocate passed the item: two blind picks of the
    same labels, code reading both, the same drawn extent and every code check
    (`full_check_advocate.decide`, under `assistant_pairing_proof`). An item the whole-drawing
    look made (`likely_problems.needs_code_ends`) needs code's own proof that both dimensions end at
    the same places: two agreeing blind picks are not enough (the paid Set 3 run of 2026-10-23: a
    height to a splash's top went red against a height to the counter's top on two picks)."""
    if finding.get("paired_by") == "code":
        return True
    verdict = _advocate(finding)
    if verdict is None or verdict.get("result") != "passed":
        return False
    if likely_problems.needs_code_ends(finding):
        answers = verdict.get("answers")
        return isinstance(answers, Mapping) and answers.get("pairing") == "proved"
    return True


def _read_by_code(finding: Mapping[str, Any]) -> bool:
    """An independent code witness, trust Confirmed or Likely, no scale note, and a governing
    value code knows: code read the numbers (whether they measure the same thing aside)."""
    if finding.get("status") != "different" or scaled(finding):
        return False
    if str(finding.get("trust_level") or "") not in _PROVED_LEVELS:
        return False
    found = _witnesses(finding)
    return bool(found) and _governing_known(finding, found)


def ai_rule_claim(finding: Mapping[str, Any]) -> bool:
    """A GV-rule item (`source: rules`) that only an AI says breaks the rule: code's own rule check
    did not confirm it (no `rule` witness: the recorded check did not run, or it is not one GV
    records) and GV's limits did not settle it. Code then read only the vendor's number; which
    rule applies, its value, and whether the reference prints the vendor's size (the reference
    wins over GV's standard, settled with GV) are the AI's word, so it is never red (the paid
    run of 2026-10-25: a sink's front offset the reference's section prints clouded against
    GV's 4" standard)."""
    return (
        finding.get("source") == "rules"
        and "rule" not in _witnesses(finding)
        and _gv_limits(finding) is None
    )


def code_proved(finding: Mapping[str, Any]) -> bool:
    """A Wrong item code proved: an independent code witness AND trust Confirmed or Likely (a check
    that disagrees makes it "AI reading only", so it never passes), with neither value measured
    off the drawing or read from a scale note (`scaled`), and, for a comparison of two drawn
    dimensions, a pairing proof (`needs_pairing`, `pairing_proved`): reading the vendor's number
    right (its millimetres and inches agreeing) does not prove the architect's number measures the
    same thing. And every number it rests on was read by two different kinds of route that agree,
    none naming a doubt (`reading_routes`, final plan #2)."""
    return _proved_but_reading(finding) and reading_routes.held(finding) is None


def reading_held(finding: Mapping[str, Any]) -> str | None:
    """Why a Wrong code proved in every other way is held back for its reading (final plan #2): a
    number it rests on has one route only, or a route or reader names a doubt. A Likely problem
    with these words, never red. `None` for any other item."""
    if finding.get("status") != "different" or _reader_error(finding):
        return None
    if likely_problems.likely_only(finding) or not _proved_but_reading(finding):
        return None
    return reading_routes.held(finding)


def _proved_but_reading(finding: Mapping[str, Any]) -> bool:
    """`code_proved` before the reading rule (`reading_routes`)."""
    if classification.internal(finding):
        return False  # read by the AI only, never at its spot (#1652)
    if classification.same_item_conflict(finding) is not None:
        return False  # its copies pair the vendor's number with different architect numbers
    if round_only(finding):
        return False  # compared only with the vendor's own earlier round (Anant, 2026-10-22)
    if spot_guard.listed_only(finding):
        return False  # kept only to be listed (`spot_guard`): no vendor spot, never red (#1808)
    if finding.get(classification.GV_STICKER) is not None:
        return False  # the vendor value read is GV's own sticker, never the vendor's (#1859)
    tag_read = finding.get("tag_read")
    if isinstance(tag_read, Mapping) and tag_read.get("outcome") != "wrong":
        return False  # the tag reader did not prove it (#1859)
    if finding.get("source") == "brand_prototype" and not classification.prototype_governs(finding):
        return False  # R1: the architect gives the item, so the prototype is reference only
    parts = finding.get("architect_parts")
    if parts and not (isinstance(parts, Mapping) and parts.get("chain_proved")):
        # A total the architect never prints (code's summed chain) is proof only when code proved
        # the parts are one chain end to end along the run (client meeting, 2026-10-23).
        return False
    if finding.get("source") == "vendor_arithmetic" and not string_verified(finding):
        return False  # an AI-grouped string: only code's geometry proves the parts are one chain
    if ai_rule_claim(finding):
        return False  # an AI's GV-rule claim that code's own rule check never confirmed
    if classification.architect_conflict(finding) is not None:
        return False  # the architect prints the vendor's value, or not the "should be" (#1802)
    if dim_ends.unlike_reason(finding) is not None:
        return False  # the two dimensions measure between different kinds of points (fix 3)
    if drafting_notes.softened(finding) is not None:
        return False  # the reference marks its size V.I.F. / +/-, or the 1/2" convention (#9)
    if not _read_by_code(finding):
        return False
    return not needs_pairing(finding) or pairing_proved(finding)


def vendor_facing(finding: Mapping[str, Any]) -> bool:
    """Whether an item may reach the vendor (the cloud drawing, the letter, the register, the
    Not-compared list): never one for GV's reviewer only
    (`classification.internal`, #1652)."""
    return not classification.internal(finding)


def stated_requirement(finding: Mapping[str, Any]) -> str | None:
    """What the vendor's value should be, as the vendor may read it (#1622): the governing value
    (`should_be_value`) when it holds a number and no internal words (`vendor_text`), else `None`
    (a GV rule result with no stated requirement is never a numbered comment)."""
    value = vendor_text(should_be_value(finding))
    if not value:
        return None
    if reviewer_should_be(finding) is not None:
        # The reviewer said what it should be (#1803): the reviewer is the witness.
        return value
    if any(ch.isdigit() for ch in value) or "text" in _witnesses(finding):
        # A size, or a finish code read from the drawing's text (#1623).
        return value
    return None


def unproved_reason(finding: Mapping[str, Any]) -> str:
    """Why a Wrong item is not clouded, in one plain line."""
    if "rule" in _witnesses(finding) and stated_requirement(finding) is None:
        return NO_REQUIREMENT_REASON
    verdict = _advocate(finding)
    if verdict is not None and verdict.get("result") == "objects" and verdict.get("note"):
        # The advocate's own reason ("Not proved: a blind second pick chose p23.D12, ...").
        return _short(verdict.get("note"), 200)
    if _read_by_code(finding) and needs_pairing(finding) and not pairing_proved(finding):
        return PAIRING_REASON
    return AI_ONLY_REASON


def _governing_known(finding: Mapping[str, Any], found: Sequence[str]) -> bool:
    """Code knows what the value should be: GV's rule check or limits decided it, code added up
    the vendor's own parts, or code read the governing value as one size. A witness that backs
    only the vendor's side (millimetres and inches agreeing) is not enough on its own when the
    governing value is a sentence or a chain code cannot read as one size."""
    if _gv_limits(finding) is not None:
        return True
    if "rule" in found:
        # A GV rule result proves the vendor is wrong, but the vendor can act on it only when the
        # requirement is stated (#1622): without one it is never a numbered comment.
        return stated_requirement(finding) is not None
    if finding.get("source") == "vendor_arithmetic" and "arithmetic" in found:
        return True
    if "text" in found:
        # The finish check (#1623): the architect's code, read from the drawing's text, governs.
        return True
    return _decimal((finding.get("inches") or {}).get("architect")) is not None


#: A Right whose ends code could not prove alike (final plan #4): its plain name on a card.
MATCHES_UNPROVED: Final = "Matches (ends not proved)"


def matches_unproved(finding: Mapping[str, Any]) -> bool:
    """A Right whose two dimensions code could not prove run between the same kinds of points
    (`dim_ends.LIKE_FOR_LIKE`, for the same two labels the item still pairs): named
    `MATCHES_UNPROVED`, to be folded under a proved sibling. Data only: still a Right."""
    held = finding.get(dim_ends.LIKE_FOR_LIKE)
    if finding.get("status") != "same" or not isinstance(held, Mapping):
        return False
    dims = finding.get("dims") if isinstance(finding.get("dims"), Mapping) else {}
    if dim_ends._label_key((dims or {}).get("vendor")) != held.get("vendor"):
        return False
    if dim_ends._label_key((dims or {}).get("architect")) != held.get("architect"):
        return False
    return held.get("proved") is False


def ends_unconfirmed(finding: Mapping[str, Any]) -> bool:
    """A Wrong code would have clouded on the AI's pairing, held back because code's geometry
    could not confirm both dimensions end at the same places (`assistant_pairing_proof` =
    `code_ends`, Anant's option C of 2026-10-25): every other check passed and the blind picks
    agree (`full_check_advocate.ENDS_NOT_CONFIRMED`), and the item is code-proved in every other
    way. Shown as a Likely problem ("please check"), never hidden on the Not-compared list."""
    if finding.get("status") != "different":
        return False
    verdict = _advocate(finding)
    if verdict is None or verdict.get("result") != "objects":
        return False
    answers = verdict.get("answers")
    if not isinstance(answers, Mapping) or answers.get("pairing") != advocate.ENDS_NOT_CONFIRMED:
        return False
    # Code-proved in every other way: the same item with the picks' pairing accepted, as before.
    return _proved_if_paired(finding, verdict, "picks")


def _proved_if_paired(finding: Mapping[str, Any], verdict: Mapping[str, Any], how: str) -> bool:
    """Whether the item is code-proved in every way but its pairing: the same item with the devil's
    advocate's verdict turned to passed (its pairing `how`)."""
    answers = verdict.get("answers") if isinstance(verdict.get("answers"), Mapping) else {}
    passed = dict(finding)
    checks = {**(finding.get("checks") or {}), "critic": "passed"}
    passed["checks"] = checks
    passed["critic"] = {
        **verdict,
        "result": "passed",
        "answers": {**(answers or {}), "pairing": how},
    }
    passed["trust_level"] = witness.trust_level(checks, "different")
    passed["witnesses"] = witness.witnesses(checks, "different")
    return _proved_but_reading(passed) and not _reader_error(passed)


#: The devil's advocate's code checks (`full_check_advocate.code_checks`, and code's own pairing
#: of the vendor's label with another): one that did not pass stopped the item by code's facts.
_CODE_CHECKS: Final = (
    "labels",
    "ink",
    "annotation",
    "roles",
    "axis",
    "round",
    "units",
    "slip",
    "code_pair",
)


def pick_disagrees(finding: Mapping[str, Any]) -> bool:
    """A Wrong whose two dimensions code's own geometry shows ending at the same places
    (`same_extent`), that passes every code check and is code-proved in every other way, held back
    only because the blind second pick named another reference dimension, or none. The pick is an
    AI: it is never enough for a red, and never the last word against code's geometry either, so
    the item is a Likely problem ("please check"), never red and never a silent Not compared (a
    real upload, 2026-10-27: an outlet's distance from a wall that differs from the reference's;
    code tied both ends on both drawings, the wall and the same outlet's centre, while the blind
    pick took the vendor's dimension for a height)."""
    if finding.get("status") != "different" or not needs_pairing(finding):
        return False
    verdict = _advocate(finding)
    if verdict is None or verdict.get("result") != "objects":
        return False
    answers = verdict.get("answers")
    if not isinstance(answers, Mapping) or "advocate_pick" not in answers:
        return False
    if answers.get("advocate_pick") == answers.get("helper_pick") or answers.get("twin"):
        return False  # the picks agree: another check stopped it
    if any(answers.get(name) not in (None, "pass") for name in _CODE_CHECKS):
        return False
    if any(answers.get(name) == "fail" for name in ("trade", "faces")):
        return False
    pairing = finding.get("pairing")
    extent = pairing.get("extent") if isinstance(pairing, Mapping) else None
    if not (isinstance(extent, Sequence) and extent and extent[0] == "same_extent"):
        return False
    return _proved_if_paired(finding, verdict, "proved")


def pick_words(finding: Mapping[str, Any]) -> str:
    """The please-check line of an item `pick_disagrees` holds back: "Please check and confirm: the
    vendor's 9" against the reference's 1' - 2" (Left wall to outlet); they differ by 5"; code's
    drawing shows both dimensions end at the same places, but a second AI look did not pair them
    (it chose no reference dimension)." """
    item = " ".join(str(finding.get("item") or "this item").split())
    inches = finding.get("inches") if isinstance(finding.get("inches"), Mapping) else {}
    difference = _decimal((inches or {}).get("difference"))
    by = f"; they differ by {inch_words(abs(difference))}" if difference else "; they differ"
    verdict = _advocate(finding) or {}
    answers = verdict.get("answers") if isinstance(verdict.get("answers"), Mapping) else {}
    pick = str((answers or {}).get("advocate_pick") or "none")
    chose = "no reference dimension" if pick == "none" else f"the reference's {pick}"
    return (
        f"{likely_problems.LETTER_LEAD}: the vendor's {vendor_value(finding)} against the "
        f"reference's {finding.get('architect')} ({item}){by}; code's drawing shows both "
        f"dimensions end at the same places, but a second AI look did not pair them (it chose "
        f"{chose})."
    )


def ends_words(finding: Mapping[str, Any]) -> str:
    """The please-check line of an item held back by `ends_unconfirmed`: "Please check and
    confirm: the AI pairs the vendor's 23" with the reference's 24" (Run: depth); they differ by 1";
    code could not confirm both dimensions measure the same thing." """
    item = " ".join(str(finding.get("item") or "this item").split())
    inches = finding.get("inches") if isinstance(finding.get("inches"), Mapping) else {}
    difference = _decimal((inches or {}).get("difference"))
    by = f"; they differ by {inch_words(difference)}" if difference else "; they differ"
    drawn = _drawn_note(finding)
    reading = reading_routes.held(finding)
    if reading is not None and not drawn:
        drawn = f"; and {reading}"
    return (
        f"{likely_problems.LETTER_LEAD}: the AI pairs the vendor's {vendor_value(finding)} with "
        f"the reference's {finding.get('architect')} ({item}){by}; code could not confirm both "
        f"dimensions measure the same thing{drawn}."
    )


def reading_words(finding: Mapping[str, Any]) -> str:
    """The please-check line of a Wrong held back for its reading (`reading_held`): "Please check
    and confirm: the vendor's 23" against the reference's 24" (Run: depth); before a red, two
    different readings of each number must agree: the vendor's 23" is read from the PDF's text,
    but its drawn length at its view's own scale is 24 3/16"." """
    item = " ".join(str(finding.get("item") or "this item").split())
    should = finding.get("architect")
    against = f" against {should}" if should else ""
    return (
        f"{likely_problems.LETTER_LEAD}: the vendor's {vendor_value(finding)}{against} ({item}); "
        f"{READING_LEAD}: {reading_held(finding)}."
    )


#: Why a red waits for its reading, before the reason (`reading_words`).
READING_LEAD: Final = "before a red, two different readings of each number must agree"


#: The words code's ends proof gives a value that does not agree with its drawn length
#: (`ends_proof.drawn_disagreement`, final plan #3).
_DRAWN = re.compile(
    r"(the (?:vendor|reference)'s [^;()]*? is drawn [^;()]*? long at its view's own scale)"
)


def _drawn_note(finding: Mapping[str, Any]) -> str:
    """`; the vendor's 23" is drawn 24" long at its view's own scale` when the advocate held the
    item back because a value does not agree with its drawn length, else nothing."""
    verdict = _advocate(finding)
    found = _DRAWN.search(str((verdict or {}).get("note") or ""))
    return f"; {found.group(1)}" if found else ""


def please_check_words(finding: Mapping[str, Any]) -> str:
    """A Likely problem's please-check line, before any held-back note: the ends' words for an item
    `ends_unconfirmed` holds back, else the whole-drawing look's (`likely_problems.please_check`).
    """
    if ends_unconfirmed(finding):
        return ends_words(finding)
    if pick_disagrees(finding):
        return pick_words(finding)
    if reading_held(finding) is not None:
        return reading_words(finding)
    return likely_problems.please_check(finding)


def proved_wrong(finding: Mapping[str, Any]) -> bool:
    """A Wrong code proved by itself, before any reviewer's decision and before its kind's track
    record (`check_record`): red when its kind of check may make reds, else a Likely problem held
    back. The reviewer's clicks on such an item are evidence about its kind (`click_cases`)."""
    return (
        finding.get("status") == "different"
        and code_proved(finding)
        and not _reader_error(finding)
        and not likely_problems.likely_only(finding)
    )


def proof_group(finding: Mapping[str, Any]) -> ProofGroup:
    """Where the finding goes on the zero-click drawing (see the module's rule). The reviewer's
    decision, when there is one, overrides: agreed = clouded, not a problem = dismissed."""
    if classification.internal(finding):
        return "not_compared"  # GV's reviewer only (#1652): never clouded, whoever clicks
    decided = _decided(finding)
    proved = proved_wrong(finding)
    if not proved and classified(finding).outcome == "right":
        return "right"  # compared and Right: nothing to override
    # The reviewer's decision on any other item (#1803: a Not-compared item whose two values look
    # the same can be decided too). Agreed = clouded, when the vendor can be told what it should
    # be (#1622: a comment with no stated requirement is never sent; the reviewer can give one).
    if decided == "agree" and stated_requirement(finding) is not None:
        return "clouded"
    if decided == "not_a_problem":
        return "dismissed"
    if drafting_notes.softened(finding) is not None:
        # The reference marks its size V.I.F. or +/-, or the difference is the half-inch drafting
        # convention to a wall's face (final plan #9): a field-verify note, never red or Likely.
        return "not_compared"
    if proved:
        # Only a kind of check with a clean track record makes reds (`check_record`, 2026-10-24):
        # another kind's red waits as a Likely problem ("this kind of check is still being proven").
        return (
            "clouded" if check_record.cleared(check_record.kind_of(finding)) else "likely_problem"
        )
    if reading_held(finding) is not None:
        # Code proved it in every other way, but a number it rests on was not read by two
        # different routes that agree (final plan #2): please check, never red.
        return "likely_problem"
    # The architect's size breaks a GV rule and the vendor follows it (#1839): decided, GV asks
    # the architect; never clouded, never on the Not-compared list.
    if classified(finding).outcome == "rule_note":
        return "rule_note"
    if classification.same_item_conflict(finding) is not None:
        return "not_compared"  # its copies disagree on the architect's size (`one_card`)
    # The whole-drawing look found it and code could not prove it (2026-10-23): please check.
    if dim_ends.unlike_reason(finding) is not None:
        return "not_compared"  # never a Likely problem on two unlike dimensions (fix 3)
    if ends_unconfirmed(finding):
        return "likely_problem"  # the AI's pairing, ends not confirmed by code (option C)
    if pick_disagrees(finding):
        return "likely_problem"  # code's geometry pairs them; a blind AI pick did not
    return "likely_problem" if likely_problems.likely(finding) else "not_compared"


def held_back(finding: Mapping[str, Any]) -> str | None:
    """Why a Wrong code proved is a Likely problem, not a red (`check_record`: its kind of check has
    no clean track record yet), in one plain line; `None` for any other item."""
    if proof_group(finding) != "likely_problem" or finding.get("status") != "different":
        return None
    if not code_proved(finding) or _reader_error(finding) or likely_problems.likely_only(finding):
        return None
    return check_record.held_reason(finding)


def likely_words(finding: Mapping[str, Any]) -> str:
    """A Likely problem's "Please check and confirm: ..." line for the letter, the register and the
    drawing (`likely_problems.please_check`), saying so when the item is held back only because its
    kind of check is still being proven (`held_back`)."""
    words = please_check_words(finding)
    if drafting_notes.hold(finding):
        # The reference marks the size HOLD (final plan #9): a hard requirement, said so.
        words = (
            f"{words.rstrip().rstrip('.')}. The reference marks this size HOLD: a size that must "
            "be kept."
        )
    if held_back(finding) is None:
        return words
    return f"{words.rstrip().rstrip('.')} ({check_record.HELD_WORDS})."


def _reader_error(finding: Mapping[str, Any]) -> bool:
    """A code reader raised on one of its numbers (#1850): never clouded, Not compared."""
    return bool(finding.get(classification.READER_ERROR))


def classified(finding: Mapping[str, Any]) -> classification.Classified:
    """The item under the architect-first rules (R1-R10), with code's proof (`code_proved`). A GV
    rule result with no stated requirement is Not compared under R6 (`no_stated_requirement`)."""
    proved = (
        finding.get("status") == "different"
        and code_proved(finding)
        and not _reader_error(finding)
        and not likely_problems.likely_only(finding)
    )
    if not proved and no_stated_requirement(finding):
        return classification.rule_without_requirement(finding)
    return classification.classify(finding, proved=proved)


def no_stated_requirement(finding: Mapping[str, Any]) -> bool:
    """A GV rule result (a Wrong by GV's rule) that states no requirement the vendor can act on
    (#1622): never a numbered comment, Not compared."""
    return (
        finding.get("status") == "different"
        and not classification.internal(finding)
        and "rule" in _witnesses(finding)
        and stated_requirement(finding) is None
    )


def proof_counts(findings: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """How many items each group has, from one final list (the cover, the report, the CSV and the
    screen's card all count with this)."""
    counts: dict[str, int] = dict.fromkeys(GROUPS, 0)
    for finding in findings:
        counts[proof_group(finding)] += 1
    return counts


# ---- the words ------------------------------------------------------------------------------------


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _short(text: object, limit: int = 120) -> str:
    words = " ".join(str(text or "").split())
    return words if len(words) <= limit else words[: limit - 1].rstrip() + "…"


def _gv_limits(finding: Mapping[str, Any]) -> str | None:
    """GV's limits that settled the item (#1554, "GV's filler limits are 1" to 2" (...)"): the
    limits' words, else `None`."""
    settled = finding.get("settled")
    if not isinstance(settled, Mapping) or settled.get("method") != "gv_limits":
        return None
    found = _LIMITS.search(str(settled.get("how") or ""))
    return found.group(1).strip() if found else None


def gv_limits(finding: Mapping[str, Any]) -> str | None:
    """GV's limits that settled the item ("1" to 2""), else `None` (`_gv_limits`)."""
    return _gv_limits(finding)


def witnesses_of(finding: Mapping[str, Any]) -> list[str]:
    """The finding's non-AI witnesses (`_witnesses`)."""
    return _witnesses(finding)


def _settled_how(finding: Mapping[str, Any]) -> str:
    settled = finding.get("settled")
    return str(settled.get("how") or "") if isinstance(settled, Mapping) else ""


def vendor_value(finding: Mapping[str, Any]) -> str:
    """The vendor's wrong value, short, as code read it: its inches (`16"` for "407 [16]"), the
    printed total of a string that does not add up, the value code read at the spot or the filler
    widths GV's limits caught (#1554); else the vendor's value as the finding words it."""
    part = wrong_part(finding)
    if part is not None:
        return _short(part[0], 40)  # the part code's geometry names (the overall is right)
    if finding.get("source") == "vendor_arithmetic" and finding.get("printed_total"):
        return _short(finding.get("printed_total"), 40)
    inches = _decimal((finding.get("inches") or {}).get("vendor"))
    if inches is not None:
        return inch_words(inches)
    how = _settled_how(finding)
    for pattern in (_HOW_VENDOR, _HOW_FILLERS):
        found = pattern.search(how)
        if found:
            return found.group(1).strip()
    return _short(finding.get("vendor"), 80)


def should_be_value(finding: Mapping[str, Any]) -> str:
    """Just the value the vendor's should be: the value GV's reviewer gave with an Agree (#1803,
    the decision's `should_be`), else code's inches of the governing value when it read one
    (`37"`, `80 1/8"`), GV's limits when they settled it (`1" to 2"`), else that value as
    printed."""
    given = reviewer_should_be(finding)
    if given is not None:
        # The reviewer's value (#1803), written as code writes it when it is the same value.
        known = _code_should_be(finding)
        return known if _same_value(known) == _same_value(given) else given
    limits = _gv_limits(finding)
    if limits is not None:
        return limits
    either = same_item_pages.either_words(finding)
    if either is not None:
        return either  # two pages disagree: code never picks which one is right (#1906)
    part = wrong_part(finding)
    if part is not None:
        return inch_words(part[1])  # the overall less the other parts
    if finding.get("source") == "vendor_arithmetic":
        summed = _decimal(finding.get("parts_sum"))
        if summed is not None:
            return inch_words(summed)
        found = _HOW_SUM.search(_settled_how(finding))
        if found:
            return found.group(1).strip()
    inches = _decimal((finding.get("inches") or {}).get("architect"))
    if inches is not None:
        return inch_words(inches)
    return _short(finding.get("architect"))


#: Who governs a value the reviewer gave (#1803), as every file says it: "24" (reviewer)".
REVIEWER: Final = "reviewer"


def _same_value(text: str) -> str:
    """A value as compared with another: no spaces, one kind of inch and foot mark."""
    return "".join(text.split()).replace("″", '"').replace("′", "'").replace("”", '"').lower()


def _code_should_be(finding: Mapping[str, Any]) -> str:
    """`should_be_value` without the reviewer's own value: what code knows."""
    decision = finding.get("decision")
    if isinstance(decision, Mapping) and decision.get("should_be"):
        finding = {**finding, "decision": {**decision, "should_be": None}}
    return should_be_value(finding)


def architect_file(finding: Mapping[str, Any]) -> str | None:
    """The architect's file when it is not the vendor's (separate PDFs, #1722): the name its spot
    is in, so "page 1" is never read as the vendor's page 1. `None` on GV's combined sheets."""
    arch, vendor = finding.get("arch_box"), finding.get("vendor_box")
    if not isinstance(arch, Mapping) or not isinstance(vendor, Mapping):
        return None
    name, other = arch.get("file"), vendor.get("file")
    return str(name) if name and other and name != other else None


def governs(finding: Mapping[str, Any]) -> str:
    """What governs the value, named plainly with where it is printed: "architect 3'-1", page 28",
    "GV rule", "brand prototype 24", page 40", "the vendor's own parts (printed 80")"."""
    given = reviewer_should_be(finding)
    if given is not None and _same_value(given) != _same_value(_code_should_be(finding)):
        # The reviewer gave a value code does not know (#1803).
        return REVIEWER
    source = str(finding.get("source") or "architect")
    words = _SOURCE_WORDS.get(source, source.replace("_", " "))
    architect = _short(finding.get("architect"), 60)
    if source == "vendor_arithmetic":
        # #1887: code's geometry named the wrong part; the overall is right.
        if wrong_part(finding) is not None:
            return "the overall less the other parts"
        return "sum of the vendor's own parts"
    if _gv_limits(finding) is not None:
        return "GV filler rule"
    pages = same_item_pages.governs_words(finding)
    if pages is not None:
        return pages
    standard = _gv_standard(finding)
    if source == "rules":
        return standard or words
    page = finding.get("vs_page")
    box = finding.get("arch_box")
    if isinstance(box, Mapping) and box.get("page"):
        page = box.get("page")
    where = f", page {page}" if page and page != finding.get("page") else ""
    separate = architect_file(finding)
    if separate is not None and page:
        where = f", {separate} page {page}"
    if classification.prototype_reference(finding):
        # R1 (Anant, 2026-10-20): no architect elevation of the item, the prototype is the
        # reference, and the words say so.
        words = classification.PROTOTYPE_REFERENCE_WORDS
    agrees = f"; {standard} agrees" if standard and _standard_agrees(finding) else ""
    if should_be_value(finding) == architect:
        return f"{words}{where}{agrees}"
    return f"{words} {architect}{where}{agrees}"


def _gv_standard(finding: Mapping[str, Any]) -> str | None:
    """A GV standard a code check compared the vendor with (`sink_offset`'s `gv_standard`, #1807):
    "GV standard: sink front offset" (or "project value: ..."), else `None`."""
    standard = finding.get("gv_standard")
    if not isinstance(standard, Mapping) or not standard.get("name"):
        return None
    return f"{standard.get('origin') or 'GV standard'}: {standard['name']}"


def _standard_agrees(finding: Mapping[str, Any]) -> bool:
    """The architect prints the GV standard's own value (`gv_standard`'s `architect`, #1807)."""
    standard = finding.get("gv_standard")
    return (
        isinstance(standard, Mapping)
        and bool(standard.get("architect"))
        and standard.get("architect") == standard.get("value")
    )


def should_be(finding: Mapping[str, Any]) -> str:
    """The green words: `37" (architect 3'-1", page 28)`."""
    value = should_be_value(finding)
    return f"{value} ({governs(finding)})"


def _sum_words(finding: Mapping[str, Any]) -> str:
    summed = _decimal(finding.get("parts_sum"))
    printed = _short(finding.get("printed_total") or finding.get("vendor"), 30)
    if summed is not None:
        return f"parts add up to {inch_words(summed)}, printed {printed}"
    found = _HOW_SUM.search(_settled_how(finding))
    if found is not None:
        return f"parts add up to {found.group(1).strip()}, printed {printed}"
    return "code added up the vendor's dimension string"


def proof_line(finding: Mapping[str, Any]) -> str:
    """The one line naming the check that proved it, in plain words: "printed at both spots, read
    by code", "parts add up to 80 1/8", printed 80"", GV's limits, "agreed by Anant". What code
    settled by itself (#1554) is said in the resolver's own words."""
    parts: list[str] = []
    if _decided(finding) == "agree":
        # The reviewer's decision (#1803), with the reviewer's note when the vendor may read it;
        # never the reviewer's login (the letter's "reviewed by" names the people).
        parts.append(reviewer_wrong(finding))
    how = _settled_how(finding)
    witnesses = _witnesses(finding)
    if needs_pairing(finding) and pairing_proved(finding):
        verdict = _advocate(finding)
        answers = verdict.get("answers") if verdict is not None else None
        by_code = finding.get("paired_by") == "code" or (
            isinstance(answers, Mapping) and answers.get("pairing") == "code"
        )
        parts.append(_PAIRED_BY_CODE if by_code else _PAIRED_BY_ADVOCATE)
    if how:
        parts.append(f"settled by GV Review: {_short(how, 200)}")
    else:
        for name in witnesses:
            if name == "arithmetic":
                parts.append(_sum_words(finding))
                if finding.get("string_geometry") == TWO_READS:
                    parts.append("a second reading, blind to the first, lists the same parts")
            elif name == "rule" and finding.get("rule_check_id"):
                parts.append(f"GV's rule check {finding.get('rule_check_id')} fails it")
            elif name == "text" and _missing_item(finding) is not None:
                parts.append(missing_items.proof_words(_missing_item(finding) or {}))
            elif name in _PROOF_WORDS:
                parts.append(_PROOF_WORDS[name])
    if witnesses:
        checks = finding.get("checks")
        ai = witness.ai_witnesses(checks) if isinstance(checks, Mapping) else []
        parts += [_AI_WORDS[name] for name in ai if name in _AI_WORDS]
    if not parts:
        parts.append("AI reading only: no code check backs it")
    if classification.prototype_reference(finding):
        # R1 (Anant, 2026-10-20): the proof names what the vendor was compared with.
        parts.insert(0, PROTOTYPE_PROOF)
    text = "; ".join(dict.fromkeys(parts))
    return text[0].upper() + text[1:]


def _missing_item(finding: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The `text_proof` of a missing millwork item code proved (`missing_items`), else None."""
    found = finding.get("text_proof")
    if isinstance(found, Mapping) and found.get("kind") == missing_items.KIND:
        return found
    return None


def reviewer_wrong(finding: Mapping[str, Any]) -> str:
    """ "decided by GV's reviewer", with the reviewer's note when the vendor may read it (#1803;
    the Right side is `vendor_text.reviewer_right`)."""
    decision = finding.get("decision")
    note = decision.get("note") if isinstance(decision, Mapping) else None
    words = vendor_text(note, "") if note else ""
    return f"{REVIEWER_WRONG}: {words}" if words else REVIEWER_WRONG


#: The reviewer's Agree as every file says it (#1803).
REVIEWER_WRONG: Final = "decided by GV's reviewer"


def could_not_compare_reason(finding: Mapping[str, Any]) -> str:
    """Why an item code could not settle could not be compared, in one line ("the architect prints
    no size here"): the resolver's reason, else the check reason, else its question or why."""
    reason = finding.get("keep_reason")
    if isinstance(reason, str) and reason in KEEP_WORDS and reason != "other":
        return KEEP_WORDS[reason]
    named = finding.get("check_reason")
    if isinstance(named, str) and named in _CHECK_REASON_WORDS:
        return _CHECK_REASON_WORDS[named]
    question = finding.get("question")
    if question:
        return _short(question, 160)
    if finding.get("why"):
        return _short(finding.get("why"), 160)
    return KEEP_WORDS["other"]


def proof_of(finding: Mapping[str, Any]) -> dict[str, Any]:
    """A finding's `proof` for the screen and the CSV: its group, the green "Should be" (Wrong
    items), the proof line (clouded items), for a Not-compared item the rule, the reason, why in
    one line and what would settle it (#1619), and for a rule note (#1839) why and the GV rule with
    GV's question for the architect."""
    group = proof_group(finding)
    found = classified(finding)
    not_compared = group == "not_compared"
    noted = group == "rule_note" and found.note is not None
    likely = group == "likely_problem"
    wrong = (
        finding.get("status") == "different" and not not_compared and not likely
    ) or group == "clouded"
    reason: str | None = None
    if not_compared:
        reason = found.why
        if unproved_reason(finding) == NO_REQUIREMENT_REASON:
            # A GV rule result with nothing the vendor can act on (#1622).
            reason = NO_REQUIREMENT_REASON
    elif noted:
        reason = found.why
    elif likely:
        reason = (
            held_back(finding)
            or (likely_problems.payload(finding) or {}).get("reason")
            or (ends_words(finding) if ends_unconfirmed(finding) else None)
            or (pick_words(finding) if pick_disagrees(finding) else None)
            or (reading_words(finding) if reading_held(finding) is not None else None)
            or found.why
        )
    elif group == "dismissed":
        reason = "The reviewer marked it Right."
    return {
        "group": group,
        "should_be": should_be(finding) if wrong else None,
        "proof": proof_line(finding) if group == "clouded" else None,
        "reason": reason,
        "rule": (
            None
            if group == "dismissed"
            else (
                found.rule
                if group != "clouded"
                else (classification.classify(finding, proved=True).rule)
            )
        ),
        "not_compared": found.reason if not_compared else None,
        "label": (
            found.label
            if not_compared
            else (
                rule_notes.SHORT_WORDS if noted else likely_problems.SHORT_WORDS if likely else None
            )
        ),
        "settle": found.settle if not_compared else None,
        # #1839: the GV rule the architect's size breaks, and GV's question for the architect: what
        # code found, also under a reviewer's override (the screen goes back to it on an undo).
        "rule_note": found.note.as_dict() if found.note is not None else None,
        # The whole-drawing look's claim (2026-10-23): what to check and confirm, its kind and
        # reason; the spots are the finding's own boxes.
        "likely_problem": likely_problems.payload(finding),
        # Plan change #10: a Wrong code proved, held back only because its kind of check is still
        # being proven (`held_back`). The reviewer's Agree / Not a problem on it is evidence for the
        # kind's track record (plan change #10), so the screen offers both.
        # Worked out on the item as code left it, so it stays the same under the reviewer's click.
        "held": held_back({k: v for k, v in finding.items() if k != "decision"}) is not None,
    }


def blocks_with_proof(blocks: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """An answer's blocks with each finding's `proof` (worked out on read; nothing stored)."""
    out: list[dict[str, Any]] = []
    for block in blocks:
        finding = block.get("finding")
        if block.get("type") == "finding" and isinstance(finding, Mapping):
            block = {**block, "finding": {**finding, "proof": proof_of(finding)}}
        out.append(dict(block))
    return out
