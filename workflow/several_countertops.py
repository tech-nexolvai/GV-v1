"""Whole-package depth and sink checks on a package with several countertops (#1297).

**The gap.** The five depth and sink checks (`workflow/depth_evidence.DEPTH_RULE_IDS`) run once for
each countertop only when the depth and sink reader is on and a countertop's run has been decided
(`workflow/stages.py`). Otherwise each runs **once for the whole package**, and so does
CT-SINK-CABINET-WIDTH-001, always. Such a result has one value per input: a number typed on the
form before #1253, an ordinary drawn label a person confirmed, or a project setting. With two
countertops of different depths, one countertop's 25" could PASS the whole package while the other
countertop's 30" was never looked at.

**The rule.** On a package with more than one countertop, every whole-package result of those six
checks is held (`REVIEW_REQUIRED`) with a plain reason that says how many countertops there are and
what clears it (`hold_for_several`). Whatever the value's source: the hold is applied to the finding
itself, after the check ran. A result scoped to one countertop is never touched, and a package
with one countertop (or none known) is exactly as before.

**Never silent** (`hold_for_unchecked`). With the reader on, the five checks run only for the
countertops whose run was decided. Any other countertop the package has gets one held
whole-package result per check, so it is never left with no result at all.

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

**This can only hold more, never less** (`tests/workflow/test_several_countertops.py`): it never
writes `PASS` or `FAIL`, it turns every outcome of those six whole-package results into
`REVIEW_REQUIRED`, it adds only held results, and it leaves any other result untouched.

Read-only and model-free: no extraction import, no write.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
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
    "COUNTERTOPS_NOTE",
    "NEVER_PER_COUNTERTOP",
    "PER_COUNTERTOP_ONCE_DECIDED",
    "READER_OFF",
    "SEVERAL_COUNTERTOPS",
    "SINK_CABINET_RULE_ID",
    "UNCHECKED",
    "UNCHECKED_NOTE",
    "WHOLE_PACKAGE_RULE_IDS",
    "CountedCountertop",
    "PackageCountertops",
    "clears_when",
    "hold_for_several",
    "hold_for_unchecked",
    "package_countertops",
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
    """One countertop on record: its name, and its live item if it has one."""

    name: str
    item_id: UUID | None = None


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
    for page in {item.page_number for item in unlinked}:
        on_page = [row for row in left if row.page_number == page]
        if sum(item.page_number == page for item in unlinked) == 1 and len(on_page) == 1:
            left.remove(on_page[0])
    counted = [(item.page_number, CountedCountertop(item.label, item.item_id)) for item in items]
    counted += [(row.page_number, CountedCountertop(row.name)) for row in left]
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
    vendor = _vendor_versions(session, [row.anchor.document_version_id for row in rows])
    found: list[_Row] = []
    for row in rows:
        if row.anchor.document_version_id not in vendor:
            continue
        found.append(_Row(row.page_number, row.label, row.anchor.id))
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
    found.extend(_Row(page.page_number, page.label, None) for page in unchosen if page.split)
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
    finding: Finding, countertops: PackageCountertops, *, depth_reader: bool
) -> Finding:
    """A whole-package result of one of the six checks, held when the package has several
    countertops; any other finding unchanged. Never returns `PASS` or `FAIL` it was not given.

    Only the caller knows a result is whole-package (no countertop and no row in scope), so it calls
    this only for those. A decided result loses its arithmetic (`trace`, `delta`): it was worked on
    one value standing for several countertops, and must not read as checked. An abstention keeps
    its own reason after the hold, without repeating what clears it. The names go in the notes.
    """
    if finding.rule_id not in WHOLE_PACKAGE_RULE_IDS or not countertops.several:
        return finding
    clears = clears_when(finding.rule_id, depth_reader=depth_reader)
    reason = SEVERAL_COUNTERTOPS.format(count=countertops.count, clears=clears)
    if not finding.is_decision:
        before = " ".join(finding.reason.replace(clears, " ").split())
        if before:
            reason = f"{reason} Also: {before}"
    note = COUNTERTOPS_NOTE.format(names=_listed(countertops.names))
    return replace(
        finding,
        outcome=Outcome.REVIEW_REQUIRED,
        reason=reason,
        trace=None,
        delta=None,
        notes=(*finding.notes, note),
    )


def hold_for_unchecked(
    *,
    rule_id: str,
    severity: Severity,
    snapshot_id: str,
    engine_version: str,
    countertops: PackageCountertops,
    scoped_item_ids: Collection[UUID],
) -> Finding | None:
    """The held whole-package result for one of the five checks run per countertop (reader on),
    when the package has a countertop none of those results is for; `None` when every countertop
    has its own. Never silent: such a countertop would otherwise have no result at all."""
    if rule_id not in DEPTH_RULE_IDS:
        return None
    unchecked = countertops.without(scoped_item_ids)
    if not unchecked:
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
        notes=(UNCHECKED_NOTE.format(names=_listed([c.name for c in unchecked])),),
    )


def _listed(names: Sequence[str]) -> str:
    if len(names) < 2:
        return "".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"
