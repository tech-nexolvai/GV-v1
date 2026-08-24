"""Which revision of a sheet governs, and why the others do not (#184, B11.2).

A package holding *Rev A* and *Rev C* of the same sheet is normal — revisions are issued as supplements,
not replacements. Something has to decide which one a check should read, and `docs/RISK_CONTROLS.md`
names the failure this prevents: *"which revision of a sheet to trust, so a superseded drawing is still
selectable and produces a"* — confident, wrong finding.

**Ordering comes from the drawing, never from a collation we invented.** This is the decision the whole
module turns on. `docs/DESIGN_EXTRACTION.md` §7 is explicit that supersession *"never resolves to 'the
last page wins' or 'the highest letter wins'"*, and the issue asks for ordering by *the revision
sequence, with the date as corroboration rather than the primary key*. Both point at the same thing: a
revision history table states its own order. If the Rev C sheet's history lists A, then B, then C, the
drawing has told us C is later than A — we are reading a fact, not applying a convention.

So `C > A` is only known when some sheet's history says so. Where no history connects two labels, the
answer is `Unresolved`, and B11.3 (#185) turns that into REVIEW REQUIRED. That is deliberately stricter
than sorting alphabetically would be, and the reason is arithmetic: `AA` sorts before `B`, `10` before
`9`, and a vendor who restarts at `01` after `Z` breaks every collation anyone would write. Each of those
mistakes silently selects the wrong sheet, and every other guard in the system assumes the source page
was the right page.

**The date corroborates and can veto; it never decides.** When the history order and the dates disagree,
that is not a tie to break — it is a sheet telling us two different stories, and this refuses rather than
preferring one. Dates that are merely ambiguous (`03/04/26`, per #183) cannot corroborate anything and
are treated as silent, not as agreement.

**Nothing is deleted.** A superseded page is retained and marked with the reason it lost, because a
reviewer asking *"what did Rev A say?"* is asking a legitimate question and the answer has to exist.

Source: backend proposal §11 · Design: `docs/DESIGN_EXTRACTION.md` §7 ·
Verification: `tests/extraction/test_supersession.py`
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from itertools import pairwise
from typing import Final

from extraction.manifest import PageRecord
from extraction.revision import RevisionBlock, normalise

__all__ = [
    "GoverningRevision",
    "SheetPage",
    "SupersededPage",
    "Unresolved",
    "governing_revision",
    "group_by_sheet",
    "recorded_order",
]

#: Why a sheet could not be resolved. One of these, never a free-form string, so `#185` can branch on the
#: cause rather than parse prose — and so a new cause has to be named here rather than smuggled in.
NO_SHEET_NUMBER: Final = "no_sheet_number"
UNKNOWN_REVISION: Final = "unknown_revision"
NO_RECORDED_ORDER: Final = "no_recorded_order"
DATE_CONTRADICTS_ORDER: Final = "date_contradicts_order"
DUPLICATE_REVISION: Final = "duplicate_revision"


@dataclass(frozen=True, slots=True)
class SheetPage:
    """One page, with what its title block said about its revision.

    The two are paired rather than merged: `PageRecord` is B6's and owns page identity, while
    `RevisionBlock` is B11.1's and owns what the sheet claims. Keeping them separate means neither story
    has to edit the other's type to make this one work.
    """

    page: PageRecord
    revision: RevisionBlock

    @property
    def sheet_number(self) -> str | None:
        """The sheet's identity — **never** the filename or the page order (§7)."""
        return self.page.sheet_number

    @property
    def label(self) -> str | None:
        """This page's revision in comparable form, or `None` when it is unknown."""
        current = self.revision.current
        return normalise(current.as_printed) if current is not None else None


@dataclass(frozen=True, slots=True)
class SupersededPage:
    """A page that lost, kept with the reason it lost.

    Retained, not deleted: *"a superseded page is retained and marked"*. A reviewer asking what the
    earlier revision said needs an answer, and "we discarded it" is not one.
    """

    page: SheetPage
    superseded_by: str
    """The normalised revision that governs instead of this one."""

    reason: str
    """Plain English, for a reviewer rather than for a branch."""


@dataclass(frozen=True, slots=True)
class GoverningRevision:
    """The revision that governs one sheet, and the record of what it displaced."""

    sheet_number: str
    governing: SheetPage
    superseded: tuple[SupersededPage, ...] = ()

    @property
    def is_resolved(self) -> bool:
        """Always true. Present so a caller can branch on the union without `isinstance`."""
        return True


@dataclass(frozen=True, slots=True)
class Unresolved:
    """No revision could be shown to govern — the fail-closed answer.

    §7: *"Unresolved supersession produces REVIEW REQUIRED for every finding drawn from that sheet."*
    That is #185's to act on; this type's job is to say *which* uncertainty occurred and to carry every
    candidate, so a reviewer sees the sheets rather than being told a number is missing.
    """

    sheet_number: str | None
    cause: str
    detail: str
    candidates: tuple[SheetPage, ...] = ()

    @property
    def is_resolved(self) -> bool:
        return False


def group_by_sheet(pages: list[SheetPage]) -> dict[str | None, tuple[SheetPage, ...]]:
    """Group pages by sheet number, keeping their given order within each group.

    Pages with no sheet number are grouped under `None` rather than dropped or invented into a group of
    their own. §7 — sheet identity *is* the sheet number, so a page without one has no identity to group
    by, and pretending otherwise (by filename, by page order) is the specific mistake the criterion
    forbids. `governing_revision` refuses that group.
    """
    grouped: dict[str | None, list[SheetPage]] = defaultdict(list)
    for page in pages:
        grouped[page.sheet_number].append(page)
    return {number: tuple(group) for number, group in grouped.items()}


def recorded_order(pages: list[SheetPage]) -> dict[str, int]:
    """Every revision these pages' history tables put in order, as `{normalised label: position}`.

    Built from the union of the history tables, so a later sheet listing `A, B, C` establishes the order
    of all three even though only `C` is on that page. This is the whole basis for comparison — nothing
    here derives order from a label's spelling.

    A label that appears in no history table is absent from the result, and that absence is what makes
    `governing_revision` refuse: not knowing where a revision sits is different from it being early.

    Where two tables disagree about position, the longer table wins, on the grounds that it has seen more
    revisions and is therefore the later sheet. Where they disagree at equal length, the label is left
    out entirely rather than guessed — an inconsistency between two histories is exactly the case that
    must reach a human.
    """
    positions: dict[str, set[int]] = defaultdict(set)
    longest: dict[str, int] = {}

    for page in pages:
        history = page.revision.history
        for row in history:
            label = normalise(row.label.as_printed)
            index = row.label.sequence_index
            if index is None:
                continue
            if len(history) > longest.get(label, -1):
                longest[label] = len(history)
                positions[label] = {index}
            elif len(history) == longest[label]:
                positions[label].add(index)

    return {label: next(iter(seen)) for label, seen in positions.items() if len(seen) == 1}


def governing_revision(pages: list[SheetPage]) -> GoverningRevision | Unresolved:
    """Which of these pages governs, or why that cannot be said.

    Every page must be of the same sheet — call `group_by_sheet` first. Refuses, in this order:

    * **No sheet number.** There is no identity to reason about, and using the filename or page order is
      the mistake §7 names.
    * **Any page's revision is unknown.** A sheet that does not say which revision it is cannot be
      ordered against one that does, and treating unknown as early is how a superseded sheet gets used
      with confidence. This refuses even when the other pages order cleanly, because the unknown page
      might be the later one.
    * **Two pages claim the same revision.** Two sheets both stamped Rev B are a package problem, not a
      tie: one of them is wrong and we cannot tell which.
    * **No recorded order links the labels.** Nothing in any history table says which is later. §7 —
      never "the highest letter wins".
    * **The dates contradict the recorded order.** The sheet is telling two stories; that is for a human.

    A single page governs its own sheet, provided its revision is readable. There is nothing to supersede
    and nothing to compare, so refusing would flag every ordinary one-revision sheet in the package and
    teach a reviewer to ignore the flag.
    """
    if not pages:
        raise ValueError("governing_revision needs at least one page")

    numbers = {page.sheet_number for page in pages}
    if len(numbers) != 1:
        raise ValueError(
            f"all pages must be of one sheet; got {sorted(str(n) for n in numbers)}. "
            "Call group_by_sheet first."
        )

    sheet_number = pages[0].sheet_number
    if sheet_number is None:
        return Unresolved(
            None,
            NO_SHEET_NUMBER,
            f"{len(pages)} page(s) carry no sheet number, so there is no identity to group them by. "
            "Sheet identity is the sheet number, never the filename or the page order.",
            tuple(pages),
        )

    unknown = [page for page in pages if page.label is None]
    if unknown:
        return Unresolved(
            sheet_number,
            UNKNOWN_REVISION,
            f"{len(unknown)} of {len(pages)} page(s) of sheet {sheet_number} do not say which revision "
            "they are. An unknown revision cannot be ordered against a known one, and treating it as "
            "the earlier is how a superseded sheet gets used with confidence.",
            tuple(pages),
        )

    by_label: dict[str, list[SheetPage]] = defaultdict(list)
    for page in pages:
        assert page.label is not None  # established above
        by_label[page.label].append(page)

    repeated = {label: found for label, found in by_label.items() if len(found) > 1}
    if repeated:
        return Unresolved(
            sheet_number,
            DUPLICATE_REVISION,
            f"sheet {sheet_number} has more than one page claiming revision "
            f"{', '.join(sorted(repeated))}. One of them is wrong and nothing here can tell which.",
            tuple(pages),
        )

    if len(pages) == 1:
        return GoverningRevision(sheet_number, pages[0])

    order = recorded_order(pages)
    unplaced = sorted(set(by_label) - set(order))
    if unplaced:
        return Unresolved(
            sheet_number,
            NO_RECORDED_ORDER,
            f"nothing in any revision history on sheet {sheet_number} says where "
            f"{', '.join(unplaced)} sits relative to the others. Ordering comes from what a drawing "
            "records, not from the spelling of a label — 'the highest letter wins' is exactly the rule "
            "that picks the wrong sheet when a vendor restarts numbering.",
            tuple(pages),
        )

    ranked = sorted(pages, key=lambda page: order[page.label or ""])
    winner = ranked[-1]

    contradiction = _date_contradiction(ranked)
    if contradiction is not None:
        return Unresolved(sheet_number, DATE_CONTRADICTS_ORDER, contradiction, tuple(pages))

    return GoverningRevision(
        sheet_number,
        winner,
        tuple(
            SupersededPage(
                page,
                superseded_by=winner.label or "",
                reason=(
                    f"revision {page.label} is earlier than {winner.label} in the revision history "
                    f"printed on sheet {sheet_number}. Retained: a reviewer may need to see what it "
                    "said."
                ),
            )
            for page in ranked[:-1]
        ),
    )


def _date_contradiction(ranked: list[SheetPage]) -> str | None:
    """Whether the dates disagree with the recorded order. `None` when they agree or cannot say.

    Corroboration only, and only where a date is *certain*: an ambiguous date (#183 keeps both readings
    of `03/04/26`) tells us nothing, and a date with an invented century is not evidence either. Treating
    either as agreement would let the weaker signal quietly confirm the stronger one.
    """
    dated: list[tuple[str, date]] = []
    for page in ranked:
        current = page.revision.current
        if current is None or current.date is None:
            continue
        certain = current.date.certain_date
        if certain is not None:
            dated.append((page.label or "", certain))

    # `pairwise` over the pages that *have* a certain date, so a sheet with no date in the middle does not
    # break the comparison of the two either side of it — it simply is not part of it.
    for (earlier_label, earlier), (later_label, later) in pairwise(dated):
        if earlier > later:
            return (
                f"the revision history puts {earlier_label} before {later_label}, but {earlier_label} "
                f"is dated {earlier} and {later_label} is dated {later}. The sheet is telling two "
                "different stories about which came first, and a date never overrides the recorded "
                "order — nor is it ignored when it contradicts it."
            )
    return None
