"""Whole-package depth and sink checks on a package with several countertops (#1297).

**The gap.** The five depth and sink checks (`workflow/depth_evidence.DEPTH_RULE_IDS`) run once for
each countertop only when the depth and sink reader is on and a countertop's run has been decided
(`workflow/stages.py`). Otherwise each runs **once for the whole package**, and so does
CT-SINK-CABINET-WIDTH-001, always. Such a result has one value per input: an ordinary drawn label a
person confirmed, or a project setting (a number typed on the form before #1253 no longer counts
since #1303). With two
countertops of different depths, one countertop's 25" could PASS the whole package while the other
countertop's 30" was never looked at.

**The rule.** On a package with more than one countertop, every whole-package result of those six
checks is held (`REVIEW_REQUIRED`) with a plain reason that says how many countertops there are and
what clears it (`hold_for_several`). Whatever the value's source: the hold is applied to the finding
itself, after the check ran. A result scoped to one countertop is never touched, and a package
with one countertop (or none known) is exactly as before.

**One result per countertop (#1201)** (`countertop_holds`). On a package with several countertops,
each of the six checks gives every countertop a result of its own. A countertop the check ran for
(reader on, its run decided) has it already. Every other countertop gets a held result
(`REVIEW_REQUIRED`) scoped to it, never decided, whose reason says in plain words what it waits for
and what clears it (`waiting_reason`). Such a result carries no value at all: a value only counts
for the countertop it belongs to, and these are written only where nothing reads one.

A held result is scoped to the countertop's row (its anchor reading, or a split page's record) in
the newest reading, so a decision on it carries over an unchanged re-run and over confirming the
row's countertop. A countertop with no row (a second box a reader named on a page, or an item, a
person's or code's, confirmed from no row) has no such result: a result scoped to an item never
carries a decision over, and would ask again on every run. It is named on the held whole-package
result instead, whose reason says how many such countertops there are, so a changed number asks
again. The whole-package result is written only when it still says something: that, or a value
given for the whole package (typed, or a reading confirmed), which it shows held
(`hold_for_several`).

What the whole-package check itself still lacks, beyond the drawn values (a project setting not
given, a wall layout not stated), is said on every countertop's own result too, after "Also:"
(`also_reason`): the same words on each, so a decision still carries over. A withheld reading is
not said there (#1306): its reason can name a countertop and its page, and the whole-package hold,
written in that case, says it in full. **Never silent** (`hold_for_unchecked`): with the reader on, such a
countertop the check did not run for is named on one held whole-package result.

A package with one countertop, or none known, is exactly as before.

**The reason never names a countertop.** A reviewer's decision on a held result carries over to an
unchanged re-run only while its reason is the same (`app/review/carry_over.py`), and names change
when a row becomes a confirmed countertop. So the reason says only how many countertops there are
(a new countertop asks again) and what clears it; the names are in the result's notes
(`COUNTERTOPS_NOTE`, `UNCHECKED_NOTE`), which the screens show beside it.

**Which countertops** (`package_countertops`), from what is on record, counting the union of:

* the live countertop items (`workflow/item_plan.live_countertops`, whoever confirmed them, code
  included), except those on a drawing a person confirmed as the architect's;
* the countertop rows the newest slot reading found on the vendor's pages: each row, each other
  numbered box a row reader named as a second countertop's row on the same page (#1108), and each
  page whose readers did not agree on a row (`split`, including a page a person answered "There is
  a countertop").

An item and a row are one countertop only when the item was confirmed from that row (its row link,
followed across a re-read), or when the page holds exactly one item without such a link and exactly
one row no item is linked to. Anything less certain counts as two: **an overcount only holds, an
undercount could let one value decide for two countertops.** A set read before either existed has
no countertop on record, so nothing changes for it.

**This can only hold more, never less** (`tests/workflow/test_several_countertops.py`,
`tests/workflow/test_one_result_per_countertop.py`): it never writes `PASS` or `FAIL`, it turns
every outcome of those six whole-package results into `REVIEW_REQUIRED`, it adds only held results,
and it leaves any other result untouched.

**The run check uses the same split** (`own_or_left`, `missing_own_results`, #1305): a
countertop with neither a live result of its own nor its name on the held whole-package result is
reported there, so one that silently lost its result is seen even while another keeps one.

Read-only and model-free: no extraction import, no write.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DrawingView, PackageRevision, ViewRole
from app.models.document import Document, DocumentVersion
from app.review.no_countertop import answered_pages
from verdict.finding import Finding
from verdict.outcomes import Outcome, Severity
from workflow.depth_evidence import DEPTH_RULE_IDS
from workflow.item_plan import LiveCountertop, live_countertops
from workflow.slot_row_scope import slot_rows_and_unchosen_pages

__all__ = [
    "ALSO",
    "COUNTERTOPS_NOTE",
    "LEFT_OVER",
    "NEVER_PER_COUNTERTOP",
    "OWN_NEVER_PER_COUNTERTOP",
    "OWN_READER_OFF",
    "OWN_RUN_NOT_DECIDED",
    "PER_COUNTERTOP_ONCE_DECIDED",
    "READER_OFF",
    "SEVERAL_COUNTERTOPS",
    "SINK_CABINET_RULE_ID",
    "UNCHECKED",
    "UNCHECKED_NOTE",
    "WAITING",
    "WAITS_FOR",
    "WHOLE_PACKAGE_RULE_IDS",
    "CountedCountertop",
    "CountertopHolds",
    "PackageCountertops",
    "also_reason",
    "clears_when",
    "countertop_holds",
    "hold_for_several",
    "hold_for_unchecked",
    "missing_own_results",
    "own_or_left",
    "package_countertops",
    "unchecked_note",
    "waiting_reason",
]

#: The one check that reads a depth-reader value and never runs per countertop.
SINK_CABINET_RULE_ID: Final = "CT-SINK-CABINET-WIDTH-001"

#: The checks whose whole-package result is held on a package with several countertops.
WHOLE_PACKAGE_RULE_IDS: Final = DEPTH_RULE_IDS | {SINK_CABINET_RULE_ID}

#: Why a whole-package result is held (#1297), and what clears it (`clears_when`). No names: the
#: reason must stay the same while the countertops do, so a reviewer's decision carries over.
SEVERAL_COUNTERTOPS: Final = (
    "This package has {count} countertops, and this check ran once for the whole package, so one "
    "value cannot stand for all of them: held for a person. {clears}"
)

#: Why a held whole-package result is written beside the countertops' own results (reader on).
UNCHECKED: Final = (
    "This package has {count} countertops, and {unchecked} of them {have} no result of {its} own "
    "for this check: held for a person. {clears}"
)

#: The names, in the result's notes (shown on screen; a decision's carry-over ignores notes). The
#: screens find these two notes by their opening words, held the same by a test.
COUNTERTOPS_NOTE: Final = "Countertops in this package: {names}."
UNCHECKED_NOTE: Final = "Countertops with no result of their own for this check: {names}."

#: What clears the hold, said truthfully for each case.
PER_COUNTERTOP_ONCE_DECIDED: Final = (
    "It clears when each countertop is confirmed and its run decided on Measurements: this check "
    "then runs for each countertop on its own, from the depth and sink reader's readings."
)
READER_OFF: Final = (
    "The depth and sink reader is switched off, so this check runs only for the whole package and "
    "stays held: check each countertop's own value and decide this result."
)
NEVER_PER_COUNTERTOP: Final = (
    "This check never runs per countertop, so it stays held: check each countertop's own value and "
    "decide this result."
)


#: What each of the six checks waits for on one countertop's own held result (#1201), and whether
#: it applies only where the countertop has a sink. Plain words, no codes and no names.
WAITS_FOR: Final[Mapping[str, tuple[str, bool]]] = {
    "CT-DEPTH-001": ("the countertop depth", False),
    "CT-SINK-CUTOUT-WIDTH-001": ("the sink cut-out width", True),
    "CT-SINK-CUTOUT-DEPTH-001": ("the sink cut-out depth", True),
    "CT-SINK-OFFSET-FRONT-001": (
        "the front offset (the strip between the cabinet front and the sink cut-out)",
        True,
    ),
    "CT-BACK-OFFSET-MIN-001": (
        "the countertop depth, the front offset and the sink cut-out depth",
        True,
    ),
    SINK_CABINET_RULE_ID: (
        (
            "the wall layout, the sink cabinet width, the sink cut-out width and the clearance "
            "either side of the cut-out"
        ),
        True,
    ),
}

#: One countertop's own held result (#1201): what it waits for, then why nothing gives it.
WAITING: Final = "Waiting for {what} of this countertop{sink}. {why}"

#: Why nothing gives it, said truthfully for each case (compare `clears_when`).
OWN_RUN_NOT_DECIDED: Final = (
    "This check runs for it from the depth and sink reader's readings only once this countertop is "
    "confirmed and its run decided on Measurements."
)

#: What the whole-package check still lacks beyond the drawn values, said on each countertop's own
#: result (`also_reason`).
ALSO: Final = "Also: {reason}"

#: How many countertops a whole-package hold stands for that have no result of their own (#1201
#: review): a count, never names, so a changed number asks again and the same one carries over.
LEFT_OVER: Final = "{left} of them {have} no result of {its} own for this check."
OWN_READER_OFF: Final = (
    "The depth and sink reader is switched off, so nothing reads this countertop's own values: "
    "check them on its drawing and decide this result."
)
OWN_NEVER_PER_COUNTERTOP: Final = (
    "This check never runs per countertop, so nothing reads this countertop's own values: check "
    "them on its drawing and decide this result."
)


def waiting_reason(rule_id: str, *, depth_reader: bool, also: str | None = None) -> str:
    """The reason on one countertop's own held result of one of the six checks (#1201), with
    `also` (`also_reason`) after it when the check lacks more than the drawn values.

    It names no countertop (the result's scope does): a decision on it carries over an unchanged
    re-run only while its reason is the same (`app/review/carry_over.py`).
    """
    what, sink = WAITS_FOR[rule_id]
    if rule_id not in DEPTH_RULE_IDS:
        why = OWN_NEVER_PER_COUNTERTOP
    else:
        why = OWN_RUN_NOT_DECIDED if depth_reader else OWN_READER_OFF
    reason = WAITING.format(what=what, sink=", if it has a sink" if sink else "", why=why)
    return reason if also is None else f"{reason} {ALSO.format(reason=also)}"


#: The engine's sentences for a value it could not find (`verdict/engine.py`), by name.
_COULD_NOT_RESOLVE: Final = re.compile(
    r"^(?:derivation '[^']+'|the final operation) could not resolve required value '([^']+)'\. "
    r"A missing (?:intermediate or parameter is not zero|parameter or intermediate is not a "
    r"default)\.$"
)
_ABSENT: Final = re.compile(
    r"^required input\(s\) absent: ([^.]+)\. A missing value is not a passing value\.$"
)
#: The engine's sentence for readings found but withheld (`verdict/engine.py`, #826): their reasons,
#: then the inputs. Such a reason can name countertops and pages (`ONE_COUNTERTOPS_READING`).
_WITHHELD: Final = re.compile(r"^.+ \(inputs: ([^()]+)\)$")


def also_reason(finding: Finding, inputs: Collection[str]) -> str | None:
    """What the whole-package result of a check says it lacks beyond the drawn values, for each
    countertop's own held result (#1201 review); `None` when it decided, or when all it lacks is
    one or more of the rule's drawn `inputs`, which the countertop's own sentence already names.

    `None` too when all it says is that readings of drawn `inputs` were withheld (#1306): that
    reason can name another countertop and its page (a person confirmed one countertop's
    depth-reader value), and a countertop's own reason never names a countertop. The whole-package
    hold, written in that case, keeps the full text. Anything else is said without what clears the
    whole-package hold (`clears_when`), as `hold_for_several` does: it is not this result's to say.

    The same for every countertop of the package: it comes from the one whole-package evaluation.
    """
    if finding.is_decision or not finding.reason.strip():
        return None
    reason = " ".join(finding.reason.split())
    resolved = _COULD_NOT_RESOLVE.match(reason)
    if resolved is not None and resolved.group(1) in inputs:
        return None
    absent = _ABSENT.match(reason)
    if absent is not None and {n.strip() for n in absent.group(1).split(",")} <= set(inputs):
        return None
    withheld = _WITHHELD.match(reason)
    if withheld is not None and {n.strip() for n in withheld.group(1).split(",")} <= set(inputs):
        return None
    for clears in (PER_COUNTERTOP_ONCE_DECIDED, READER_OFF, NEVER_PER_COUNTERTOP):
        reason = reason.replace(clears, " ")
    return " ".join(reason.split()) or None


def clears_when(rule_id: str, *, depth_reader: bool) -> str:
    """What clears a whole-package hold on this check, truthfully.

    The five depth and sink checks run per countertop only with the reader on, and only once a
    countertop's run is decided; CT-SINK-CABINET-WIDTH-001 never does.
    """
    if rule_id not in DEPTH_RULE_IDS:
        return NEVER_PER_COUNTERTOP
    return PER_COUNTERTOP_ONCE_DECIDED if depth_reader else READER_OFF


@dataclass(frozen=True, slots=True)
class CountedCountertop:
    """One countertop on record: its name, its live item if it has one, and what its own results
    can be scoped to (#1201)."""

    name: str
    item_id: UUID | None = None
    row_id: UUID | None = None
    """Its row's record in the newest slot reading: the row's anchor reading, or the "row not
    chosen" record of a page whose readers did not agree on a row; `None` for a second box a reader
    named on a page (nothing on record stands for it), and for an item with no row of its own."""

    @property
    def scope(self) -> tuple[UUID | None, UUID | None] | None:
        """`(item, row)` its own result is scoped to, or `None` when it cannot have one.

        Only its row: a decision on a row's result carries over an unchanged re-run, and over a
        person confirming the row's countertop (`app/review/carry_over.py`). A result scoped to an
        item never carries one over (the legacy manual path's rule), so a countertop with no row,
        a person's or code's, is named on the whole-package hold instead, as on main before #1201.
        """
        if self.row_id is not None:
            return (None, self.row_id)
        return None


@dataclass(frozen=True, slots=True)
class PackageCountertops:
    """The countertops a revision is known to have, in page order."""

    countertops: tuple[CountedCountertop, ...] = ()

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(countertop.name for countertop in self.countertops)

    @property
    def count(self) -> int:
        return len(self.countertops)

    @property
    def several(self) -> bool:
        return self.count > 1

    def without(self, item_ids: Collection[UUID]) -> tuple[CountedCountertop, ...]:
        """The countertops that are none of these items: those with no result of their own."""
        return tuple(c for c in self.countertops if c.item_id is None or c.item_id not in item_ids)


@dataclass(frozen=True, slots=True)
class _Row:
    page_number: int
    name: str
    anchor_id: UUID | None
    """The row's anchor reading; `None` for a page or a box with no row of its own."""
    record_id: UUID | None = None
    """What a result for it is scoped to: the anchor, or a split page's own record; `None` for a
    box with no record of its own."""


def package_countertops(session: Session, revision_id: UUID) -> PackageCountertops:
    """The countertops this revision has: the union of its live countertop items and its vendor
    pages' countertop rows, an item and a row merged only when surely one (module docstring)."""
    items = _items(session, revision_id)
    rows = _rows(session, revision_id)
    # By the row link: the item was confirmed from that row (followed across a re-read).
    linked = {item.row_candidate_id for item in items if item.row_candidate_id is not None}
    left = [row for row in rows if row.anchor_id is None or row.anchor_id not in linked]
    # By the page: exactly one unlinked item and exactly one row no item is linked to.
    row_ids = {row.anchor_id for row in rows if row.anchor_id is not None}
    unlinked = [item for item in items if item.row_candidate_id not in row_ids]
    # The row such an item is merged with: its own results are keyed by that row (#1201 review).
    merged: dict[UUID, UUID | None] = {}
    for page in {item.page_number for item in unlinked}:
        on_page = [row for row in left if row.page_number == page]
        if sum(item.page_number == page for item in unlinked) == 1 and len(on_page) == 1:
            (item,) = [item for item in unlinked if item.page_number == page]
            merged[item.item_id] = on_page[0].record_id
            left.remove(on_page[0])
    counted = [
        (
            item.page_number,
            CountedCountertop(
                item.label,
                item.item_id,
                row_id=(
                    item.row_candidate_id
                    if item.row_candidate_id in row_ids
                    else merged.get(item.item_id)
                ),
            ),
        )
        for item in items
    ]
    counted += [
        (row.page_number, CountedCountertop(row.name, row_id=row.record_id)) for row in left
    ]
    # In page order (`sorted` is stable: items first within a page, as listed).
    return PackageCountertops(tuple(c for _page, c in sorted(counted, key=lambda e: e[0])))


def _items(session: Session, revision_id: UUID) -> list[LiveCountertop]:
    countertops = live_countertops(session, revision_id)
    if not countertops:
        return []
    architect = set(
        session.scalars(
            select(DrawingView.id).where(
                DrawingView.id.in_({countertop.view_id for countertop in countertops}),
                DrawingView.role == ViewRole.ARCH.value,
            )
        ).all()
    )
    return [countertop for countertop in countertops if countertop.view_id not in architect]


def _rows(session: Session, revision_id: UUID) -> list[_Row]:
    rows, unchosen = slot_rows_and_unchosen_pages(session, revision_id)
    if not rows and not unchosen:
        return []
    vendor = _vendor_versions(
        session,
        [
            *(row.anchor.document_version_id for row in rows),
            *(page.record.document_version_id for page in unchosen),
        ],
    )
    found: list[_Row] = []
    for row in rows:
        if row.anchor.document_version_id not in vendor:
            continue
        found.append(_Row(row.page_number, row.label, row.anchor.id, row.anchor.id))
        found.extend(
            _Row(
                row.page_number,
                f"another countertop row on page {row.page_number} (box {box})",
                None,
            )
            for box in sorted({box for _model, box in row.also})
        )
    revision = session.get(PackageRevision, revision_id)
    if unchosen and revision is not None:
        # A person's "There is a countertop" turns a page listed with none into a split page.
        unchosen, _answers = answered_pages(session, revision, unchosen)
    # A split page counts wherever it is; its own result needs its record on a vendor page.
    found.extend(
        _Row(
            page.page_number,
            page.label,
            None,
            page.record.id if page.record.document_version_id in vendor else None,
        )
        for page in unchosen
        if page.split
    )
    return found


def _vendor_versions(session: Session, versions: Sequence[UUID]) -> set[UUID]:
    if not versions:
        return set()
    return set(
        session.scalars(
            select(DocumentVersion.id)
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(DocumentVersion.id.in_(set(versions)), Document.kind == "shop")
        ).all()
    )


def hold_for_several(
    finding: Finding,
    countertops: PackageCountertops,
    *,
    depth_reader: bool,
    left: Sequence[CountedCountertop] = (),
) -> Finding:
    """A whole-package result of one of the six checks, held when the package has several
    countertops; any other finding unchanged. Never returns `PASS` or `FAIL` it was not given.

    Only the caller knows a result is whole-package (no countertop and no row in scope), so it calls
    this only for those. A decided result loses its arithmetic (`trace`, `delta`): it was worked on
    one value standing for several countertops, and must not read as checked. An abstention keeps
    its own reason after the hold, without repeating what clears it. The names go in the notes.

    `left` are the countertops with no result of their own (#1201): their number is in the reason
    (so a changed set asks again), their names in a note.
    """
    if finding.rule_id not in WHOLE_PACKAGE_RULE_IDS or not countertops.several:
        return finding
    clears = clears_when(finding.rule_id, depth_reader=depth_reader)
    reason = SEVERAL_COUNTERTOPS.format(count=countertops.count, clears=clears)
    if left:
        one = len(left) == 1
        counted = LEFT_OVER.format(
            left=len(left), have="has" if one else "have", its="its" if one else "their"
        )
        reason = f"{reason} {counted}"
    if not finding.is_decision:
        before = " ".join(finding.reason.replace(clears, " ").split())
        if before:
            reason = f"{reason} Also: {before}"
    notes = [COUNTERTOPS_NOTE.format(names=_listed(countertops.names))]
    if left:
        notes.append(unchecked_note(left))
    return replace(
        finding,
        outcome=Outcome.REVIEW_REQUIRED,
        reason=reason,
        trace=None,
        delta=None,
        notes=(*finding.notes, *notes),
    )


def hold_for_unchecked(
    *,
    rule_id: str,
    severity: Severity,
    snapshot_id: str,
    engine_version: str,
    countertops: PackageCountertops,
    unchecked: Sequence[CountedCountertop],
) -> Finding | None:
    """The held whole-package result for one of the five checks run per countertop (reader on),
    naming the `unchecked` countertops: those none of its results is for, and (#1201) that cannot
    have a held result of their own either. `None` when there are none. Never silent: such a
    countertop would otherwise have no result at all."""
    if rule_id not in DEPTH_RULE_IDS or not unchecked:
        return None
    one = len(unchecked) == 1
    return Finding(
        rule_id=rule_id,
        outcome=Outcome.REVIEW_REQUIRED,
        severity=severity,
        reason=UNCHECKED.format(
            count=countertops.count,
            unchecked=len(unchecked),
            have="has" if one else "have",
            its="its" if one else "their",
            clears=PER_COUNTERTOP_ONCE_DECIDED,
        ),
        snapshot_id=snapshot_id,
        engine_version=engine_version,
        notes=(unchecked_note(unchecked),),
    )


@dataclass(frozen=True, slots=True)
class CountertopHolds:
    """Each countertop's own held result of one check (#1201), and the countertops left over."""

    own: tuple[tuple[CountedCountertop, Finding], ...] = ()
    left: tuple[CountedCountertop, ...] = ()
    """The countertops that cannot have a result of their own: no record to scope it to, or one
    another countertop's result already uses (fail closed: named on a whole-package result)."""


def countertop_holds(
    *,
    rule_id: str,
    severity: Severity,
    snapshot_id: str,
    engine_version: str,
    countertops: Sequence[CountedCountertop],
    depth_reader: bool,
    also: str | None = None,
) -> CountertopHolds:
    """A held result of one of the six checks for each of these countertops, each to be scoped to
    the countertop (`CountedCountertop.scope`), saying what it waits for (`waiting_reason`).

    Never `PASS` or `FAIL`, and never a value: they are written only for countertops nothing read
    a value for. `REVIEW_REQUIRED`, so a person decides each one before sign-off.
    """
    if rule_id not in WHOLE_PACKAGE_RULE_IDS:
        return CountertopHolds()
    reason = waiting_reason(rule_id, depth_reader=depth_reader, also=also)
    own, left = own_or_left(countertops)
    return CountertopHolds(
        tuple(
            (
                countertop,
                Finding(
                    rule_id=rule_id,
                    outcome=Outcome.REVIEW_REQUIRED,
                    severity=severity,
                    reason=reason,
                    snapshot_id=snapshot_id,
                    engine_version=engine_version,
                ),
            )
            for countertop in own
        ),
        left,
    )


def own_or_left(
    countertops: Sequence[CountedCountertop],
) -> tuple[tuple[CountedCountertop, ...], tuple[CountedCountertop, ...]]:
    """Which of these countertops can have a held result of their own (`own`, each scoped to
    `CountedCountertop.scope`), and which are left over (`left`): no record to scope one to, or a
    record an earlier countertop's result already uses (fail closed: named on a whole-package
    result). The one definition: `countertop_holds` writes by it, and the run check
    (`missing_own_results`) checks by it (#1305)."""
    own: list[CountedCountertop] = []
    left: list[CountedCountertop] = []
    used: set[tuple[UUID | None, UUID | None]] = set()
    for countertop in countertops:
        scope = countertop.scope
        if scope is None or scope in used:
            left.append(countertop)
            continue
        used.add(scope)
        own.append(countertop)
    return tuple(own), tuple(left)


def missing_own_results(
    countertops: PackageCountertops,
    *,
    item_ids: Collection[UUID],
    row_ids: Collection[UUID],
    whole_package_notes: Iterable[str],
) -> tuple[CountedCountertop, ...]:
    """The countertops with no live result of one of the six checks, in page order (#1305): what
    the run check reports. Empty on a package with one countertop, or none known.

    Given that check's live results: `item_ids` and `row_ids` they are scoped to, and the notes of
    its whole-package results. The same split the check stage writes by:

    * a countertop with a result scoped to its item (reader on, its run decided) has its own;
    * of the others, one that can have a held result of its own (`own_or_left`) needs one scoped
      to its row;
    * one left over needs its name on a whole-package result (`unchecked_note`).

    Reads nothing and decides nothing: a report of a result gone missing, never a result.
    """
    if not countertops.several:
        return ()
    own, left = own_or_left(countertops.without(item_ids))
    listed = [
        note.removeprefix(_UNCHECKED_OPENING)
        for note in whole_package_notes
        if note.startswith(_UNCHECKED_OPENING)
    ]
    missing = [
        countertop
        for countertop in own
        if countertop.scope is None or countertop.scope[1] not in row_ids
    ]
    missing += [
        countertop
        for countertop in left
        if not any(_listed_includes(names, countertop.name) for names in listed)
    ]
    gone = {id(countertop) for countertop in missing}
    return tuple(countertop for countertop in countertops.countertops if id(countertop) in gone)


def unchecked_note(countertops: Sequence[CountedCountertop]) -> str:
    """The note naming the countertops with no result of their own for a check."""
    return UNCHECKED_NOTE.format(names=_listed([c.name for c in countertops]))


#: How `unchecked_note` opens, before the names.
_UNCHECKED_OPENING: Final = UNCHECKED_NOTE.split("{names}")[0]


def _listed(names: Sequence[str]) -> str:
    if len(names) < 2:
        return "".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"


def _listed_includes(listed: str, name: str) -> bool:
    """Whether `listed` (`_listed`'s names and the note's closing full stop) names `name`: one whole
    entry, never part of a longer name."""
    return re.search(rf"(?:^|, | and ){re.escape(name)}(?:, | and |\.$)", listed) is not None
