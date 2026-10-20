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

from app.review.assistant import classification, spot_guard, witness
from app.review.assistant import full_check_advocate as advocate
from app.review.assistant.chat_trust import inch_words
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
    "REVIEWER",
    "REVIEWER_WRONG",
    "RULE_LINE",
    "TWO_READS",
    "ProofGroup",
    "architect_file",
    "blocks_with_proof",
    "classified",
    "code_proved",
    "could_not_compare_reason",
    "governs",
    "group_words",
    "gv_limits",
    "needs_pairing",
    "no_stated_requirement",
    "pairing_proved",
    "proof_counts",
    "proof_group",
    "proof_line",
    "proof_of",
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

type ProofGroup = Literal["clouded", "right", "not_compared", "dismissed"]
GROUPS: Final[tuple[ProofGroup, ...]] = ("clouded", "right", "not_compared", "dismissed")
#: Each group as the drawing, the report, the CSV and the screen name it.
GROUP_WORDS: Final[Mapping[str, str]] = {
    "clouded": "Wrong, proved by code (clouded)",
    "right": "Right",
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
    if finding.get("check_reason") == "scaled":
        return True
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
    consistency check: both dimension lines end on the same two extension lines), else `None`:
    then there is no architect's side to show."""
    if finding.get("source") == "vendor_arithmetic":
        return "sum"
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
    (`full_check_advocate.decide`, under `assistant_pairing_proof`)."""
    if finding.get("paired_by") == "code":
        return True
    verdict = _advocate(finding)
    return verdict is not None and verdict.get("result") == "passed"


def _read_by_code(finding: Mapping[str, Any]) -> bool:
    """An independent code witness, trust Confirmed or Likely, no scale note, and a governing
    value code knows: code read the numbers (whether they measure the same thing aside)."""
    if finding.get("status") != "different" or scaled(finding):
        return False
    if str(finding.get("trust_level") or "") not in _PROVED_LEVELS:
        return False
    found = _witnesses(finding)
    return bool(found) and _governing_known(finding, found)


def code_proved(finding: Mapping[str, Any]) -> bool:
    """A Wrong item code proved: an independent code witness AND trust Confirmed or Likely (a check
    that disagrees makes it "AI reading only", so it never passes), with neither value measured
    off the drawing or read from a scale note (`scaled`), and, for a comparison of two drawn
    dimensions, a pairing proof (`needs_pairing`, `pairing_proved`): reading the vendor's number
    right (its millimetres and inches agreeing) does not prove the architect's number measures the
    same thing."""
    if classification.internal(finding):
        return False  # read by the AI only, never at its spot (#1652)
    if spot_guard.listed_only(finding):
        return False  # kept only to be listed (`spot_guard`): no vendor spot, never red (#1808)
    if finding.get("source") == "brand_prototype" and not classification.prototype_governs(finding):
        return False  # R1: the architect gives the item, so the prototype is reference only
    if finding.get("architect_parts"):
        return False  # a total the architect never prints (code's summed chain) is never proof
    if finding.get("source") == "vendor_arithmetic" and not string_verified(finding):
        return False  # an AI-grouped string: only code's geometry proves the parts are one chain
    if classification.architect_conflict(finding) is not None:
        return False  # the architect prints the vendor's value, or not the "should be" (#1802)
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


def proof_group(finding: Mapping[str, Any]) -> ProofGroup:
    """Where the finding goes on the zero-click drawing (see the module's rule). The reviewer's
    decision, when there is one, overrides: agreed = clouded, not a problem = dismissed."""
    if classification.internal(finding):
        return "not_compared"  # GV's reviewer only (#1652): never clouded, whoever clicks
    status = finding.get("status")
    decided = _decided(finding)
    proved = status == "different" and code_proved(finding)
    if not proved and classified(finding).outcome == "right":
        return "right"  # compared and Right: nothing to override
    # The reviewer's decision on any other item (#1803: a Not-compared item whose two values look
    # the same can be decided too). Agreed = clouded, when the vendor can be told what it should
    # be (#1622: a comment with no stated requirement is never sent; the reviewer can give one).
    if decided == "agree" and stated_requirement(finding) is not None:
        return "clouded"
    if decided == "not_a_problem":
        return "dismissed"
    return "clouded" if proved else "not_compared"


def classified(finding: Mapping[str, Any]) -> classification.Classified:
    """The item under the architect-first rules (R1-R10), with code's proof (`code_proved`). A GV
    rule result with no stated requirement is Not compared under R6 (`no_stated_requirement`)."""
    proved = finding.get("status") == "different" and code_proved(finding)
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
        return "sum of the vendor's own parts"
    if _gv_limits(finding) is not None:
        return "GV filler rule"
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
    items), the proof line (clouded items), and for a Not-compared item the rule, the reason, why
    in one line and what would settle it (#1619)."""
    group = proof_group(finding)
    found = classified(finding)
    not_compared = group == "not_compared"
    wrong = (finding.get("status") == "different" and not not_compared) or group == "clouded"
    reason: str | None = None
    if not_compared:
        reason = found.why
        if unproved_reason(finding) == NO_REQUIREMENT_REASON:
            # A GV rule result with nothing the vendor can act on (#1622).
            reason = NO_REQUIREMENT_REASON
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
        "label": found.label if not_compared else None,
        "settle": found.settle if not_compared else None,
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
