"""The places the next answer key is drawn from, and the sample drawn from them (#867; #728 steps 2-3).

**Why a frame of its own.** The scaffold sampled every region next to a detected dimension line, and
on the first real set about 9 of its 59 crops held a dimension (#728): hatching, outlet symbols and
single strokes sit next to lines too. A key mostly of symbols measures nothing about reading. So the
frame here is built to be mostly labels, from four places a label is likely to be:

- **printed text inside the pasted drawings** — the runs the file's own text reader finds, the ones
  it sets aside unread (a stacked fraction, millimetres over inches, a piece of a longer label)
  included, and the rest only where they are shaped like a label;
- **path regions shaped like a label** — a closed label, two to twelve glyphs of one height, and one
  dimension line clearly nearest it (`label_shaped`);
- **the vendor's label at each place GV's reviewer corrected** — the one place under a note box, or
  under a coloured correction inside the drawing, nearest its middle: #850's sites;
- **regions a run agreed on** — where two readers, or a label's own two units, gave one value.

**It never selects on a reading.** No place holds a value or a text: a `Place` is where a label is
and what the file's geometry says about it. What the vendor's drawing says, what any reader read
and the value a run agreed on never decide whether a place is kept, which kind it is, or whether it
is drawn; where a run agreed, and where GV corrected, are places like any other. The script that
builds places (`scripts/author_reading_answer_key.py`) hands over geometry only — GV's own text is
read there for one thing, to find where GV corrected a dimension — and its tests change what the
drawing says without changing where, and get the same sample.

**Kinds come from geometry only**, and each drawing's sample is drawn kind by kind, to a quota the
caller states (`sample`): a key made only of the easy labels measures nothing that matters.

**Then two checks a person runs.** `triage` counts, per drawing, the crops a person marked as not a
dimension, so a sample that is mostly symbols is caught before anyone reads a hundred crops; and
`look_again` lists the crops where what was typed differs from the drawing's own text or from GV's,
**by crop and never by value**, so the typist looks again without being told the answer.

Pure: no file, no database, no model. Every threshold is the caller's, and none has a default.

Source: issue #867 and the #728 plan. Verification: `tests/eval/test_key_frame.py`.
"""

from __future__ import annotations

import random
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from enum import StrEnum
from fractions import Fraction

__all__ = [
    "Box",
    "Differs",
    "Drawn",
    "Frame",
    "Geometry",
    "Glyph",
    "Kind",
    "LabelShape",
    "Layout",
    "LookAgain",
    "Place",
    "Sample",
    "Site",
    "Source",
    "Triage",
    "Typed",
    "Witnessed",
    "frame",
    "kind_of",
    "label_shaped",
    "look_again",
    "sample",
    "triage",
    "two_rows",
]

type Box = tuple[int, int, int, int]
"""left, top, right, bottom, in a page's pixels at the key's frame resolution."""

type Glyph = tuple[Decimal, Decimal, Decimal, Decimal]
"""One glyph of a label in the label's own reading frame, in PDF points: where it starts and ends
along the line it reads on, and where its bottom and top are across that line. For an upright label
that is its left, right, bottom and top; for a sideways one the page's axes are swapped."""


class Source(StrEnum):
    """Where in the file a place was found. A place may be found from more than one."""

    PRINTED_TEXT = "printed text"
    PATH_LABEL = "path label"
    GV_SITE = "GV site"
    AGREED = "agreed by a run"


class Layout(StrEnum):
    """A label layout the file's own text reader found, which it sets aside or marks (#738).

    The reader finds these from where a label's digits sit; the layout is used here, never the text.
    """

    STACKED_FRACTION = "stacked fraction"
    TWO_LINES = "millimetres over inches"
    FRAGMENT = "piece of a longer label"
    MISSING_SPACE = "inches with a space left out"


class Kind(StrEnum):
    """What a place is, from geometry only. **In the order a place is given its kind**: a stacked
    fraction set sideways is a stacked fraction, the kind two readers from different vendors were
    measured agreeing on wrongly (#726).
    """

    STACKED = "stacked"
    DUAL = "dual"
    SIDEWAYS = "sideways"
    CUT = "cut"
    """The crop production cuts round the place cuts its label off."""
    GV_MARK = "gv_mark"
    """GV's markup, drawn in colour inside the vendor's drawing, shows in the crop."""
    SMALL = "small"
    PLAIN = "plain"


@dataclass(frozen=True, slots=True)
class LabelShape:
    """What makes a place shaped like a label. None has a default."""

    minimum_glyphs: int
    maximum_glyphs: int
    height_ratio: Decimal
    """How many times taller than another a glyph may be and the two still be of one height."""

    def __post_init__(self) -> None:
        for name in ("minimum_glyphs", "maximum_glyphs"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive whole number")
        if self.maximum_glyphs < self.minimum_glyphs:
            raise ValueError("maximum_glyphs must not be below minimum_glyphs")
        if not isinstance(self.height_ratio, Decimal) or not self.height_ratio.is_finite():
            raise TypeError("height_ratio must be a finite Decimal, never a float")
        if self.height_ratio < 1:
            raise ValueError("height_ratio must be at least 1")


@dataclass(frozen=True, slots=True)
class Geometry:
    """What the file's own geometry says about one place. No value and no text is in it.

    `glyphs` are the label's glyphs as the file draws them — its path objects, or the boxes of its
    printed characters — in the label the reading agent's rule gathers there.
    """

    glyphs: tuple[Glyph, ...]
    closed: bool
    """Whether the label's end is settled: it holds no more than one label's length, nothing more
    joins it, and no character just past its end shares its line
    (`extraction.glyph_reader.gather_label`)."""
    one_nearest_line: bool
    """Whether one dimension line is clearly the nearest, by the stage's own association rule."""
    sideways: bool
    stacked: bool
    """Whether the crop shows a stacked fraction the bar detector found, or the file's text reader
    laid the label out as one."""
    cut: bool
    gv_mark: bool
    layout: Layout | None = None


@dataclass(frozen=True, slots=True)
class Place:
    """One place in the frame: a page, a box, and the geometry there. No value and no text."""

    page_index: int
    box: Box
    sources: frozenset[Source]
    geometry: Geometry

    def __post_init__(self) -> None:
        left, top, right, bottom = self.box
        if right <= left or bottom <= top:
            raise ValueError(f"a place on page {self.page_index + 1} has a box with no area")
        if not self.sources:
            raise ValueError("a place must say where in the file it was found")

    @property
    def long_axis_px(self) -> int:
        return max(self.box[2] - self.box[0], self.box[3] - self.box[1])


@dataclass(frozen=True, slots=True)
class Site:
    """Where GV's reviewer corrected a dimension: a note box, or a coloured correction (#850)."""

    page_index: int
    box: Box


def _height(glyph: Glyph) -> Decimal:
    return glyph[3] - glyph[2]


def _character_height(glyphs: Sequence[Glyph], shape: LabelShape) -> list[Glyph]:
    """The glyphs of one height with the tallest: the label's characters. Smaller marks — an inch
    mark's ticks, a fraction bar, a dot — are not counted as characters and do not disqualify."""
    if not glyphs:
        return []
    tallest = max(_height(glyph) for glyph in glyphs)
    return [glyph for glyph in glyphs if _height(glyph) * shape.height_ratio >= tallest]


def label_shaped(geometry: Geometry, shape: LabelShape) -> bool:
    """Whether a place is shaped like a label: closed, `minimum_glyphs` to `maximum_glyphs` glyphs
    with at least `minimum_glyphs` of them of one height, and one dimension line clearly nearest.

    A lone mark fails the count and a cluster sitting between several lines fails the last part.
    It is a test of shape, not of meaning: line-work drawn like a row of characters passes it, and
    that is what the triage counts.
    """
    count = len(geometry.glyphs)
    return (
        geometry.closed
        and geometry.one_nearest_line
        and shape.minimum_glyphs <= count <= shape.maximum_glyphs
        and len(_character_height(geometry.glyphs, shape)) >= shape.minimum_glyphs
    )


def two_rows(glyphs: Sequence[Glyph], shape: LabelShape) -> bool:
    """Whether a label's characters sit in exactly two rows, one over the other, each holding at
    least `minimum_glyphs`: millimetres over their bracketed inches, as both client sets draw them.

    Two characters are in one row when their extents across the line overlap. A stacked `3/4"` is
    two rows of one character each, so it is not this; it is a stacked fraction.
    """
    characters = sorted(_character_height(glyphs, shape), key=lambda glyph: glyph[2])
    rows: list[list[Glyph]] = []
    top = Decimal(0)
    for glyph in characters:
        if rows and glyph[2] <= top:
            rows[-1].append(glyph)
            top = max(top, glyph[3])
        else:
            rows.append([glyph])
            top = glyph[3]
    return len(rows) == 2 and all(len(row) >= shape.minimum_glyphs for row in rows)


def kind_of(place: Place, *, shape: LabelShape, small_px: int) -> Kind:
    """The place's kind: the first, in `Kind`'s order, its geometry shows.

    `small_px` is the longest side, in the frame's pixels, below which a label is small.
    """
    if isinstance(small_px, bool) or not isinstance(small_px, int) or small_px < 1:
        raise ValueError("small_px must be a positive whole number of pixels")
    geometry = place.geometry
    if geometry.stacked or geometry.layout is Layout.STACKED_FRACTION:
        return Kind.STACKED
    if geometry.layout is Layout.TWO_LINES or two_rows(geometry.glyphs, shape):
        return Kind.DUAL
    if geometry.sideways:
        return Kind.SIDEWAYS
    if geometry.cut:
        return Kind.CUT
    if geometry.gv_mark:
        return Kind.GV_MARK
    if place.long_axis_px < small_px:
        return Kind.SMALL
    return Kind.PLAIN


def _overlaps(first: Box, second: Box) -> bool:
    return (
        first[0] <= second[2]
        and second[0] <= first[2]
        and first[1] <= second[3]
        and second[1] <= first[3]
    )


def _same_label(first: Geometry, second: Geometry) -> bool:
    """Whether two places gathered one label: the same glyphs, at least one."""
    return bool(first.glyphs) and sorted(first.glyphs) == sorted(second.glyphs)


def _centre_distance(first: Box, second: Box) -> int:
    """The squared distance between two boxes' centres, in doubled pixels: exact, and only compared."""
    x = (first[0] + first[2]) - (second[0] + second[2])
    y = (first[1] + first[3]) - (second[1] + second[3])
    return x * x + y * y


def _centre_inside(inner: Box, outer: Box) -> bool:
    x2 = inner[0] + inner[2]
    y2 = inner[1] + inner[3]
    return 2 * outer[0] <= x2 <= 2 * outer[2] and 2 * outer[1] <= y2 <= 2 * outer[3]


#: Which place's box and geometry stand for a group of places found at one spot: the file's own text
#: first, whose box is exactly the run, then a path label, then a run's region.
_REPRESENTATIVE = (Source.PRINTED_TEXT, Source.PATH_LABEL, Source.AGREED, Source.GV_SITE)


def _rank(place: Place) -> tuple[int, int, Box]:
    first = min(_REPRESENTATIVE.index(source) for source in place.sources)
    return (first, place.page_index, place.box)


def _merged(places: Sequence[Place]) -> list[Place]:
    """One place per spot. Two places are one spot when either's centre lies in the other's box, or
    when both gathered the same label — two planned regions over the two halves of one `24 3/4"`
    lie side by side, and each gathers the whole of it. The grouping is followed through, so a
    label found three ways is drawn at most once."""
    parent = list(range(len(places)))

    def root(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    by_page: dict[int, list[int]] = {}
    for index, place in enumerate(places):
        by_page.setdefault(place.page_index, []).append(index)
    for indexes in by_page.values():
        for position, first in enumerate(indexes):
            for second in indexes[position + 1 :]:
                one, other = places[first].box, places[second].box
                if (
                    _centre_inside(one, other)
                    or _centre_inside(other, one)
                    or _same_label(places[first].geometry, places[second].geometry)
                ):
                    parent[root(second)] = root(first)
    groups: dict[int, list[Place]] = {}
    for index, place in enumerate(places):
        groups.setdefault(root(index), []).append(place)
    merged = []
    for members in groups.values():
        chosen = min(members, key=_rank)
        sources = frozenset(source for member in members for source in member.sources)
        merged.append(replace(chosen, sources=sources))
    return sorted(merged, key=lambda place: (place.page_index, place.box))


@dataclass(frozen=True, slots=True)
class Frame:
    """The places kept, and how many of what was found were not."""

    places: tuple[Place, ...]
    found: int
    """Places handed in, before any was dropped or merged."""
    not_label_shaped: int
    """Places dropped for not being shaped like a label, with nothing else vouching for them."""
    sites_without_a_place: int
    """GV's sites with no place within reach of them."""


def frame(
    places: Sequence[Place], sites: Sequence[Site], *, shape: LabelShape, site_reach_px: int
) -> Frame:
    """The frame: every place found that is shaped like a label, or vouched for, one per spot.

    **Printed text takes the shape test too.** Most of what a drawing prints is not a dimension —
    notes, titles, tags, the title block — and a run of letters is shaped like a run of digits, so
    printed text is held to the test a path region is.

    **What is kept without the shape test.** A place the text reader laid out as a stacked fraction,
    two lines or a piece of a longer label is one it already found to be a label; and the vendor's
    label at a GV site, or a place where a run's readings agreed, is one a person or a run pointed
    at. **The vendor's label at a site is one place**: of the places within `site_reach_px` of the
    site's box, the one whose centre is nearest the site's, because GV writes over the label it
    corrects and the line-work round it lies off to the side.
    """
    if isinstance(site_reach_px, bool) or not isinstance(site_reach_px, int) or site_reach_px < 0:
        raise ValueError("site_reach_px must be a whole number of pixels, zero or more")
    at_a_site: set[int] = set()
    unmatched = 0
    for site in sites:
        reach = (
            site.box[0] - site_reach_px,
            site.box[1] - site_reach_px,
            site.box[2] + site_reach_px,
            site.box[3] + site_reach_px,
        )
        near = [
            number
            for number, place in enumerate(places)
            if place.page_index == site.page_index and _overlaps(place.box, reach)
        ]
        if not near:
            unmatched += 1
            continue
        at_a_site.add(
            min(near, key=lambda number: (_centre_distance(places[number].box, site.box), number))
        )
    marked = [
        replace(place, sources=place.sources | {Source.GV_SITE}) if number in at_a_site else place
        for number, place in enumerate(places)
    ]
    kept: list[Place] = []
    dropped = 0
    for place in marked:
        vouched = place.sources & {Source.GV_SITE, Source.AGREED}
        if vouched or place.geometry.layout is not None or label_shaped(place.geometry, shape):
            kept.append(place)
        else:
            dropped += 1
    return Frame(
        places=tuple(_merged(kept)),
        found=len(places),
        not_label_shaped=dropped,
        sites_without_a_place=unmatched,
    )


@dataclass(frozen=True, slots=True)
class Drawn:
    """One place the sample drew, and the kind it was drawn as."""

    place: Place
    kind: Kind


@dataclass(frozen=True, slots=True)
class Sample:
    """What was drawn, and how many of each kind the frame held."""

    drawn: tuple[Drawn, ...]
    available: Mapping[Kind, int]

    @property
    def counts(self) -> Counter[Kind]:
        return Counter(entry.kind for entry in self.drawn)

    def short(self, quotas: Mapping[Kind, int]) -> dict[Kind, int]:
        """The kinds the frame held fewer of than their quota, and by how many."""
        counts = self.counts
        return {kind: quotas[kind] - counts[kind] for kind in Kind if counts[kind] < quotas[kind]}


def sample(
    places: Sequence[Place],
    *,
    quotas: Mapping[Kind, int],
    shape: LabelShape,
    small_px: int,
    seed: int,
) -> Sample:
    """Draw each kind's quota from the places of that kind, at random, seeded.

    **A kind the frame holds too few of is drawn short, never topped up from another**, so a quota
    says what it says and the shortfall is reported. Every kind needs a stated quota; zero is a
    quota. Seeded, so the same frame gives the same sample: a person half way through reading must
    not have the crops renumbered underneath them.
    """
    missing = [kind.value for kind in Kind if kind not in quotas]
    if missing:
        raise ValueError(f"every kind needs a stated quota, and {missing} have none")
    for kind, quota in quotas.items():
        if isinstance(quota, bool) or not isinstance(quota, int) or quota < 0:
            raise ValueError(f"the quota for {kind.value} must be a whole number, zero or more")
    pools: dict[Kind, list[Place]] = {kind: [] for kind in Kind}
    for place in sorted(places, key=lambda place: (place.page_index, place.box)):
        pools[kind_of(place, shape=shape, small_px=small_px)].append(place)
    rng = random.Random(seed)
    drawn: list[Drawn] = []
    for kind in Kind:
        pool = list(pools[kind])
        rng.shuffle(pool)
        drawn.extend(Drawn(place, kind) for place in pool[: quotas[kind]])
    drawn.sort(key=lambda entry: (entry.place.page_index, entry.place.box))
    return Sample(drawn=tuple(drawn), available={kind: len(pools[kind]) for kind in Kind})


# ---------------------------------------------------------------------------
# The person's two checks
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Triage:
    """One drawing's crops, and how many a person marked as not a dimension."""

    drawing: str
    crops: int
    not_a_dimension: int

    @property
    def dimensions(self) -> int:
        return self.crops - self.not_a_dimension

    @property
    def share(self) -> Fraction | None:
        """The share of the crops that are dimensions; `None` for a drawing with no crops."""
        return None if self.crops == 0 else Fraction(self.dimensions, self.crops)

    def meets(self, minimum: Fraction) -> bool:
        """Whether at least `minimum` of the crops are dimensions. A drawing with no crops does not."""
        share = self.share
        return share is not None and share >= minimum


def triage(drawing: str, marks: Iterable[bool]) -> Triage:
    """Count one drawing's crops from each crop's mark: `True` where it is not a dimension."""
    listed = list(marks)
    return Triage(drawing=drawing, crops=len(listed), not_a_dimension=sum(listed))


@dataclass(frozen=True, slots=True)
class Typed:
    """What the typist entered for one crop, in inches: the vendor's number and GV's, if any."""

    crop_id: str
    vendor_value: Fraction | None
    gv_value_seen: Fraction | None


@dataclass(frozen=True, slots=True)
class Witnessed:
    """What the file itself says inside one crop, in inches: the drawing's own printed numbers, in
    black, and GV's, in colour. Only numbers the parser reads as one value are here."""

    crop_id: str
    printed: frozenset[Fraction] = field(default_factory=frozenset)
    gv: frozenset[Fraction] = field(default_factory=frozenset)


class Differs(StrEnum):
    FILE_TEXT = "the drawing's own printed text"
    GV_TEXT = "GV's text"


@dataclass(frozen=True, slots=True)
class LookAgain:
    """A crop to look at again, and which of the file's texts differs from what was typed. **No
    value is carried**, the typed one or the file's: the list must not tell the typist the answer.
    """

    crop_id: str
    differs: Differs


def look_again(typed: Sequence[Typed], witnessed: Mapping[str, Witnessed]) -> tuple[LookAgain, ...]:
    """The crops where what was typed differs from what the file says inside the crop.

    - **The drawing's own text**: it prints at least one number in the crop, a vendor's number was
      typed, and it is none of them.
    - **GV's text**: GV's coloured text holds at least one number in the crop, and the GV number
      typed is none of them, or none was typed; or the vendor's number typed is one of GV's and none
      of the drawing's, which is GV's number taken for the vendor's.

    Where the file says nothing, nothing is compared: a crop with no printed text is a crop drawn
    in paths, and its typed value is the only one there is.
    """
    listed: list[LookAgain] = []
    for entry in typed:
        seen = witnessed.get(entry.crop_id, Witnessed(entry.crop_id))
        vendor = entry.vendor_value
        if vendor is not None and seen.printed and vendor not in seen.printed:
            listed.append(LookAgain(entry.crop_id, Differs.FILE_TEXT))
        gv_missed = bool(seen.gv) and entry.gv_value_seen not in seen.gv
        gv_taken = vendor is not None and vendor in seen.gv and vendor not in seen.printed
        if gv_missed or gv_taken:
            listed.append(LookAgain(entry.crop_id, Differs.GV_TEXT))
    return tuple(listed)
