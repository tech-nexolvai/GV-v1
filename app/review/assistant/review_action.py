"""The review result on the send-ready package (#1622), decided by code from the final list.

Every reviewed submittal gets one result on its cover. GV uses the woodwork trade's own four (AWI,
Architectural Woodwork Standards Section 1; also Procore's defaults): **Approved**, **Approved as
noted**, **Revise and resubmit**, **Rejected**. A check that did not finish gets **no stamp** at all.
A finished check that would approve while too many items were Not compared gets **Review needed**
(Anant, 2026-10-22: never "Approved" just because the system could not look): a person decides
those items first, and the vendor reads only "Under review".

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
- **RN** the result would be an approval (S5, S6 or S7) but more than `review_needed_share()` of
  all items (Wrong + Right + Ask the architect + Not compared, a reviewer's decided items included)
  are Not compared: Review needed, "N items need a person before this can be approved". Ask the
  architect items (#1839) are decided, so they never count as not checked. The reviewer's
  decisions on Not compared items (#1814) take them out of the Not compared count, so deciding
  enough of them turns it into Approved as noted (the result is worked out again on every read);
- **S5** 1 to `MAX_NOTED` corrections, all minor: Approved as noted;
- **S6** no correction, but items code could not prove or compare (other than "the architect gives
  no information"), or sizes the architect specifies that break a GV rule (#1839: GV asks the
  architect; nothing is asked of the vendor): Approved as noted, with the note that says which;
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

import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final, Literal

from app.review.assistant.impact import TOP_IMPACT, touches
from app.review.assistant.proof import classified, proof_group

__all__ = [
    "ACTION_REQUIRED_BY",
    "ACTION_WORDS",
    "DISCLAIMER",
    "MAX_NOTED",
    "MINOR_IN",
    "NEXT_STEP",
    "NOT_REVIEWED_NOTE",
    "REVIEW_NEEDED_SHARE",
    "REVIEW_NEEDED_VARIABLE",
    "RULE_NOTE_NOTE",
    "VENDOR_WORDS",
    "Action",
    "CheckState",
    "ReviewAction",
    "changes_fabrication",
    "corrections",
    "item_action",
    "review_action",
    "review_needed_share",
    "stamp_words",
]

type Action = Literal[
    "approved",
    "approved_as_noted",
    "review_needed",
    "revise_resubmit",
    "rejected",
    "not_for_sending",
]
type Rule = Literal["S0", "S1", "S2", "S3", "S4", "RN", "S5", "S6", "S7"]

#: The results as the letter, the cover and the screen print them (AWI's four, and no stamp).
ACTION_WORDS: Final[Mapping[Action, str]] = {
    "approved": "Approved",
    "approved_as_noted": "Approved as noted",
    "review_needed": "Review needed",
    "revise_resubmit": "Revise and resubmit",
    "rejected": "Rejected",
    "not_for_sending": "Check incomplete: not for sending",
}
#: The result as the vendor reads it (the letter, the cloud drawing's cover, the register, the
#: stamp line): the same, except that a check waiting for GV's person is only "Under review",
#: never an approval.
VENDOR_WORDS: Final[Mapping[Action, str]] = {
    **ACTION_WORDS,
    "review_needed": "Under review",
}
#: Who must act now (the submittal log's "ball in court").
ACTION_REQUIRED_BY: Final[Mapping[Action, str]] = {
    "approved": "No action required",
    "approved_as_noted": "Vendor: make the corrections noted",
    "review_needed": "GV: confirm the remaining items",
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
    "review_needed": (
        "Under review: GV will confirm the remaining items and then send its review result. "
        "Please wait for it before fabrication."
    ),
    "rejected": "Please make a new submittal of the specified product.",
    "not_for_sending": "This check did not finish. It is not to be sent.",
}
#: What a Review needed letter tells the vendor in place of the reason (never an approval).
_UNDER_REVIEW_REASON: Final = "GV will confirm the remaining items before giving its review result."
#: S6's note: what was not reviewed, said plainly.
NOT_REVIEWED_NOTE: Final = (
    "Items in the Not-compared list were not reviewed; each is listed with the reason and what is "
    "needed to compare it."
)
#: S6's note when sizes the architect specifies break a GV rule (#1839).
RULE_NOTE_NOTE: Final = (
    'Sizes the architect specifies that break a GV rule are listed under "Questions for the '
    'architect (GV rules)"; GV is confirming them with the architect, and no change is asked of '
    "the vendor."
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
_NO_INFORMATION: Final = frozenset({"nothing_to_compare"})
#: Above this share of all items Not compared, a finished check that would approve is Review
#: needed instead (Anant, 2026-10-22: "more than 10% of all items"). The environment variable
#: `GV_REVIEW_NEEDED_SHARE` (a number from 0 to 1, `Settings.review_needed_share`) changes it.
REVIEW_NEEDED_SHARE: Final = Decimal("0.10")
REVIEW_NEEDED_VARIABLE: Final = "GV_REVIEW_NEEDED_SHARE"
_TENTH: Final = Decimal("0.1")


def review_needed_share(environ: Mapping[str, str] | None = None) -> Decimal:
    """The share of Not compared items above which an approval is Review needed: the environment's
    `GV_REVIEW_NEEDED_SHARE` when it is a number from 0 to 1, else `REVIEW_NEEDED_SHARE`."""
    raw = (os.environ if environ is None else environ).get(REVIEW_NEEDED_VARIABLE, "").strip()
    if not raw:
        return REVIEW_NEEDED_SHARE
    try:
        share = Decimal(raw)
    except ArithmeticError:
        return REVIEW_NEEDED_SHARE
    return share if share.is_finite() and 0 <= share <= 1 else REVIEW_NEEDED_SHARE


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
    """S6 and RN: what was not reviewed."""
    not_compared: int = 0
    """Items Not compared (the reviewer's decided items are no longer counted)."""
    items: int = 0
    """All items: Wrong + Right + Ask the architect + Not compared (decided items included)."""
    disclaimer: str = DISCLAIMER

    @property
    def stamped(self) -> bool:
        """A result the package may be sent with (S0 is not)."""
        return self.action != "not_for_sending"

    @property
    def next_step(self) -> str:
        return NEXT_STEP[self.action]

    @property
    def vendor_words(self) -> str:
        """The result as the vendor reads it ("Under review" for Review needed)."""
        return VENDOR_WORDS[self.action]

    @property
    def vendor_reason(self) -> str:
        """Why, as the vendor reads it: Review needed says only that GV is confirming the
        remaining items (its reason counts what GV's person must decide)."""
        return _UNDER_REVIEW_REASON if self.action == "review_needed" else self.reason

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
            "not_compared": self.not_compared,
            "items": self.items,
            "vendor_words": self.vendor_words,
            "next_step": self.next_step,
            "required_by": self.required_by,
            "disclaimer": self.disclaimer,
        }


def stamp_words(action: ReviewAction, date: str, submittal: str = "", round_no: int = 1) -> str:
    """The stamp line: "GV REVIEW: REVISE AND RESUBMIT. Checked automatically by GV Review on
    2026-10-18. Submittal 25A, round 2." """
    identity = f"Submittal {submittal}, round {round_no}." if submittal else f"Round {round_no}."
    return (
        f"GV REVIEW: {action.vendor_words.upper()}. Checked automatically by GV Review on {date}. "
        f"{identity}"
    )


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _reason(finding: Mapping[str, Any]) -> str:
    """A Not-compared item's reason under the architect-first rules (#1619, `classification`)."""
    return str(classified(finding).reason or "")


def _not_reviewed(findings: Sequence[Mapping[str, Any]]) -> int:
    """Items code could not prove or compare, other than "the architect gives no information"."""
    return sum(
        1
        for finding in findings
        if proof_group(finding) == "not_compared" and _reason(finding) not in _NO_INFORMATION
    )


def _illegible(findings: Sequence[Mapping[str, Any]]) -> bool:
    unreadable = sum(
        1 for f in findings if proof_group(f) == "not_compared" and _reason(f) == "unreadable"
    )
    return unreadable >= _ILLEGIBLE_MIN and unreadable > _ILLEGIBLE_SHARE * len(findings)


def _review_needed(not_compared: int, items: int, share: Decimal) -> bool:
    """More than `share` of all items Not compared (exact: 1 of 10 is not more than 10%)."""
    return items > 0 and Decimal(not_compared) > share * items


def _percent(value: Decimal) -> str:
    return f"{(value * 100).quantize(_TENTH).normalize():f}%"


def _review_needed_reason(not_compared: int, items: int, share: Decimal, noted: int) -> str:
    """ "52 items need a person before this can be approved: 52 of 55 items (95%) were not
    compared, more than the 10% allowed." """
    need = "needs" if not_compared == 1 else "need"
    reason = (
        f"{_plural(not_compared, 'item')} {need} a person before this can be approved: "
        f"{not_compared} of {_plural(items, 'item')} "
        f"({_percent(Decimal(not_compared) / items)}) {'was' if not_compared == 1 else 'were'} "
        f"not compared, more than the {_percent(share)} allowed."
    )
    if noted:
        reason += f" {_plural(noted, 'minor correction')} noted as well."
    return reason


def review_action(
    findings: Iterable[Mapping[str, Any]],
    state: CheckState | None = None,
    share: Decimal | None = None,
) -> ReviewAction:
    """The review result for one final finding list and what the check knew (see the module's
    rules S0 to S7 and RN; the first that matches wins). `share`: the Review needed threshold
    (`review_needed_share()` when not given)."""
    given = list(findings)
    state = state or CheckState()
    sent = corrections(given, state.closed)
    fabrication = sum(1 for finding in sent if changes_fabrication(finding))
    not_compared = sum(1 for finding in given if proof_group(finding) == "not_compared")
    items = len(given)

    def result(action: Action, rule: Rule, reason: str, note: str | None = None) -> ReviewAction:
        return ReviewAction(
            action=action,
            rule=rule,
            words=ACTION_WORDS[action],
            reason=reason,
            corrections=len(sent),
            fabrication=fabrication,
            note=note,
            not_compared=not_compared,
            items=items,
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
    limit = review_needed_share() if share is None else share
    unreviewed = _not_reviewed(given)
    asked = sum(1 for finding in given if proof_group(finding) == "rule_note")
    if _review_needed(not_compared, items, limit):
        notes = [NOT_REVIEWED_NOTE]
        if asked:
            notes.append(RULE_NOTE_NOTE)
        return result(
            "review_needed",
            "RN",
            _review_needed_reason(not_compared, items, limit, len(sent)),
            " ".join(notes),
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
    if unreviewed or asked:
        parts = [f"{_plural(unreviewed, 'item')} not reviewed by code"] if unreviewed else []
        if asked:
            parts.append(
                f"{_plural(asked, 'size')} the architect specifies "
                f"{'breaks' if asked == 1 else 'break'} a GV rule (GV asks the architect)"
            )
        notes = [NOT_REVIEWED_NOTE] if unreviewed else []
        if asked:
            notes.append(RULE_NOTE_NOTE)
        return result(
            "approved_as_noted",
            "S6",
            f"No correction; {'; '.join(parts)}.",
            " ".join(notes),
        )
    return result("approved", "S7", "No correction: every item compared matches.")
