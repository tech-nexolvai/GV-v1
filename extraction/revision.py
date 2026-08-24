"""Reading the revision block: what the sheet says about which revision it is (#183, B11.1).

`docs/DESIGN_EXTRACTION.md` §7: *"`C5` pins the bytes; this decides which sheet governs. Both are
required and neither substitutes for the other."* This module does the reading half only. Deciding which
of several sheets governs is B11.2 (#184), and turning an unresolved supersession into REVIEW REQUIRED is
B11.3 (#185) — nothing here ranks, compares or chooses.

Four things this module refuses to do, each because doing it is how a superseded sheet gets used with
confidence:

**It never turns "no revision block" into a revision.** `RevisionBlock.current` is `None` for a sheet
that says nothing, and §7 is explicit: *"Unknown revision and the first revision are different facts."*
A sheet with no block is not revision 0, not revision A, and not "the original" — it is a sheet we could
not read a revision from, which is a fact a reviewer can act on. `PageRecord.page_type` already uses
`None` this way for classification, and this follows it.

**It never normalises in place.** `as_printed` is what the drawing says — `A`, `01`, `Rev C` — and
`normalise()` is a separate function returning a separate value. A vendor who prints `01` and a vendor
who prints `1` are telling us the same thing and must still be quotable exactly as they wrote it, because
a reviewer's report cites the sheet, not our tidied version of it.

**It never resolves an ambiguous date.** `03/04/26` is the third of April or the fourth of March, and
this module records both readings rather than picking. That matters here more than in most places: dates
are how supersession gets decided, so a silently chosen interpretation is a silently chosen governing
sheet.

**It fails to unknown rather than guessing.** The patterns below are deliberately narrow: an explicit
`REV`/`REVISION` label, or a recognisable history table. Anything else reads as unknown.
`docs/DESIGN_EXTRACTION.md` §9 is the reason — *"a fixture invented today encodes today's guess as
ground truth"* — and no real drawings exist yet. When they arrive these patterns will need widening
against them, and widening a conservative reader is safe in a way narrowing an eager one is not: every
sheet this version cannot read is visibly unknown rather than quietly wrong.

Source: backend proposal §11; `AGENTS.md` §2.7 · Design: `docs/DESIGN_EXTRACTION.md` §7 ·
Verification: `tests/extraction/test_revision.py`
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Final

__all__ = [
    "RevisionBlock",
    "RevisionDate",
    "RevisionHistoryRow",
    "RevisionLabel",
    "normalise",
    "read_revision_block",
    "read_revision_date",
]

#: An explicit revision label: `REV A`, `Revision: 01`, `REV. C`.
#:
#: The label word is required. A bare `A` in a title block is a column heading as often as a revision,
#: and guessing wrong here chooses a governing sheet.
_LABELLED = re.compile(
    r"\bREV(?:ISION)?\b\.?\s*[:\-]?\s*(?P<value>[A-Za-z]{0,3}\s?\d{1,3}[A-Za-z]?|[A-Za-z]{1,2})\b",
    re.IGNORECASE,
)

#: A date printed with separators: `03/04/26`, `3-4-2026`, `2026.03.04`.
_SEPARATED_DATE = re.compile(
    r"\b(?P<a>\d{1,4})\s*[/\-.]\s*(?P<b>\d{1,2})\s*[/\-.]\s*(?P<c>\d{1,4})\b"
)

#: How many leading characters of a history line are searched for a revision label.
#:
#: A revision table's label is its first column. Without a bound, a description mentioning "revision"
#: further along the line would be read as the row's own label — so the search is anchored to the start.
_LABEL_WINDOW: Final = 24


@dataclass(frozen=True, slots=True)
class RevisionDate:
    """A date as printed, with every reading it could have.

    `candidates` holds one entry when the printed form admits exactly one interpretation, several when it
    is ambiguous, and **none** when the text could not be read as a date at all. That last case still
    keeps `as_printed`: a date we could not parse is evidence about the sheet, and dropping it would turn
    "there is a date here we do not understand" into "there is no date", which are different facts.

    `century_assumed` is separate from the candidate count on purpose. `04/05/2026` has two readings and
    no century guess; `04/05/26` has two readings *and* required a century. Both are uncertain, and a
    caller that treats only multi-candidate dates as uncertain would accept the second as settled.
    """

    as_printed: str
    candidates: tuple[date, ...] = ()
    century_assumed: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.as_printed, str) or not self.as_printed.strip():
            raise ValueError("as_printed must be the date text as the drawing shows it")
        if len(set(self.candidates)) != len(self.candidates):
            raise ValueError("candidates must not repeat a reading")
        if tuple(sorted(self.candidates)) != self.candidates:
            raise ValueError("candidates must be in order, so two equal dates compare equal")

    @property
    def is_certain(self) -> bool:
        """One reading, and no century was invented to get it.

        The property is phrased positively and narrowly so the safe answer is the default: anything this
        module was not sure about answers `False`, including a date it could not parse.
        """
        return len(self.candidates) == 1 and not self.century_assumed

    @property
    def certain_date(self) -> date | None:
        """The date, only when there is exactly one and nothing was assumed. Otherwise `None`."""
        return self.candidates[0] if self.is_certain else None


@dataclass(frozen=True, slots=True)
class RevisionLabel:
    """One revision as the sheet prints it — `docs/DESIGN_EXTRACTION.md` §7's shape.

    `sequence_index` is where this row sat in the history table, 0-based, and it is **not** a revision
    number. A sheet whose first history row reads `C` has `sequence_index=0` and `as_printed="C"`; those
    are different facts and conflating them is how the first revision and the earliest-listed revision
    become the same thing. It is `None` for a label read from outside a table, where there is no order to
    record.
    """

    as_printed: str
    date: RevisionDate | None = None
    sequence_index: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.as_printed, str) or not self.as_printed.strip():
            raise ValueError(
                "as_printed must be the revision identifier exactly as the drawing prints it"
            )
        if self.sequence_index is not None and (
            isinstance(self.sequence_index, bool) or not isinstance(self.sequence_index, int)
        ):
            raise TypeError("sequence_index must be an integer or None")
        if self.sequence_index is not None and self.sequence_index < 0:
            raise ValueError("sequence_index is 0-based and cannot be negative")


@dataclass(frozen=True, slots=True)
class RevisionHistoryRow:
    """One row of the revision history table, with the text beside it kept as printed."""

    label: RevisionLabel
    description: str | None = None


@dataclass(frozen=True, slots=True)
class RevisionBlock:
    """Everything one sheet says about its revision.

    `current is None` means **unknown revision**, and that is the whole point of the type. §7: *"Unknown
    revision and the first revision are different facts. Treating the first as the second is exactly how
    a superseded sheet gets used with confidence."*
    """

    current: RevisionLabel | None = None
    history: tuple[RevisionHistoryRow, ...] = ()

    @property
    def is_unknown(self) -> bool:
        """No revision could be read. Named so a caller cannot mistake it for "revision zero"."""
        return self.current is None


def normalise(as_printed: str) -> str:
    """A comparable form of a printed revision label — separate from the label itself.

    `Rev C`, `REV. C` and `C` all normalise to `C`; `01` and `1` both to `1`. Comparison needs this;
    a reviewer's report needs `as_printed`. Keeping them apart is what lets both be true.

    This does not decide which of two revisions is later. Ordering revisions is B11.2's, and a function
    here that returned something sortable would be that decision made in the wrong place.
    """
    stripped = re.sub(r"^\s*REV(?:ISION)?\b\.?\s*[:\-]?\s*", "", as_printed, flags=re.IGNORECASE)
    collapsed = re.sub(r"\s+", "", stripped).upper()
    # Leading zeros only, and only when what remains is a number: `01` and `1` are one revision, while
    # `0A` is not `A` and must not become it.
    if collapsed.isdigit():
        return str(int(collapsed))
    return collapsed


def read_revision_date(text: str) -> RevisionDate | None:
    """Read a date from `text`, keeping every reading it could have. `None` if there is no date at all.

    The distinction between `None` and a `RevisionDate` with no candidates is deliberate: the first means
    nothing date-shaped was found, the second means something was and could not be interpreted. A caller
    deciding whether to trust a sheet needs to tell those apart.
    """
    found = _SEPARATED_DATE.search(text)
    if found is None:
        iso = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
        if iso is None:
            return None
        printed = iso.group(0)
        year, month, day = (int(part) for part in iso.groups())
        try:
            return RevisionDate(printed, (date(year, month, day),))
        except ValueError:
            # `2026-02-30` matches the shape and is not a date. Kept as printed with no reading, for the
            # same reason as any other unparseable date: it is evidence, and dropping it would turn "a
            # date we cannot interpret" into "no date".
            return RevisionDate(printed)

    printed = found.group(0)
    a, b, c = (found.group("a"), found.group("b"), found.group("c"))
    century_assumed = False
    readings: set[date] = set()

    # Every field order this project might meet, tried rather than chosen — but year-first only when the
    # year is written in full.
    #
    # **Why that restriction.** Without it, `03/04/26` also reads as 2003-04-26, and `25/12/26` as
    # 2025-12-26. Those are arithmetically valid and not conventions anyone prints: a two-digit
    # year-first date is not a form in use, while `2026-03-04` plainly is. Keeping them would add
    # readings nobody meant, and "more ambiguous" is only the safe direction when the ambiguity is real —
    # a date that can never be certain stops being a signal and starts being noise, which is how a
    # fail-closed rule gets switched off for being too noisy.
    #
    # Found by writing the test first and getting three candidates where I expected two.
    orders: tuple[tuple[str, str, str], ...] = ((c, a, b), (c, b, a))
    if len(a) == 4:
        orders += ((a, b, c),)

    for year_raw, month_raw, day_raw in orders:
        if len(year_raw) == 4:
            year, assumed = int(year_raw), False
        elif len(year_raw) <= 2:
            # The century is not this module's to choose, and it is recorded rather than hidden. A
            # drawing dated `26` is 2026 in every realistic case, but "realistic" is a judgement and
            # `century_assumed` is how the caller gets told one was made.
            year, assumed = 2000 + int(year_raw), True
        else:
            continue
        try:
            readings.add(date(year, int(month_raw), int(day_raw)))
        except ValueError:
            continue
        century_assumed = century_assumed or assumed

    return RevisionDate(printed, tuple(sorted(readings)), century_assumed and bool(readings))


def read_revision_block(lines: list[str]) -> RevisionBlock:
    """Read a sheet's revision block from title-block text, in the order the lines appear.

    `lines` is text somebody else extracted — this module does no rendering and opens no files, which is
    what keeps it testable now, while B2 (real PDFs) is still blocked on drawings.

    **The current revision is the last history row when there is a table**, because that is what a
    revision table means: rows are added as revisions are issued. Where there is no table, an explicit
    `REV`-labelled line is used instead. Where there is neither, the block is unknown — never a default.

    Deliberately not done here: no ranking of one sheet against another, and no attempt to read a
    revision out of a filename or a page position. §7 — *"Sheet identity is the sheet number — never the
    filename or page order."*
    """
    if not isinstance(lines, list) or any(not isinstance(line, str) for line in lines):
        raise TypeError("lines must be a list of strings — the title-block text, in reading order")

    history: list[RevisionHistoryRow] = []
    standalone: RevisionLabel | None = None

    for line in lines:
        labelled = _LABELLED.search(line[:_LABEL_WINDOW])
        if labelled is None:
            continue

        value = labelled.group("value").strip()
        found_date = read_revision_date(line)
        # What is left of the line once the label and any date are removed: the description column.
        remainder = line[labelled.end() :]
        if found_date is not None:
            remainder = remainder.replace(found_date.as_printed, " ")
        description = re.sub(r"\s+", " ", remainder).strip(" \t|,-") or None

        if description is not None or found_date is not None:
            # A row with something beside the label is a history row: a table entry.
            history.append(
                RevisionHistoryRow(
                    RevisionLabel(value, found_date, sequence_index=len(history)),
                    description,
                )
            )
        elif standalone is None:
            # A bare labelled line — "REV C" on its own — is the sheet's current revision, and the first
            # one wins so a repeated label further down cannot displace it.
            standalone = RevisionLabel(value, found_date)

    if history:
        # The last row, kept with its own `sequence_index`, so "which revision" and "where it sat in the
        # table" stay separate facts.
        return RevisionBlock(current=history[-1].label, history=tuple(history))
    return RevisionBlock(current=standalone, history=())
