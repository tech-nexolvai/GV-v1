"""An architect dimension measured to a centre line is never a size of the item (2026-11-04).

A real upload (Set A, a hotel bathroom) showed the gap (values made up here). The architect's
bath elevation prints 3'-0" from the mirror's centre line to the toilet's centre line, and 1'-8"
from the toilet's centre line to the wall. A helper paired the vendor's vanity top (36" wide, 20"
deep) with them. Code confirmed both numbers are printed there, and the items went green:
"Right". Nobody checked what the architect's dimension measures. The 1'-8" places the toilet; it
is not the vanity's depth, so the green was false (and the 36" was right only by luck). On the
next sheet the same kind of dimension stood against another vanity's depth as a possible
difference.

**The rule.** Before an architect value is set against a vendor size (Right, Wrong or a
please-check), code reads the architect's own dimension line from the page's line work (the
label's own line, `dim_ends.Page.spans`) and looks at both its ends. An end is **on a centre
line** when the line through it (its extension line, dashes followed) is drawn as one:

- marked: a ``CL``, ``C L``, ``C/L``, ``℄`` or ``CENTER LINE`` mark printed on that line, centred
  across it (the mark may name its thing: ``CL SINK``, ``C/L OF TOILET``); or
- drawn dash-dot from the dimension line out: long and short dashes in turn, at least five in a
  row, each kind one length (`dash_dot`; a building's grid lines, a sink's centre line).

Plain dashes alone are not a centre line (many architects draw extension lines and hidden edges
dashed), and the middle of a drawn thing found by geometry alone is left to the like-for-like
check, which needs both drawings read (`dim_ends.alike`).

A dimension with an end on a centre line places a fixture or a centre; it is not a width, depth
or length of the vanity, the top or a cabinet. So when the architect's dimension has more such
ends than the vendor's dimension it is set against, the two are never compared: the item is
**Not compared** with the plain reason. Never Right, never Wrong, never a please-check. The
vendor's dimension is read generously for its own centres, so a true centre-to-centre pair keeps
its outcome: a mark or dash-dot line as above, an end the like-for-like reader names as the middle
of a sink or of a small symbol (`dim_ends`: ``sink_centre``, ``symbol_centre``), or the shared end
of an equal split (``EQ`` / ``EQ``, the middle of the run). The item's own words are never used.

What it never does: decide from a guess. An end code cannot read stays as it was (a Right stays
"Matches (ends not proved)", Anant's option B of 2026-10-27); only an end code sees drawn as a
centre line takes a comparison away. It never makes a Right or a red.

Left as they are: an item with no vendor size (the vendor leaves the thing out: the architect's
locating dimension IS the item, "outlet not shown"), items code compared itself (its own pair by
drawn position, both ends on each other's ticks; GV's formulas; the field cut; the sink-centre
check), and an item the reviewer decided.

The record (`FIELD`) is made by the escalation's free step for the two labels the item pairs then
(`escalation._mark_centre_lines`); `dim_ends.unlike_reason` reads it, so the item is Not compared
wherever two unlike dimensions are (`classification`, `proof`). Pure code, $0, no AI.
Verification: `tests/review/assistant/test_centre_lines.py` (made-up drawings).
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Any, Final

from app.review.assistant import classification, dim_ends

__all__ = [
    "CLEAR",
    "FIELD",
    "VENDOR_CENTRE_KINDS",
    "CentreEnd",
    "centre_ends",
    "dash_dot",
    "is_mark",
    "judge",
    "reason",
]

#: The finding's record: {"outcome": "centre_line", "why": plain words, "vendor": label key,
#: "architect": label key, "ends": how code knows each architect end is on a centre line}.
FIELD: Final = "centre_line_end"
#: `judge`'s answer when code read the architect's line and it does not run to more centre lines
#: than the vendor's: any older record for the item is taken off.
CLEAR: Final[Mapping[str, Any]] = {"outcome": "clear"}
#: The like-for-like reader's end kinds that are a centre, counted on the vendor's side only.
VENDOR_CENTRE_KINDS: Final = frozenset({"sink_centre", "symbol_centre"})
#: A piece this close across (points) to the end's station is on the end's own line.
ON_LINE_PT: Final = 1.0
#: Gaps (points) a line may have and still be one line (a centre line's dashes, a faucet on it).
DASH_GAP_PT: Final = 20.0
#: The end's own line reaches the dimension line within this (points).
MEETS_PT: Final = 3.0
#: A mark is on the line when its middle is this close across (points), or this share of its
#: width, whichever is more; and along the line within this many of its own heights of it.
MARK_ACROSS_PT: Final = 1.5
MARK_ACROSS_SHARE: Final = 0.3
MARK_ALONG_HEIGHTS: Final = 1.5
#: At most this many words in a mark (``C/L OF SINK``): a sentence of notes is no mark.
MARK_WORDS: Final = 4
#: A dash-dot line: at least this many dashes in turn, the short ones at most this share of the
#: long ones, each kind within its share of the kind's middle length, the gaps within this ratio
#: of each other.
DASH_DOT_RUN: Final = 5
DASH_DOT_SHORT_SHARE: Final = 0.4
DASH_DOT_LONG_SPREAD: Final = 0.2
DASH_DOT_SHORT_SPREAD: Final = 0.35
DASH_DOT_GAP_RATIO: Final = 2.5
#: The least length (points) of a dash-dot line's long dash: hatching is finer.
DASH_DOT_LONG_MIN_PT: Final = 2.0
#: A dash-dot line's pieces lie this close across (points) to the end's station: a drawn thing's
#: edge beside the line is not one of its dashes.
DASH_DOT_ON_PT: Final = 0.3
#: The pattern starts within this many pieces of the dimension line (the first may be cut short
#: where the extension line begins).
DASH_DOT_START: Final = 2
#: A real gap between two dashes (points): pieces closer are one drawn thing.
DASH_DOT_GAP_MIN_PT: Final = 0.2

#: A centre-line mark, as a word of its own: CL, C L, C/L, C.L., ℄, CENTER LINE, CENTRELINE.
_MARK: Final = re.compile(
    r"(?<![A-Z0-9-])(?:C\s*/\s*L|C\.?\s?L\.?|℄|CENT(?:ER|RE)\s*-?\s*LINE)(?![A-Z0-9-])"
)
#: Items code compared itself: their own checks speak.
_CODE_COMPARED: Final = frozenset({"sink_centre", "sink_offset", "filler_range", "run_fit"})

type Piece = tuple[float, float]


def is_mark(text: object) -> bool:
    """Whether a printed phrase is a centre-line mark (it may name its thing: ``CL SINK``)."""
    words = " ".join(str(text or "").split()).upper()
    if not words or len(words.split()) > MARK_WORDS:
        return False
    return _MARK.search(words) is not None


@dataclass(frozen=True, slots=True)
class CentreEnd:
    """One end of a dimension that lands on a centre line, and how code knows."""

    side: str
    at: float
    how: str


def _pieces(page: dim_ends.Page, span: dim_ends.Span, at: float, across: float) -> list[Piece]:
    """The pieces at right angles to the dimension within `across` of its end at `at`, in order
    along the end's line, overlapping copies merged."""
    which = "v" if span.axis == "h" else "h"
    merged: list[Piece] = []
    for lo, hi in sorted(
        (lo, hi) for lo, hi, _place in dim_ends._near(page, which, at - across, at + across)
    ):
        if merged and lo <= merged[-1][1] + 0.05:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return merged


def _runs(pieces: Sequence[Piece]) -> list[Piece]:
    """The lines the pieces make, joined over gaps up to `DASH_GAP_PT`."""
    runs: list[Piece] = []
    for lo, hi in pieces:
        if runs and lo <= runs[-1][1] + DASH_GAP_PT:
            runs[-1] = (runs[-1][0], max(runs[-1][1], hi))
        else:
            runs.append((lo, hi))
    return runs


def _outward(pieces: Sequence[Piece], row: float) -> tuple[list[Piece], list[Piece]]:
    """The end's line from the dimension line (at `row`) outward each way, each in order away from
    it: it must start within `MEETS_PT` of the dimension line and stops at the first gap longer
    than `DASH_GAP_PT`."""

    def walk(way: Sequence[Piece], start: float) -> list[Piece]:
        kept: list[Piece] = []
        for lo, hi in way:
            if hi <= start:
                continue  # wholly on the other side of the dimension line
            edge = kept[-1][1] if kept else start
            if lo - edge > (DASH_GAP_PT if kept else MEETS_PT):
                break  # the line stops (or never reaches the dimension line)
            kept.append((max(lo, start), hi))
        return kept

    return walk(pieces, row), walk([(-hi, -lo) for lo, hi in reversed(pieces)], -row)


def dash_dot(parts: Sequence[Piece]) -> bool:
    """Whether a line's pieces (in order along it, from the dimension line out) begin a dash-dot
    pattern within their first few dashes: at least `DASH_DOT_RUN` dashes in a row, long and short
    in turn starting with a long one (so three long dashes agree), each kind about one length, the
    short ones much shorter, the gaps about even. A solid line drawn over a dash-dot line joins its
    long dashes into runs of different lengths: no pattern on its own."""
    lengths = [hi - lo for lo, hi in parts]
    gaps = [b[0] - a[1] for a, b in pairwise(parts)]
    for start in range(min(len(lengths) - DASH_DOT_RUN + 1, DASH_DOT_START + 1)):
        window = lengths[start : start + DASH_DOT_RUN]
        spaces = gaps[start : start + DASH_DOT_RUN - 1]
        if min(spaces) <= DASH_DOT_GAP_MIN_PT:
            continue
        if max(spaces) > DASH_DOT_GAP_RATIO * min(spaces) + 0.5:
            continue
        longs, shorts = window[0::2], window[1::2]
        long_mid, short_mid = statistics.median(longs), statistics.median(shorts)
        if long_mid < DASH_DOT_LONG_MIN_PT or short_mid > DASH_DOT_SHORT_SHARE * long_mid:
            continue
        if any(abs(v - long_mid) > DASH_DOT_LONG_SPREAD * long_mid for v in longs):
            continue
        if any(abs(v - short_mid) > max(DASH_DOT_SHORT_SPREAD * short_mid, 0.3) for v in shorts):
            continue
        return True
    return False


def _mark_on(
    span: dim_ends.Span, at: float, run: Piece, phrases: Sequence[Mapping[str, Any]]
) -> str | None:
    """The centre-line mark printed on the end's line (`run`, from-to along it): centred across
    it, within `MARK_ALONG_HEIGHTS` of its own height of the line along it."""
    low, high = run
    for phrase in phrases:
        text = str(phrase.get("text") or "")
        if not is_mark(text):
            continue
        box = dim_ends._box(phrase.get("box"))
        if box is None:
            continue
        if span.axis == "h":  # the end's line is upright: across is x, along is y
            near, far, first, last = box[0], box[2], box[1], box[3]
        else:
            near, far, first, last = box[1], box[3], box[0], box[2]
        if abs((near + far) / 2 - at) > max(MARK_ACROSS_PT, MARK_ACROSS_SHARE * (far - near)):
            continue
        reach = MARK_ALONG_HEIGHTS * max(last - first, 1.0)
        if last < low - reach or first > high + reach:
            continue
        return f'a centre-line mark ("{" ".join(text.split())}") on the line at that end'
    return None


def _centre_at(
    page: dim_ends.Page,
    span: dim_ends.Span,
    at: float,
    phrases: Sequence[Mapping[str, Any]],
) -> str | None:
    """How code knows the end at `at` is drawn on a centre line; `None` when it sees none."""
    if page.lines is None:
        return None
    for run in _runs(_pieces(page, span, at, ON_LINE_PT)):
        if not run[0] - MEETS_PT <= span.row <= run[1] + MEETS_PT:
            continue  # not the end's own line: it never reaches the dimension line
        mark = _mark_on(span, at, run, phrases)
        if mark is not None:
            return mark
    if any(dash_dot(way) for way in _outward(_pieces(page, span, at, DASH_DOT_ON_PT), span.row)):
        return "a dash-dot centre line drawn through that end"
    return None


def centre_ends(
    page: dim_ends.Page | None,
    label_id: object,
    phrases: Sequence[Mapping[str, Any]],
    *,
    kinds: bool = False,
) -> tuple[CentreEnd, ...] | None:
    """The ends of the label's dimension drawn on a centre line (see the module); with `kinds`,
    also the ends the like-for-like reader names as a centre (`VENDOR_CENTRE_KINDS`). `None` when
    code has no line for the label (nothing is known of its ends)."""
    if page is None:
        return None
    span = page.spans.get(str(label_id))
    if span is None:
        return None
    found: list[CentreEnd] = []
    for side, at in (("a", span.a), ("b", span.b)):
        how = _centre_at(page, span, at, phrases)
        named = page.kinds.get((span.id, side), set()) & VENDOR_CENTRE_KINDS if kinds else set()
        if how is None and named:
            how = dim_ends.KIND_WORDS[min(named)]
        if how is not None:
            found.append(CentreEnd(side, at, how))
    return tuple(found)


def _equal_split(label: Mapping[str, Any]) -> int:
    """The centre ends of an equal split's part (``EQ`` / ``EQ``: the middle of the run): one
    shared end with the next part; none for any other label."""
    derived = label.get("derived")
    if isinstance(derived, Mapping) and derived.get("rule") == "equal_parts":
        return 1
    return 1 if re.fullmatch(r"\s*EQ\.?\s*", str(label.get("text") or ""), re.IGNORECASE) else 0


def _exempt(finding: Mapping[str, Any]) -> bool:
    """An item the rule leaves: not a vendor size set against a reference label (the vendor leaves
    the thing out, so the architect's locating dimension is the item itself), one code compared
    itself (its own pair by drawn position, a GV formula, the field cut, the sink-centre check),
    or one the reviewer decided on the pair it shows."""
    if finding.get("status") not in ("same", "different", "check"):
        return True
    if finding.get("source") not in (None, "architect", "brand_prototype"):
        return True
    if finding.get("created_by") in _CODE_COMPARED or finding.get("paired_by") == "code":
        return True
    if finding.get("formula") or finding.get("field_cut"):
        return True
    decision = finding.get("decision")
    if isinstance(decision, Mapping) and decision.get("decision"):
        return True
    vendor = str(finding.get("vendor") or "")
    return classification.missing_on_vendor(finding) or not re.search(
        r"\d|\bEQ\b", vendor, re.IGNORECASE
    )


def _sides(
    finding: Mapping[str, Any],
) -> tuple[Mapping[str, Any], Mapping[str, Any] | None] | None:
    dims = finding.get("dims")
    if not isinstance(dims, Mapping):
        return None
    reference, vendor = dims.get("architect"), dims.get("vendor")
    if not isinstance(reference, Mapping) or not reference.get("id"):
        return None
    return reference, vendor if isinstance(vendor, Mapping) and vendor.get("id") else None


def _where(side: Mapping[str, Any]) -> tuple[str, int]:
    return str(side.get("file") or ""), int(side.get("page") or 0)


def judge(
    finding: Mapping[str, Any],
    pages: Callable[[str, int], dim_ends.Page | None],
    phrases: Mapping[tuple[str, int], Sequence[Mapping[str, Any]]],
) -> Mapping[str, Any] | None:
    """The record for an item whose architect dimension runs to more centre lines than the
    vendor's dimension it is set against (see the module); `CLEAR` when code read the architect's
    line and it does not; `None` when the rule does not apply or code has no line to read."""
    if _exempt(finding):
        return None
    sides = _sides(finding)
    if sides is None:
        return None
    reference, vendor = sides
    where = _where(reference)
    ends = centre_ends(pages(*where), reference.get("id"), phrases.get(where, ()))
    if ends is None:
        return None
    if not ends:
        return CLEAR
    theirs = 0
    if vendor is not None:
        v_where = _where(vendor)
        found = centre_ends(pages(*v_where), vendor.get("id"), phrases.get(v_where, ()), kinds=True)
        theirs = max(len(found or ()), _equal_split(vendor))
    if theirs >= len(ends):
        return CLEAR  # both run to centres: like with like, left to the other checks
    value = " ".join(str(reference.get("text") or finding.get("architect") or "").split())
    which = "both its ends are on centre lines" if len(ends) == 2 else "one end is on a centre line"
    return {
        "outcome": "centre_line",
        "why": (
            f"the architect's {value or 'dimension'} is measured to a centre line, not between the "
            f"item's own edges: {which} ({ends[0].how}). It places a fixture or a centre; it is "
            "not a size of the item, so it is not compared"
        ),
        "vendor": dim_ends._label_key(vendor),
        "architect": dim_ends._label_key(reference),
        "ends": [end.how for end in ends],
    }


def reason(finding: Mapping[str, Any]) -> str | None:
    """The plain reason the item is not compared (its architect dimension is measured to a centre
    line), while the item still pairs the two labels the record was made for and the rule still
    applies to it; `None` otherwise."""
    held = finding.get(FIELD)
    if not isinstance(held, Mapping) or held.get("outcome") != "centre_line":
        return None
    if _exempt(finding):
        return None
    dims = finding.get("dims") if isinstance(finding.get("dims"), Mapping) else {}
    if dim_ends._label_key((dims or {}).get("vendor")) != held.get("vendor"):
        return None
    if dim_ends._label_key((dims or {}).get("architect")) != held.get("architect"):
        return None
    return str(held.get("why") or "") or None
