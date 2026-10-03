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

**It sees paths only.** A label the vendor drew as font text inside the stamp is invisible to it, as
it is to the rest of the reader (#738). That is every label on `AI_Set 1`, whose fractions are text
objects either side of a 0.58 pt filled bar, and some on `AI_Set 2`: its stamps hold 1,050 text
objects, and the stacked fractions on pages 15 and 17 are drawn that way. The 12 of 12 measured on
#735 counts the fractions drawn as paths, because the recall net searched paths too.

**The error it is allowed to make is the safe one.** A false detection costs a reviewer one look at a
crop. A missed one lets a misread fraction reach the agreement gate. Where the two trade, the rules
here give up precision, never recall.

**Each detection also says where every part of the label was drawn** (#834): the bar, the whole
number's characters, the numerator's, the denominator's, and the inch mark (`FractionLayout`). That
is still not a reading — it counts characters and never says which digit one is. It is what lets a
reading of the label be checked against the drawing: a stacked `3/4"` read as `3 3/4"` has a whole
number the drawing does not, and `extraction/models/validation.py` refuses it on that count.

Source: issues #541, #735, #834. Verification: tests/extraction/test_glyph_bands.py.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

__all__ = [
    "FractionBarGeometry",
    "FractionLayout",
    "GlyphBox",
    "GlyphCharacter",
    "stacked_fractions",
]

#: One path's bounding box as `annotations.py` holds it: `(x0, y0, x1, y1)` in the page's own units,
#: y increasing upward as PDF space does.
type GlyphBox = tuple[Decimal, Decimal, Decimal, Decimal]

#: One character of a label as the file draws it: the boxes of the paths that make it, in the order
#: they run along the baseline. Usually one path; a `4` drawn as a body and a stem is two.
type GlyphCharacter = tuple[GlyphBox, ...]


@dataclass(frozen=True, slots=True)
class FractionBarGeometry:
    """The seven lengths and ratios the bar detector and its layout run under. Stated by a deployment,
    never defaulted.

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

    character_gap_pt: Decimal
    """How far apart along the baseline two characters of one label may be (#834): the whole
    number's digits from each other and from the fraction, and the inch mark from the fraction.
    Also how near either end of the label a path the layout counts as nothing must come to be its
    neighbour (`FractionLayout.neighbours`, #848).

    It decides only the layout, never whether a fraction is found. Too small, and a whole number's
    first digit is left out, so a reading that drops it — `9 1/2"` for `39 1/2"` — would match the
    count. Too large, and a neighbouring label's character is counted as part of this one."""

    def __post_init__(self) -> None:
        for name in (
            "bar_thickness_max_pt",
            "bar_length_min_pt",
            "reach_pt",
            "glyph_min_pt",
            "glyph_max_pt",
            "proportion_max",
            "character_gap_pt",
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
            f"proportion<={self.proportion_max};character_gap<={self.character_gap_pt}"
        )


@dataclass(frozen=True, slots=True)
class FractionLayout:
    """Where each part of one stacked label was drawn, in the page's own axes (#834).

    **Counted, never read.** Each part is a tuple of characters, and each character the boxes of the
    paths that draw it, so how many characters a part has is `len` of it. Nothing here says which
    digit one is: that is a reader's job, and the layout is what a reading is checked against.

    **What counts as one of the label's characters.** A path the caller did not mark as drawing ink
    (black or grey) is not one, whatever its shape: a reviewer's markup baked into a snapshot sits on
    the label in colour, and counting it would add a character the vendor never drew. Paths that
    overlap along the baseline are one character, so a `4` drawn as a body and a stem counts once.
    Beside the fraction, only a path lying wholly inside the label's digit band — from the foot of
    the denominator to the head of the numerator — is a character, which leaves out the dimension
    ticks drawn below a label.
    """

    box: GlyphBox
    """The bar, numerator and denominator together — what the detector has reported since #735, and
    what a crop is checked against. The whole number and inch mark are not in it."""

    bar: GlyphBox
    whole: tuple[GlyphCharacter, ...]
    """The whole number's characters in reading order; empty for a bare fraction like `3/4"`."""

    numerator: tuple[GlyphCharacter, ...]
    denominator: tuple[GlyphCharacter, ...]
    inch_mark: GlyphCharacter
    """The paths drawn after the fraction and wholly above its bar, as the inch mark is: one path
    with two ticks, or two paths of one tick each. Empty where nothing like that follows."""

    neighbours: tuple[GlyphBox, ...]
    """Black or grey paths the layout counts as no part of the label, though they are centred across
    its digit band and lie within `character_gap_pt` of either end of it (#848). Empty where there
    are none.

    **Each one may be a character the count is missing.** A whole number's first digit drawn a
    little taller than the band is not taken into it, and then `39 1/2"` lays out as `9 1/2"`; a
    `+` after the inch mark is the start of more label, as in `39 1/2"+6"`. A reading of the label
    put together from its parts is refused while any is here. A dimension tick below a label is not
    one: it is centred below the band, though it may reach into it."""

    rotation_degrees: int
    """How far the label's baseline is turned on the page, anticlockwise: 0 for a label that reads
    upright, else 90, 180 or 270 (#848). Every box above is in the page's own axes whatever it is;
    this says which way the characters in them face."""


#: `(along_low, across_low, along_high, across_high)` — a box on the baseline's own axes, with
#: `along` running the way the label reads and `across` running up its characters.
type _Span = tuple[Decimal, Decimal, Decimal, Decimal]


def _frame(box: GlyphBox, rotation_degrees: int) -> _Span:
    """A page box on the axes of a baseline turned `rotation_degrees` anticlockwise.

    Turned, not only swapped: which side of the bar is the numerator, and which end of the label the
    whole number is at, depend on the direction the label reads. Every step is an exact swap or
    negation, so nothing is rounded.
    """
    x0, y0, x1, y1 = box
    turn = rotation_degrees % 360
    if turn == 0:
        return (x0, y0, x1, y1)
    if turn == 90:
        return (y0, -x1, y1, -x0)
    if turn == 180:
        return (-x1, -y1, -x0, -y0)
    if turn == 270:
        return (-y1, x0, -y0, x1)
    raise ValueError(f"only quarter turns are read, not {rotation_degrees} degrees")


def _unframe(span: _Span, rotation_degrees: int) -> GlyphBox:
    """`_frame` undone: a span on the baseline's axes as a box in the page's own."""
    a0, c0, a1, c1 = span
    turn = rotation_degrees % 360
    if turn == 0:
        return (a0, c0, a1, c1)
    if turn == 90:
        return (-c1, a0, -c0, a1)
    if turn == 180:
        return (-a1, -c1, -a0, -c0)
    return (c0, -a1, c1, -a0)


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


def _overlap(first: _Span, second: _Span) -> bool:
    """Whether two paths share more along the baseline than an edge, or are drawn at the same place."""
    if (first[0], first[2]) == (second[0], second[2]):
        return True
    return first[0] < second[2] and second[0] < first[2]


def _characters(indices: list[int], spans: list[_Span]) -> list[list[int]]:
    """Paths grouped into characters: those that overlap along the baseline are one, in reading order.

    **Overlap means sharing more than an edge.** Two digits set so tight that they touch stay two;
    merging them would undercount the number, and an undercount is what lets a reading that dropped a
    digit match. A stroke with no width merges only with a path it lies strictly inside, or with one
    drawn at the same place — the same stroke drawn twice to embolden it. Overlap is followed from
    path to path, so a character's paths need not all overlap each other.
    """
    groups: list[list[int]] = []
    for index in indices:
        joined = [group for group in groups if any(_overlap(spans[index], spans[m]) for m in group)]
        merged = [index, *(member for group in joined for member in group)]
        groups = [group for group in groups if group not in joined] + [merged]
    ordered = [sorted(group, key=lambda position: (spans[position], position)) for group in groups]
    return sorted(ordered, key=lambda group: (spans[group[0]], group[0]))


def _chain(
    edge: Decimal, candidates: list[int], spans: list[_Span], gap: Decimal, *, before: bool
) -> list[int]:
    """The candidates that run on from one end of the fraction, each within `gap` of the last.

    `before` follows the label back from `edge` towards its start, where the whole number is;
    otherwise it follows on from `edge`, where the inch mark is. A candidate that crosses `edge` is
    never taken. Each one taken can only bring more within reach, never fewer, so the set reached is
    the same whatever order the paths were drawn in.
    """
    taken: list[int] = []
    reach = edge
    grew = True
    while grew:
        grew = False
        for index in candidates:
            if index in taken:
                continue
            span = spans[index]
            if before:
                joins = reach - gap <= span[2] <= edge
            else:
                joins = edge <= span[0] <= reach + gap
            if joins:
                taken.append(index)
                reach = min(reach, span[0]) if before else max(reach, span[2])
                grew = True
    return taken


def _layout(
    bar: int,
    above: list[int],
    below: list[int],
    small: list[int],
    spans: list[_Span],
    ink: Sequence[bool],
    *,
    geometry: FractionBarGeometry,
    rotation_degrees: int,
) -> FractionLayout:
    """One detection's parts: its bar, the ink paths above and below it, and those beside it.

    `above` and `below` are every path the detector took for the numerator and denominator, and the
    detection's box is theirs whatever their colour, as it always was. The characters are drawn from
    ink paths only. Beside the fraction, an ink path is a character only inside the digit band and
    only where `_chain` reaches it; an ink path centred in the band within the gap of either end,
    and counted as nothing, is a neighbour.
    """
    detected = _union([spans[bar], *(spans[index] for index in (*above, *below))])
    numerator = [index for index in above if ink[index]]
    denominator = [index for index in below if ink[index]]
    whole: list[int] = []
    mark: list[int] = []
    neighbours: list[int] = []
    stacked = [spans[index] for index in (*numerator, *denominator)]
    if stacked:
        foot = min(span[1] for span in stacked)
        head = max(span[3] for span in stacked)
        taken = {bar, *above, *below}
        band = [
            index
            for index in small
            if ink[index]
            and index not in taken
            and foot <= spans[index][1]
            and spans[index][3] <= head
        ]
        gap = geometry.character_gap_pt
        whole = _chain(detected[0], band, spans, gap, before=True)
        # **Wholly above the bar**, because that is where an inch mark sits and where no digit
        # does: a digit beside a stacked fraction is centred on its bar.
        over_bar = [index for index in band if spans[index][1] > spans[bar][3]]
        mark = _chain(detected[2], over_bar, spans, gap, before=False)
        # **Compared by place, not by path**, because the same stroke drawn twice to embolden it is
        # two paths: the bar's twin is no neighbour of the bar.
        counted = {spans[index] for index in (*taken, *whole, *mark)}
        label = _union([detected, *(spans[index] for index in (*whole, *mark))])
        neighbours = [
            index
            for index in small
            if ink[index]
            and spans[index] not in counted
            and foot <= (spans[index][1] + spans[index][3]) / 2 <= head
            and spans[index][0] <= label[2] + gap
            and label[0] - gap <= spans[index][2]
        ]

    def characters(indices: list[int]) -> tuple[GlyphCharacter, ...]:
        return tuple(
            tuple(_unframe(spans[member], rotation_degrees) for member in group)
            for group in _characters(indices, spans)
        )

    return FractionLayout(
        box=_unframe(detected, rotation_degrees),
        bar=_unframe(spans[bar], rotation_degrees),
        whole=characters(whole),
        numerator=characters(numerator),
        denominator=characters(denominator),
        inch_mark=tuple(
            _unframe(spans[index], rotation_degrees)
            for index in sorted(mark, key=lambda position: (spans[position], position))
        ),
        neighbours=tuple(
            _unframe(spans[index], rotation_degrees)
            for index in sorted(neighbours, key=lambda position: (spans[position], position))
        ),
        rotation_degrees=rotation_degrees % 360,
    )


def stacked_fractions(
    boxes: Sequence[GlyphBox],
    *,
    geometry: FractionBarGeometry,
    ink: Sequence[bool],
    rotation_degrees: int = 0,
) -> tuple[FractionLayout, ...]:
    """Every stacked fraction drawn among these paths, each with where its parts were drawn.

    `boxes` is every path's bounding box in one stamp, **not only the glyph runs**. The runs have
    already lost the denominator and the bar (see the module docstring); this has to be handed what
    was drawn, not what survived clustering.

    `ink` says, box for box, whether that path is drawn in black or grey. It has no default: a caller
    that cannot say must say so in its own code. It decides only which paths are counted in a
    layout; finding the fraction looks at every path, so a bar is not missed for its colour.

    A bar is a flat stroke (`bar_thickness_max_pt`, `bar_length_min_pt`). It is a fraction's bar when:

    1. shapes start within `reach_pt` above it and below it, overlapping it along the baseline;
       each side together is at least `glyph_min_pt` tall, and at least one path on one side is at
       least `glyph_min_pt` in both directions — a glyph, not a scatter of arc pieces;
    2. both sides are centred on it — each side's middle within one bar length of the bar's;
    3. the two sides are within `proportion_max` of each other in height, and the bar within it of the
       wider side's width;
    4. no other stroke on the bar's line comes within `reach_pt` of it (`_is_isolated`).

    Each side is taken as a whole rather than glyph by glyph, so a numerator drawn as several strokes
    — or a `1` that is one vertical stroke with no width at all — still counts as one shape. The
    layout then splits each side into its characters (`FractionLayout`).

    Only multiples of 90° are handled, because those are the only baseline rotations
    `annotations.py` reads from a stamp; any other is refused. Text turned inside a stamp that does
    not say so is not detected: its bar is vertical, and a vertical flat stroke between two shapes is
    also what a stroke-font `1` looks like beside its neighbours.
    """
    if len(ink) != len(boxes):
        raise ValueError(
            f"ink must say for each of the {len(boxes)} boxes whether its path is drawn in black "
            f"or grey; it has {len(ink)} entries"
        )
    spans = [_frame(box, rotation_degrees) for box in boxes]
    small = [
        index
        for index, span in enumerate(spans)
        if _along(span) < geometry.glyph_max_pt and _across(span) < geometry.glyph_max_pt
    ]

    found: list[FractionLayout] = []
    for bar_index in small:
        bar = spans[bar_index]
        if _across(bar) > geometry.bar_thickness_max_pt or _along(bar) < geometry.bar_length_min_pt:
            continue
        above: list[int] = []
        below: list[int] = []
        for index in small:
            other = spans[index]
            if other == bar or _across(other) <= geometry.bar_thickness_max_pt:
                continue
            if not (other[0] < bar[2] and bar[0] < other[2]):
                continue
            if bar[3] <= other[1] <= bar[3] + geometry.reach_pt:
                above.append(index)
            elif bar[1] - geometry.reach_pt <= other[3] <= bar[1]:
                below.append(index)
        if not above or not below:
            continue
        # At least one side must hold a path with real extent in *both* axes. Only one, because a
        # stroke-font `1` can be a single stroke with no width at all, and `1/2`, `1/4`, `1/8` and
        # `1/16` are the commonest fractions in the trade; a denominator is never a bare `1`.
        # Measured on the client set (#735): each of the 12 real fractions has such a path on both
        # sides, and each of the 28 electrical-outlet symbols that otherwise pass has it on neither —
        # their circles are drawn as dozens of short arc pieces.
        if not any(
            _along(spans[index]) >= geometry.glyph_min_pt
            and _across(spans[index]) >= geometry.glyph_min_pt
            for index in (*above, *below)
        ):
            continue

        numerator = _union([spans[index] for index in above])
        denominator = _union([spans[index] for index in below])
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

        layout = _layout(
            bar_index,
            above,
            below,
            small,
            spans,
            ink,
            geometry=geometry,
            rotation_degrees=rotation_degrees,
        )
        # The same stroke drawn twice finds the same fraction twice; it is one label.
        if all(earlier.box != layout.box for earlier in found):
            found.append(layout)
    return tuple(found)
