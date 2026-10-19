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
    "MIN_LAYOUT_SHARE",
    "MIN_LAYOUT_VALUES",
    "UNPAIRED",
    "Candidate",
    "Decision",
    "Facts",
    "decide",
    "references_in",
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
