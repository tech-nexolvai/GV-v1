"""The send-ready package (#1622): everything GV sends the vendor after a full check, made by code.

claude.ai answers in prose, and its numbering drifts from any markup. A full check here ends with
one download the vendor can be sent as it is, with no editing:

1. **The GV cloud drawing** (`proof_drawing`): red clouds at the wrong values code proved, its cover
   stamped with the review result and the disclaimer (`review_action`).
2. **The comment letter** (`comment_letter.comment_letter_pdf`): a transmittal-style letter with the
   submittal's identity, the review result and one entry per comment.
3. **The issue register** (XLSX, every cell text): the submittal sheet, then one row per item,
   Wrong / Ask the architect (GV rule) / Not compared / Right, with a blank "Vendor response"
   column; and, when there are any, the "Architect questions" sheet (#1839): GV's ready question
   for each size the architect specifies that breaks a GV rule. A Wrong row's "Comment" is
   the proof card's own sentence (`proof_words`, #1804), its "Why" how it was checked.
4. **The Not-compared list** (PDF and workbook), when the full check made it. GV's own check report
   never goes in (`internal_report`).

**Comment numbers are stable** (`comment_labels`): "C-01", "C-02", … from the job tracker's item
identity (`job_tracker.comment_numbers`, #1591), so an item keeps its number across checks and
rounds; the cloud drawing, the letter and the register all print the same C-number. Biggest first
(`impact.by_impact`) stays the ORDER they are listed in, not the number.

**Round 2 in the trade's words**: a previous comment fixed is "Closed", one still wrong is "Previous
comment not addressed", and a comment first seen this round is "New".

The status words follow the owner's rule (2026-10-18): proven differences are **Wrong** (the
clouds); everything code could not prove or compare is **Not compared**; an item that matches is
**Right** (one the reviewer marked not a problem is Right too, and says so). A size the architect
specifies that breaks a GV rule, printed the same by the vendor (#1839), is **Ask the architect
(GV rule)**: decided, never a comment to the vendor; the letter lists it under "Questions for the
architect (GV rules)" and says no change is asked of the vendor.

Pure code here except `package_context` and `make_package_file`, which read the chat's workspace;
the letter is drawn by `comment_letter`, loaded by name, because `app/api/` must not reach a PDF
renderer by import (`tests/api/test_no_heavy_work.py`). No AI call.
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import re
import zipfile
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from typing import Any, Final, Literal

from app.review.assistant import not_compared, proof_words
from app.review.assistant.impact import by_impact
from app.review.assistant.proof import (
    NO_REQUIREMENT_REASON,
    REVIEWER,
    governs,
    proof_group,
    proof_of,
    should_be_value,
    stated_requirement,
    vendor_own,
    vendor_value,
)
from app.review.assistant.review_action import (
    CheckState,
    ReviewAction,
    changes_fabrication,
    corrections,
    item_action,
    review_action,
    stamp_words,
)
from app.review.assistant.rule_notes import HEADING as RULE_NOTE_HEADING
from app.review.assistant.rule_notes import SHORT_WORDS as RULE_NOTE_WORDS
from app.review.assistant.vendor_text import item_name, reviewer_right, vendor_text

__all__ = [
    "LETTER_KIND",
    "PACKAGE_KIND",
    "QUESTION_COLUMNS",
    "QUESTION_SHEET",
    "REGISTER_COLUMNS",
    "REGISTER_KIND",
    "REPORT_IN_PACKAGE",
    "ROUND_WORDS",
    "STATUS_WORDS",
    "XLSX_MIME",
    "ZIP_MIME",
    "Correction",
    "Letter",
    "Member",
    "PackageContext",
    "PackageIdentity",
    "Previous",
    "Question",
    "Status",
    "check_state",
    "comment_labels",
    "corrections_of",
    "incomplete_reason",
    "internal_report",
    "labelled",
    "letter_of",
    "make_package_file",
    "package_context",
    "package_zip",
    "questions_of",
    "register_csv",
    "register_rows",
    "register_status",
    "register_xlsx",
    "requires_words",
    "set_round",
    "submittal_label",
    "submittal_rows",
]

_log = logging.getLogger(__name__)

type Status = Literal["wrong", "right", "rule_note", "not_compared"]
#: The register's status words.
STATUS_WORDS: Final[Mapping[Status, str]] = {
    "wrong": "Wrong",
    "right": "Right",
    "rule_note": RULE_NOTE_WORDS,
    "not_compared": "Not compared",
}
#: The register's "Architect questions" sheet (#1839): one row per size the architect specifies
#: that breaks a GV rule.
QUESTION_SHEET: Final = "Architect questions"
QUESTION_COLUMNS: Final = (
    "No.",
    "Page",
    "Area",
    "Product",
    "Item",
    "Architect's size",
    "Vendor value",
    "GV rule",
    "In force",
    "Where it comes from",
    "Question for the architect",
)
#: The round-2 check's statuses (`round_check`) in the trade's words.
ROUND_WORDS: Final[Mapping[str, str]] = {
    "fixed": "Closed",
    "still_wrong": "Previous comment not addressed",
    "also_in_round_1": "Previous comment not addressed",
    "new": "New",
    "not_compared": "Could not be compared by code",
}
#: A finding's stored `round_status` (`job_tracker.ROUND_STATUS_WORDS`) as a round-2 status.
_STORED_ROUND: Final[Mapping[str, str]] = {
    "fixed_in_later_round": "fixed",
    "fixed": "fixed",
    "still_wrong": "still_wrong",
    "new_in_later_round": "new",
    "new": "new",
}
#: The files' names end with these (" - Comment letter.pdf", …); the screen finds the zip by its.
LETTER_KIND: Final = "Comment letter"
REGISTER_KIND: Final = "Issue register"
PACKAGE_KIND: Final = "GV review package"
#: The check report's other name: a file so named is GV's own and never goes in the package.
REPORT_IN_PACKAGE: Final = "Check report"
ZIP_MIME: Final = "application/zip"
XLSX_MIME: Final = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
#: The register's comment sheet, in this order.
REGISTER_COLUMNS: Final = (
    "Comment no.",
    "Page",
    "Area",
    "Product",
    "Item",
    "Status",
    "Action",
    "Comment",
    "Vendor value",
    "Should be",
    "Reference",
    "Why",
    "Impact rank",
    "Round history",
    "Vendor response",
)
#: Where each column sits in a register row (`register_rows`).
_COL: Final[Mapping[str, int]] = {name: at for at, name in enumerate(REGISTER_COLUMNS)}
#: What governs, as the letter's "… requires" line names it.
_WHO: Final[Mapping[str, str]] = {
    "rules": "GV rule",
    "brand_prototype": "Brand prototype",
}
#: Why a check did not finish (the message's `stopped`), for S0.
_STOPPED_WORDS: Final[Mapping[str, str]] = {
    "cost_cap": "it stopped at its fixed price",
    "turn_limit": "it reached its step limit",
    "cancelled": "it was stopped before the end",
    "error": "it ended with an error",
    "interrupted": "it was interrupted",
}


# ---- who and what -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PackageIdentity:
    """The submittal's identity, from the database (`package_store.identity`)."""

    project: str = ""
    vendor: str = ""
    submittal: int | None = None
    """The drawing set's place among its project's sets (1, 2, …), else `None`."""
    received: str = ""
    """The date the latest drawings were received (YYYY-MM-DD)."""
    products: tuple[str, ...] = ()
    spec_section: str = ""
    """The specification section, when GV knows it (blank otherwise)."""


def submittal_label(number: int | None, round_no: int) -> str:
    """ "25" for the first round, "25A" for the first resubmittal, "25B" for the next; "" when
    the set has no number."""
    if number is None:
        return ""
    letters = ""
    n = max(0, round_no - 1)
    while n > 0:
        n, rest = divmod(n - 1, 26)
        letters = chr(ord("A") + rest) + letters
    return f"{number}{letters}"


def incomplete_reason(stopped: str | None, coverage: Mapping[str, Any] | None) -> str | None:
    """Why a full check did not finish, in plain words (S0), else `None`: it stopped early, or
    pages were never reached."""
    if stopped and stopped != "done":
        return _STOPPED_WORDS.get(stopped, "it did not finish")
    missing = int((coverage or {}).get("not_opened") or 0)
    if missing > 0:
        return f"{missing} {'page was' if missing == 1 else 'pages were'} not reached"
    return None


def register_status(finding: Mapping[str, Any], closed: Sequence[str] = ()) -> Status:
    """Wrong (clouded), Right (matches, the reviewer marked it not a problem, or a previous
    comment this round fixed: `closed`), Ask the architect (the architect's size breaks a GV rule,
    #1839), else Not compared."""
    group = proof_group(finding)
    if group == "clouded" and str(finding.get("id") or "") in closed:
        return "right"
    if group == "clouded":
        return "wrong"
    if group in ("right", "dismissed"):
        return "right"
    if group == "rule_note":
        return "rule_note"
    return "not_compared"


def comment_labels(
    findings: Sequence[Mapping[str, Any]], numbers: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Each comment's number (finding id -> "C-07"): the stable numbers given (from the set's
    earlier checks, `package_store.numbering`), else this list's own (`job_tracker.comment_numbers`
    of this one check). Only the comments (the clouded items) are numbered: a Right or Not-compared
    finding of a tracked item gets none. Each comment of the list has its own number: one with
    none (an item the reviewer agreed that was never Wrong) or one sharing its tracker item with a
    bigger comment of the same list (the same words on another page) gets the next free number,
    biggest first."""
    tracker = import_module("app.review.assistant.job_tracker")
    if numbers is None:
        from datetime import UTC, datetime
        from uuid import UUID

        alone = tracker.TrackerAnswer(
            package_id=UUID(int=0),
            chat_id=UUID(int=0),
            message_id=UUID(int=0),
            at=datetime.now(UTC),
            kind="full_check",
            title="",
            stopped=None,
            findings=list(findings),
        )
        found = tracker.comment_numbers([alone])
        numbers = {finding_id: label for (_message, finding_id), label in found.items()}
    out = {str(k): str(v) for k, v in numbers.items()}
    used = [int(label[2:]) for label in out.values() if label[2:].isdigit()]
    after = max(used, default=0)
    # Every comment of this list has its own number: one with none, or one whose item the tracker
    # links to an earlier comment of the same list (the same words on another page), gets the next
    # free number, biggest first.
    # Only the comments carry a number (a Right or Not-compared item of the same tracked item is
    # never "C-04" on the screen or the drawing).
    given: set[str] = set()
    kept: dict[str, str] = {}
    for finding in corrections(by_impact(findings)):
        finding_id = str(finding.get("id") or "")
        if not finding_id:
            continue
        if finding_id not in out or out[finding_id] in given:
            after += 1
            out[finding_id] = str(tracker.comment_label(after))
        given.add(out[finding_id])
        kept[finding_id] = out[finding_id]
    return kept


def labelled(
    findings: Sequence[Mapping[str, Any]], labels: Mapping[str, str]
) -> list[dict[str, Any]]:
    """The findings (copied) with their comment number as `comment_no` (the cloud drawing prints
    it on the cloud and the note)."""
    out: list[dict[str, Any]] = []
    for finding in findings:
        copy = dict(finding)
        label = labels.get(str(finding.get("id") or ""))
        if label:
            copy["comment_no"] = label
        out.append(copy)
    return out


def requires_words(finding: Mapping[str, Any]) -> str:
    """The letter's line: "Architect requires 37"", "GV rule requires 1" to 2"", "The vendor's own
    parts add up to 80 1/8"". Only ever a stated requirement (`proof.stated_requirement`, through
    the guard on internal words): a comment without one is never made (`proof_group`)."""
    value = stated_requirement(finding) or "the value on the cloud drawing's note"
    if governs(finding) == REVIEWER:
        # A value GV's reviewer typed (#1803): never credited to the architect or to the
        # vendor's own arithmetic.
        return f"GV's reviewer requires {value}"
    own = vendor_own(finding)
    if own == "sum":
        return f"The vendor's own parts add up to {value}"
    if own == "span":
        return f"The vendor's other dimension of the same span shows {value}"
    source = str(finding.get("source") or "architect")
    settled = finding.get("settled")
    if isinstance(settled, Mapping) and settled.get("method") == "gv_limits":
        source = "rules"
    return f"{_WHO.get(source, 'Architect')} requires {value}"


def set_round(page_map: Mapping[str, Any] | None, findings: Sequence[Mapping[str, Any]]) -> int:
    """The drawing set's round: the latest round of the vendor's drawings in the page map (GV's
    headings), else the latest a finding names, else 1."""
    rounds: list[int] = []
    for entry in (page_map or {}).get("pages") or []:
        if not isinstance(entry, Mapping) or entry.get("skip"):
            continue
        for half in entry.get("halves") or []:
            if isinstance(half, Mapping) and half.get("role") == "vendor":
                value = half.get("round")
                if isinstance(value, int) and not isinstance(value, bool):
                    rounds.append(value)
    for finding in findings:
        value = finding.get("round")
        if isinstance(value, int) and not isinstance(value, bool):
            rounds.append(value)
    return max([1, *rounds])


def check_state(
    *,
    incomplete: str | None,
    page_map: Mapping[str, Any] | None,
    rounds: Any,
    comments: Collection[str] | None = None,
) -> CheckState:
    """What the review result needs beyond the findings: did the check finish (S0), the areas the
    architect draws with no vendor sheet (S1, `vendor_consistency.completeness`), and the previous
    comments still wrong (S3, the round-2 check). S3 counts the same list the letter shows: the
    round-1 comments (`comments`: this check's clouded finding ids) still wrong, one verdict per
    item (a round-2 item already wrong in round 1 is its comment's, counted once)."""
    consistency = import_module("app.review.assistant.vendor_consistency")
    missing = sum(
        1
        for gap in consistency.completeness(page_map)
        if str(gap.get("what") or "").endswith("no vendor sheet")
    )
    items = list(getattr(rounds, "items", None) or [])
    still = sum(
        1
        for item in items
        if item.status == "still_wrong" and (comments is None or item.finding_id in comments)
    )
    closed = tuple(item.finding_id for item in items if item.status == "fixed")
    return CheckState(incomplete=incomplete, missing_areas=missing, still_open=still, closed=closed)


# ---- the context of one package ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PackageContext:
    """Everything one package is made from besides the findings, worked out once for the cloud
    drawing, the letter, the register and the screen."""

    identity: PackageIdentity
    date: str
    """The date reviewed (YYYY-MM-DD)."""
    round_no: int
    rounds: Any
    """The round-2 check (`round_check.RoundReport`), or `None`."""
    state: CheckState
    labels: Mapping[str, str]
    """Finding id -> comment number ("C-07")."""
    areas: Mapping[int, str] = field(default_factory=dict)
    """Page -> the area's title (the page map's)."""

    @property
    def submittal(self) -> str:
        return submittal_label(self.identity.submittal, self.round_no)

    def action(self, findings: Sequence[Mapping[str, Any]]) -> ReviewAction:
        return review_action(findings, self.state)

    def stamp(self, action: ReviewAction) -> str:
        return stamp_words(action, self.date, self.submittal, self.round_no)

    def block(self) -> dict[str, Any]:
        """The answer's `package` block: what the screen and a remade package read again."""
        return {
            "state": self.state.as_dict(),
            "numbers": dict(self.labels),
            "submittal": self.submittal,
            "round": self.round_no,
        }


def _areas(page_map: Mapping[str, Any] | None, main_file: str) -> dict[int, str]:
    scope = import_module("app.review.assistant.chat_scope")
    out: dict[int, str] = {}
    for title, pages in scope.area_pages(page_map, main_file).values():
        for page in pages:
            out.setdefault(int(page), str(title))
    return out


def package_context(
    workspace: Any,
    findings: Sequence[Mapping[str, Any]],
    identity: PackageIdentity,
    *,
    numbers: Mapping[str, str] | None = None,
    incomplete: str | None = None,
    today: str | None = None,
) -> PackageContext:
    """The context of a full check's package: the page map's round and areas, the round-2 check
    (worked out once here), the review result's state and the comment numbers. Raises
    `LookupError` when the chat has no copy of the set's PDFs."""
    from datetime import UTC, datetime

    chat = import_module("app.review.assistant.chat_findings")
    plan = import_module("app.review.assistant.full_check_plan")
    main = chat.main_upload(chat.read_listing(workspace))
    if main is None or not (workspace.uploads / str(main["name"])).is_file():
        raise LookupError("no uploads")
    name = str(main["name"])
    pack = workspace.set_pack
    page_map = plan.read_page_map(pack, assume=True, main_file=name) if pack is not None else None
    rounds = chat.full_check_rounds(workspace, findings)
    unstated = [
        str(f.get("id")) for f in findings if proof_of(f).get("reason") == NO_REQUIREMENT_REASON
    ]
    if unstated:
        _log.warning(
            "send-ready package: %d GV rule results without a stated requirement, not comments: %s",
            len(unstated),
            ", ".join(unstated[:20]),
        )
    return PackageContext(
        identity=identity,
        date=today or datetime.now(UTC).strftime("%Y-%m-%d"),
        round_no=set_round(page_map, findings),
        rounds=rounds,
        state=check_state(
            incomplete=incomplete,
            page_map=page_map,
            rounds=rounds,
            comments={str(f.get("id")) for f in findings if proof_group(f) == "clouded"},
        ),
        labels=comment_labels(findings, numbers),
        areas=_areas(page_map, name),
    )


# ---- the letter ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Correction:
    label: str
    """The comment number, the same on the cloud ("C-07")."""
    page: int | None
    item: str
    vendor_shows: str
    requires: str
    source: str
    action: str
    """"Correct as noted" or "Submit specified item"."""
    round_word: str
    """"New" or "Previous comment not addressed" on a later round, else ""."""
    fabrication: bool
    finding_id: str
    sentence: tuple[tuple[str, str], ...] = ()
    """The one sentence (`proof_words.sentence_runs`): the proof card's, piece by piece, the
    vendor's value marked `vendor` and the should-be `should` (#1804)."""
    sum_line: str = ""
    """The sum written out when it is the proof (`2" + 86 1/2" + 2" = 90 1/2", not the 88 1/2"
    printed`), or GV's rule's working; else ""."""
    checked: str = ""
    """How it was checked, in the vendor's words ("Proved by code: …", "Decided by GV's
    reviewer")."""

    @property
    def words(self) -> str:
        """The sentence as one line."""
        return "".join(text for text, _role in self.sentence)


@dataclass(frozen=True, slots=True)
class Previous:
    """A previous round's comment and what this round did with it."""

    label: str
    item: str
    words: str
    """"Closed", "Previous comment not addressed", "Could not be compared by code"."""
    detail: str


@dataclass(frozen=True, slots=True)
class Question:
    """A size the architect specifies that breaks a GV rule, printed the same by the vendor
    (#1839): one line of the letter's "Questions for the architect (GV rules)" and one row of the
    register's "Architect questions" sheet."""

    page: int | None
    area: str
    product: str
    item: str
    architect: str
    vendor: str
    rule: str
    settings: str
    origin: str
    question: str
    finding_id: str = ""

    def row(self, number: int) -> list[str]:
        return [
            str(number),
            "" if self.page is None else str(self.page),
            self.area,
            self.product,
            self.item,
            self.architect,
            self.vendor,
            self.rule,
            self.settings,
            self.origin,
            self.question,
        ]


def questions_of(
    findings: Sequence[Mapping[str, Any]],
    *,
    areas: Mapping[int, str] | None = None,
    product_label: Any = None,
) -> list[Question]:
    """The sizes the architect specifies that break a GV rule (`proof_group` "rule_note", #1839),
    by page, each with GV's ready question for the architect. Every cell goes through the
    vendor-text guard: the letter and the register are sent to the vendor."""
    out: list[Question] = []
    for finding in findings:
        if proof_group(finding) != "rule_note":
            continue
        note = proof_of(finding).get("rule_note")
        if not isinstance(note, Mapping):
            continue
        page = _page_of(finding)
        category = str(finding.get("category") or "other")
        out.append(
            Question(
                page=page,
                area=vendor_text((areas or {}).get(page or 0, ""), "") or "",
                product=str(product_label(category)) if product_label is not None else category,
                item=_item(finding),
                architect=vendor_text(note.get("architect"), "") or "",
                vendor=vendor_text(vendor_value(finding), "") or "",
                rule=vendor_text(f"GV rule: {note.get('name')}", "") or "",
                settings=vendor_text(note.get("settings"), "") or "",
                origin=vendor_text(note.get("origin"), "") or "",
                question=vendor_text(note.get("question"), "") or "",
                finding_id=str(finding.get("id") or ""),
            )
        )
    out.sort(key=lambda q: (q.page or 0, q.item))
    return out


@dataclass(frozen=True, slots=True)
class Letter:
    identity: PackageIdentity
    drawing_set: str
    submittal: str
    round_no: int
    date: str
    reviewers: tuple[str, ...]
    action: ReviewAction
    stamp: str
    corrections: tuple[Correction, ...]
    previous: tuple[Previous, ...]
    not_compared: int
    not_compared_where: str
    """Where the Not-compared items are listed, in the letter's words."""
    enclosures: tuple[str, ...]
    products: str = ""
    """The products checked, in the registry's words ("Countertops and Cabinets")."""
    questions: tuple[Question, ...] = ()
    """The sizes the architect specifies that break a GV rule (#1839): GV asks the architect;
    nothing is asked of the vendor."""

    @property
    def reviewer_line(self) -> str:
        line = "Checked automatically by GV Review"
        return f"{line}, and reviewed by {', '.join(self.reviewers)}" if self.reviewers else line

    @property
    def previous_line(self) -> str | None:
        """ "3 closed, 1 not addressed" (with any code could not compare)."""
        if not self.previous:
            return None
        closed = sum(1 for p in self.previous if p.words == ROUND_WORDS["fixed"])
        still = sum(1 for p in self.previous if p.words == ROUND_WORDS["still_wrong"])
        line = f"{closed} closed, {still} not addressed"
        grey = len(self.previous) - closed - still
        if grey:
            line += f", {grey} could not be compared by code"
        return line


def _item(finding: Mapping[str, Any]) -> str:
    """An item's name as the vendor reads it (the guard, `vendor_text.item_name`: never a record
    id), else "Item on page N" when there are no words to name it by."""
    page = _page_of(finding)
    fallback = f"Item on page {page}" if page is not None else "Item"
    return item_name(dict(finding)) or fallback


def _page_of(finding: Mapping[str, Any]) -> int | None:
    page = finding.get("page")
    return page if isinstance(page, int) and not isinstance(page, bool) else None


def corrections_of(
    findings: Sequence[Mapping[str, Any]],
    labels: Mapping[str, str],
    rounds: Any = None,
    closed: Sequence[str] = (),
) -> list[Correction]:
    """The letter's comments: the clouded items (less the previous comments this round fixed),
    biggest first, each with its comment number; on a later round each says "New" or "Previous
    comment not addressed"."""
    by_round = rounds.by_finding() if rounds is not None else {}
    out: list[Correction] = []
    for finding in corrections(by_impact(findings), closed):
        finding_id = str(finding.get("id") or "")
        later = by_round.get(finding_id)
        round_word = ""
        if later is not None and (
            later.status in ("new", "still_wrong")
            or (later.status == "also_in_round_1" and getattr(later, "twin", None))
        ):
            # "Previous comment not addressed" only where there was a previous comment: a round-2
            # item already wrong in round 1, with round 1's comment at its place.
            round_word = ROUND_WORDS[later.status]
        out.append(
            Correction(
                label=labels.get(finding_id, ""),
                page=_page_of(finding),
                item=_item(finding),
                vendor_shows=vendor_text(vendor_value(finding), "see the cloud drawing") or "",
                requires=requires_words(finding),
                source=vendor_text(proof_words.reference_words(finding), "GV") or "GV",
                action=item_action(finding),
                round_word=round_word,
                fabrication=changes_fabrication(finding),
                finding_id=finding_id,
                sentence=tuple(proof_words.sentence_runs(finding)),
                sum_line=proof_words.sum_line(finding) or proof_words.worked_line(finding) or "",
                checked=proof_words.checked_words(finding),
            )
        )
    return out


def _previous(rounds: Any, labels: Mapping[str, str], clouded: set[str]) -> list[Previous]:
    """Each previous round's comment the round-2 check followed (not the new ones), when it is a
    cloud on this drawing (`clouded`: finding ids), so its number is the cloud's."""
    out: list[Previous] = []
    for item in sorted(
        getattr(rounds, "items", None) or [], key=lambda i: labels.get(i.finding_id, "~")
    ):
        if item.status not in ("fixed", "still_wrong", "not_compared"):
            continue
        if item.finding_id not in clouded:
            continue
        if item.status == "fixed":
            detail = f"now {item.now}" if item.now else ""
        elif item.status == "still_wrong":
            detail = f"vendor still shows {item.now or item.was}; requires {item.should_be}"
        else:
            detail = round_reason_words(item.reason)
        out.append(
            Previous(
                label=labels.get(item.finding_id, ""),
                item=vendor_text(item.item, "Previous comment") or "Previous comment",
                words=ROUND_WORDS[item.status],
                detail=vendor_text(detail, "") or "",
            )
        )
    return out


#: The round-2 check's working words (`round_check`): measurements in points, "code would not
#: guess". The vendor reads why in plain words (#1804).
_ROUND_WORKING: Final = re.compile(
    r"\s*\((?:its values miss by|\d+)[^)]*\)|;\s*code would not guess|\s*\(\d+\)", re.IGNORECASE
)


def round_reason_words(reason: object) -> str:
    """Why a previous comment could not be followed, as the vendor reads it: "round 2 is a
    different view or redrawn, so it could not be lined up with round 1"."""
    words = _ROUND_WORKING.sub("", " ".join(str(reason or "").split())).strip(" ;,")
    if words.startswith("round 2 is a different view or redrawn"):
        return "round 2 is a different view or redrawn, so it could not be lined up with round 1"
    if words.startswith("too few values printed in both rounds"):
        return "too few values are printed in both rounds to line them up"
    return vendor_text(words, "") or ""


def letter_of(
    findings: Sequence[Mapping[str, Any]],
    context: PackageContext,
    *,
    drawing_set: str,
    reviewers: Sequence[str] = (),
    enclosures: Sequence[str] = (),
    not_compared_list: bool = False,
) -> Letter:
    """The comment letter for one final finding list. `not_compared_list`: the package holds the
    Not-compared list (else the items are on the cloud drawing's notes pages)."""
    action = context.action(findings)
    return Letter(
        identity=context.identity,
        drawing_set=drawing_set,
        submittal=context.submittal,
        round_no=context.round_no,
        date=context.date,
        reviewers=tuple(reviewers),
        action=action,
        stamp=context.stamp(action),
        corrections=tuple(
            corrections_of(findings, context.labels, context.rounds, context.state.closed)
        ),
        previous=(
            tuple(
                _previous(
                    context.rounds,
                    context.labels,
                    {str(f.get("id")) for f in findings if proof_group(f) == "clouded"},
                )
            )
            if context.round_no >= 2
            else ()
        ),
        not_compared=sum(
            1 for f in findings if register_status(f, context.state.closed) == "not_compared"
        ),
        not_compared_where=(
            "the enclosed Not-compared list"
            if not_compared_list
            else "the notes pages at the end of the GV cloud drawing"
        ),
        enclosures=tuple(enclosures),
        products=(
            str(import_module("vocabulary.product_registry").registry().words(products))
            if (products := context.identity.products)
            else ""
        ),
        questions=tuple(questions_of(findings, areas=context.areas)),
    )


# ---- the register -------------------------------------------------------------------------------


def _round_history(
    finding: Mapping[str, Any], status: Status, by_round: Mapping[str, Any], set_round_no: int
) -> str:
    """ "Round 1: wrong · Round 2: Closed", from the round-2 check (else the finding's stored round
    status), else the item's own round."""
    word = STATUS_WORDS[status].lower()
    given = finding.get("round")
    known = given if isinstance(given, int) and not isinstance(given, bool) else None
    later = by_round.get(str(finding.get("id")))
    raw = str(later.status) if later is not None else ""
    if not raw:
        raw = _STORED_ROUND.get(str(finding.get("round_status") or ""), "")
    if raw in ("new", "also_in_round_1"):
        return f"Round {known or max(set_round_no, 2)}: {ROUND_WORDS[raw]}"
    if raw == "not_compared":
        return f"Round {known or set_round_no}: {word} · round check: {ROUND_WORDS[raw]}"
    if raw in ("fixed", "still_wrong"):
        # A previous comment: it was Wrong in its round, whatever it is now.
        return f"Round {known or 1}: wrong · Round {(known or 1) + 1}: {ROUND_WORDS[raw]}"
    return f"Round {known or set_round_no}: {word}"


def _proof_words(finding: Mapping[str, Any], status: Status) -> str:
    if status == "wrong":
        # The proof card's words (#1804): never the reviewer's login or the check's machinery.
        return proof_words.checked_words(finding)
    if proof_group(finding) == "dismissed":
        return reviewer_right(finding)
    if status == "right" and proof_group(finding) == "clouded":
        return "Closed: this round prints what it should be"
    if status == "right":
        return "Matches the drawings"
    if status == "rule_note":
        # #1839: the architect's size breaks a GV rule; GV asks the architect, not the vendor.
        note = proof_of(finding).get("rule_note") or {}
        return (
            f"The architect specifies {note.get('architect')}; {note.get('standard')}. GV is "
            "confirming the size with the architect; no change is asked of the vendor."
        )
    # The Not-compared list's own reason for the item (#1766): the two files say the same.
    listed = not_compared.rows([finding])
    return not_compared.vendor_reason(listed[0]) if listed else "Not compared by code."


def _item_action_of(finding: Mapping[str, Any], status: Status) -> str:
    if status == "wrong":
        return item_action(finding)
    if status == "rule_note":
        return "None (GV asks the architect)"
    return "None" if status == "right" else "Not reviewed"


def register_rows(
    findings: Sequence[Mapping[str, Any]],
    context: PackageContext,
    *,
    product_label: Any = None,
) -> list[list[str]]:
    """One row per item (`REGISTER_COLUMNS`): Wrong first (biggest first, each with its comment
    number), then Ask the architect (GV rule, #1839), then Not compared, then Right, each biggest
    first. Impact rank: the row's place in
    that order (1 = matters most), Right rows none. "Vendor response" is left blank for the
    vendor. Every cell is text and vendor-facing (no internal "check before sending" words)."""
    ordered = by_impact(findings)
    by_round = context.rounds.by_finding() if context.rounds is not None else {}
    closed = context.state.closed
    groups: dict[Status, list[list[str]]] = {
        "wrong": [],
        "rule_note": [],
        "not_compared": [],
        "right": [],
    }
    for finding in ordered:
        status = register_status(finding, closed)
        page = _page_of(finding)
        category = str(finding.get("category") or "other")
        finding_id = str(finding.get("id") or "")
        # The comment number is shown where the item is (or was) a comment to the vendor: a Wrong
        # row, or a previous comment this round closed.
        commented = status == "wrong" or finding_id in closed
        wrong = status == "wrong"
        groups[status].append(
            [
                context.labels.get(finding_id, "") if commented else "",
                "" if page is None else str(page),
                context.areas.get(page or 0, ""),
                str(product_label(category)) if product_label is not None else category,
                " ".join(str(finding.get("item") or "").split()),
                STATUS_WORDS[status],
                _item_action_of(finding, status),
                _comment_words(finding) if wrong else "",
                vendor_value(finding),
                (
                    (proof_words.should_text(finding) if wrong else should_be_value(finding))
                    if status not in ("not_compared", "rule_note")
                    else ""
                ),
                (
                    proof_words.reference_words(finding)
                    if wrong
                    else _rule_reference(finding) if status == "rule_note" else governs(finding)
                ),
                _proof_words(finding, status),
                "",
                _round_history(finding, status, by_round, context.round_no),
                "",
            ]
        )
    rows = [*groups["wrong"], *groups["rule_note"], *groups["not_compared"], *groups["right"]]
    ranked = len(groups["wrong"]) + len(groups["rule_note"]) + len(groups["not_compared"])
    for rank, row in enumerate(rows[:ranked], start=1):
        row[_COL["Impact rank"]] = str(rank)
    # The guard on internal words (#1622): every cell is vendor-facing.
    guarded = ("Area", "Product", "Comment", "Vendor value", "Should be", "Reference", "Why",
               "Round history")  # fmt: skip
    for row in rows:
        row[_COL["Item"]] = _item(
            {"item": row[_COL["Item"]], "page": int(row[1]) if row[1] else None}
        )
        for name in guarded:
            row[_COL[name]] = vendor_text(row[_COL[name]], "") or ""
    return rows


def _rule_reference(finding: Mapping[str, Any]) -> str:
    """A rule note's reference (#1839): the architect's size, and the GV rule it breaks."""
    note = proof_of(finding).get("rule_note") or {}
    return f"architect {note.get('architect')}; GV rule: {note.get('name')} {note.get('settings')}"


def _comment_words(finding: Mapping[str, Any]) -> str:
    """The register's comment: the proof card's one sentence, then its sum or rule working
    (#1804), so the register, the letter and the card say the same."""
    said = proof_words.sentence(finding)
    worked = proof_words.sum_line(finding) or proof_words.worked_line(finding)
    return (
        f"{said} Sum: {worked}."
        if worked and proof_words.sum_line(finding)
        else (f"{said} Worked out: {worked}." if worked else said)
    )


def submittal_rows(letter: Letter) -> list[list[str]]:
    """The register's first sheet: the submittal's identity, its result and the disclaimer."""
    identity = letter.identity
    rows = [
        ["Project", identity.project],
        ["Vendor", identity.vendor],
        ["Submittal", letter.submittal],
        ["Spec section", identity.spec_section],
        ["Drawing set", letter.drawing_set],
        ["Round", str(letter.round_no)],
        ["Received", identity.received],
        ["Reviewed", letter.date],
        # The vendor's words ("Under review" while GV's person decides the rest).
        ["Review result", letter.action.vendor_words],
        ["Why", letter.action.vendor_reason],
        ["Action required by", letter.action.required_by],
        ["Reviewer", letter.reviewer_line],
        ["Comments", str(len(letter.corrections))],
        ["Not compared", str(letter.not_compared)],
    ]
    if letter.questions:
        rows.append([RULE_NOTE_HEADING, str(len(letter.questions))])
    if letter.previous_line:
        rows.append(["Previous comments", letter.previous_line])
    rows += [["Stamp", letter.stamp], ["Disclaimer", letter.action.disclaimer]]
    return rows


def register_xlsx(
    rows: Sequence[Sequence[str]],
    letter: Letter,
    questions: Sequence[Question] | None = None,
) -> bytes:
    """The issue register workbook: "Submittal" (identity, result, disclaimer), "Comments" (the
    rows) and, when there are any, "Architect questions" (#1839: `questions`, else the letter's),
    every cell text (`workbook.write_xlsx`). Raises `ModuleNotFoundError` without openpyxl."""
    workbook = import_module("app.review.assistant.workbook")
    asked = list(letter.questions if questions is None else questions)
    sheets: list[tuple[str, Sequence[str], Sequence[Sequence[str]]]] = [
        ("Submittal", ("Field", "Value"), submittal_rows(letter)),
        ("Comments", REGISTER_COLUMNS, rows),
    ]
    if asked:
        sheets.append(
            (
                QUESTION_SHEET,
                QUESTION_COLUMNS,
                [question.row(n) for n, question in enumerate(asked, start=1)],
            )
        )
    data: bytes = workbook.write_xlsx(sheets)
    return data


def register_csv(rows: Sequence[Sequence[str]]) -> bytes:
    """The register's comments as CSV (the fallback without openpyxl): formulas defused."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(REGISTER_COLUMNS)
    for row in rows:
        writer.writerow(["'" + c if c.startswith(("=", "+", "-", "@")) else c for c in row])
    return out.getvalue().encode("utf-8-sig")


# ---- the zip ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Member:
    name: str
    """The file's name inside the zip."""
    path: Path


def package_zip(members: Sequence[Member], out: Path, date: str) -> tuple[int, str]:
    """The zip of `members` (in order, names kept unique), with every entry dated `date`
    (YYYY-MM-DD) so the same files give the same bytes. Returns its size and sha256."""
    year, month, day = (int(part) for part in date.split("-"))
    seen: set[str] = set()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for member in members:
            name = member.name
            stem, dot, ending = name.rpartition(".")
            n = 2
            while name in seen:
                name = f"{stem} ({n}).{ending}" if dot else f"{member.name} ({n})"
                n += 1
            seen.add(name)
            info = zipfile.ZipInfo(name, date_time=(year, month, day, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, member.path.read_bytes())
    data = buffer.getvalue()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    return len(data), hashlib.sha256(data).hexdigest()


# ---- the package of a full check ----------------------------------------------------------------

#: A made file's name ends with this when it is the check report; the cloud drawing; the
#: Not-compared list (#1619).
_REPORT_ENDING: Final = " - AI check report.pdf"
_CLOUD_ENDING: Final = " - GV cloud drawing.pdf"
_NOT_COMPARED: Final = " - Not compared."


def internal_report(name: str) -> bool:
    """Whether a made file is GV's own check report (never in the vendor's package)."""
    return name.endswith((_REPORT_ENDING, f" - {REPORT_IN_PACKAGE}.pdf"))


def _member_order(name: str) -> int:
    return 0 if name.endswith(_CLOUD_ENDING) else 5


def make_package_file(
    workspace: Any,
    findings: Sequence[Mapping[str, Any]],
    made: Sequence[tuple[str, Path]],
    out: Path,
    context: PackageContext,
) -> Any:
    """The send-ready package of a full check, written to `out` as one zip
    (`chat_findings.MarkupFile`): the files the check made (`made`: each one's name and path: the
    GV cloud drawing, the Not-compared list), the comment letter and the issue register, both made
    here from the same final `findings` and `context` as the cloud drawing. GV's own check report
    (its AI and trust columns, the model, rule check ids) never goes in, whoever hands it over: it
    stays a file of the chat for GV (orchestrator, 2026-10-19: nothing internal reaches the
    vendor). Raises `LookupError` when the chat has no copy of the set's PDFs."""
    chat = import_module("app.review.assistant.chat_findings")
    registry = import_module("vocabulary.product_registry").registry()
    main = chat.main_upload(chat.read_listing(workspace))
    if main is None or not (workspace.uploads / str(main["name"])).is_file():
        raise LookupError("no uploads")
    name = str(main["name"])
    members = sorted(
        (Member(given, path) for given, path in made if not internal_report(given)),
        key=lambda member: _member_order(member.name),
    )
    stem = name[:-4] if name.lower().endswith(".pdf") else name
    letter_path = out.with_name(f"{out.name}.letter.pdf")
    register_path = out.with_name(f"{out.name}.register")
    try:
        rows = register_rows(findings, context, product_label=registry.category_label)
        letter_name = chat.markup_name(name, LETTER_KIND)
        try:
            import_module("openpyxl")
            ending = "xlsx"
        except ModuleNotFoundError:
            ending = "csv"
        register_name = f"{stem} - {REGISTER_KIND}.{ending}"
        cloud = [m for m in members if _member_order(m.name) == 0]
        rest = [m for m in members if _member_order(m.name) != 0]
        everything = [
            *cloud,
            Member(letter_name, letter_path),
            Member(register_name, register_path),
            *rest,
        ]
        letter = letter_of(
            findings,
            context,
            drawing_set=name,
            reviewers=chat.reviewers(findings),
            enclosures=[m.name for m in everything if m.name != letter_name],
            not_compared_list=any(_NOT_COMPARED in m.name for m in members),
        )
        questions = questions_of(
            findings, areas=context.areas, product_label=registry.category_label
        )
        register_path.write_bytes(
            register_xlsx(rows, letter, questions) if ending == "xlsx" else register_csv(rows)
        )
        import_module("app.review.assistant.comment_letter").comment_letter_pdf(letter, letter_path)
        size, digest = package_zip(everything, out, context.date)
    finally:
        letter_path.unlink(missing_ok=True)
        register_path.unlink(missing_ok=True)
    return chat.MarkupFile(f"{stem} - {PACKAGE_KIND}.zip"[:255], size, digest)
