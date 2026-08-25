"""What a drawing page is, or the honest admission that we could not tell (#161, B6.2).

A vendor package is dozens of pages — plans, elevations, sections, schedules, title sheets — and which
check runs against which page depends on knowing what each page **is**.
`docs/DESIGN_EXTRACTION.md` §3.2 states the stake plainly: *"A countertop width found on a cabinet
elevation is a plausible number attached to the wrong drawing, and no tolerance check catches it."*
Nothing downstream can recover from a page classified wrongly, because every later guard assumes the
number came off the right sheet.

So four things hold here.

**`None` is a real answer, not a gap.** A page nobody could classify is not rounded to the nearest
plausible type. `vocabulary/page_types.py` deliberately has no `UNKNOWN` member for the same reason: an
unknown that is a value gets compared, filtered and displayed like any other type.

**The title block decides; view tags only speak when it is silent.** This is the one precedence rule in
the module and it is not arbitrary. A title block describes *this sheet*; a view tag on the sheet is a
callout to a different one. A plan sheet carrying `SECTION A-A` markers is completely ordinary, so
treating that as a contradiction would make almost every page unknown — and a classifier that answers
"unknown" for everything is one somebody switches off.

**Two type words in the title block is unknown, not a choice between them.** `PLAN AND ELEVATION` is a
real sheet and this refuses it, on purpose: a check scoped to the plan must not silently receive a sheet
that is half elevation. Assigning one of the two halves would be picking, and picking is the failure
this module exists to avoid.

**Confidence is carried and never consulted.** Backend §6.3: a model may report how sure it is, and that
number has no authority. It is recorded for diagnosis and no branch reads it —
`tests/extraction/test_page_type.py` asserts that changing it never changes an answer.

**Geometry is deliberately not implemented, and that is a narrowing of scope worth stating.** The issue
names page geometry as a third signal. Any threshold — an aspect ratio that means "schedule", a line
density that means "detail" — is a number that has to come from real drawings, and there are none yet
(#274). §9: *"a fixture invented today encodes today's guess as ground truth."* `Signal.GEOMETRY` is
reserved so the shape is ready; inventing the thresholds would be choosing values this story does not
give me, and a wrong one classifies confidently.

Source: backend proposal §10.1 · Design: `docs/DESIGN_EXTRACTION.md` §3.2 ·
Verification: `tests/extraction/test_page_type.py`
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final

from extraction.manifest import PageRecord
from vocabulary.page_types import PageType

__all__ = [
    "Classification",
    "PageText",
    "Signal",
    "classify",
    "is_same_view_eligible",
]


class Signal(StrEnum):
    """Which kind of evidence decided a classification.

    Recorded on every answer because *"every classification records the evidence that produced it"* —
    and because a reviewer disputing a page type needs to know whether we read it off the title block or
    inferred it, which are very different claims.
    """

    TITLE_TEXT = "title_text"
    VIEW_TAG = "view_tag"
    GEOMETRY = "geometry"
    """Reserved, and nothing produces it. See the module docstring: the thresholds need real drawings."""


#: The words that name a page type in a title block, and the type each names.
#:
#: Ordered longest-first when rendered into the pattern below, so `TITLE SHEET` is not read as `TITLE`
#: inside a longer phrase and `PARTIAL PLAN` still matches `PLAN`. Matched on word boundaries, so
#: `SECTIONAL` is not `SECTION` and `PLANNING` is not `PLAN` — a substring search here would classify a
#: sheet by a word that happened to contain a type name.
TYPE_WORDS: Final[dict[str, PageType]] = {
    "TITLE SHEET": PageType.TITLE,
    "COVER SHEET": PageType.TITLE,
    "SCHEDULE": PageType.SCHEDULE,
    "ELEVATION": PageType.ELEVATION,
    "SECTION": PageType.SECTION,
    "DETAIL": PageType.DETAIL,
    "PLAN": PageType.PLAN,
}

_WORD_PATTERN: Final = re.compile(
    r"\b("
    + "|".join(re.escape(word) for word in sorted(TYPE_WORDS, key=len, reverse=True))
    + r")\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class PageText:
    """The text somebody else extracted from a page.

    Two separate fields rather than one bag, because the precedence rule above depends on telling them
    apart: a title block describes this sheet and a view tag points at another. Flattening them would
    make that distinction unavailable exactly where it matters.

    `confidence` is whatever a model reported about its reading, if a model was involved. It is
    diagnostic and no branch in this module reads it.
    """

    title_block: tuple[str, ...] = ()
    view_tags: tuple[str, ...] = ()
    confidence: Decimal | None = None

    def __post_init__(self) -> None:
        for name, lines in (("title_block", self.title_block), ("view_tags", self.view_tags)):
            if isinstance(lines, str) or not all(isinstance(line, str) for line in lines):
                raise TypeError(f"{name} must be a sequence of strings, not a single string")


@dataclass(frozen=True, slots=True)
class Classification:
    """What a page is, what said so, and how sure a model claimed to be.

    `page_type is None` means unclassified — and `signal` is `None` with it, because there is no evidence
    for a conclusion that was not reached. `reason` always says something a person can read.
    """

    page_type: PageType | None
    signal: Signal | None
    evidence: str | None
    reason: str
    confidence: Decimal | None = None

    @property
    def is_classified(self) -> bool:
        """Named rather than left to `page_type is not None`, so call sites read as intent."""
        return self.page_type is not None


def _words_in(lines: tuple[str, ...]) -> list[tuple[str, PageType, str]]:
    """Every type word found, as `(matched word, type, the line it was on)`, in reading order."""
    found: list[tuple[str, PageType, str]] = []
    for line in lines:
        for match in _WORD_PATTERN.finditer(line):
            word = match.group(1).upper()
            found.append((word, TYPE_WORDS[word], line.strip()))
    return found


def classify(page: PageRecord, text: PageText) -> Classification:
    """Classify one page. Deterministic: the same inputs always give the same answer.

    Nothing here reads a clock, a random source, or `text.confidence`. The only ordering that matters is
    the order the lines were given in, which comes from the page itself.

    `page` is accepted and not currently read. That is deliberate rather than an oversight: it is the
    argument a geometry signal would need, and the design fixes this signature
    (`classify(page, text) -> Classification`). Taking it now means adding geometry later does not change
    every call site.
    """
    del page  # the geometry signal that would use it is not implemented; see the module docstring

    title_hits = _words_in(text.title_block)
    distinct = {found_type for _, found_type, _ in title_hits}

    if len(distinct) > 1:
        named = ", ".join(sorted(found_type.value for found_type in distinct))
        return Classification(
            None,
            None,
            None,
            f"the title block names more than one page type ({named}), so this sheet is both or "
            "neither. Choosing one would give a check scoped to that type a sheet that is only "
            "partly it.",
            text.confidence,
        )

    if len(distinct) == 1:
        word, found_type, line = title_hits[0]
        return Classification(
            found_type,
            Signal.TITLE_TEXT,
            line,
            f"the title block says {word!r}",
            text.confidence,
        )

    # The title block said nothing about the page type. Only now do the view tags get a say.
    tag_hits = _words_in(text.view_tags)
    tag_types = {found_type for _, found_type, _ in tag_hits}

    if len(tag_types) == 1:
        word, found_type, line = tag_hits[0]
        return Classification(
            found_type,
            Signal.VIEW_TAG,
            line,
            f"the title block names no page type; a view tag says {word!r}",
            text.confidence,
        )

    if len(tag_types) > 1:
        named = ", ".join(sorted(found_type.value for found_type in tag_types))
        return Classification(
            None,
            None,
            None,
            f"the title block names no page type and the view tags disagree ({named}). A tag points at "
            "another sheet, so several of them say nothing about this one.",
            text.confidence,
        )

    return Classification(
        None,
        None,
        None,
        "nothing on this page names a page type. It is still extracted; it is only excluded from "
        "same_view scope resolution.",
        text.confidence,
    )


def is_same_view_eligible(classification: Classification) -> bool:
    """Whether a page may take part in `scope: same_view` selection.

    The fifth acceptance criterion, and the only consequence an unknown page carries: *"an `unknown` page
    is still extracted; it is excluded only from `scope: same_view` selectors."*

    A function rather than a rule written at each call site, so "unknown pages are excluded" is one fact
    with one test. §3.3 covers the other half — `same_view` returns an explicit empty result rather than
    widening — and that belongs to #162.
    """
    return classification.is_classified
