"""Sheet identity, and the `same_view` scope resolver (#162, B6.3).

`docs/RULE_ENGINE_SPEC.md` §3a names `scope: same_view` as an input selector and nothing resolved it.
This answers *"which observations are on the same view as this one?"* — and, more importantly, refuses to
answer when it cannot.

**Never widening is the whole point.** `docs/DESIGN_EXTRACTION.md` §3.3: *"`same_view` returns an explicit
empty result when it cannot resolve. It never widens to 'the whole package' — silently widening scope is
how a rule finds a number that satisfies it somewhere."* A rule looking for one cabinet width will find
*a* cabinet width if you let it search far enough, and it will then compute a confident, fully traced,
completely wrong answer. So the failure direction here is always "nothing", never "more".

**Two bases, and the resolver says which it used.** A sheet number groups pages that are the same sheet.
Where a page has no readable number, the scope is that page alone — because one page is trivially the same
view as itself, and that is a real answer rather than a fallback to everything. The result records
`ScopeBasis.PAGE_INDEX` so a reviewer reading a finding can tell "we grouped by sheet number" from "we
could only see this page".

**As printed, with normalisation separate.** `A-101`, `A101` and `A 101` are one sheet and must still be
quotable exactly as the vendor drew them, for the same reason as a revision label (#183): a report cites
the sheet, not our tidied version.

**An unclassified page is excluded, and this is where that bites.** §3.2 says a page nobody could classify
*"is still extracted; it is only excluded from `scope: same_view` selectors"* — this module is that
exclusion. A page whose type is unknown cannot be asserted to be the same *view* as anything, so it is not
a member; and when the subject itself is unclassified the scope is empty rather than "just this page".

**What this deliberately does not do.** A view is `(page_id, tag)` per §4.1, and tags are B7.1 (#164).
Until then "same view" means "same sheet", which is coarser: two views on one sheet resolve to each other.
That is stated rather than hidden because coarser scope is the *less* safe direction — a rule could reach a
number from the neighbouring view on the same sheet. It is bounded to one sheet rather than the package,
and #164 narrows it further.

Source: `docs/RULE_ENGINE_SPEC.md` §3a · Design: `docs/DESIGN_EXTRACTION.md` §3.3 ·
Verification: `tests/extraction/test_sheet.py`
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from extraction.page_type import Classification, is_same_view_eligible

__all__ = [
    "SameViewScope",
    "ScopeBasis",
    "SheetIdentity",
    "ViewCandidate",
    "normalise_sheet_number",
    "read_sheet_identity",
    "resolve_same_view",
]

#: The digits-and-letters shape of a sheet number, without deciding where it may appear.
_NUMBER_SHAPE: Final = r"[A-Z]{1,3}\s?[-.]?\s?\d{1,4}(?:\.\d{1,2})?"

#: A sheet number introduced by a label: `SHEET A-101`, `DWG: AD 07`, `SHEET NO. M-2.1`.
#:
#: A label licenses the spaced form, because whoever wrote `DWG: AD 07` has told us what it is.
_LABELLED_NUMBER: Final = re.compile(
    r"\b(?:SHEET|SHT|DWG|DRAWING)\s*(?:NO\.?|#|:)?\s*(?P<number>" + _NUMBER_SHAPE + r")\b",
    re.IGNORECASE,
)

#: An unlabelled sheet number at the start of a line: `A-101`, `A101`, `M-2.1`.
#:
#: **No space is allowed here, and that restriction is the whole difference.** `JOB 4471` has the same
#: shape as `AD 07` — three letters, a gap, digits — and my first pattern read it as a sheet number. A job
#: number grouped as a sheet merges unrelated drawings into one view, which is exactly the widening this
#: module exists to prevent, arriving through the reader instead of the resolver.
#:
#: So an unlabelled number must be joined (`A101`) or separated by a hyphen or dot (`A-101`, `M-2.1`).
#: A spaced form needs a label to say what it is. Found by writing the test for a page with no sheet
#: number and getting `JOB 4471` back.
_BARE_NUMBER: Final = re.compile(
    r"^\s*(?P<number>[A-Z]{1,3}[-.]\d{1,4}(?:\.\d{1,2})?|[A-Z]{1,3}\d{1,4}(?:\.\d{1,2})?)\b",
    re.IGNORECASE,
)

#: What a title line looks like when it is the sheet *title* rather than its number.
_TITLE_LABEL: Final = re.compile(r"\b(?:SHEET\s+)?TITLE\s*[:\-]\s*(?P<title>.+)$", re.IGNORECASE)


class ScopeBasis(StrEnum):
    """What the resolver grouped by, recorded on every answer.

    A reviewer disputing a finding's scope needs to know whether we matched a sheet number or could only
    see one page. Those are different confidence claims and collapsing them would hide the weaker one.
    """

    SHEET_NUMBER = "sheet_number"
    PAGE_INDEX = "page_index"


@dataclass(frozen=True, slots=True)
class SheetIdentity:
    """What a page's title block says it is.

    `number_as_printed is None` means no sheet number could be read — which is a real state, not an
    error, and the resolver handles it by scoping to the page alone.
    """

    page_index: int
    number_as_printed: str | None = None
    title: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.page_index, bool) or not isinstance(self.page_index, int):
            raise TypeError("page_index must be an integer")
        if self.page_index < 0:
            raise ValueError("page_index is 0-based and cannot be negative")

    @property
    def normalised_number(self) -> str | None:
        """The comparable form, or `None`. Separate from `number_as_printed`, never replacing it."""
        if self.number_as_printed is None:
            return None
        return normalise_sheet_number(self.number_as_printed)


@dataclass(frozen=True, slots=True)
class ViewCandidate:
    """One page the resolver may include: its identity and whether it was classifiable."""

    identity: SheetIdentity
    classification: Classification


@dataclass(frozen=True, slots=True)
class SameViewScope:
    """Which pages are the same view as the subject — possibly none.

    `members` is page indices rather than objects so a caller cannot mistake this for the observations
    themselves; selecting those is the rule engine's job, and this only bounds where it may look.
    """

    members: tuple[int, ...]
    basis: ScopeBasis | None
    reason: str

    @property
    def is_resolved(self) -> bool:
        """Whether anything is in scope. An unresolved scope has no basis and no members."""
        return bool(self.members)


def normalise_sheet_number(as_printed: str) -> str:
    """A comparable sheet number: uppercase, with spaces and separators removed.

    `A-101`, `A101` and `a 101` all become `A101`. The decimal part is kept, because `M-2.1` and `M-2.10`
    are different sheets and dropping the dot would merge them.

    Comparison needs this; a report needs `number_as_printed`. Keeping them apart is what lets both be
    true, and it is the same split `extraction/revision.py` makes for a revision label.
    """
    return re.sub(r"[\s\-]", "", as_printed).upper()


def read_sheet_identity(page_index: int, lines: list[str]) -> SheetIdentity:
    """Read the sheet number and title from title-block text, as printed.

    Takes text somebody else extracted — no rendering, no file access — for the same reason
    `extraction/revision.py` does: it keeps this testable while B2 is still blocked on real drawings.

    `SHEET 3 OF 7` does not read as a sheet number, and no special case is needed for it: after a label
    the pattern requires letters before digits, and `3` is a digit. I had written an explicit strip for
    that phrase and then found that deleting it failed no test — a guard with nothing behind it. The two
    tests that cover the phrase stay, because the requirement is real even though the pattern shape is
    what now enforces it.
    """
    if not isinstance(lines, list) or any(not isinstance(line, str) for line in lines):
        raise TypeError("lines must be a list of strings — the title-block text, in reading order")

    number: str | None = None
    title: str | None = None

    for line in lines:
        if title is None:
            labelled = _TITLE_LABEL.search(line)
            if labelled is not None:
                title = labelled.group("title").strip() or None

        if number is None:
            # Labelled first: a label is a stronger statement than position on the line.
            found = _LABELLED_NUMBER.search(line) or _BARE_NUMBER.search(line)
            if found is not None:
                number = found.group("number").strip()

    return SheetIdentity(page_index=page_index, number_as_printed=number, title=title)


def resolve_same_view(subject: ViewCandidate, candidates: list[ViewCandidate]) -> SameViewScope:
    """Which of `candidates` share the subject's view. Never widens; may be empty.

    Refuses — with an empty scope — when:

    * **The subject page could not be classified.** §3.2 makes this the one consequence of an unknown
      page type. A page we cannot say is a plan cannot be asserted to be the same *view* as anything.
    * **The subject is not among the candidates.** A caller comparing a page against a manifest that does
      not contain it has made a mistake, and inventing a scope from the rest would answer a question
      nobody asked.

    Otherwise it groups by sheet number where there is one, and by the page itself where there is not.
    **In no branch does it return every candidate as a fallback** — that is the widening §3.3 forbids, and
    the tests assert it for each refusal separately rather than once.
    """
    if not is_same_view_eligible(subject.classification):
        return SameViewScope(
            (),
            None,
            f"page {subject.identity.page_index} could not be classified, so it is excluded from "
            "same_view selection. It is still extracted — this only bounds what a rule may compare it "
            "with.",
        )

    present = any(
        candidate.identity.page_index == subject.identity.page_index for candidate in candidates
    )
    if not present:
        return SameViewScope(
            (),
            None,
            f"page {subject.identity.page_index} is not among the {len(candidates)} candidate page(s), "
            "so there is nothing to resolve against. Returning the others would answer a different "
            "question.",
        )

    subject_number = subject.identity.normalised_number
    if subject_number is None:
        return SameViewScope(
            (subject.identity.page_index,),
            ScopeBasis.PAGE_INDEX,
            f"page {subject.identity.page_index} has no readable sheet number, so the scope is that page "
            "alone. One page is trivially the same view as itself; widening to the package would let a "
            "rule find a number on an unrelated sheet.",
        )

    members = sorted(
        candidate.identity.page_index
        for candidate in candidates
        # Unclassified pages are excluded as members too, not only as subjects — the same §3.2 rule
        # read from the other side.
        if is_same_view_eligible(candidate.classification)
        and candidate.identity.normalised_number == subject_number
    )

    return SameViewScope(
        tuple(members),
        ScopeBasis.SHEET_NUMBER,
        f"{len(members)} page(s) carry sheet number {subject.identity.number_as_printed!r} "
        f"(normalised {subject_number}) and could be classified.",
    )
