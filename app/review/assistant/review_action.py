"""The review result on the send-ready package (#1622), decided by code from the final list.

Every reviewed submittal gets one result on its cover. GV uses the woodwork trade's own four (AWI,
Architectural Woodwork Standards Section 1; also Procore's defaults): **Approved**, **Approved as
noted**, **Revise and resubmit**, **Rejected**. A check that did not finish gets **no stamp** at all.

The rules are checked top to bottom and **the first that matches wins** (research note
INDUSTRY-REVIEW.md, section 1.2). "Corrections" are the Wrong items clouded on the GV cloud drawing
(`proof.proof_group == "clouded"`; an item the reviewer agreed is one, one marked not a problem is
not, and a previous round's comment this round fixed is Closed, not a correction):

- **S0** the check did not finish (stopped early, or pages not reached): no stamp, "Check
  incomplete: not for sending";
- **S1** the vendor's set has no sheet for an area the architect draws, or most of the vendor's
  values could not be read: Revise and resubmit, "incomplete / illegible submittal";
- **S2** the vendor drew a different product than the one checked (code must prove it; nothing
  proves it yet, so it is an input that stays off): Rejected;
- **S3** a previous comment is still wrong in this round: Revise and resubmit, "previous comment
  not addressed";
- **S4** a correction changes fabrication (`changes_fabrication`), or more than `MAX_NOTED`
  corrections: Revise and resubmit;
- **S5** 1 to `MAX_NOTED` corrections, all minor: Approved as noted;
- **S6** no correction, but items code could not prove or compare (other than "the architect gives
  no information"): Approved as noted, with the note that those items were not reviewed;
- **S7** otherwise: Approved.

**What changes fabrication** comes from the impact score's own words (#1569, `impact.touches`), so
the order of the biggest items and the result never disagree: a missing or extra part, a cut-out or
appliance opening, an overall size, a height, the parts or their arrangement, an outlet (the score
says "changes fabrication"); a plain size, a dimension or hardware off by more than `MINOR_IN` (or by
an amount code did not work out, never assumed small); and a wrong material, finish or product
(`item_action` "Submit specified item"). A tag, label or note otherwise is minor.

The words, the stamp line and the disclaimer are constants here (one place to change them). Pure
code: no I/O, no AI call. The letter, the cloud drawing's cover, the issue register and the screen
all take the result from `review_action`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final, Literal

from app.review.assistant.impact import TOP_IMPACT, touches
from app.review.assistant.proof import proof_group

__all__ = [
    "ACTION_REQUIRED_BY",
    "ACTION_WORDS",
    "DISCLAIMER",
    "MAX_NOTED",
    "MINOR_IN",
    "NEXT_STEP",
    "NOT_REVIEWED_NOTE",
    "Action",
    "CheckState",
    "ReviewAction",
    "changes_fabrication",
    "corrections",
    "item_action",
    "review_action",
    "stamp_words",
]

type Action = Literal[
    "approved", "approved_as_noted", "revise_resubmit", "rejected", "not_for_sending"
]
type Rule = Literal["S0", "S1", "S2", "S3", "S4", "S5", "S6", "S7"]

#: The results as the letter, the cover and the screen print them (AWI's four, and no stamp).
ACTION_WORDS: Final[Mapping[Action, str]] = {
    "approved": "Approved",
    "approved_as_noted": "Approved as noted",
    "revise_resubmit": "Revise and resubmit",
    "rejected": "Rejected",
    "not_for_sending": "Check incomplete: not for sending",
}
#: Who must act now (the submittal log's "ball in court").
ACTION_REQUIRED_BY: Final[Mapping[Action, str]] = {
    "approved": "No action required",
    "approved_as_noted": "Vendor: make the corrections noted",
    "revise_resubmit": "Vendor: revise and resubmit",
    "rejected": "Vendor: make a new submittal",
    "not_for_sending": "GV: finish the check before sending",
}
#: What the vendor does next (the letter's closing line).
NEXT_STEP: Final[Mapping[Action, str]] = {
    "approved": "Fabrication may proceed in line with the contract documents.",
    "approved_as_noted": (
        "Fabrication may proceed once every correction noted is made. A resubmittal is not "
        "required for these items."
    ),
    "revise_resubmit": (
        "Please revise the drawings, answer each comment in writing, cloud every change, and "
        "resubmit them for review before fabrication."
    ),
    "rejected": "Please make a new submittal of the specified product.",
    "not_for_sending": "This check did not finish. It is not to be sent.",
}
#: S6's note: what was not reviewed, said plainly.
NOT_REVIEWED_NOTE: Final = (
    "Items in the Not-compared list were not reviewed; vendor to field verify / confirm as listed."
)
# Pending legal review: GV's or Nexolv's lawyer approves this wording before it is relied on.
#: The standard disclaimer printed under the result on the letter, the cloud drawing's cover and
#: the register (one constant: change it here).
DISCLAIMER: Final = (
    "This check compares the dimensions, notes and tags printed on this shop drawing with the "
    "architect's drawings listed on the cover and with GV's published standards. It checks "
    'general conformance with that information only. Items in the "Not compared" list were not '
    "reviewed. GV's corrections, comments, or the absence of a comment do not relieve the vendor "
    "of complying with the contract documents, or of its responsibility for field verifying, "
    "confirming and correlating all quantities and dimensions, fabrication methods, coordination "
    "with other trades, and safe performance of the work. Any deviation from the contract "
    "documents the vendor did not identify in writing is not approved. Approval of an item is not "
    "approval of an assembly it is part of."
)
#: A plain size, dimension or hardware value off by at most this much (inches) is minor.
MINOR_IN: Final = Decimal(1) / Decimal(8)
#: More corrections than this is Revise and resubmit (S4), whatever they touch.
MAX_NOTED: Final = TOP_IMPACT
#: More than this share of the items unreadable (and at least this many) is illegible (S1).
_ILLEGIBLE_SHARE: Final = 0.5
_ILLEGIBLE_MIN: Final = 3
_FABRICATION: Final = "changes fabrication"
_COSMETIC: Final = "cosmetic"
#: A correction about what is supplied, not its size: the per-item "Submit specified item".
_SPECIFIED: Final = re.compile(
    r"\b(materials?|finish(?:es)?|species|laminates?|veneers?|products?|models?|colou?rs?|tags?)\b",
    re.IGNORECASE,
)
#: Not-compared reasons that only say the architect gives no information (S7 allows them).
_NO_INFORMATION: Final = frozenset({"architect_prints_nothing", "nothing_to_compare"})


def item_action(finding: Mapping[str, Any]) -> str:
    """A correction's own action: "Submit specified item" when a material, finish, product or tag
    differs (both printed), else "Correct as noted"."""
    vendor = str(finding.get("vendor") or "").strip()
    architect = str(finding.get("architect") or "").strip()
    if vendor and architect and _SPECIFIED.search(str(finding.get("item") or "")):
        return "Submit specified item"
    return "Correct as noted"


def changes_fabrication(finding: Mapping[str, Any]) -> bool:
    """Whether a Wrong item changes what the shop builds (see the module's words)."""
    if item_action(finding) == "Submit specified item":
        return True
    what, difference = touches(finding)
    if what.startswith(_FABRICATION):
        return True
    if what.startswith(_COSMETIC):
        return False
    return difference is None or difference > MINOR_IN


def corrections(
    findings: Iterable[Mapping[str, Any]], closed: Iterable[str] = ()
) -> list[Mapping[str, Any]]:
    """The corrections sent to the vendor, in the order given: the Wrong items clouded on the GV
    cloud drawing (`proof_group` "clouded"), less the previous round's comments this round fixed
    (`closed`: their finding ids; they are listed as Closed instead)."""
    done = frozenset(closed)
    return [
        finding
        for finding in findings
        if proof_group(finding) == "clouded" and str(finding.get("id") or "") not in done
    ]


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


@dataclass(frozen=True, slots=True)
class CheckState:
    """What the result needs beyond the findings, worked out by code when the check ends."""

    incomplete: str | None = None
    """Why the check did not finish ("it stopped at its fixed price"), else `None` (S0)."""
    missing_areas: int = 0
    """Areas the architect draws with no vendor sheet (`vendor_consistency.completeness`, S1)."""
    wrong_product: bool = False
    """Code proved the vendor drew a different product (S2). Nothing sets it yet."""
    still_open: int = 0
    """Previous comments still wrong in this round (the round-2 check, S3)."""
    closed: tuple[str, ...] = ()
    """The finding ids of previous comments this round fixed: Closed, no longer corrections."""

    def as_dict(self) -> dict[str, Any]:
        return {
            "incomplete": self.incomplete,
            "missing_areas": self.missing_areas,
            "wrong_product": self.wrong_product,
            "still_open": self.still_open,
            "closed": list(self.closed),
        }

    @classmethod
    def from_dict(cls, raw: object) -> CheckState:
        if not isinstance(raw, Mapping):
            return cls()
        incomplete = raw.get("incomplete")
        return cls(
            incomplete=str(incomplete) if incomplete else None,
            missing_areas=_count(raw.get("missing_areas")),
            wrong_product=raw.get("wrong_product") is True,
            still_open=_count(raw.get("still_open")),
            closed=tuple(str(c) for c in raw.get("closed") or () if isinstance(c, str)),
        )


@dataclass(frozen=True, slots=True)
class ReviewAction:
    action: Action
    rule: Rule
    """Which rule decided it (S0 to S7)."""
    words: str
    """"Revise and resubmit", …"""
    reason: str
    """Why, in one plain line ("2 of 5 corrections change fabrication.")."""
    corrections: int
    fabrication: int
    """How many corrections change fabrication."""
    note: str | None = None
    """S6: what was not reviewed."""
    disclaimer: str = DISCLAIMER

    @property
    def stamped(self) -> bool:
        """A result the package may be sent with (S0 is not)."""
        return self.action != "not_for_sending"

    @property
    def next_step(self) -> str:
        return NEXT_STEP[self.action]

    @property
    def required_by(self) -> str:
        return ACTION_REQUIRED_BY[self.action]

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "rule": self.rule,
            "words": self.words,
            "reason": self.reason,
            "corrections": self.corrections,
            "fabrication": self.fabrication,
            "note": self.note,
            "next_step": self.next_step,
            "required_by": self.required_by,
            "disclaimer": self.disclaimer,
        }


def stamp_words(action: ReviewAction, date: str, submittal: str = "", round_no: int = 1) -> str:
    """The stamp line: "GV REVIEW: REVISE AND RESUBMIT. Checked automatically by GV Review on
    2026-10-18. Submittal 25A, round 2." """
    identity = f"Submittal {submittal}, round {round_no}." if submittal else f"Round {round_no}."
    return (
        f"GV REVIEW: {action.words.upper()}. Checked automatically by GV Review on {date}. "
        f"{identity}"
    )


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _not_reviewed(findings: Sequence[Mapping[str, Any]]) -> int:
    """Items code could not prove or compare, other than "the architect gives no information"
    (and other than a Right item simply not seen on both sides)."""
    count = 0
    for finding in findings:
        if proof_group(finding) in ("clouded", "right", "dismissed", "not_compared"):
            continue
        reason = str(finding.get("keep_reason") or finding.get("not_compared_reason") or "")
        if reason not in _NO_INFORMATION:
            count += 1
    return count


def _illegible(findings: Sequence[Mapping[str, Any]]) -> bool:
    unreadable = sum(
        1
        for f in findings
        if f.get("keep_reason") == "unreadable" or f.get("not_compared_reason") == "unreadable"
    )
    return unreadable >= _ILLEGIBLE_MIN and unreadable > _ILLEGIBLE_SHARE * len(findings)


def review_action(
    findings: Iterable[Mapping[str, Any]], state: CheckState | None = None
) -> ReviewAction:
    """The review result for one final finding list and what the check knew (see the module's
    rules S0 to S7; the first that matches wins)."""
    given = list(findings)
    state = state or CheckState()
    sent = corrections(given, state.closed)
    fabrication = sum(1 for finding in sent if changes_fabrication(finding))

    def result(action: Action, rule: Rule, reason: str, note: str | None = None) -> ReviewAction:
        return ReviewAction(
            action=action,
            rule=rule,
            words=ACTION_WORDS[action],
            reason=reason,
            corrections=len(sent),
            fabrication=fabrication,
            note=note,
        )

    if state.incomplete:
        return result("not_for_sending", "S0", f"The check did not finish: {state.incomplete}.")
    if state.missing_areas:
        has = "has" if state.missing_areas == 1 else "have"
        return result(
            "revise_resubmit",
            "S1",
            f"Incomplete submittal: {_plural(state.missing_areas, 'area')} the architect draws "
            f"{has} no vendor sheet.",
        )
    if _illegible(given):
        return result(
            "revise_resubmit",
            "S1",
            "Illegible submittal: most of the vendor's values cannot be read.",
        )
    if state.wrong_product:
        return result(
            "rejected", "S2", "The drawings show a different product than the one checked."
        )
    if state.still_open:
        return result(
            "revise_resubmit",
            "S3",
            f"{_plural(state.still_open, 'previous comment')} not addressed.",
        )
    if fabrication:
        if fabrication < len(sent):
            changes = "changes" if fabrication == 1 else "change"
            reason = f"{fabrication} of {_plural(len(sent), 'correction')} {changes} fabrication."
        elif fabrication == 1:
            reason = "1 correction, and it changes fabrication."
        else:
            reason = f"{fabrication} corrections, all changing fabrication."
        return result("revise_resubmit", "S4", reason)
    if len(sent) > MAX_NOTED:
        return result(
            "revise_resubmit",
            "S4",
            f"{len(sent)} corrections: more than {MAX_NOTED} to make as noted.",
        )
    if sent:
        return result(
            "approved_as_noted",
            "S5",
            (
                "1 correction, and it is minor (no change to fabrication)."
                if len(sent) == 1
                else f"{len(sent)} corrections, all minor (no change to fabrication)."
            ),
        )
    unreviewed = _not_reviewed(given)
    if unreviewed:
        return result(
            "approved_as_noted",
            "S6",
            f"No correction; {_plural(unreviewed, 'item')} not reviewed by code.",
            NOT_REVIEWED_NOTE,
        )
    return result("approved", "S7", "No correction: every item compared matches.")
