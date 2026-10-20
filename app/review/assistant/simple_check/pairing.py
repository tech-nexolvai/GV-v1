"""Which architect drawing a vendor drawing of a separate file is compared with (#1723). Code, $0.

GV's combined sheets say it by themselves: the architect's drawing is pasted on the vendor's sheet,
or GV typed the area. A vendor PDF and an architect PDF uploaded apart (#1719) say neither, and the
owner's rule is zero human clicks: code pairs each vendor drawing by itself, or the drawing is Not
compared with code's reason (never a question to the reviewer). The October separate-PDF study
(type 1 plan, 2026-10-10) and the industry tools agree on the order:

1. **Printed references.** A sheet number or a view reference printed on both drawings
   (`REF. ELEV 2/A-501` on the vendor's sheet, the architect's sheet number `A-501` in its title
   block): exact, normalised (case, spaces and dashes ignored; `02/A-501` is `2/A501`). The one
   candidate sharing the most references wins; several tied (the views of one referenced sheet) go
   to the layout among themselves.
2. **Layout.** The same dimensions on both: the share of the vendor's printed values the architect's
   drawing also prints (each value once), and, where both drawings' scales are measured, how many of
   the vendor's spans code's drawn-position pairing lines up with the architect's in real inches
   (`dim_pairs.page_pairs`, the old pipeline's `extraction/architect/pairing.py`, page and file
   independent). Only a clear winner pairs: twins (two architect drawings matching equally) and
   drawings whose dimensions the architect does not print stay unpaired.
3. **The compare call** (`schema.Reply.same_drawing`) may still reject the pair: its "no" makes the
   whole drawing Not compared (`run.py`).

A printed reference is never trusted against the layout: when another candidate's dimensions match
clearly better, or the referenced drawing prints too few of the vendor's dimensions, nothing is
paired and the reason says so.

Several vendor drawings may pair with one architect drawing (five variants of one layout).

**Since #1798** printed values rarely agree across the two files (the vendor's mistakes are what
the check looks for: on two real sets split into two PDFs, `decide` paired 1 vendor drawing of
19), so the work list ranks architect ITEMS (the drawings of one page of the architect's file)
with `rank`, on evidence that needs no equal value: an item's title GV typed printed (or typed) on
the vendor's sheet, the same section of the divider pages (another section contradicts), a view
reference, spans lined up, item tags and naming words both print (weighted by how few items print
them), the same overall width or height within 4%, a plan against a single elevation
(contradicts). Code pairs alone only on strong evidence well ahead of the next item
(`clear_winner`, or `decide`); otherwise the AI page matcher (`page_match.py`) is asked, and
`agree` makes the pair only when the matcher is sure and code holds strong evidence for its pick
or ranks it clearly first, never against a contradiction or code's own other pick. Zero wrong
pairs matters more than coverage.

Pure: plain values in, a decision out. The work list (`worklist._pair_across`) builds the facts.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Final

__all__ = [
    "CODE_BAR",
    "CODE_MARGIN",
    "MIN_LAYOUT_SHARE",
    "MIN_LAYOUT_VALUES",
    "TIER",
    "UNPAIRED",
    "Candidate",
    "Decision",
    "Facts",
    "Scored",
    "clear_winner",
    "decide",
    "evidence_words",
    "rank",
    "references_in",
    "section_key",
    "tags_in",
    "title_key",
    "views_in",
    "words_in",
]

#: The Not-compared words of a vendor drawing code could not pair (the run lists these drawings).
UNPAIRED: Final = "not paired with an architect drawing"
#: A vendor drawing needs this many printed values to be paired by its layout.
MIN_LAYOUT_VALUES: Final = 3
#: The share of the vendor's values the architect must also print for a layout pairing.
MIN_LAYOUT_SHARE: Final = 0.5
#: A referenced drawing must print at least this share of the vendor's values (or line up
#: `MIN_SPANS` spans): a reference alone never pairs two drawings that share no dimension.
MIN_REFERENCE_SHARE: Final = 0.25
#: How much better (share of values) the winner must be than the next candidate.
MARGIN: Final = 0.15
#: Spans lined up by drawn position that count as layout evidence on their own.
MIN_SPANS: Final = 3

#: `02/A-501`, `3/A401`, `01/A13.02`: a view number over a sheet number.
_VIEW_REF: Final = re.compile(
    r"\b(\d{1,3})\s*/\s*([A-Z]{1,3}\s*[-.]?\s*\d{1,3}(?:\.\d{1,3})?[A-Z]?)\b"
)
#: `A-501`, `A13.02`, `ID-201`, `A501`: a sheet number.
_SHEET: Final = re.compile(
    r"\b([A-Z]{1,3}-\d{1,3}(?:\.\d{1,3})?|[A-Z]{1,3}\d{1,3}\.\d{1,3}|A\d{3})\b"
)


def _sheet(text: str) -> str:
    return re.sub(r"[\s-]+", "", text.upper())


def references_in(texts: Iterable[str]) -> frozenset[str]:
    """The sheet numbers and view references printed in `texts`, normalised: a view reference
    gives itself (`2/A501`) and its sheet (`A501`)."""
    found: set[str] = set()
    for text in texts:
        upper = str(text or "").upper()
        for view, sheet in _VIEW_REF.findall(upper):
            found.add(f"{int(view)}/{_sheet(sheet)}")
            found.add(_sheet(sheet))
        for sheet in _SHEET.findall(upper):
            found.add(_sheet(sheet))
    return frozenset(found)


@dataclass(frozen=True)
class Facts:
    """What code knows of one drawing for pairing."""

    ref: str
    values: tuple[Fraction, ...] = ()
    """Its printed dimension values, in inches."""
    references: frozenset[str] = frozenset()
    spans: Callable[[Facts], int] | None = field(default=None, compare=False)
    """For a vendor drawing: how many of its spans code's drawn-position pairing lines up with a
    candidate's (`dim_pairs`); `None` when the scales are not known."""
    rows: tuple[Any, ...] | None = field(default=None, compare=False)
    """The drawing's raw labels, its page's scales and its side, for `spans`."""
    oriented: tuple[tuple[str, Fraction], ...] = ()
    """Its printed dimension values with their direction (`h` / `v`), in inches (`rank`)."""
    tags: frozenset[str] = frozenset()
    """The tag codes printed on it, normalised (`tags_in`: `EQ-512_ALT-X` is `EQ512`)."""
    words: frozenset[str] = frozenset()
    """The naming words of its titles and notes (`words_in`: `WARDROBE`, `LIBRARY`)."""
    views: frozenset[str] = frozenset()
    """The kinds of view its titles name (`views_in`: plan, elevation, section)."""
    section: str | None = None
    """The project and section GV's divider pages put it in (`section_key`), when known."""
    title: str | None = None
    """An architect item's title GV typed on its page (`title_key`: "library shelving unit
    at study"), when there is one."""
    text: str = ""
    """A vendor drawing's printed words and GV's typed title boxes on its page, normalised as
    `title_key` normalises a title (so a title printed in its title block is found in it)."""
    phrases: tuple[str, ...] = ()
    """A vendor drawing's printed phrases of two words or more (lines joined down a title block's
    cell too), normalised: a shorter name of an item ("window seat") found in its title."""


@dataclass(frozen=True)
class Candidate:
    ref: str
    shared_references: tuple[str, ...]
    share: float
    """The share of the vendor's values this drawing also prints."""
    matched: int
    spans: int


@dataclass(frozen=True)
class Decision:
    chosen: str | None
    """The architect drawing's ref, or `None`."""
    why: str
    """How code chose it, or why it chose none (plain words)."""
    candidates: tuple[Candidate, ...] = ()


def _share(vendor: Sequence[Fraction], architect: Sequence[Fraction]) -> tuple[int, float]:
    if not vendor:
        return 0, 0.0
    common = Counter(vendor) & Counter(architect)
    matched = sum(common.values())
    return matched, matched / len(vendor)


def _layout_words(one: Candidate, total: int) -> str:
    words = f"{one.matched} of its {total} dimensions printed on both"
    if one.spans:
        words += f", {one.spans} spans lined up by drawn position"
    return words


def _clearly_better(best: Candidate, other: Candidate) -> bool:
    return best.share - other.share >= MARGIN or (
        best.spans - other.spans >= 2 and best.share >= other.share
    )


def decide(vendor: Facts, candidates: Sequence[Facts]) -> Decision:
    """The architect drawing `vendor` is compared with (see the module), or none and why."""
    if not candidates:
        return Decision(None, f"{UNPAIRED}: the set has no architect drawing to pair it with")
    total = len(vendor.values)
    scored: list[Candidate] = []
    for one in candidates:
        matched, share = _share(vendor.values, one.values)
        spans = vendor.spans(one) if vendor.spans is not None else 0
        scored.append(
            Candidate(
                one.ref,
                tuple(sorted(vendor.references & one.references)),
                share,
                matched,
                spans,
            )
        )
    found = tuple(scored)
    by_layout = sorted(scored, key=lambda c: (c.spans, c.share), reverse=True)
    most = max(len(c.shared_references) for c in scored)
    pool = scored
    if most:
        tied = [c for c in scored if len(c.shared_references) == most]
        if len(tied) == 1:
            winner = tied[0]
            rival = next((c for c in by_layout if c.ref != winner.ref), None)
            if rival is not None and _clearly_better(rival, winner):
                return Decision(
                    None,
                    f"{UNPAIRED}: the printed reference {', '.join(winner.shared_references)} "
                    f"names {winner.ref}, but {rival.ref} matches its dimensions better "
                    f"({_layout_words(rival, total)} against {winner.matched})",
                    found,
                )
            if winner.share < MIN_REFERENCE_SHARE and winner.spans < MIN_SPANS:
                return Decision(
                    None,
                    f"{UNPAIRED}: the printed reference {', '.join(winner.shared_references)} "
                    f"names {winner.ref}, but it prints only {winner.matched} of the vendor "
                    f"drawing's {total} dimensions",
                    found,
                )
            return Decision(
                winner.ref,
                f"printed reference {', '.join(winner.shared_references)} on both; "
                f"{_layout_words(winner, total)}",
                found,
            )
        pool = tied
    if total < MIN_LAYOUT_VALUES:
        return Decision(
            None,
            f"{UNPAIRED}: no printed reference names one architect drawing, and the vendor "
            f"drawing prints too few dimensions ({total}) to pair it by its layout",
            found,
        )
    ranked = sorted(pool, key=lambda c: (c.spans, c.share), reverse=True)
    best = ranked[0]
    if best.share < MIN_LAYOUT_SHARE and best.spans < MIN_SPANS:
        return Decision(
            None,
            f"{UNPAIRED}: no architect drawing prints its dimensions (the closest, {best.ref}: "
            f"{_layout_words(best, total)})",
            found,
        )
    if len(ranked) > 1 and not _clearly_better(best, ranked[1]):
        return Decision(
            None,
            f"{UNPAIRED}: {best.ref} and {ranked[1].ref} match its dimensions equally "
            f"({best.matched} and {ranked[1].matched} of {total}), so code cannot tell which "
            "one it is",
            found,
        )
    how = "printed reference shared by several views; " if most else ""
    return Decision(best.ref, f"{how}layout: {_layout_words(best, total)}", found)


def values_of(labels: Iterable[Any]) -> tuple[Fraction, ...]:
    """The printed values (inches) of a drawing's labels, each label once."""
    out: list[Fraction] = []
    for label in labels:
        raw = label.get("inches") if isinstance(label, Mapping) else getattr(label, "inches", None)
        try:
            value = Fraction(str(raw))
        except (TypeError, ValueError, ZeroDivisionError):
            continue
        if value > 0:
            out.append(value)
    return tuple(out)


# ---- the code score across separate files (#1798) ----------------------------------------------
#
# `decide` pairs only on printed references or the same printed values, and a vendor's values
# rarely equal the architect's (that is what the check looks for): on two real sets split into an
# architect's PDF and a vendor's PDF it paired 1 vendor drawing of 19. `rank` scores every
# architect drawing on evidence that does not need equal values; `page_match.agree` lets the AI
# page matcher pick only among code's top candidates.

#: A tag code: letters, a dash or underscore, digits (`EQ-512`, `PL-07`, `FX-210-CUST`, `LM-007.C`).
_TAG: Final = re.compile(r"\b([A-Z]{1,4})[-_](\d{1,4})(?![\d/])")
#: An item tag (a number of 100 or more): names one piece, unlike a finish tag (`PL-07`).
_ITEM_TAG: Final = re.compile(r"^[A-Z]{1,4}[1-9]\d{2,3}$")
#: Letters that start a view marker, a count or a size, never a tag.
_NOT_TAG: Final = frozenset({"ID", "X", "NO", "QTY", "TYP", "REF", "SK", "W", "D", "H", "R", "L"})
_WORD: Final = re.compile(r"[A-Z]{4,}")
#: Words every drawing may print: never evidence of which drawing it is.
_STOP_WORDS: Final = frozenset(
    [
        "ELEVATION",
        "ELEVATIONS",
        "VIEW",
        "VIEWS",
        "FRONT",
        "SIDE",
        "BACK",
        "REAR",
        "LEFT",
        "RIGHT",
        "PLAN",
        "PLANS",
        "SECTION",
        "SECTIONS",
        "DETAIL",
        "DETAILS",
        "ENLARGED",
        "SCALE",
        "TYPICAL",
        "NOTE",
        "NOTES",
        "SEE",
        "WITH",
        "FROM",
        "THIS",
        "THAT",
        "THESE",
        "FOR",
        "AND",
        "THE",
        "FINISH",
        "FINISHES",
        "MATCH",
        "PANEL",
        "PANELS",
        "FILLER",
        "FILLERS",
        "CABINET",
        "CABINETS",
        "DOOR",
        "DOORS",
        "DRAWER",
        "DRAWERS",
        "SHELF",
        "SHELVES",
        "TOP",
        "BOTTOM",
        "BASE",
        "UPPER",
        "LOWER",
        "WALL",
        "WALLS",
        "FLOOR",
        "CEILING",
        "OPEN",
        "CLOSED",
        "FIELD",
        "SITE",
        "CONDITION",
        "CONDITIONS",
        "REQUIREMENT",
        "PROVIDED",
        "OWNER",
        "VENDOR",
        "ARCHITECT",
        "DRAWING",
        "DRAWINGS",
        "SHEET",
        "SHOP",
        "MATERIAL",
        "MATERIALS",
        "SPEC",
        "SPECS",
        "SPECIFICATION",
        "CONSTRUCTION",
        "ASSEMBLY",
        "CENTER",
        "CENTRE",
        "LINE",
        "LINES",
        "EQUAL",
        "TYPE",
        "TYPES",
        "ITEM",
        "ITEMS",
        "QTY",
        "QUANTITY",
        "TOTAL",
        "MODEL",
        "HANDLE",
        "HANDLES",
        "PULL",
        "PULLS",
        "HINGE",
        "HINGES",
        "SOFT",
        "CLOSING",
        "BLOCKING",
        "REMOVABLE",
        "ACCESS",
        "FIXED",
        "ADJUSTABLE",
        "HEAVY",
        "DUTY",
        "LEVELERS",
        "GLIDES",
        "NYLON",
        "WOOD",
        "WOODEN",
        "METAL",
        "STONE",
        "QUARTZ",
        "LAMINATE",
        "VENEER",
        "PAINT",
        "PAINTED",
        "BRUSHED",
        "NICKEL",
        "BLACK",
        "WHITE",
        "GREY",
        "GRAY",
        "COLOR",
        "COLOUR",
        "CUST",
        "CUSTOM",
        "INTERIOR",
        "EXTERIOR",
        "INSTALL",
        "INSTALLATION",
        "CONFIRM",
        "APPROVAL",
        "REFERENCE",
        "ONLY",
        "GENERIC",
        "SHALL",
        "WILL",
        "BEFORE",
        "AFTER",
        "ABOVE",
        "BELOW",
        "EACH",
        "ALL",
        "PER",
        "CUT",
        "CLIP",
        "CLIPS",
        "ROOM",
        "ROOMS",
        "UNIT",
        "UNITS",
        "AREA",
        "DIMENSION",
        "DIMENSIONS",
        "VERIFY",
        "OVERALL",
        "HEIGHT",
        "WIDTH",
        "DEPTH",
    ]
)
#: View kinds a title names.
_VIEW_WORDS: Final = (
    ("plan", re.compile(r"\bPLANS?\b")),
    ("elevation", re.compile(r"\b(?:ELEVATIONS?|FRONT\s+VIEW|SIDE\s+VIEW|ELEV\.?)\b")),
    ("section", re.compile(r"\bSECTIONS?\b")),
)
#: The share (or 1") two overall sizes may differ by and still count as the same size.
SIZE_TOLERANCE: Final = 0.04
#: Sizes smaller than this (inches) say nothing about which drawing it is (a 6" filler, a 4" toe).
MIN_TELLING_INCHES: Final = 24
#: Evidence weights (a shared printed reference or 3 spans lined up count most).
_W_REFERENCE: Final = 2.0
_W_SPANS: Final = 2.0
_W_SHARE: Final = 2.0
_W_SIZE: Final = 1.0
_W_TITLE: Final = 4.0
_W_NAMED_IN_TITLE: Final = 2.0
_W_SECTION: Final = 1.5
_W_ONLY_IN_SECTION: Final = 2.0
_W_NEAR: Final = 0.25
_MAX_NEAR: Final = 1.0
_MAX_NAMED: Final = 3.0
#: Code pairs on its own score only above this, and this far ahead of the next candidate.
CODE_BAR: Final = 3.0
CODE_MARGIN: Final = 2.0
#: A sure AI pick with strong code evidence is refused when another item scores this much more;
#: the matcher's further picks are kept only within this of its first (`agree`).
TIER: Final = 1.0
#: With only weak code evidence, a sure AI pick must be code's first by this much (two pieces of
#: weak evidence: two close sizes, or a size and a naming word).
WEAK_LEAD: Final = 0.5


def tags_in(texts: Iterable[str]) -> frozenset[str]:
    """The tag codes printed in `texts`, normalised: letters and the number without its leading
    zeros (`EQ-512_ALT-X` is `EQ512`, `PL-07` is `PL7`); view markers and sizes left out."""
    found: set[str] = set()
    for text in texts:
        for letters, digits in _TAG.findall(str(text or "").upper()):
            if letters not in _NOT_TAG:
                found.add(f"{letters}{int(digits)}")
    return frozenset(found)


def words_in(texts: Iterable[str]) -> frozenset[str]:
    """The naming words of `texts` (4 letters or more, upper case, a plural's S dropped), drawing
    words left out (`_STOP_WORDS`): what names a room or a piece (WARDROBE, LIBRARY, CONSERVATORY).
    """
    found: set[str] = set()
    for text in texts:
        for word in _WORD.findall(str(text or "").upper()):
            if len(word) >= 5 and word.endswith("S") and not word.endswith("SS"):
                word = word[:-1]
            if word not in _STOP_WORDS:
                found.add(word)
    return frozenset(found)


def views_in(texts: Iterable[str]) -> frozenset[str]:
    """The kinds of view the titles in `texts` name (plan, elevation, section)."""
    found: set[str] = set()
    for text in texts:
        upper = str(text or "").upper()
        for kind, pattern in _VIEW_WORDS:
            if pattern.search(upper):
                found.add(kind)
    return frozenset(found)


_NUMBERING: Final = re.compile(r"^\s*\d{1,3}\s*[.)]\s*")
_NOT_WORD: Final = re.compile(r"[^a-z0-9]+")


def title_key(text: object) -> str:
    """A typed or printed title normalised for matching: lower case, its leading item number
    dropped ("4.Wardrobe" is "wardrobe"), "@" read as "at", "w/" as "with", every other sign a space
    ("Wet Bar @Guest Lounge" is "wet bar at guest lounge")."""
    plain = _NUMBERING.sub("", " ".join(str(text or "").split()))
    plain = plain.lower().replace("@", " at ").replace("w/", " with ").replace("&", " and ")
    return " ".join(_NOT_WORD.sub(" ", plain).split())


def section_key(project: object, section: object) -> str | None:
    """A drawing's place in the set's divider pages ("PROJECT A / 1. STRAIGHT RUNS"), the
    same on the architect's and the vendor's files; `None` when no divider names a section."""
    if not str(section or "").strip():
        return None
    return f"{title_key(project)} / {title_key(section)}"


def _title_on(title: str, text: str) -> bool:
    """Whether an item title (2 words or more, or one word of 5 letters or more) is printed whole
    in a vendor drawing's normalised words."""
    words = title.split()
    if not words or (len(words) == 1 and len(words[0]) < 5):
        return False
    return f" {title} " in f" {text} "


#: Words a title block prints beside the item's name: never a shorter name of an item.
_GENERIC_PHRASES: Final = frozenset(
    {"project name", "item no", "overall size", "drawn by", "rev date", "front view", "top plan"}
)


def _named_in(title: str, phrases: Sequence[str]) -> str | None:
    """The longest vendor phrase (2 words or more, 8 letters or more) printed whole inside an
    item's title ("window seat" in "reading nook window seat at library"): ranks only."""
    padded = f" {title} "
    found = [
        p
        for p in phrases
        if len(p) >= 8 and p not in _GENERIC_PHRASES and p != title and f" {p} " in padded
    ]
    return max(found, key=len) if found else None


@dataclass(frozen=True)
class Scored:
    """One architect drawing's code score for one vendor drawing, with its evidence."""

    ref: str
    score: float
    evidence: tuple[str, ...]
    """The evidence in plain words, strongest first (empty: code has none)."""
    contradicts: str | None = None
    """Why code says it is not this drawing (a plan against a single elevation), or `None`."""
    strong: bool = False
    """Code holds evidence it may pair on by itself: the item's title printed or typed on the
    vendor's sheet, a view reference, spans lined up, most of its sizes printed on both, the
    only item of its section, or an item tag (a number of 100 or more: `EQ-512`, `RM-204`)
    printed on both. Finish tags and naming words only rank."""


def _same_size(a: Fraction, b: Fraction) -> bool:
    big = max(a, b)
    return abs(a - b) <= max(Fraction(1), big * Fraction(SIZE_TOLERANCE).limit_denominator(100))


def _overall(values: Sequence[tuple[str, Fraction]], axis: str) -> Fraction | None:
    found = [v for d, v in values if d == axis]
    return max(found) if found else None


def _idf(counts: Counter[str], total: int, key: str) -> float:
    """How telling a tag or word is among `total` candidates: 0 when every candidate prints it."""
    seen = counts.get(key, 0)
    if not seen or total <= 1:
        return 0.0
    from math import log

    return log(total / seen)


def _inch_words(value: Fraction) -> str:
    whole = float(value)
    return f'{whole:g}"'


def rank(vendor: Facts, candidates: Sequence[Facts]) -> tuple[Scored, ...]:
    """Every candidate scored for `vendor`, the best first (ties in the given order). Evidence:
    printed references on both; spans lined up by drawn position (`dim_pairs`); the share of its
    printed values the candidate also prints; tags and naming words both print (weighted by how
    few candidates print them); the same overall width or height within `SIZE_TOLERANCE`; other
    telling sizes close on both. A plan against a vendor drawing whose titles name only an
    elevation (or the other way round) is contradicted."""
    total = len(candidates)
    tag_counts: Counter[str] = Counter(t for one in candidates for t in one.tags)
    word_counts: Counter[str] = Counter(w for one in candidates for w in one.words)
    telling = [(d, v) for d, v in vendor.oriented if v >= MIN_TELLING_INCHES]
    in_section = sum(
        1 for one in candidates if vendor.section is not None and one.section == vendor.section
    )
    out: list[Scored] = []
    for one in candidates:
        score = 0.0
        evidence: list[str] = []
        contradicts = None
        strong = False
        if one.title and _title_on(one.title, vendor.text):
            strong = True
            score += _W_TITLE
            evidence.append(f'its title "{one.title}" is printed on the vendor\'s sheet')
        elif one.title and (named_in := _named_in(one.title, vendor.phrases)):
            score += _W_NAMED_IN_TITLE
            evidence.append(f'the vendor\'s sheet names "{named_in}", part of its title')
        if vendor.section is not None and one.section is not None:
            if one.section == vendor.section:
                score += _W_SECTION
                evidence.append("the same section of the set's divider pages")
                if in_section == 1:
                    strong = True
                    score += _W_ONLY_IN_SECTION
                    evidence.append("the only architect item in that section")
            else:
                contradicts = "it is in another section of the set's divider pages"
        # A view reference (`2/A-501`) names one drawing; a bare sheet or tag number (`EQ-310`)
        # is often a product tag printed on several, so it counts with the tags instead.
        shared = sorted(r for r in vendor.references & one.references if "/" in r)
        if shared:
            strong = True
            score += _W_REFERENCE
            evidence.append(f"printed reference {', '.join(shared)} on both")
        spans = vendor.spans(one) if vendor.spans is not None else 0
        if spans:
            strong = strong or spans >= MIN_SPANS
            score += _W_SPANS * min(1.0, spans / MIN_SPANS)
            evidence.append(f"{spans} spans lined up by drawn position")
        matched, share = _share(
            [v for v in vendor.values if v >= MIN_TELLING_INCHES / 2],
            [v for v in one.values if v >= MIN_TELLING_INCHES / 2],
        )
        if matched >= MIN_LAYOUT_VALUES:
            strong = strong or share >= MIN_LAYOUT_SHARE
            score += _W_SHARE * share
            evidence.append(f"{matched} of its sizes printed on both")
        named = 0.0
        tags = sorted(t for t in vendor.tags & one.tags if _idf(tag_counts, total, t) > 0)
        for tag in tags:
            named += _idf(tag_counts, total, tag)
        words = sorted(w for w in vendor.words & one.words if _idf(word_counts, total, w) > 0)
        for word in words:
            named += _idf(word_counts, total, word)
        if tags or words:
            # An item tag counts as strong only where this item alone prints it.
            strong = strong or any(_ITEM_TAG.match(t) and tag_counts[t] == 1 for t in tags)
            score += min(_MAX_NAMED, named)
            evidence.append("both print " + ", ".join([*tags, *words][:6]))
        for axis, name in (("h", "width"), ("v", "height")):
            mine, theirs = _overall(vendor.oriented, axis), _overall(one.oriented, axis)
            if (
                mine is not None
                and theirs is not None
                and min(mine, theirs) >= MIN_TELLING_INCHES
                and _same_size(mine, theirs)
            ):
                score += _W_SIZE
                evidence.append(f"overall {name} {_inch_words(mine)} against {_inch_words(theirs)}")
        overall = {_overall(vendor.oriented, "h"), _overall(vendor.oriented, "v")}
        near = {
            (d, v)
            for d, v in telling
            if v not in overall and any(e == d and _same_size(v, w) for e, w in one.oriented)
        }
        if near:
            score += min(_MAX_NEAR, _W_NEAR * len(near))
            evidence.append(f"{len(near)} other sizes close on both")
        if contradicts is None and (
            len(vendor.views) == 1
            and len(one.views) == 1
            and vendor.views != one.views
            and {*vendor.views, *one.views} == {"plan", "elevation"}
        ):
            contradicts = (
                f"its title names a {next(iter(one.views))}, the vendor drawing's a "
                f"{next(iter(vendor.views))}"
            )
        out.append(Scored(one.ref, round(score, 3), tuple(evidence), contradicts, strong))
    order = {s.ref: n for n, s in enumerate(out)}
    return tuple(
        sorted(out, key=lambda s: (s.contradicts is None, s.score, -order[s.ref]), reverse=True)
    )


def clear_winner(ranked: Sequence[Scored]) -> Scored | None:
    """Code's own pick from `rank`: the best above `CODE_BAR`, `CODE_MARGIN` ahead of the next
    candidate code does not contradict, and not contradicted itself; else `None`."""
    open_ = [s for s in ranked if s.contradicts is None]
    if not open_:
        return None
    best = open_[0]
    if best.score < CODE_BAR or not best.strong:
        return None
    if len(open_) > 1 and best.score - open_[1].score < CODE_MARGIN:
        return None
    return best


def evidence_words(scored: Scored) -> str:
    """One candidate's code evidence in plain words."""
    if not scored.evidence:
        return "no code evidence"
    return "; ".join(scored.evidence)


# ---- the agreement rule (#1798) ----------------------------------------------------------------


@dataclass(frozen=True)
class MatchAnswer:
    """The AI page matcher's answer for one vendor drawing (`page_match.py`): the architect items
    it shows, the best first (empty: none), whether the matcher is sure, and its short reason."""

    matches: tuple[str, ...]
    confident: bool
    reason: str = ""


def agree(
    ranked: Sequence[Scored], code_pick: str | None, answer: MatchAnswer | None
) -> tuple[tuple[str, ...], str]:
    """The architect items a vendor drawing is paired with, and why; or none and why. A wrong
    pair can make false reds, so a pair is made only when code and the AI page matcher agree:

    - no answer (the matcher was not asked, or failed): code's own clear pick (`clear_winner`, or
      a printed reference / the same printed values, `decide`), else none;
    - the matcher picks an item code contradicts (another section, a plan for an elevation), or
      code clearly picked another one: none (they disagree);
    - the matcher's pick is code's own clear pick: paired, sure or not;
    - otherwise the matcher must be sure, AND either code holds strong evidence for the pick
      (no other item `TIER` ahead of it), or the pick is code's first on weak evidence by at
      least `WEAK_LEAD` over every other item. A sure pick among look-alikes code cannot tell
      apart is never paired (the paid test, 2026-10-20: the matcher was sure of a wrong
      look-alike once). The matcher's further picks are kept only when code holds strong
      evidence for them too (a plan and an elevation of one item);
    - the matcher finds none while code picked one: none (they disagree)."""
    by = {s.ref: s for s in ranked}
    open_ = sorted((s for s in ranked if s.contradicts is None), key=lambda s: -s.score)
    if answer is None:
        if code_pick is not None and code_pick in by:
            return (code_pick,), (
                f"code paired it with {code_pick}: {evidence_words(by[code_pick])} "
                "(no AI page matcher answer)"
            )
        return (), f"{UNPAIRED}: code found no clear architect drawing for it"
    reason = f" ({answer.reason})" if answer.reason else ""
    if not answer.matches:
        if code_pick is not None:
            return (), (
                f"{UNPAIRED}: code chose {code_pick} ({evidence_words(by[code_pick])}), but the "
                f"AI page matcher found no architect drawing of the same item{reason}"
            )
        return (), (
            f"{UNPAIRED}: the AI page matcher found no architect drawing of the same "
            f"item{reason}, and code found no clear one"
        )
    pick = answer.matches[0]
    scored = by.get(pick)
    if scored is None:
        return (), f"{UNPAIRED}: the AI page matcher named {pick}, which is not a candidate"
    if scored.contradicts is not None:
        return (), (
            f"{UNPAIRED}: the AI page matcher picked {pick}{reason}, but code says "
            f"{scored.contradicts}"
        )
    if code_pick is not None and pick != code_pick:
        return (), (
            f"{UNPAIRED}: the AI page matcher picked {pick}{reason}, but code chose "
            f"{code_pick} ({evidence_words(by[code_pick])}), so they disagree"
        )
    if pick == code_pick:
        # Code's own clear pick, and the matcher's: paired, sure or not.
        extra = [m for m in dict.fromkeys(answer.matches[1:]) if m != pick and _also(by, m, scored)]
        chosen = (pick, *extra)
        return chosen, (
            f"paired with {', '.join(chosen)}: code's own pick, and the AI page matcher's"
            f"{reason} ({evidence_words(scored)})"
        )
    if not answer.confident:
        return (), (
            f"{UNPAIRED}: the AI page matcher picked {pick} but is not sure{reason}, and code "
            "has no clear pick of its own"
        )
    best = open_[0]
    others = [s for s in open_ if s.ref != pick]
    if scored.strong:
        # Strong code evidence for the pick (its title on the vendor's sheet, a view reference,
        # its own item tag...), and no other item clearly ahead of it.
        if best.ref != pick and best.score - scored.score >= TIER:
            return (), (
                f"{UNPAIRED}: the AI page matcher picked {pick}{reason}, but code ranks "
                f"{best.ref} higher ({evidence_words(best)}; {pick}: {evidence_words(scored)})"
            )
        how = "code holds strong evidence for it"
    else:
        # Weak evidence only (finish tags, naming words, close sizes): the pick must be code's
        # first, ahead of every other item by `WEAK_LEAD` (two pieces of weak evidence), so a
        # look-alike the matcher was sure of but code cannot tell apart is never paired.
        runner = others[0].score if others else 0.0
        if best.ref != pick or scored.score - runner < WEAK_LEAD:
            return (), (
                f"{UNPAIRED}: the AI page matcher is sure of {pick}{reason}, but code holds no "
                "strong evidence for it and does not put it clearly first"
                + (
                    f" ({best.ref}: {evidence_words(best)})"
                    if best.ref != pick
                    else f" ({pick}: {evidence_words(scored)})"
                )
            )
        how = "code's clear first, on weak evidence"
    extra = [m for m in dict.fromkeys(answer.matches[1:]) if m != pick and _also(by, m, scored)]
    chosen = (pick, *extra)
    return chosen, (
        f"paired with {', '.join(chosen)}: the AI page matcher is sure{reason}, and {how} "
        f"({evidence_words(scored)})"
    )


def _also(by: Mapping[str, Scored], ref: str, pick: Scored) -> bool:
    """Whether the matcher's further pick is kept beside its first (a plan and an elevation of one
    item): code holds strong evidence for it too, within `TIER` of the first, uncontradicted."""
    other = by.get(ref)
    return (
        other is not None
        and other.contradicts is None
        and other.strong
        and other.score >= pick.score - TIER
    )
