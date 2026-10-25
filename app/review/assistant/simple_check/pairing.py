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

**A real two-file upload (2026-10-23, four hotels in each file)** made the rule stricter and the
items finer: only the vendor drawing's own project is a candidate, never a reference set nor a key
plan (the work list, from the set index); an architect sheet of several titled views is narrowed
to one view (`narrow`; look-alikes stay unpaired); item codes printed with three digits count as
tags (`item_tags_in`; on their own they only agree with the matcher); a dense sheet's shared sizes
count for nothing (`dense`); weak evidence never pairs among fewer than `MIN_WEAK_POOL`; a key
plan's callout beside the vendor's item tag points to a view (`Facts.linked`); an accessible
variant never pairs with a standard one (`markers_in`); an opposite-hand unit is flagged
(`opposite_hand`); a saved matcher answer still holds when code only ruled candidates out
(`reuse`).

**The set index's item titles (2026-10-23):** a set with no GV headings had the matcher right and
sure on 20 of 20 vendor drawings, and code accepted 4. The item title the set index read on both
pages (checked printed) now counts as strong evidence when the two match exactly after a fixed
tidy-up (`unit_title`, `same_item`: "KITCHEN" is never "KITCHENETTE"); two numbered unit types
that differ ("TYPE 1", "TYPE 3") rule the pair out. A matcher pick code still cannot confirm
is "pairing unsure" (`PAIRING_UNSURE`): both pages are named to the reviewer, never "drawing
needed", never compared. Since 2026-10-25 a pick the matcher is not sure of counts too, and so
does code's own clear pick the matcher did not name: "no drawing to compare with" only when
neither names a page (owner: it must be very sure).

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
    "DENSE_SHARE",
    "DENSE_VALUES",
    "MIN_LAYOUT_SHARE",
    "MIN_LAYOUT_VALUES",
    "MIN_WEAK_POOL",
    "PAIRING_UNSURE",
    "TIER",
    "UNPAIRED",
    "Candidate",
    "Decision",
    "Facts",
    "Scored",
    "SizeMatch",
    "clear_winner",
    "decide",
    "dense",
    "evidence_words",
    "item_tags_in",
    "markers_in",
    "narrow",
    "opposite_hand",
    "rank",
    "references_in",
    "reuse",
    "same_item",
    "section_key",
    "size_match",
    "size_rows",
    "size_words",
    "swap_check",
    "tags_in",
    "title_key",
    "unit_title",
    "views_in",
    "with_sizes",
    "words_in",
]

#: The Not-compared words of a vendor drawing code could not pair (the run lists these drawings).
UNPAIRED: Final = "not paired with an architect drawing"
#: An unpaired drawing's reason when the AI page matcher names an architect page (sure or not), or
#: code has a clear pick, and the two cannot confirm it: the drawing may be in the upload, so it is
#: never "drawing needed"; the reviewer is told the candidate pages (`worklist._unsure`).
PAIRING_UNSURE: Final = "pairing unsure"
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
    item_tags: frozenset[str] | None = None
    """A vendor drawing's tags that name one piece (`item_tags_in`, printed on few of the vendor's
    drawings: `XX-001`, `XX-801`), normalised as `tags`; `None`: every tag numbered 100 or more."""
    linked: Mapping[str, str] = field(default_factory=dict, compare=False)
    """For a vendor drawing: the candidates an architect plan points to, and how in plain words
    (its item tag printed on the plan beside a callout to that view)."""
    view_count: int = 1
    """How many titled views the drawing's sheet holds (a multi-view sheet's titles say nothing
    of which view is which kind)."""
    whole_sheet: bool = False
    """The merged facts of a sheet of several titled views (a variant marker on one of its views
    says nothing of the others: `markers` is checked view by view, `narrow`)."""
    markers: frozenset[str] = frozenset()
    """The variant markers it prints (accessible: `ADA`, `ACCESSIBLE`; the set index's checked
    words too): printed on one side only, the two are different variants (`rank`)."""
    hand: frozenset[str] = frozenset()
    """The opposite-hand markers it prints (`MIRROR`, `OPP. HAND`; the set index's checked words
    too): printed on one side only, the pair is compared mirrored (`opposite_hand`)."""
    index_title: str | None = None
    """The item title the set index read on its page, as printed (checked printed by code:
    `set_index.verify`): "PRIMARY EN SUITE VANITY - TYPE 1". Two pages naming the same item,
    exactly after a fixed tidy-up (`unit_title`), is strong evidence; two numbered types that
    differ ("TYPE 1", "TYPE 3") rule the pair out (`rank`)."""
    place: tuple[str, int] | None = None
    """An architect item's file and page in it: several candidates of one title are one item only
    when they are one run of pages of one file (its sheets, `rank`)."""
    size_rows: tuple[tuple[str, tuple[Fraction, ...]], ...] = ()
    """Its dimension rows (`size_rows`): the printed values on one dimension line, in order along
    it, with the line's direction (`h` / `v`). The swap check (`swap_check`) compares them."""


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
            if winner.spans < MIN_SPANS and _dense_for(vendor, candidates, winner.ref):
                return Decision(
                    None,
                    f"{UNPAIRED}: the printed reference {', '.join(winner.shared_references)} "
                    f"names {winner.ref}, but it is a dense sheet: the sizes they share say "
                    "nothing",
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
    if best.spans < MIN_SPANS and _dense_for(vendor, candidates, best.ref):
        return Decision(
            None,
            f"{UNPAIRED}: {best.ref} prints most of its dimensions, but it is a dense sheet, "
            "so sharing them says nothing",
            found,
        )
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


def _dense_for(vendor: Facts, candidates: Sequence[Facts], ref: str) -> bool:
    """Whether candidate `ref` is a dense sheet for the vendor drawing (`dense`), on sizes of a
    foot or more."""
    theirs = next((c.values for c in candidates if c.ref == ref), ())
    big = [v for v in theirs if v >= MIN_TELLING_INCHES / 2]
    matched, _ = _share([v for v in vendor.values if v >= MIN_TELLING_INCHES / 2], big)
    return dense(big, matched)


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
#: ... and among at least this many candidates: being first of two or three on weak evidence says
#: nothing (a real two-file upload: the vendor's new desk and an older desk of the same building, the
#: only two architect drawings of its project, the matcher sure of the old one).
MIN_WEAK_POOL: Final = 4
#: A candidate printing more values than this, of which the vendor drawing shares fewer than
#: `DENSE_SHARE`, is a dense sheet: it prints most sizes of any piece, so sharing them is no
#: evidence of the piece (a real two-file upload: a prototype sheet with 162 dimensions was code's
#: pick for 6 unrelated vendor drawings, sharing 6 to 8 of them).
DENSE_VALUES: Final = 60
DENSE_SHARE: Final = 0.2


def tags_in(texts: Iterable[str]) -> frozenset[str]:
    """The tag codes printed in `texts`, normalised: letters and the number without its leading
    zeros (`EQ-512_ALT-X` is `EQ512`, `PL-07` is `PL7`); view markers and sizes left out."""
    found: set[str] = set()
    for text in texts:
        for letters, digits in _TAG.findall(str(text or "").upper()):
            if letters not in _NOT_TAG:
                found.add(f"{letters}{int(digits)}")
    return frozenset(found)


#: A tag whose number is printed with three digits or more (`XX-001`, `XX-801`, `EQ-512`).
_PADDED_TAG: Final = re.compile(r"\b([A-Z]{1,4})[-_](\d{3,4})(?![\d/])")


def item_tags_in(texts: Iterable[str]) -> frozenset[str]:
    """The tags in `texts` printed the way an item code is (a number of three digits or more,
    zero-padded or not: `XX-001`, `XX-801`, `XX-318`), normalised as `tags_in` (`ML1`). Whether one
    names a piece or a finish (`LM-004` is a laminate) is decided by how many of the vendor's
    drawings print it (`worklist`)."""
    found: set[str] = set()
    for text in texts:
        for letters, digits in _PADDED_TAG.findall(str(text or "").upper()):
            if letters not in _NOT_TAG:
                found.add(f"{letters}{int(digits)}")
    return frozenset(found)


def dense(candidate: Sequence[object], matched: int) -> bool:
    """Whether a candidate is a dense sheet for this vendor drawing: it prints more than
    `DENSE_VALUES` values and the ones it shares with the vendor drawing are fewer than
    `DENSE_SHARE` of them, so sharing them says nothing of which piece it is (any piece's common
    sizes are among them)."""
    return len(candidate) > DENSE_VALUES and matched < DENSE_SHARE * len(candidate)


#: Code's own variant and hand markers (the set index's checked words come on top: an office's own
#: words such as `HC` or `REV` are read there, where the model says what they mean). `HC` and `REV`
#: alone are never read here: hollow core, revision.
_ACCESSIBLE: Final = re.compile(r"\b(ADA|ACCESSIBLE|HANDICAP(?:PED)?)\b(?![-_.]?\d)")
_HAND: Final = re.compile(
    r"\b(MIRROR(?:ED)?|OPP(?:OSITE|\.)?\s*HAND(?:ED)?|REVERSED?(?:\s+HAND)?|REVERSE)\b"
)


def markers_in(texts: Iterable[str]) -> tuple[frozenset[str], frozenset[str]]:
    """The variant markers and the opposite-hand markers printed in `texts` (code's own words)."""
    variant: set[str] = set()
    hand: set[str] = set()
    for text in texts:
        upper = " ".join(str(text or "").upper().split())
        variant.update(m.group(1) for m in _ACCESSIBLE.finditer(upper))
        hand.update(" ".join(m.group(1).split()) for m in _HAND.finditer(upper))
    return frozenset(variant), frozenset(hand)


def opposite_hand(vendor: Facts, architect: Facts) -> str | None:
    """Why a pair is compared mirrored (an opposite-hand marker printed on one side only, or on
    both: an architect's page listing its unit's reverse type beside the regular one says nothing
    of which hand it draws), or `None` (no marker on either side)."""
    if not vendor.hand and not architect.hand:
        return None
    if vendor.hand and architect.hand:
        return (
            f"both drawings name a hand (the vendor's {', '.join(sorted(vendor.hand))}; the "
            f"architect's {', '.join(sorted(architect.hand))}): which hand each draws is not sure"
        )
    side, words = (
        ("the vendor's drawing", vendor.hand)
        if vendor.hand
        else ("the architect's", architect.hand)
    )
    return f"{side} is marked {', '.join(sorted(words))}: the unit is drawn opposite-hand"


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


#: Words that say which hand or copy of a unit a sheet draws, never which unit: a "REVERSED UNITS"
#: sheet draws the same unit as its regular sheet (compared mirrored, `opposite_hand`). Only words
#: `markers_in` also reads as a hand (or no hand at all: "regular"), so a title left without them
#: is always flagged opposite-hand where it said one (`worklist._with_marks`).
_HAND_WORDS: Final = re.compile(r"\b(?:reversed?|regular)(?: units?)?\b")
#: A list of unit types in a tidied title, joined by "and" / "or" ("TYPE 1 & 2", "TYPE A OR B"):
#: a bare number after a type is never read as one ("TYPE A - 2 BEDROOM").
_TYPE_LIST: Final = re.compile(r"\btype ([a-z0-9]{1,4}(?: (?:and|or) (?:type )?[a-z0-9]{1,4}\b)*)")


def unit_title(text: object) -> tuple[str, frozenset[str]]:
    """An item title tidied for an exact match (`title_key`: lower case, signs as spaces, so
    "TYPE-1" is "type 1"), the hand words left out ("KITCHEN REVERSED UNITS" is "kitchen"); and its
    numbered unit types ("1" of "VANITY - TYPE 1"). Only an exact match counts: "kitchen" is not
    "standard kitchen", nor "kitchenette" (other tools ask the reviewer then, never guess)."""
    tidy = " ".join(_HAND_WORDS.sub(" ", title_key(text)).split())
    found = {
        t for one in _TYPE_LIST.findall(tidy) for t in one.split() if t not in ("and", "or", "type")
    }
    return tidy, frozenset(found)


def _index_title_words(title: str) -> bool:
    """Whether a tidied title says enough to name an item: two words or more besides its unit
    type ("primary vanity type 1"; never "kitchen", "vanity", nor "type 1" alone: generic names
    repeat from one unit to the next)."""
    return len(_TYPE_LIST.sub(" ", title).split()) >= 2


def same_item(one: str | None, other: str | None) -> bool:
    """Whether two set index item titles name the same item: equal after `unit_title`'s tidy-up
    (so "VANITY - TYPE 1 REVERSED UNITS" is "Vanity type-1"), and enough words to name one."""
    if not one or not other:
        return False
    mine, theirs = unit_title(one)[0], unit_title(other)[0]
    return mine == theirs and _index_title_words(mine)


def _one_titled_item(vendor: Facts, candidates: Sequence[Facts]) -> frozenset[str]:
    """The candidate whose set index title is the vendor's item (`same_item`), when it is the only
    one. Two pages of that title, even side by side in one file, could be two units drawn alike (a
    new desk and an older one of the same building): then none, and the reviewer is asked, never
    a guess (review of #1913: a run of same-titled pages let the matcher alone choose among
    look-alikes)."""
    found = [c for c in candidates if same_item(vendor.index_title, c.index_title)]
    return frozenset(c.ref for c in found) if len(found) == 1 else frozenset()


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
    vendor's sheet, a view reference, spans lined up, most of its sizes printed on both (the
    candidate not a dense sheet), the only item of its section, an item tag (`item_tags_in`:
    `EQ-512`, `XX-001`) printed on both and on no other candidate, or a plan's callout pointing
    to it. Finish tags and naming words only rank."""
    agree_only: bool = False
    """Its only strong evidence is a plan's callout (`Facts.linked`) or an item code numbered
    under 100 (`XX-001`): enough to agree with the AI page matcher, never to pair without it
    (`clear_winner`)."""
    linked: bool = False
    """A key plan's callout beside the vendor's item tag points to it (`Facts.linked`)."""
    titled: float = 0.0
    """The part of `score` the set index's item title gave it: an AI's read, so never counted
    toward code's own pick (`clear_winner`)."""
    sized: float = 0.0
    """The part of `score` the swap check gave it (`with_sizes`): found among the pages of the AI
    page matcher's pick's title, so it only helps agree with the matcher; like the title, never
    counted toward code's own pick, nor against another item's own evidence (`agree`)."""

    @property
    def own(self) -> float:
        """Its score on code's own evidence: the title's and the swap check's weights left out."""
        return self.score - self.titled - self.sized


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
    items = (
        vendor.item_tags
        if vendor.item_tags is not None
        else frozenset(t for t in vendor.tags if _ITEM_TAG.match(t))
    )
    tag_counts: Counter[str] = Counter(t for one in candidates for t in one.tags)
    word_counts: Counter[str] = Counter(w for one in candidates for w in one.words)
    telling = [(d, v) for d, v in vendor.oriented if v >= MIN_TELLING_INCHES]
    in_section = sum(
        1 for one in candidates if vendor.section is not None and one.section == vendor.section
    )
    titled = _one_titled_item(vendor, candidates)
    out: list[Scored] = []
    for one in candidates:
        score = 0.0
        evidence: list[str] = []
        contradicts = None
        strong = False
        small_tag = False
        index_titled = False
        linked = vendor.linked.get(one.ref)
        if linked:
            # A plan's callout beside the vendor's item tag points to it: strong enough to agree
            # with the AI page matcher, never to pair without it (`link_only`).
            score += _W_REFERENCE
            evidence.append(linked)
        if one.title and _title_on(one.title, vendor.text):
            strong = True
            score += _W_TITLE
            evidence.append(f'its title "{one.title}" is printed on the vendor\'s sheet')
        elif one.title and (named_in := _named_in(one.title, vendor.phrases)):
            score += _W_NAMED_IN_TITLE
            evidence.append(f'the vendor\'s sheet names "{named_in}", part of its title')
        if vendor.index_title and one.index_title:
            my_type = unit_title(vendor.index_title)[1]
            their_type = unit_title(one.index_title)[1]
            if my_type and their_type and not my_type & their_type:
                # "TYPE 1" against "TYPE 3": another unit, whatever else they share.
                contradicts = (
                    f'the vendor\'s sheet is "{vendor.index_title}" and this one is '
                    f'"{one.index_title}": another unit type'
                )
            elif one.ref in titled and not strong:
                # The same item title on both pages (each checked printed): strong enough to agree
                # with the AI page matcher, never to pair without it (`agree_only`: an AI read the
                # titles; a second, independent witness must look at the drawings).
                index_titled = True
                score += _W_TITLE
                evidence.append(
                    f'both pages are titled "{one.index_title}"'
                    if vendor.index_title == one.index_title
                    else f'the vendor\'s "{vendor.index_title}" is its "{one.index_title}"'
                )
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
            sizes = [v for v in one.values if v >= MIN_TELLING_INCHES / 2]
            if dense(sizes, matched):
                # A dense sheet prints most sizes of any piece: its share is no evidence.
                evidence.append(
                    f"{matched} of its sizes printed on both (a dense sheet: "
                    f"{len(sizes)} sizes, not counted)"
                )
            else:
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
            # An item tag counts as strong only where this item alone prints it; one numbered
            # under 100 (`XX-001`) only to agree with the AI page matcher (`agree_only`): code
            # never pairs on it alone.
            unique = [t for t in tags if t in items and tag_counts[t] == 1]
            if any(_ITEM_TAG.match(t) for t in unique):
                strong = True
            elif unique:
                small_tag = True
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
        if (
            contradicts is None
            and not one.whole_sheet
            and bool(vendor.markers) != bool(one.markers)
        ):
            # An accessible variant against a standard one (`ADA` printed on one side only):
            # another piece, whatever else they share.
            contradicts = (
                f"the vendor's drawing is marked {', '.join(sorted(vendor.markers))} and this "
                "one is not"
                if vendor.markers
                else f"it is marked {', '.join(sorted(one.markers))} and the vendor's drawing is not"
            )
        if contradicts is None and (
            # A sheet of several titled views (vendor's or architect's) says nothing by its kinds
            # of view: its titles are not read one by one (a real two-file upload lost the right
            # elevation sheets for 25 vendor drawings whose only title read was "PLAN VIEW").
            vendor.view_count <= 1
            and one.view_count <= 1
            and len(vendor.views) == 1
            and len(one.views) == 1
            and vendor.views != one.views
            and {*vendor.views, *one.views} == {"plan", "elevation"}
        ):
            contradicts = (
                f"its title names a {next(iter(one.views))}, the vendor drawing's a "
                f"{next(iter(vendor.views))}"
            )
        agree_only = (bool(linked) or small_tag or index_titled) and not strong
        out.append(
            Scored(
                one.ref,
                round(score, 3),
                tuple(evidence),
                contradicts,
                strong or bool(linked) or small_tag or index_titled,
                agree_only,
                bool(linked),
                _W_TITLE if index_titled else 0.0,
            )
        )
    order = {s.ref: n for n, s in enumerate(out)}
    return tuple(
        sorted(out, key=lambda s: (s.contradicts is None, s.score, -order[s.ref]), reverse=True)
    )


def clear_winner(ranked: Sequence[Scored]) -> Scored | None:
    """Code's own pick from `rank`: the best above `CODE_BAR`, `CODE_MARGIN` ahead of the next
    candidate code does not contradict, and not contradicted itself, on strong evidence of its own
    (a plan's callout or a small item code alone is not enough); else `None`."""
    open_ = [s for s in ranked if s.contradicts is None]
    if not open_:
        return None
    # The set index's titles are an AI's read: code's own pick stands on code's evidence alone.
    own = sorted(open_, key=lambda s: -s.own)
    best = own[0]
    if best.own < CODE_BAR or not best.strong or best.agree_only:
        return None
    if len(own) > 1 and best.own - own[1].own < CODE_MARGIN:
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
    - the matcher finds none while code picked one: none (they disagree).

    Weak evidence never pairs among fewer than `MIN_WEAK_POOL` candidates; a matcher answer naming
    code's own clear pick among several items agrees on that one; with no clear pick of code's, a
    matcher answer naming the one item a key plan's callout points to (`Scored.linked`), sure or
    not, agrees on that one (two independent witnesses: the architect's printed pointer and the
    AI's look)."""
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
    if code_pick is not None and code_pick in answer.matches:
        # The matcher names code's own clear pick among the items that show the piece: agreed.
        pick = code_pick
    elif code_pick is None:
        pointed = [
            m
            for m in dict.fromkeys(answer.matches)
            if m in by and by[m].linked and by[m].contradicts is None
        ]
        if len(pointed) == 1:
            # The architect's own key plan points to it (its callout beside the vendor's item
            # tag), and the matcher names it among the items that show the piece: agreed.
            return tuple(pointed), (
                f"paired with {pointed[0]}: the architect's key plan points to it, and the AI "
                f"page matcher names it{reason} ({evidence_words(by[pointed[0]])})"
            )
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
        # The set index's title is an AI's read: on code's own evidence alone, no other item may
        # be clearly ahead of the pick either (review of #1913: a title on one page and a view
        # reference both print on another is a conflict, never a pair).
        own = max((s for s in others if s.strong), key=lambda s: s.own, default=None)
        if own is not None and own.own - scored.own >= TIER:
            return (), (
                f"{UNPAIRED}: the AI page matcher picked {pick}{reason}, but code's own evidence "
                f"puts {own.ref} ahead ({evidence_words(own)}; {pick}: {evidence_words(scored)})"
            )
        how = (
            "the set index reads the same item title on both pages"
            if scored.titled and not scored.sized and scored.own < CODE_BAR
            else "code holds strong evidence for it"
        )
    else:
        # Weak evidence only (finish tags, naming words, close sizes): the pick must be code's
        # first, ahead of every other item by `WEAK_LEAD` (two pieces of weak evidence), so a
        # look-alike the matcher was sure of but code cannot tell apart is never paired.
        runner = others[0].score if others else 0.0
        if best.ref == pick and others and scored.score - runner < WEAK_LEAD:
            return (), (
                f"{UNPAIRED}: the AI page matcher is sure of {pick}{reason}, but two architect "
                f"drawings could be this item ({pick}, {others[0].ref}): code holds no strong "
                f"evidence for it and does not put it clearly first ({pick}: "
                f"{evidence_words(scored)}; {others[0].ref}: {evidence_words(others[0])})"
            )
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
        if len(open_) < MIN_WEAK_POOL:
            return (), (
                f"{UNPAIRED}: the AI page matcher is sure of {pick}{reason}, but code holds no "
                f"strong evidence for it, and being first of {len(open_)} architect drawings "
                f"on weak evidence says nothing ({pick}: {evidence_words(scored)})"
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
    item): code holds strong evidence for it too, within `TIER` of the first, uncontradicted. The
    set index's title is an AI's read: never the only evidence for a further pick, and never
    counted in the scores compared."""
    other = by.get(ref)
    return (
        other is not None
        and other.contradicts is None
        and other.strong
        and not (other.titled and other.agree_only)
        and other.own >= pick.own - TIER
    )


# ---- the swap check: same-titled sheets told apart by their printed sizes ----------------------
#
# A run of architect sheets carrying one title (a kitchen's plan and its elevations; two units
# drawn alike) leaves the title no help: which sheet is the vendor's drawing? Other tools pair the
# sheets, then re-check look-alikes by their content (a "pair-swap audit"). Code does it with the
# printed sizes: the vendor's overall sizes printed on the sheet (or one dimension line's parts
# adding up to one), and the vendor's own parts in the same order on one dimension line (read
# backwards: a mirrored unit). The matcher's pick wins only when its sizes match clearly better
# than every other sheet of its title; a tie or nothing found stays "pairing unsure".

#: Two printed sizes are the same within this (a CAD print's rounding to a sixteenth).
SAME_SIZE: Final = Fraction(1, 32)
#: The vendor's overall sizes are its largest this many distinct sizes (its length and depth; a
#: note number among them only adds a size no sheet prints).
OVERALLS: Final = 2
#: At most this many parts in a row on one dimension line are added up to a vendor's overall.
MAX_CHAIN: Final = 6
#: The vendor's parts found in the same order on one dimension line count when they are at least
#: two and add up to a foot or more (two 1 1/2" fillers in a row say nothing).
MIN_RUN_INCHES: Final = 12
#: Dimension lines this close across (points) are one line, and labels along it whose lines meet
#: or overlap within this are one string of dimensions (a gap: another string, another view).
_ROW_PT: Final = 2.0
#: A vendor's overall is added up from a string's parts only when it is this large (inches):
#: smaller sizes are made of common parts too easily (12" + 12").
MIN_ADDED_INCHES: Final = MIN_TELLING_INCHES
#: The weight the swap check adds to the matcher's pick's score (`Scored.sized`; as much as most of
#: the vendor's sizes printed on both, `_W_SHARE`). Never counted as code's own evidence.
W_SWAP: Final = 2.0


def size_rows(labels: Iterable[Mapping[str, Any]]) -> tuple[tuple[str, tuple[Fraction, ...]], ...]:
    """A drawing's strings of dimensions (`dims.json` labels with a dimension line): the values
    whose lines lie on one line and meet or overlap end to end, in order along it, with its
    direction (`h` / `v`). A gap along the line starts another string (two views drawn side by
    side are never one row). Strings of two values or more only."""
    points: list[tuple[str, float, float, float, Fraction]] = []
    for label in labels:
        line = label.get("dim_line")
        try:
            value = Fraction(str(label.get("inches")))
            (x0, y0), (x1, y1) = ((float(a), float(b)) for a, b in line)  # type: ignore[union-attr]
        except (TypeError, ValueError, ZeroDivisionError):
            continue
        if value <= 0:
            continue
        across = abs(x1 - x0) >= abs(y1 - y0)
        axis = "h" if across else "v"
        level = (y0 + y1) / 2 if across else (x0 + x1) / 2
        start, end = sorted((x0, x1) if across else (y0, y1))
        points.append((axis, level, start, end, value))
    lines: list[tuple[str, float, list[tuple[float, float, Fraction]]]] = []
    for axis, level, start, end, value in sorted(points, key=lambda p: (p[0], p[1])):
        if lines and lines[-1][0] == axis and abs(lines[-1][1] - level) <= _ROW_PT:
            lines[-1][2].append((start, end, value))
        else:
            lines.append((axis, level, [(start, end, value)]))
    rows: list[tuple[str, tuple[Fraction, ...]]] = []
    for axis, _, found in lines:
        string: list[Fraction] = []
        reach = float("-inf")
        for start, end, value in sorted(found, key=lambda f: (f[0], f[1])):
            if string and start > reach + _ROW_PT:
                rows.append((axis, tuple(string)))
                string = []
            string.append(value)
            reach = max(reach, end) if len(string) > 1 else end
        rows.append((axis, tuple(string)))
    return tuple(row for row in rows if len(row[1]) >= 2)


def _same(a: Fraction, b: Fraction) -> bool:
    return abs(a - b) <= SAME_SIZE


def size_words(value: Fraction) -> str:
    """`55 1/2"`, `60"`, `3/4"`: a size in inches as a drawing prints it (to a 32nd)."""
    whole, part = divmod(Fraction(value).limit_denominator(32), 1)
    if not part:
        return f'{whole}"'
    return f'{part}"' if not whole else f'{whole} {part}"'


@dataclass(frozen=True)
class SizeMatch:
    """How one architect sheet's printed sizes match a vendor drawing's (`swap_check`)."""

    ref: str
    printed: tuple[Fraction, ...] = ()
    """The vendor's overall sizes it prints."""
    added: tuple[tuple[Fraction, tuple[Fraction, ...]], ...] = ()
    """The vendor's overall sizes one of its dimension lines adds up to, with the parts."""
    parts: tuple[Fraction, ...] = ()
    """The longest run of the vendor's parts it prints in the same order on one dimension line."""
    mirrored: bool = False
    """That run is found only read backwards: the sheet draws the unit mirrored."""

    @property
    def points(self) -> int:
        """An overall printed 2, one added up 1, the vendor's parts in order 1."""
        return 2 * len(self.printed) + len(self.added) + (1 if self.parts else 0)

    def words(self) -> str:
        """What matched, in plain words (empty: nothing)."""
        out = [f"prints the vendor's overall {size_words(v)}" for v in self.printed]
        out += [
            f"adds up to the vendor's overall {size_words(v)} ("
            + " + ".join(size_words(p) for p in parts)
            + ")"
            for v, parts in self.added
        ]
        if self.parts:
            run = ", ".join(size_words(p) for p in self.parts)
            out.append(
                f"prints the vendor's parts {run} in reverse order (a mirrored unit)"
                if self.mirrored
                else f"prints the vendor's parts {run} in the same order"
            )
        return "; ".join(out)


def _overalls(vendor: Facts) -> tuple[Fraction, ...]:
    sizes = sorted({v for v in vendor.values if v >= MIN_TELLING_INCHES / 2}, reverse=True)
    return tuple(sizes[:OVERALLS])


def _chain(row: Sequence[Fraction], total: Fraction) -> tuple[Fraction, ...] | None:
    """The parts in a row on one dimension line that add up to `total`, or `None`."""
    for start in range(len(row)):
        added = Fraction(0)
        for end in range(start, min(len(row), start + MAX_CHAIN)):
            added += row[end]
            if end > start and _same(added, total):
                return tuple(row[start : end + 1])
            if added > total + SAME_SIZE:
                break
    return None


def _run(mine: Sequence[Fraction], theirs: Sequence[Fraction]) -> tuple[Fraction, ...]:
    """The longest run of `mine`'s values `theirs` prints in the same order, side by side."""
    best: tuple[Fraction, ...] = ()
    for i in range(len(mine)):
        for j in range(len(theirs)):
            k = 0
            while i + k < len(mine) and j + k < len(theirs) and _same(mine[i + k], theirs[j + k]):
                k += 1
            if k > len(best):
                best = tuple(mine[i : i + k])
    return best if len(best) >= 2 and sum(best) >= MIN_RUN_INCHES else ()


def size_match(vendor: Facts, sheet: Facts) -> SizeMatch:
    """How `sheet`'s printed sizes match the vendor drawing's (`SizeMatch`): its overall sizes
    printed or added up on one dimension line, its parts in the same order (or backwards)."""
    printed: list[Fraction] = []
    added: list[tuple[Fraction, tuple[Fraction, ...]]] = []
    for total in _overalls(vendor):
        if any(_same(total, v) for v in sheet.values):
            printed.append(total)
            continue
        if total < MIN_ADDED_INCHES:
            continue
        chain = next(
            (found for _, row in sheet.size_rows if (found := _chain(row, total)) is not None),
            None,
        )
        if chain is not None:
            added.append((total, chain))
    forward: tuple[Fraction, ...] = ()
    backward: tuple[Fraction, ...] = ()
    for axis, mine in vendor.size_rows:
        for their_axis, theirs in sheet.size_rows:
            if their_axis != axis:
                continue
            if len(found := _run(mine, theirs)) > len(forward):
                forward = found
            # Only an across string read backwards is a mirror (an up-and-down one is upside down).
            if axis == "h" and len(found := _run(mine, theirs[::-1])) > len(backward):
                backward = found
    mirrored = len(backward) > len(forward)
    return SizeMatch(
        sheet.ref,
        tuple(printed),
        tuple(added),
        backward if mirrored else forward,
        mirrored,
    )


def swap_check(vendor: Facts, pick: Facts, others: Sequence[Facts]) -> tuple[SizeMatch | None, str]:
    """Whether the matcher's pick is the vendor's drawing among the sheets of its title
    (`others`), by the printed sizes (`size_match`): it must match the vendor's overall size
    (printed, or added up on one dimension line), and match clearly better than every other sheet
    (a point more: an overall printed beats one added up). Its match, or `None` and why."""
    if not _overalls(vendor):
        return None, "the vendor drawing prints no overall size"
    mine = size_match(vendor, pick)
    if not mine.printed and not mine.added:
        return None, f"{pick.ref} neither prints nor adds up to the vendor's overall sizes"
    if not mine.printed:
        # Won only by a sum of parts: a sibling matching nothing must have been read (its sizes
        # and a string of them), never a page whose numbers code could not read.
        unread = [o.ref for o in others if len(o.values) < MIN_LAYOUT_VALUES or not o.size_rows]
        if unread:
            return None, (
                f"{pick.ref} only adds up to the vendor's overall, and code read too few sizes on "
                f"{', '.join(unread)} to rule {'it' if len(unread) == 1 else 'them'} out"
            )
    theirs = [size_match(vendor, other) for other in others]
    runner = max(theirs, key=lambda m: m.points, default=None)
    if runner is not None and mine.points <= runner.points:
        return None, (
            f"{pick.ref} and {runner.ref} match its sizes alike ({pick.ref}: {mine.words()}; "
            f"{runner.ref}: {runner.words() or 'none of them'})"
        )
    return mine, ""


def with_sizes(
    ranked: Sequence[Scored], pick: str, match: SizeMatch, others: Sequence[str], title: str
) -> tuple[Scored, ...]:
    """`ranked` with the swap check's evidence added to the matcher's pick: strong enough to
    agree with the sure AI page matcher, never to pair without it (`agree_only`), and never
    against a contradiction (the pick is not changed then)."""
    out: list[Scored] = []
    for one in ranked:
        if one.ref == pick and one.contradicts is None:
            words = (
                f'of the {len(others) + 1} pages titled "{title}", its sizes match clearly best: '
                f"it {match.words()}"
            )
            one = Scored(
                one.ref,
                round(one.score + W_SWAP, 3),
                (words, *one.evidence),
                one.contradicts,
                True,
                one.agree_only or not one.strong,
                one.linked,
                one.titled,
                one.sized + W_SWAP,
            )
        out.append(one)
    order = {s.ref: n for n, s in enumerate(out)}
    return tuple(
        sorted(out, key=lambda s: (s.contradicts is None, s.score, -order[s.ref]), reverse=True)
    )


def reuse(shown: Sequence[str], answer: MatchAnswer, asked: Sequence[str]) -> MatchAnswer | None:
    """A saved matcher answer for the candidates `asked` now, or `None` (ask again). The same
    candidates: the answer. Fewer (code since rules some out: another project, the brand
    prototype, a key plan): the answer still holds when every candidate asked now was shown, and
    its picks are kept among them; an answer of none holds (none of more showed the piece). When
    every pick it made is ruled out now, what it would pick among the rest is not known."""
    now, then = set(asked), set(shown)
    if now == then:
        return answer
    if not now or not now <= then:
        return None
    if not answer.matches:
        return answer
    kept = tuple(m for m in answer.matches if m in now)
    if not kept:
        return None
    return MatchAnswer(kept, answer.confident, answer.reason)


def narrow(vendor: Facts, views: Sequence[Facts]) -> tuple[Scored | None, str]:
    """Which view of a paired architect sheet shows the vendor's piece (`views`: the sheet's
    titled views), by code: the one view a key plan's callout points to (no other holding strong
    evidence), else the view code ranks first (`rank` among the sheet's views) with evidence of
    its own, `WEAK_LEAD` ahead of the next view (`TIER` when that one holds strong evidence too).
    Look-alike views (four "HALL ELEVATION"s) or no evidence: `None` and why."""
    if len(views) == 1:
        return Scored(views[0].ref, 0.0, ("the sheet's only titled view",)), ""
    ranked = [s for s in rank(vendor, views) if s.contradicts is None]
    if not ranked or not ranked[0].evidence or ranked[0].score <= 0:
        return None, f"code cannot tell which of its {len(views)} views shows the piece"
    pointed = [s for s in ranked if s.linked]
    if len(pointed) == 1 and not any(s.strong for s in ranked if s.ref != pointed[0].ref):
        # The architect's own key plan points to this view and no other view holds strong
        # evidence: the printed pointer decides over look-alike words (a finish tag, a room name).
        return pointed[0], ""
    best = ranked[0]
    runner = ranked[1] if len(ranked) > 1 else None
    lead = best.score - (runner.score if runner else 0.0)
    need = TIER if runner is not None and runner.strong else WEAK_LEAD
    if runner is not None and lead < need:
        return None, (
            f"two architect drawings could be this item ({best.ref}, {runner.ref}): "
            f"{best.ref}: {evidence_words(best)}; {runner.ref}: {evidence_words(runner)}"
        )
    return best, ""
