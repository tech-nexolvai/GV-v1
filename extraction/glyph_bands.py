"""Where a stacked fraction was drawn, found by its bar (#541, #735).

**The failure this exists to catch.** On the client's drawings a fraction is drawn stacked: a
numerator, a short bar, a denominator. Vision readers get these wrong in ways no check on the string
can see. `28 3/4"` came back as `284`; a stacked `3/4"` came back as `3 3/4"` from two readers of
**different vendors** that then agreed with each other (#726), which is exactly the agreement the
second-reader lane would have sealed. Both misreadings are well-formed dimensions. The admin's rule
(#726) is therefore that a stacked fraction always goes to a reviewer, however many readers agree —
and that rule can only be enforced by something that can tell a label is stacked.

**So this asks the sheet, not the model.** It finds the bar: a short, flat stroke with a glyph-sized
shape directly above it and another directly below, the three lined up along the baseline. It never
reads a digit, never says what the number is, and a detection only ever sends a reading to a
reviewer.

**Why the bar, and not "two rows of glyphs".** The first version (#541) looked for two glyphs one
above the other *inside a text run*. It could never fire on a real drawing: `_glyph_runs` in
`extraction/annotations.py` groups glyphs along the baseline, so a denominator and its bar — which
sit *below* the numerator and beside nothing — are orphaned before any run exists (#735, measured on
page 4). And "two rows" is true of every two-line note. A bar between the rows is not.

**Every threshold is the caller's.** They are `FractionBarGeometry`, stated by a deployment next to
the other reader lengths and never defaulted, for the reason `line_minimum_pt` has none (#179): the
values that separate a fraction from hatching were measured on one client set, and a number fitted
there would look like a detector everywhere else. What they were measured against, and what they
found, is recorded on #735.

**It sees paths only.** A label the vendor drew as font text inside the stamp — every label on
`AI_Set 1`, whose fractions are text objects either side of a 0.58 pt filled bar — is invisible to
it, as it is to the rest of the reader (#738).

**The error it is allowed to make is the safe one.** A false detection costs a reviewer one look at a
crop. A missed one lets a misread fraction reach the agreement gate. Where the two trade, the rules
here give up precision, never recall.

Source: issues #541, #735. Verification: tests/extraction/test_glyph_bands.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

__all__ = [
    "FractionBarGeometry",
    "GlyphBox",
    "stacked_fractions",
]

#: One path's bounding box as `annotations.py` holds it: `(x0, y0, x1, y1)` in the page's own units,
#: y increasing upward as PDF space does.
type GlyphBox = tuple[Decimal, Decimal, Decimal, Decimal]


@dataclass(frozen=True, slots=True)
class FractionBarGeometry:
    """The six lengths and ratios the bar detector runs under. Stated by a deployment, never defaulted.

    All lengths are in **PDF points** (72 to the inch), measured along and across the text baseline
    rather than the page, so a label turned a quarter turn is measured the way it is read.
    """

    bar_thickness_max_pt: Decimal
    """How thick across the baseline a stroke may be and still be a bar. The client's bars are drawn
    as zero-width strokes, so their boxes are flat. The same distance is the tolerance for another
    stroke lying on the bar's line (see `reach_pt`)."""

    bar_length_min_pt: Decimal
    """How long along the baseline a bar must be — shorter is a dot or a tick."""

    reach_pt: Decimal
    """How far above or below the bar the numerator and denominator may begin.

    Also how near another stroke on the bar's own line may come to either end before the "bar" is
    taken to be one dash of a longer line. Hatching and dashed line-work are the false detections a
    flat stroke with shapes either side of it attracts, and this is what rules them out."""

    glyph_min_pt: Decimal
    """How tall across the baseline the numerator and the denominator must each be. A flat stroke
    above a flat stroke is two lines, not a fraction."""

    glyph_max_pt: Decimal
    """A path larger than this in either axis is not part of a fraction.

    **The detector's own bound, and deliberately not `glyph_maximum_pt`.** That one decides which
    strokes may be text *for the dimension-line detector*, and is set small so dimension lines are not
    swallowed as glyphs: at the demo's 5 pt it excludes the 5.5 pt numerator of the very fraction this
    exists to find. Tying the two together would let a change to one silently blind the other."""

    proportion_max: Decimal
    """How far apart in proportion the three parts may be: the taller of numerator and denominator
    against the shorter, and the bar's length against the wider of the two, each within this ratio
    either way. A digit stacked over a digit is the same height as it and spans the same bar."""

    def __post_init__(self) -> None:
        for name in (
            "bar_thickness_max_pt",
            "bar_length_min_pt",
            "reach_pt",
            "glyph_min_pt",
            "glyph_max_pt",
            "proportion_max",
        ):
            value = getattr(self, name)
            if isinstance(value, float):
                raise TypeError(f"{name} must be a Decimal, never a float")
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ValueError(f"{name} must be a finite positive Decimal")
        if self.proportion_max < 1:
            raise ValueError(
                f"proportion_max must be at least 1, got {self.proportion_max}: it is a ratio of "
                "the larger part to the smaller, so below 1 nothing could match"
            )
        if self.glyph_min_pt >= self.glyph_max_pt:
            raise ValueError("glyph_min_pt must be smaller than glyph_max_pt")

    @property
    def config_hash(self) -> str:
        """Every number, as text for the identity of a run whose results these settings change."""
        return (
            f"bar<={self.bar_thickness_max_pt}x>={self.bar_length_min_pt};"
            f"reach<={self.reach_pt};glyph={self.glyph_min_pt}..{self.glyph_max_pt};"
            f"proportion<={self.proportion_max}"
        )


#: `(along_low, across_low, along_high, across_high)` — a box on the baseline's own axes.
type _Span = tuple[Decimal, Decimal, Decimal, Decimal]


def _span(box: GlyphBox, *, turned: bool) -> _Span:
    return (box[1], box[0], box[3], box[2]) if turned else box


def _along(span: _Span) -> Decimal:
    return span[2] - span[0]


def _across(span: _Span) -> Decimal:
    return span[3] - span[1]


def _union(spans: list[_Span]) -> _Span:
    return (
        min(span[0] for span in spans),
        min(span[1] for span in spans),
        max(span[2] for span in spans),
        max(span[3] for span in spans),
    )


def _centre(span: _Span) -> Decimal:
    return (span[0] + span[2]) / 2


def _within(larger: Decimal, smaller: Decimal, ratio: Decimal) -> bool:
    """Whether two positive sizes are within `ratio` of each other, compared by multiplication.

    Multiplied rather than divided so a zero-width stroke cannot raise `DivisionByZero`, and so the
    comparison stays exact in Decimal.
    """
    high, low = max(larger, smaller), min(larger, smaller)
    return high <= low * ratio


def _is_isolated(bar: _Span, spans: list[_Span], geometry: FractionBarGeometry) -> bool:
    """Whether nothing else on the bar's own line comes within `reach_pt` of it.

    A fraction bar stands alone. A dash in a dashed line, or one stroke of a hatch, has a neighbour
    collinear with it a short gap away — and that neighbour is all that separates the two on the
    client's sheets: on page 9, 17 hatch strokes had a shape above and below them just as a bar does.

    An identical box is the same stroke drawn twice, which PDFs do to embolden, and is not a
    neighbour.
    """
    line = (bar[1] + bar[3]) / 2
    for other in spans:
        if other == bar or _across(other) > geometry.bar_thickness_max_pt:
            continue
        if abs((other[1] + other[3]) / 2 - line) > geometry.bar_thickness_max_pt:
            continue
        gap = max(other[0] - bar[2], bar[0] - other[2])
        if gap <= geometry.reach_pt:
            return False
    return True


def stacked_fractions(
    boxes: list[GlyphBox] | tuple[GlyphBox, ...],
    *,
    geometry: FractionBarGeometry,
    rotation_degrees: int = 0,
) -> tuple[GlyphBox, ...]:
    """Every stacked fraction drawn among these paths, each as the box around bar, numerator and
    denominator together, in the page's own axes.

    `boxes` is every path's bounding box in one stamp, **not only the glyph runs**. The runs have
    already lost the denominator and the bar (see the module docstring); this has to be handed what
    was drawn, not what survived clustering.

    A bar is a flat stroke (`bar_thickness_max_pt`, `bar_length_min_pt`). It is a fraction's bar when:

    1. shapes start within `reach_pt` above it and below it, overlapping it along the baseline;
       each side together is at least `glyph_min_pt` tall, and at least one path on one side is at
       least `glyph_min_pt` in both directions — a glyph, not a scatter of arc pieces;
    2. both sides are centred on it — each side's middle within one bar length of the bar's;
    3. the two sides are within `proportion_max` of each other in height, and the bar within it of the
       wider side's width;
    4. no other stroke on the bar's line comes within `reach_pt` of it (`_is_isolated`).

    Each side is taken as a whole rather than glyph by glyph, so a numerator drawn as several strokes
    — or a `1` that is one vertical stroke with no width at all — still counts as one shape.

    Only multiples of 90° are handled, because those are the only baseline rotations
    `annotations.py` reads from a stamp. Text turned inside a stamp that does not say so is not
    detected: its bar is vertical, and a vertical flat stroke between two shapes is also what a
    stroke-font `1` looks like beside its neighbours.
    """
    turned = rotation_degrees % 180 != 0
    spans = [_span(box, turned=turned) for box in boxes]
    small = [
        span
        for span in spans
        if _along(span) < geometry.glyph_max_pt and _across(span) < geometry.glyph_max_pt
    ]

    found: list[GlyphBox] = []
    for bar in small:
        if _across(bar) > geometry.bar_thickness_max_pt or _along(bar) < geometry.bar_length_min_pt:
            continue
        above: list[_Span] = []
        below: list[_Span] = []
        for other in small:
            if other == bar or _across(other) <= geometry.bar_thickness_max_pt:
                continue
            if not (other[0] < bar[2] and bar[0] < other[2]):
                continue
            if bar[3] <= other[1] <= bar[3] + geometry.reach_pt:
                above.append(other)
            elif bar[1] - geometry.reach_pt <= other[3] <= bar[1]:
                below.append(other)
        if not above or not below:
            continue
        # At least one side must hold a path with real extent in *both* axes. Only one, because a
        # stroke-font `1` can be a single stroke with no width at all, and `1/2`, `1/4`, `1/8` and
        # `1/16` are the commonest fractions in the trade; a denominator is never a bare `1`.
        # Measured on the client set (#735): each of the 12 real fractions has such a path on both
        # sides, and each of the 28 electrical-outlet symbols that otherwise pass has it on neither —
        # their circles are drawn as dozens of short arc pieces.
        if not any(
            _along(side) >= geometry.glyph_min_pt and _across(side) >= geometry.glyph_min_pt
            for side in (*above, *below)
        ):
            continue

        numerator, denominator = _union(above), _union(below)
        bar_length = _along(bar)
        if (
            _across(numerator) < geometry.glyph_min_pt
            or _across(denominator) < geometry.glyph_min_pt
        ):
            continue
        if (
            abs(_centre(numerator) - _centre(bar)) > bar_length
            or abs(_centre(denominator) - _centre(bar)) > bar_length
        ):
            continue
        if not _within(_across(numerator), _across(denominator), geometry.proportion_max):
            continue
        widest = max(_along(numerator), _along(denominator))
        if not _within(bar_length, widest, geometry.proportion_max):
            continue
        if not _is_isolated(bar, spans, geometry):
            continue

        whole = _union([bar, numerator, denominator])
        box = (whole[1], whole[0], whole[3], whole[2]) if turned else whole
        if box not in found:
            found.append(box)
    return tuple(found)
