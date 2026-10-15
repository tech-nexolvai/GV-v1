"""Whole-package depth and sink checks on a package with several countertops (#1297).

**The gap.** The five depth and sink checks (`workflow/depth_evidence.DEPTH_RULE_IDS`) run once for
each countertop only when the depth and sink reader is on and a countertop's run has been decided
(`workflow/stages.py`). Otherwise each runs **once for the whole package**, and so does
CT-SINK-CABINET-WIDTH-001, always. Such a result has one value per input: a number typed on the
form before #1253, an ordinary drawn label a person confirmed, or a project setting. With two
countertops of different depths, one countertop's 25" could PASS the whole package while the other
countertop's 30" was never looked at.

**The rule.** On a package with more than one countertop, every whole-package result of those six
checks is held (`REVIEW_REQUIRED`) with a plain reason that names the countertops and says what
clears it (`hold_for_several`). Whatever the value's source: the hold is applied to the finding
itself, after the check ran. A result scoped to one countertop is never touched, and a package
with one countertop (or none known) is exactly as before.

**How many countertops** (`package_countertops`), counted from what is on record, and the larger of
two counts so that neither can hide a countertop the other knows about:

* the live countertop items (`workflow/item_plan.live_countertops`, whoever confirmed them),
  except those on a drawing a person confirmed as the architect's;
* the countertop rows the newest slot reading found on the vendor's pages: each row, each other
  numbered box a row reader named as a second countertop's row on the same page (#1108), and each
  page whose readers did not agree on a row (`split`, including a page a person answered "There is
  a countertop").

A set read before either existed has neither count, so nothing can be said and nothing changes.

**This can only hold more, never less** (`tests/workflow/test_several_countertops.py`): it never
writes `PASS` or `FAIL`, it turns every outcome of those six whole-package results into
`REVIEW_REQUIRED`, and it leaves any other result untouched.

Read-only and model-free: no extraction import, no write.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DrawingView, PackageRevision, ViewRole
from app.models.document import Document, DocumentVersion
from app.review.no_countertop import answered_pages
from verdict.finding import Finding
from verdict.outcomes import Outcome
from workflow.depth_evidence import DEPTH_RULE_IDS
from workflow.item_plan import live_countertops
from workflow.slot_row_scope import slot_rows_and_unchosen_pages

__all__ = [
    "NEVER_PER_COUNTERTOP",
    "PER_COUNTERTOP_ONCE_DECIDED",
    "READER_OFF",
    "SEVERAL_COUNTERTOPS",
    "SINK_CABINET_RULE_ID",
    "WHOLE_PACKAGE_RULE_IDS",
    "PackageCountertops",
    "clears_when",
    "hold_for_several",
    "package_countertops",
]

#: The one check that reads a depth-reader value and never runs per countertop.
SINK_CABINET_RULE_ID: Final = "CT-SINK-CABINET-WIDTH-001"

#: The checks whose whole-package result is held on a package with several countertops.
WHOLE_PACKAGE_RULE_IDS: Final = DEPTH_RULE_IDS | {SINK_CABINET_RULE_ID}

#: Why a whole-package result is held (#1297), and what clears it (`clears_when`).
SEVERAL_COUNTERTOPS: Final = (
    "This package has {count} countertops ({names}), and this check ran once for the whole "
    "package, so one value cannot stand for all of them: held for a person. {clears}"
)

#: What clears the hold, said truthfully for each case.
PER_COUNTERTOP_ONCE_DECIDED: Final = (
    "It clears when each countertop's run is decided on Measurements: this check then runs for "
    "each countertop on its own, from the depth and sink reader's readings."
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
class PackageCountertops:
    """The countertops a revision is known to have, by name, for the reviewer's sentence."""

    names: tuple[str, ...] = ()

    @property
    def count(self) -> int:
        return len(self.names)

    @property
    def several(self) -> bool:
        return self.count > 1


def package_countertops(session: Session, revision_id: UUID) -> PackageCountertops:
    """How many countertops this revision has: the larger of its live countertop items and its
    vendor pages' countertop rows (module docstring). Read-only."""
    items = _item_names(session, revision_id)
    rows = _row_names(session, revision_id)
    return PackageCountertops(names=items if len(items) >= len(rows) else rows)


def _item_names(session: Session, revision_id: UUID) -> tuple[str, ...]:
    countertops = live_countertops(session, revision_id, follow_rows=False)
    if not countertops:
        return ()
    architect = set(
        session.scalars(
            select(DrawingView.id).where(
                DrawingView.id.in_({countertop.view_id for countertop in countertops}),
                DrawingView.role == ViewRole.ARCH.value,
            )
        ).all()
    )
    return tuple(
        countertop.label for countertop in countertops if countertop.view_id not in architect
    )


def _row_names(session: Session, revision_id: UUID) -> tuple[str, ...]:
    rows, unchosen = slot_rows_and_unchosen_pages(session, revision_id)
    if not rows and not unchosen:
        return ()
    vendor = _vendor_versions(session, [row.anchor.document_version_id for row in rows])
    names: list[tuple[int, str]] = []
    for row in rows:
        if row.anchor.document_version_id not in vendor:
            continue
        names.append((row.page_number, row.label))
        boxes = sorted({box for _model, box in row.also})
        names.extend(
            (row.page_number, f"another countertop row on page {row.page_number} (box {box})")
            for box in boxes
        )
    revision = session.get(PackageRevision, revision_id)
    if unchosen and revision is not None:
        # A person's "There is a countertop" turns a page listed with none into a split page.
        unchosen, _answers = answered_pages(session, revision, unchosen)
    split = [(page.page_number, page.label) for page in unchosen if page.split]
    # In page order, a split page among the rows (`sorted` is stable within a page).
    return tuple(name for _page, name in sorted([*names, *split], key=lambda entry: entry[0]))


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
    its own reason after the hold, without repeating what clears it.
    """
    if finding.rule_id not in WHOLE_PACKAGE_RULE_IDS or not countertops.several:
        return finding
    clears = clears_when(finding.rule_id, depth_reader=depth_reader)
    reason = SEVERAL_COUNTERTOPS.format(
        count=countertops.count, names=_listed(countertops.names), clears=clears
    )
    if not finding.is_decision:
        before = " ".join(finding.reason.replace(clears, " ").split())
        if before:
            reason = f"{reason} Also: {before}"
    return replace(finding, outcome=Outcome.REVIEW_REQUIRED, reason=reason, trace=None, delta=None)


#: How many countertops a reason names before it says how many more there are.
NAMED_AT_MOST: Final = 8


def _listed(names: Sequence[str]) -> str:
    if len(names) > NAMED_AT_MOST:
        shown = ", ".join(names[:NAMED_AT_MOST])
        return f"{shown} and {len(names) - NAMED_AT_MOST} more"
    if len(names) < 2:
        return "".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"
