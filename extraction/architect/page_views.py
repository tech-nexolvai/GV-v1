"""Find the architect's drawing views printed as normal page content, view by view (#1163).

An architect issues elevations as ordinary page content: each drawing sits above its own **label
block** — a view bubble (a circle holding the view number and the sheet reference), the view title
(`SAMPLE ROOM ELEVATION`) and the scale note under it (`1/2" = 1'-0"`). Nothing is pasted, so the
combined-sheet reader, which reads only pasted drawings, finds nothing on such a page. This module
finds the views from what is printed, by code:

1. **Anchors.** Every scale note on the page (`text.find_printed`) with a title printed directly
   above it (an upright phrase with letters, its bottom within `title_gap_em` of the note's top and
   overlapping it across) is one view's label block. The bubble, when there is one, is the round
   ink just left of the title and scale (near-square, small, holding text); what it prints is kept
   for the reviewer. A scale note with no title above it is not a view, and nor is a title over a
   scale note with no drawing standing above it (`minimum_drawing_strokes`): a title block's
   "DRAWING TITLE" over its "SCALE" field.
2. **The drawing's extent.** The page's black and grey ink — every stroke and character — is joined
   into clusters (two pieces within `join_pt` are one cluster). A cluster belongs to the nearest
   row of label blocks it stands above (or level with, reaching at most `below_reach_em` below
   them), and, in that row, to the one label block whose column it lies in (columns split halfway
   between neighbouring label blocks). A cluster that encloses a label block with room on all four
   sides is the sheet's border, never a drawing; one below every label block (the title block)
   belongs to no view. Of the clusters a view owns, only those joined to its drawing — each within
   `side_reach_em` across of what is joined already — are its extent: notes and legends standing
   apart beside it are not.
3. **Clearly separated or not at all.** A cluster that crosses a column boundary, runs into another
   view's label block or stands right above another view's title (nearer to it than to its own),
   two label blocks overlapping across one row, or two extents that overlap: the views concerned
   are reported **not separated**, with the reason, and the caller reads none of their values.
4. **Never silent.** `survey_page_views` also returns a note for everything left out of every view
   — a border holding more than a border's strokes (a drawing joined to it), ink running far below
   the titles, ink standing apart beside a view, a title with no drawing — and why no view was
   found on a page with none.

**What it never does.** It reads no value (the reader does that inside each extent), never reads
coloured ink (the caller passes black and grey only), and never guesses which view an ambiguous
piece of drawing belongs to.

Pure: plain values in, plain values out, everything in page points with `top` downward and the
page's origin at (0, 0). Source: issue #1163 · Verification:
`tests/extraction/architect/test_architect_sheet.py`
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from itertools import pairwise
from typing import Final, Self

from extraction.architect.labels import DIMENSION_PATTERN
from extraction.architect.text import (
    Orientation,
    PrintedPhrase,
    ScaleNote,
    TextChar,
    TextSettings,
    find_phrases,
    find_printed,
)
from extraction.geometry.rows import Box, PageInk
from units.normalise import plain_marks

__all__ = [
    "INK_OUTSIDE_VIEWS",
    "NO_VIEW_FOUND",
    "TITLE_NOT_VIEW",
    "PageNote",
    "PageView",
    "PageViewSettings",
    "PageViewSurvey",
    "find_page_views",
    "survey_page_views",
]

_TWO: Final = Decimal(2)
_ZERO: Final = Decimal(0)


@dataclass(frozen=True, slots=True)
class PageViewSettings:
    """Every length the view finder uses. No defaults: `reader.MEASURED_ARCHITECT_SETTINGS` names
    the one set used."""

    title_gap_em: Decimal
    """A title's bottom is at most this many scale-note heights above the note's top."""
    title_reach_em: Decimal
    """...and overlaps the note across, the note widened by this many of its heights each side."""
    title_minimum_letters: int
    """A title prints at least this many letters."""
    bubble_reach_em: Decimal
    """A bubble's right edge is at most this many title heights left of the title and scale."""
    bubble_maximum_em: Decimal
    """A bubble is at most this many title heights across."""
    bubble_squareness: Decimal
    """A bubble's width and height differ by at most this fraction of the larger."""
    join_pt: Decimal
    """Two pieces of ink this close (page points) are one cluster."""
    label_slack_pt: Decimal
    """A cluster stands above (or beside) a row of label blocks when its top is no more than this
    far below their bottom..."""
    below_reach_em: Decimal
    """...and its bottom no more than this many scale-note heights below it: a wall line drawn a
    little past the title is the drawing's, a title block running down the sheet is not."""
    frame_margin_em: Decimal
    """A cluster reaching this many scale-note heights past a label block on all four sides is the
    sheet's border, not a drawing."""
    border_strokes: int
    """A border holding more strokes than this is reported: a drawing may be joined to it (a
    border and its title block's rules are a handful of strokes; a drawing is hundreds)."""
    side_reach_em: Decimal
    """A cluster joins a view only within this many scale-note heights across of the view's
    drawing (or label block): notes and legends standing apart beside it are left out."""
    minimum_drawing_strokes: int
    """A title over a scale note is a view only with at least this many strokes of drawing standing
    above it: a title block's or a note's title has none."""


@dataclass(frozen=True, slots=True)
class PageView:
    """One drawing view printed on the page: its label block, its extent, and whether it stands
    clearly apart from every other view."""

    number: int
    """Page-local and stable: label blocks numbered from 1, top to bottom, then left to right."""
    title: str
    bubble: str | None
    """What the view bubble prints (view number and sheet reference), or `None` without one."""
    scale: ScaleNote
    label_box: Box
    extent: Box
    separated: bool
    reason: str


@dataclass(frozen=True, slots=True)
class _Anchor:
    title: str
    title_box: Box
    title_height: Decimal
    bubble: str | None
    bubble_box: Box | None
    scale: ScaleNote

    @property
    def label(self) -> Box:
        box = self.title_box.union(self.scale.box)
        return box if self.bubble_box is None else box.union(self.bubble_box)


def _letters(text: str) -> int:
    return sum(1 for character in text if character.isalpha())


def _within(inner: Box, outer: Box) -> bool:
    return (
        outer.x0 <= inner.x0
        and inner.x1 <= outer.x1
        and outer.top <= inner.top
        and inner.bottom <= outer.bottom
    )


def _intersects(first: Box, second: Box) -> bool:
    return (
        first.x0 < second.x1
        and second.x0 < first.x1
        and first.top < second.bottom
        and second.top < first.bottom
    )


def _title_candidates(
    phrases: Sequence[PrintedPhrase], scales: Sequence[ScaleNote], settings: PageViewSettings
) -> list[PrintedPhrase]:
    return [
        phrase
        for phrase in phrases
        if phrase.orientation is Orientation.UPRIGHT
        and _letters(phrase.text) >= settings.title_minimum_letters
        and not DIMENSION_PATTERN.search(plain_marks(phrase.text))
        and not any(_intersects(phrase.box, scale.box) for scale in scales)
    ]


def _title_line(first: PrintedPhrase, candidates: Sequence[PrintedPhrase]) -> tuple[str, Box]:
    """The whole title line `first` is part of: phrases on its baseline, of its size, each within
    two of its heights of the next."""
    line = [
        phrase
        for phrase in candidates
        if abs(phrase.box.bottom - first.box.bottom) <= first.height * Decimal("0.3")
        and abs(phrase.height - first.height) <= first.height * Decimal("0.2")
    ]
    line.sort(key=lambda phrase: phrase.box.x0)
    start = line.index(first)
    chosen = [first]
    for phrase in line[start + 1 :]:
        if phrase.box.x0 - chosen[-1].box.x1 > first.height * _TWO:
            break
        chosen.append(phrase)
    for phrase in reversed(line[:start]):
        if chosen[0].box.x0 - phrase.box.x1 > first.height * _TWO:
            break
        chosen.insert(0, phrase)
    box = chosen[0].box
    for phrase in chosen[1:]:
        box = box.union(phrase.box)
    return " ".join(phrase.text for phrase in chosen), box


def _segment_boxes(points: Sequence[tuple[Decimal, Decimal]], closed: bool) -> Iterable[Box]:
    pairs = list(pairwise(points))
    if closed and len(points) > 2:
        pairs.append((points[-1], points[0]))
    for (x0, y0), (x1, y1) in pairs:
        yield Box(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
    if len(points) == 1:
        ((x, y),) = points
        yield Box(x, y, x, y)


@dataclass(frozen=True, slots=True)
class _Cluster:
    """Ink joined into one piece: its box and how many strokes and characters it holds."""

    box: Box
    strokes: int
    chars: int


def _pieces(ink: PageInk) -> list[tuple[Box, bool]]:
    """Every stroke segment and character of the page's ink, as boxes, each marked whether it is a
    character. A rectangle or a long curve is its segments, so a border's box never swallows what
    it surrounds."""
    pieces: list[tuple[Box, bool]] = [
        (
            Box(
                min(line.x0, line.x1),
                min(line.y0, line.y1),
                max(line.x0, line.x1),
                max(line.y0, line.y1),
            ),
            False,
        )
        for line in ink.lines
    ]
    for curve in ink.curves:
        pieces.extend((box, False) for box in _segment_boxes(curve.points, curve.closed))
    pieces.extend((character.box, True) for character in ink.characters)
    return pieces


def _joined(boxes: Sequence[Box], join: Decimal) -> list[list[int]]:
    """The boxes joined into groups (indices): two within `join` of each other are one."""
    parent = list(range(len(boxes)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    cell = Decimal(24)
    grid: dict[tuple[int, int], list[int]] = defaultdict(list)
    half = join / _TWO
    grown = [Box(b.x0 - half, b.top - half, b.x1 + half, b.bottom + half) for b in boxes]
    for index, box in enumerate(grown):
        for column in range(int(box.x0 // cell), int(box.x1 // cell) + 1):
            for row in range(int(box.top // cell), int(box.bottom // cell) + 1):
                grid[(column, row)].append(index)
    for members in grid.values():
        for position, first in enumerate(members):
            a = grown[first]
            for second in members[position + 1 :]:
                b = grown[second]
                if a.x0 <= b.x1 and b.x0 <= a.x1 and a.top <= b.bottom and b.top <= a.bottom:
                    root_a, root_b = find(first), find(second)
                    if root_a != root_b:
                        parent[root_b] = root_a
    groups: dict[int, list[int]] = {}
    for index in range(len(boxes)):
        groups.setdefault(find(index), []).append(index)
    return list(groups.values())


def _union(boxes: Iterable[Box]) -> Box:
    result: Box | None = None
    for box in boxes:
        result = box if result is None else result.union(box)
    assert result is not None
    return result


def _clusters(boxes: Sequence[Box], join: Decimal) -> list[Box]:
    """The boxes joined into clusters: two within `join` of each other are one."""
    return [_union(boxes[index] for index in group) for group in _joined(boxes, join)]


def _ink_clusters(ink: PageInk, join: Decimal) -> list[_Cluster]:
    pieces = _pieces(ink)
    boxes = [box for box, _char in pieces]
    found: list[_Cluster] = []
    for group in _joined(boxes, join):
        chars = sum(1 for index in group if pieces[index][1])
        found.append(_Cluster(_union(boxes[index] for index in group), len(group) - chars, chars))
    return found


def _bubble(
    ink: PageInk,
    phrases: Sequence[PrintedPhrase],
    label: Box,
    title_height: Decimal,
    settings: PageViewSettings,
) -> tuple[str | None, Box | None]:
    """The round ink just left of the title and scale, and what it prints, or `(None, None)`."""
    biggest = settings.bubble_maximum_em * title_height
    small = [
        curve.box
        for curve in ink.curves
        if curve.box.width <= biggest and curve.box.height <= biggest
    ]
    best: tuple[Decimal, Box] | None = None
    for group in _clusters(small, settings.join_pt):
        side = max(group.width, group.height)
        if side <= 0 or side > biggest:
            continue
        if abs(group.width - group.height) > settings.bubble_squareness * side:
            continue
        gap = label.x0 - group.x1
        if gap < -title_height / _TWO or gap > settings.bubble_reach_em * title_height:
            continue
        if group.bottom < label.top or group.top > label.bottom:
            continue
        inside = [
            phrase
            for phrase in phrases
            if _within(
                Box(
                    phrase.box.centre_x,
                    phrase.box.centre_y,
                    phrase.box.centre_x,
                    phrase.box.centre_y,
                ),
                group,
            )
        ]
        if not inside:
            continue
        if best is None or gap < best[0]:
            best = (gap, group)
    if best is None:
        return None, None
    box = best[1]
    printed = sorted(
        (
            phrase
            for phrase in phrases
            if box.x0 <= phrase.box.centre_x <= box.x1
            and box.top <= phrase.box.centre_y <= box.bottom
        ),
        key=lambda phrase: (phrase.box.top, phrase.box.x0),
    )
    return " ".join(phrase.text for phrase in printed), box


def _anchors(
    chars: Sequence[TextChar], ink: PageInk, text: TextSettings, settings: PageViewSettings
) -> list[_Anchor]:
    scales = list(find_printed(chars, text).scales)
    phrases = find_phrases(chars, text)
    candidates = _title_candidates(phrases, scales, settings)
    anchors: list[_Anchor] = []
    for scale in scales:
        height = scale.box.height
        reach = settings.title_reach_em * height
        above = [
            phrase
            for phrase in candidates
            if phrase.box.bottom <= scale.box.top + height / 4
            and scale.box.top - phrase.box.bottom <= settings.title_gap_em * height
            and phrase.box.x0 <= scale.box.x1 + reach
            and phrase.box.x1 >= scale.box.x0 - reach
        ]
        if not above:
            continue
        nearest = min(above, key=lambda phrase: (scale.box.top - phrase.box.bottom, phrase.box.x0))
        title, title_box = _title_line(nearest, candidates)
        bubble, bubble_box = _bubble(
            ink, phrases, title_box.union(scale.box), nearest.height, settings
        )
        anchors.append(_Anchor(title, title_box, nearest.height, bubble, bubble_box, scale))
    # Two scale notes under one title (a note repeated) are one view: keep the first.
    kept: list[_Anchor] = []
    for anchor in sorted(anchors, key=lambda a: (a.label.top, a.label.x0)):
        if any(_intersects(anchor.label, other.label) for other in kept):
            continue
        kept.append(anchor)
    return kept


def _bands(anchors: Sequence[_Anchor]) -> list[list[int]]:
    """Anchors grouped into rows of label blocks that overlap top to bottom."""
    order = sorted(range(len(anchors)), key=lambda index: anchors[index].label.top)
    bands: list[list[int]] = []
    bottom = None
    for index in order:
        label = anchors[index].label
        if bands and bottom is not None and label.top <= bottom:
            bands[-1].append(index)
            bottom = max(bottom, label.bottom)
        else:
            bands.append([index])
            bottom = label.bottom
    return [sorted(band, key=lambda index: anchors[index].label.x0) for band in bands]


class PageNote(str):
    """A note about a page, in plain words, with its kind (#1163): one of `NO_VIEW_FOUND`,
    `TITLE_NOT_VIEW`, `INK_OUTSIDE_VIEWS` here, and the reader's own kinds. It is the text itself
    (a `str`), so it reads and compares as one; `kind` says what sort of note it is, for storing."""

    kind: str

    def __new__(cls, text: str, kind: str) -> Self:
        note = super().__new__(cls, text)
        note.kind = kind
        return note


#: The kinds of note this module writes (`app.models.evidence.ARCHITECT_PAGE_NOTE_KINDS`).
NO_VIEW_FOUND = "no_view_found"
TITLE_NOT_VIEW = "title_not_view"
INK_OUTSIDE_VIEWS = "ink_outside_views"


@dataclass(frozen=True, slots=True)
class PageViewSurvey:
    """The views found on a page, and what was left out of every view and why (never silent)."""

    views: tuple[PageView, ...]
    notes: tuple[PageNote, ...]


@dataclass
class _Ownership:
    owned: dict[int, list[_Cluster]]
    problems: dict[int, list[str]]
    touched_strokes: dict[int, int]
    border: list[_Cluster]
    far_below: list[_Cluster]


def _boundaries(
    found: Sequence[_Anchor], bands: Sequence[Sequence[int]], problems: dict[int, list[str]]
) -> list[list[Decimal]]:
    """Where each row of label blocks splits into columns: halfway between neighbouring blocks."""
    cuts: list[list[Decimal]] = []
    for band in bands:
        edges: list[Decimal] = []
        for left, right in pairwise(band):
            first, second = found[left].label, found[right].label
            if first.x1 >= second.x0:
                for index in (left, right):
                    problems[index].append("its title overlaps another view's title across")
                edges.append(first.x1)
            else:
                edges.append((first.x1 + second.x0) / _TWO)
        cuts.append(edges)
    return cuts


def _is_border(cluster: _Cluster, found: Sequence[_Anchor], settings: PageViewSettings) -> bool:
    return any(
        _within(
            Box(
                anchor.label.x0 - settings.frame_margin_em * anchor.scale.box.height,
                anchor.label.top - settings.frame_margin_em * anchor.scale.box.height,
                anchor.label.x1 + settings.frame_margin_em * anchor.scale.box.height,
                anchor.label.bottom + settings.frame_margin_em * anchor.scale.box.height,
            ),
            cluster.box,
        )
        for anchor in found
    )


def _own(
    found: Sequence[_Anchor], clusters: Sequence[_Cluster], settings: PageViewSettings
) -> _Ownership:
    """Give each cluster of ink to the one view whose label block it stands above, or to none."""
    bands = _bands(found)
    problems: dict[int, list[str]] = defaultdict(list)
    cuts_by_band = _boundaries(found, bands, problems)
    result = _Ownership(
        owned={index: [] for index in range(len(found))},
        problems=problems,
        touched_strokes=defaultdict(int),
        border=[],
        far_below=[],
    )
    for cluster in clusters:
        box = cluster.box
        if _is_border(cluster, found, settings):
            result.border.append(cluster)  # the sheet's border, round a label block
            continue
        below = next(
            (
                position
                for position, row in enumerate(bands)
                if box.top
                <= max(found[index].label.bottom for index in row) + settings.label_slack_pt
            ),
            None,
        )
        if below is None:
            continue  # below every label block: the title block, not a view
        row_of_labels = bands[below]
        lowest = max(found[index].label.bottom for index in row_of_labels)
        reach = settings.below_reach_em * max(
            found[index].scale.box.height for index in row_of_labels
        )
        if box.bottom > lowest + max(settings.label_slack_pt, reach):
            result.far_below.append(cluster)  # runs far down past the titles
            continue
        cuts = cuts_by_band[below]
        columns = [
            index
            for position, index in enumerate(row_of_labels)
            if (position == 0 or box.x1 > cuts[position - 1])
            and (position == len(row_of_labels) - 1 or box.x0 < cuts[position])
        ]
        for index in columns:
            result.touched_strokes[index] += cluster.strokes
        if len(columns) != 1:
            for index in columns:
                problems[index].append(
                    "a piece of drawing runs across the gap between it and the next view"
                )
            continue
        (owner,) = columns
        # Ink standing right above another view's label block — its bottom nearer to that title
        # than to its own view's — is not the owner's to take, whatever column it falls in.
        own_distance = abs(box.bottom - found[owner].label.centre_y)
        under = [
            index
            for index, anchor in enumerate(found)
            if index != owner
            and anchor.label.x0 < box.x1
            and box.x0 < anchor.label.x1
            and anchor.label.top - box.bottom >= -settings.label_slack_pt
            and anchor.label.top - box.bottom < own_distance
        ]
        if under:
            for each in under:
                result.touched_strokes[each] += cluster.strokes
            for each in (owner, *under):
                problems[each].append("a piece of drawing stands above another view's title")
            continue
        for index, anchor in enumerate(found):
            if index != owner and _intersects(box, anchor.label):
                for each in (owner, index):
                    problems[each].append("a piece of its drawing runs into another view's title")
        result.owned[owner].append(cluster)
    return result


def _left_out(
    ownership: _Ownership, beside: Sequence[_Cluster], settings: PageViewSettings
) -> list[PageNote]:
    """A note for each kind of ink left out of every view, with how much (never silent)."""
    notes: list[PageNote] = []
    joined_border = [
        cluster for cluster in ownership.border if cluster.strokes > settings.border_strokes
    ]
    if joined_border:
        notes.append(
            PageNote(
                "left out as the sheet's border, round a view title: "
                + ", ".join(
                    f"{cluster.strokes} strokes and {cluster.chars} characters"
                    for cluster in joined_border
                )
                + " (drawing ink may be joined to the border)",
                INK_OUTSIDE_VIEWS,
            )
        )
    if ownership.far_below:
        notes.append(
            PageNote(
                f"left out: {len(ownership.far_below)} piece(s) of ink running far below the view "
                f"titles, as a title block does ({sum(c.strokes for c in ownership.far_below)} "
                f"strokes, {sum(c.chars for c in ownership.far_below)} characters)",
                INK_OUTSIDE_VIEWS,
            )
        )
    if beside:
        notes.append(
            PageNote(
                f"left out: {len(beside)} piece(s) of ink standing apart beside a view (notes or a "
                f"legend; {sum(cluster.strokes for cluster in beside)} strokes, "
                f"{sum(cluster.chars for cluster in beside)} characters)",
                INK_OUTSIDE_VIEWS,
            )
        )
    return notes


def _reachable(
    label: Box, owned: Sequence[_Cluster], reach: Decimal
) -> tuple[list[_Cluster], list[_Cluster]]:
    """The owned clusters joined to the label block's drawing, each within `reach` across of what is
    joined already, and the ones beside it further away (notes, a legend)."""
    extent = label
    joined: list[_Cluster] = []
    waiting = sorted(owned, key=lambda cluster: -cluster.strokes)
    grew = True
    while grew:
        grew = False
        for cluster in list(waiting):
            gap = max(cluster.box.x0 - extent.x1, extent.x0 - cluster.box.x1, _ZERO)
            if gap <= reach:
                joined.append(cluster)
                extent = extent.union(cluster.box)
                waiting.remove(cluster)
                grew = True
    return joined, waiting


def survey_page_views(
    chars: Sequence[TextChar],
    ink: PageInk,
    *,
    text: TextSettings,
    settings: PageViewSettings,
) -> PageViewSurvey:
    """The drawing views printed on a page, each with its extent and whether it is clearly apart,
    and a note for everything left out of every view.

    `chars` are the page's black and grey characters, `ink` its black and grey ink (strokes and the
    same characters), both with the page's origin at (0, 0).
    """
    notes: list[PageNote] = []
    found = _anchors(chars, ink, text, settings)
    if not found:
        scales = find_printed(chars, text).scales
        notes.append(
            PageNote(
                "no view found: "
                + (
                    "no scale note on this page has a view title printed right above it"
                    if scales
                    else "no scale note is printed on this page"
                ),
                NO_VIEW_FOUND,
            )
        )
        return PageViewSurvey((), tuple(notes))
    clusters = _ink_clusters(ink, settings.join_pt)
    # A title over a scale note with no drawing standing above it is a title block's or a note's,
    # not a view: left out, and said.
    first = _own(found, clusters, settings)
    kept = [
        anchor
        for index, anchor in enumerate(found)
        if first.touched_strokes.get(index, 0) >= settings.minimum_drawing_strokes
    ]
    for index, anchor in enumerate(found):
        if first.touched_strokes.get(index, 0) < settings.minimum_drawing_strokes:
            notes.append(
                PageNote(
                    f"not a view: {anchor.title!r} over the scale note {anchor.scale.text!r} has "
                    "no drawing standing above it (a title block's or a note's text)",
                    TITLE_NOT_VIEW,
                )
            )
    if not kept:
        notes.extend(_left_out(first, [], settings))
        notes.append(
            PageNote(
                "no view found: no view title on this page has a drawing above it", NO_VIEW_FOUND
            )
        )
        return PageViewSurvey((), tuple(notes))
    found = kept
    ownership = _own(found, clusters, settings)
    problems = ownership.problems
    bands = _bands(found)
    numbering = [index for band in bands for index in band]
    number = {index: position + 1 for position, index in enumerate(numbering)}

    extents: dict[int, Box] = {}
    beside: list[_Cluster] = []
    for index, anchor in enumerate(found):
        joined, apart = _reachable(
            anchor.label,
            ownership.owned[index],
            settings.side_reach_em * anchor.scale.box.height,
        )
        beside.extend(apart)
        extents[index] = _union([anchor.label, *(cluster.box for cluster in joined)])

    indices = list(range(len(found)))
    for one in indices:
        for other in indices[one + 1 :]:
            if _intersects(extents[one], extents[other]):
                for index in (one, other):
                    problems[index].append("its drawing overlaps another view's drawing")

    notes.extend(_left_out(ownership, beside, settings))

    views: list[PageView] = []
    for index in numbering:
        anchor = found[index]
        reasons = list(dict.fromkeys(problems.get(index, [])))
        separated = not reasons
        reason = (
            f"view {anchor.title!r} found by its title above the scale note {anchor.scale.text!r}"
            + ("" if anchor.bubble is None else f" and its bubble ({anchor.bubble})")
            + ("" if len(found) == 1 else f"; one of {len(found)} views on the sheet")
            + ("" if separated else "; not clearly separated: " + "; ".join(reasons))
        )
        views.append(
            PageView(
                number=number[index],
                title=anchor.title,
                bubble=anchor.bubble,
                scale=anchor.scale,
                label_box=anchor.label,
                extent=extents[index],
                separated=separated,
                reason=reason,
            )
        )
    return PageViewSurvey(tuple(views), tuple(notes))


def find_page_views(
    chars: Sequence[TextChar],
    ink: PageInk,
    *,
    text: TextSettings,
    settings: PageViewSettings,
) -> tuple[PageView, ...]:
    """The drawing views printed on a page (`survey_page_views` without its notes)."""
    return survey_page_views(chars, ink, text=text, settings=settings).views
