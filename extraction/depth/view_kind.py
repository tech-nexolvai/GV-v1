"""What kind of drawing a vendor view is, as code sees it: one judgment, never a decision (#1256).

The depth and sink reader reads a countertop's depth on a **plan** (top view), or across the stone
on a **section or side view**, and the sink cut-out on a plan. Which kind a view is therefore
decides which way a depth runs and which spans can be a cut-out. Phase 5 makes that kind need **two
independent judgments** (design §4): two AI readers name it from a picture of the view with its
heading hidden, and code judges it from the heading words and the drawing. This module is the code
half. Its answer is **one judgment**: a caller that acted on it alone would be breaking the rule
"an automatic result needs two independent judgments", so the type says `judged_by="code"` and no
function here returns a decision.

**Why code cannot decide alone.** The vendor's heading can be wrong. On one real sheet the heading
"A PLAN VIEW" sits under the front elevation and "B FRONT VIEW" under the plan; a heading-only rule
reads the elevation as a plan. So the heading is checked against the drawing where the drawing can
say something: a plan looks down on the stone, and the wall behind it is a band (hatched or
filled) along one long edge of the drawing, with nothing of the drawing beyond it. A heading that
says plan with no such band, or says elevation with one, is a conflict, and the conflict makes the
code judgment **unknown** (`kind=None`) with the reason. Unknown is safe: the view is not read.

**What the drawing is not asked.** A section's stone slab is a thin band too, and a hatched floor
can run along an elevation's bottom edge, so the band test is used only where it separates the
two kinds that get confused: plan and elevation. A section, side, detail or other heading is taken
from its words alone and says so in its reason.

**Finding the views on a vendor panel.** Where each view prints a title over a scale note
(`A PLAN VIEW` over `1:10`), `extraction/architect/page_views.py` already finds the views and
their extents, so it is reused unchanged. Some vendors print only a caption under each drawing
(`ELEVATION`, `SECTION AT ...`, `PLAN`) with no scale note; then the captions are found by their
words and size and the panel is cut by them. The captions are also used when `page_views` leaves
drawing out (it rejects a title for having no drawing above it, takes drawing joined to the sheet's
frame for the border, or leaves out ink running far below the titles); then a title printed right
over a scale note counts as a caption too, whatever its size.

**The vendor's sheet frame (#1313).** A pasted vendor sheet often keeps its own frame round the
drawing area. Drawing that touches it made `page_views` reject the drawing as border, and the
caption cut gave the frame to one view, which then covered every other view, so no view on the
panel was cut apart. The frame (`sheet_frame`: straight strokes along the outer edge, past every
caption, peeled from the outside in, with a corner) is taken off before `page_views` is tried
again and before the caption cut, with a note. Views `page_views` finds only after that are read
only where it reports them clearly separated.

**The caption cut, never wrongly (#1313).** Each caption owns the part of the panel above it, down
to its row's bottom; captions printed at slightly different heights are one row; captions on a row
split it at the widest clear gap between them. A stroke is a view's only when both its ends are in
the view's part; ink printed level with a row of captions is the caption's label (bubble,
underline, scale) or the drawing's above only where joined to it; of the rest, only ink joined to
the drawing right above the caption is the view's extent, and notes or a legend standing apart are
left out. A view is **not cut apart** (and nothing on it is read) when its cut takes in readable
ink that is not its drawing's, lettering as large as a caption, or stands right above another
row's caption; when a stroke from its drawing runs well into a neighbour's part; when no clear gap
at least the joining reach wide divides it from a neighbour on its row; or when readable ink stands
just beyond that reach. A stroke that crosses from one view's part into another's marks both views
not separated, as `page_views` does. Nothing is silent: every kind of ink left out, a caption with
no drawing and a panel with no view each leave a note, and every held view says why.

Pure: plain values in, plain values out, page points with `top` downward (pdfplumber's frame).

Source: issues #1256 (phase 5 design §4, §7 PR 1) and #1313 · Verification:
`tests/extraction/depth/test_view_kind.py`
"""

from __future__ import annotations

import re
import statistics
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise
from typing import Final, Literal

from extraction.architect.page_views import (
    INK_OUTSIDE_VIEWS,
    TITLE_NOT_VIEW,
    PageNote,
    PageViewSettings,
    PageViewSurvey,
    _joined,
    survey_page_views,
)
from extraction.architect.reader import MEASURED_ARCHITECT_SETTINGS
from extraction.architect.text import (
    Orientation,
    TextChar,
    TextSettings,
    find_phrases,
    find_printed,
)
from extraction.depth.spans import Span, WallBand
from extraction.geometry.dimension_lines import Axis
from extraction.geometry.rows import Box, InkLine, PageInk
from extraction.page_type import TYPE_WORDS
from vocabulary.page_types import PageType

__all__ = [
    "HEADING_WORDS",
    "MEASURED_VIEW_SETTINGS",
    "CodeViewKind",
    "HeadingKind",
    "VendorView",
    "ViewFinderSettings",
    "ViewKind",
    "ViewSurvey",
    "code_view_kind",
    "find_vendor_views",
    "kind_from_heading",
    "panel_ink",
    "plan_by_projection",
    "separate_views",
    "sheet_frame",
]

_TWO: Final = Decimal(2)
_ZERO: Final = Decimal(0)


class ViewKind(StrEnum):
    """The kinds the depth reader tells apart (design §4)."""

    PLAN = "plan"
    """Looking down on the stone: a depth runs top to bottom; a sink is a rectangle."""
    SECTION = "section"
    """A section or a side view: a cut through the counter, where a depth runs left to right."""
    ELEVATION = "elevation"
    """A front view: doors standing up. Widths and heights, never a depth."""
    DETAIL = "detail"
    OTHER = "other"

    @property
    def page_type(self) -> PageType | None:
        """The shared page vocabulary's word for this kind (`vocabulary/page_types.py`)."""
        return _PAGE_TYPE[self]


_PAGE_TYPE: Final[dict[ViewKind, PageType | None]] = {
    ViewKind.PLAN: PageType.PLAN,
    ViewKind.SECTION: PageType.SECTION,
    ViewKind.ELEVATION: PageType.ELEVATION,
    ViewKind.DETAIL: PageType.DETAIL,
    ViewKind.OTHER: None,
}

#: The page vocabulary's words (`extraction/page_type.TYPE_WORDS`, built on `PageType`), each as the
#: view kind it names. A schedule or a title sheet is a view of no kind the reader uses.
_FROM_PAGE_TYPE: Final[dict[PageType, ViewKind]] = {
    PageType.PLAN: ViewKind.PLAN,
    PageType.SECTION: ViewKind.SECTION,
    PageType.ELEVATION: ViewKind.ELEVATION,
    PageType.DETAIL: ViewKind.DETAIL,
    PageType.SCHEDULE: ViewKind.OTHER,
    PageType.TITLE: ViewKind.OTHER,
}

#: Every heading word and the kind it names: the page vocabulary's words, plus the vendors' own
#: phrases for a view (design §4). Matched longest first on word boundaries, so `SIDE ELEVATION`
#: is a side view and not an elevation, and `PLANNING` is not `PLAN`.
HEADING_WORDS: Final[dict[str, ViewKind]] = {
    **{word: _FROM_PAGE_TYPE[page_type] for word, page_type in TYPE_WORDS.items()},
    "TOP PLAN": ViewKind.PLAN,
    # A floor plan and a base cabinet plan look down on the countertop and print its depth (the
    # answer key reads depths on both). A wall cabinet plan or a ceiling plan looks at something
    # else (#1265 review): a view of another kind for the depth reader, never a countertop plan.
    "FLOOR PLAN": ViewKind.PLAN,
    "BASE CABINET PLAN": ViewKind.PLAN,
    "WALL CABINET PLAN": ViewKind.OTHER,
    "UPPER CABINET PLAN": ViewKind.OTHER,
    "REFLECTED CEILING PLAN": ViewKind.OTHER,
    "CEILING PLAN": ViewKind.OTHER,
    "TOP VIEW": ViewKind.PLAN,
    "SECTION VIEW": ViewKind.SECTION,
    "SECTION AT": ViewKind.SECTION,
    "SIDE VIEW": ViewKind.SECTION,
    "SIDE ELEVATION": ViewKind.SECTION,
    "FRONT VIEW": ViewKind.ELEVATION,
    "FRONT ELEVATION": ViewKind.ELEVATION,
    "DETAIL VIEW": ViewKind.DETAIL,
}

_WORD_PATTERN: Final = re.compile(
    r"\b("
    + "|".join(
        re.escape(word).replace(r"\ ", r"\s+")
        for word in sorted(HEADING_WORDS, key=len, reverse=True)
    )
    + r")\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class HeadingKind:
    """What a view's printed heading says its kind is."""

    heading: str | None
    kind: ViewKind | None
    """`None` when the heading names no kind, names two, or there is no heading."""
    words: tuple[str, ...]
    """The heading words found, as printed (upper-cased, spaces folded)."""
    reason: str


def kind_from_heading(heading: str | None) -> HeadingKind:
    """The kind a heading's words name, or `None` with the reason when they name none or two.

    Two kinds in one heading (`PLAN AND ELEVATION`) is unknown, never a choice between them, as
    `extraction/page_type.classify` rules for a title block.
    """
    if heading is None or not heading.strip():
        return HeadingKind(None, None, (), "the view has no printed heading")
    words = tuple(
        " ".join(match.group(1).upper().split()) for match in _WORD_PATTERN.finditer(heading)
    )
    kinds = {HEADING_WORDS[word] for word in words}
    if not kinds:
        return HeadingKind(
            heading, None, (), f"the heading {heading!r} names no kind of view the reader knows"
        )
    if len(kinds) > 1:
        named = ", ".join(sorted(kind.value for kind in kinds))
        return HeadingKind(
            heading, None, words, f"the heading {heading!r} names more than one kind ({named})"
        )
    (kind,) = kinds
    if kind is ViewKind.OTHER and any(word.endswith("PLAN") for word in words):
        return HeadingKind(
            heading,
            kind,
            words,
            f"the heading {heading!r} names a plan of wall cabinets or a ceiling "
            f"({', '.join(words)}), not a plan of the countertop",
        )
    return HeadingKind(
        heading, kind, words, f"the heading {heading!r} says {kind.value} ({', '.join(words)})"
    )


@dataclass(frozen=True, slots=True)
class CodeViewKind:
    """Code's judgment of one view's kind. **One judgment, never a decision**: the reader acts on a
    kind only when the two AI readers, shown the view with its heading hidden, name the same one."""

    kind: ViewKind | None
    """`None` is "code cannot say": no heading kind, or the heading and the drawing disagree."""
    heading: HeadingKind
    plan_band: bool
    """Whether a wall band runs along a long edge of the drawing, with nothing beyond it."""
    reason: str
    judged_by: Literal["code"] = "code"
    by_projection: bool = False
    """A plan only by `plan_by_projection` (the heading and a projection, no wall band). That cue
    cannot tell a plan from a front view drawn under it, so such a view's kind is reported but
    nothing on it is read (#1265 re-review)."""


def code_view_kind(heading: HeadingKind, bands: Sequence[WallBand]) -> CodeViewKind:
    """Code's judgment of a view's kind from its heading words and its wall bands.

    `bands` are the view's wall bands as `spans.wall_bands` finds them. A **plan band** is one that
    runs along the drawing's width (a horizontal band) at the drawing's edge: the wall behind a
    stone seen from above. Only plan and elevation headings are checked against it; see the module
    docstring for why.
    """
    plan_band = any(band.axis is Axis.HORIZONTAL and band.at_edge for band in bands)
    kind = heading.kind
    if kind is None:
        return CodeViewKind(None, heading, plan_band, heading.reason)
    if kind is ViewKind.PLAN:
        if plan_band:
            return CodeViewKind(
                ViewKind.PLAN,
                heading,
                plan_band,
                f"{heading.reason}, and a wall band runs along a long edge of the drawing",
            )
        return CodeViewKind(
            None,
            heading,
            plan_band,
            f"{heading.reason}, but no wall band runs along a long edge of the drawing, as it "
            "does on a plan: the heading may be wrong (a front view headed plan), so code cannot "
            "say",
        )
    if kind is ViewKind.ELEVATION:
        if plan_band:
            return CodeViewKind(
                None,
                heading,
                plan_band,
                f"{heading.reason}, but a wall band runs along a long edge of the drawing, as it "
                "does on a plan: the heading may be wrong (a plan headed front view), so code "
                "cannot say",
            )
        return CodeViewKind(
            ViewKind.ELEVATION,
            heading,
            plan_band,
            f"{heading.reason}, and no wall band runs along a long edge of the drawing",
        )
    return CodeViewKind(
        kind,
        heading,
        plan_band,
        f"{heading.reason} (taken from the words alone: the wall-band test separates only plan "
        "from elevation)",
    )


def plan_by_projection(
    judged: CodeViewKind,
    overall: Span | None,
    fronts: Sequence[tuple[int, Span]],
    *,
    reach: Decimal,
) -> CodeViewKind:
    """A weak plan cue for a view headed plan with no wall band drawn (#1265 review): it draws the
    same stone width at the same place as a view code judges a front view (its horizontal overall
    starts and ends where the front view's does, within `reach`, on another line), as a plan
    projected from that front view does. `fronts` are `(view number, horizontal overall)` of the
    views code calls front views, cut apart from this one.

    **It is not independent of the heading.** The projection is symmetric: a front view drawn
    under a plan shares its overall too, so with swapped headings and no wall band (a front view
    headed plan, a plan headed front view) code calls the front view a plan. So a plan judged this
    way is marked `by_projection`: its kind is reported, and the reader reads nothing on it. Only a
    wall band makes a plan whose values can be read. A heading that does not say plan is never
    turned into a plan here."""
    if (
        judged.kind is not None
        or judged.heading.kind is not ViewKind.PLAN
        or judged.plan_band
        or overall is None
    ):
        return judged
    for number, front in fronts:
        if (
            abs(front.start_pt - overall.start_pt) <= reach
            and abs(front.end_pt - overall.end_pt) <= reach
            and abs(front.offset_pt - overall.offset_pt) > reach
        ):
            return CodeViewKind(
                ViewKind.PLAN,
                judged.heading,
                judged.plan_band,
                f"{judged.heading.reason}; no wall band is drawn, but the view draws the same "
                f"stone width at the same place as front view {number} (projected from it, as a "
                "plan is; a weak cue, so nothing on it is read)",
                by_projection=True,
            )
    return judged


# ---------------------------------------------------------------------------
# Finding the views on a vendor panel
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ViewFinderSettings:
    """Every length the view finder uses. No defaults; `MEASURED_VIEW_SETTINGS` is the measured
    set."""

    text: TextSettings
    views: PageViewSettings
    """`page_views`' own settings, for panels whose views print a title over a scale note."""
    heading_height_ratio: Decimal
    """A caption is printed at least this many times as tall as the panel's median phrase."""
    heading_line_em: Decimal
    """A second caption line starts within this many caption heights below the first."""
    heading_row_em: Decimal
    """Two captions less than this many caption heights apart, top to bottom, are on one row: no
    drawing fits between them (#1313)."""
    caption_size_fraction: Decimal
    """Lettering at least this fraction of the panel's smallest caption height is printed "as large
    as a caption": inside a view cut by captions it is another caption, a legend's or a title
    block's, and the view is held (#1313)."""
    frame_slack_pt: Decimal
    """A sheet-frame stroke is straight within this, and has no ink beyond it but within this
    (#1313)."""


@dataclass(frozen=True, slots=True)
class VendorView:
    """One drawing view on a vendor panel."""

    number: int
    """Panel-local: top to bottom, then left to right, from 1."""
    heading: str | None
    heading_box: Box | None
    """Everything printed as the view's label (bubble, title, scale): what its picture hides."""
    extent: Box
    separated: bool
    reason: str
    found_by: Literal["title over scale", "heading words"]
    cut_apart: bool = True
    """False when this view's extent overlaps another view's, or takes in another view's caption
    (`separate_views`): the cut could not keep them apart, and nothing on either is read. Unlike
    `separated` (a stroke crossing between views, common where a leader line runs across), this
    always holds."""


@dataclass(frozen=True, slots=True)
class ViewSurvey:
    """The views on one panel, and a note for everything left out."""

    views: tuple[VendorView, ...]
    notes: tuple[str, ...]


def _inside(x: Decimal, y: Decimal, box: Box) -> bool:
    return box.x0 <= x <= box.x1 and box.top <= y <= box.bottom


def panel_ink(ink: PageInk, panel: Box) -> PageInk:
    """The part of a page's ink that lies in `panel`: lines with both ends in it, curves and
    characters with their middle in it."""
    return PageInk(
        width=ink.width,
        height=ink.height,
        lines=tuple(
            line
            for line in ink.lines
            if _inside(line.x0, line.y0, panel) and _inside(line.x1, line.y1, panel)
        ),
        curves=tuple(
            curve for curve in ink.curves if _inside(curve.box.centre_x, curve.box.centre_y, panel)
        ),
        characters=tuple(
            char for char in ink.characters if _inside(char.box.centre_x, char.box.centre_y, panel)
        ),
        drawing_boxes=ink.drawing_boxes,
    )


def find_vendor_views(
    chars: Sequence[TextChar], ink: PageInk, panel: Box, settings: ViewFinderSettings
) -> ViewSurvey:
    """The drawing views on one vendor panel, each with its heading and extent.

    `chars` and `ink` are the page's black and grey text and ink (pdfplumber's frame); only what lies
    in `panel` (the vendor's pasted drawing) is looked at. Views the cut could not keep apart are
    marked not separated (`separate_views`), and the reader reads nothing on them.
    """
    return separate_views(_find_vendor_views(chars, ink, panel, settings))


#: Two views' extents may share this much along each side and still be apart (a shared border).
_SHARED_EDGE_PT: Final = Decimal(1)
#: Two views' drawings overlap when their extents share more than this part of the smaller one's
#: area. Extents are the boxes round each view's ink, so a stroke reaching a few points past the
#: line between two captions makes a sliver of overlap, which is not one drawing taking in another.
_SHARED_AREA_FRACTION: Final = Decimal("0.1")


def _shared(first: Box, second: Box) -> tuple[Decimal, Decimal]:
    return (
        min(first.x1, second.x1) - max(first.x0, second.x0),
        min(first.bottom, second.bottom) - max(first.top, second.top),
    )


def _overlap(first: Box, second: Box) -> bool:
    """Whether a view's extent reaches into a caption (more than a shared border)."""
    width, height = _shared(first, second)
    return width > _SHARED_EDGE_PT and height > _SHARED_EDGE_PT


def _drawings_overlap(first: Box, second: Box) -> bool:
    width, height = _shared(first, second)
    if width <= 0 or height <= 0:
        return False
    smaller = min(first.width * first.height, second.width * second.height)
    return width * height > _SHARED_AREA_FRACTION * smaller


def separate_views(survey: ViewSurvey) -> ViewSurvey:
    """A view's extent must never take in another view's caption or another view's drawing (#1265
    review: a front view's extent swallowed the plan on one sheet, which then got a sliver). Where
    two views' extents overlap, or one holds the other's heading, the cut could not keep them
    apart: **both** are marked not cut apart, with the reason, and nothing on either is read."""
    clashes: dict[int, list[str]] = {view.number: [] for view in survey.views}
    for view in survey.views:
        for other in survey.views:
            if other.number <= view.number:
                continue
            caption = (
                other.heading_box is not None and _overlap(view.extent, other.heading_box)
            ) or (view.heading_box is not None and _overlap(other.extent, view.heading_box))
            if caption or _drawings_overlap(view.extent, other.extent):
                what = "takes in the other's caption" if caption else "overlaps the other's drawing"
                clashes[view.number].append(f"view {other.number} ({what})")
                clashes[other.number].append(f"view {view.number} ({what})")
    if not any(clashes.values()):
        return survey
    views = tuple(
        (
            replace(
                view,
                cut_apart=False,
                reason=(
                    f"{view.reason}; not cut apart from "
                    + ", ".join(clashes[view.number])
                    + ": both views are held, nothing on them is read"
                ),
            )
            if clashes[view.number]
            else view
        )
        for view in survey.views
    )
    return ViewSurvey(views, survey.notes)


def _find_vendor_views(
    chars: Sequence[TextChar], ink: PageInk, panel: Box, settings: ViewFinderSettings
) -> ViewSurvey:
    inside_chars = [char for char in chars if _inside(char.box.centre_x, char.box.centre_y, panel)]
    inside_ink = panel_ink(ink, panel)
    survey = survey_page_views(
        inside_chars, inside_ink, text=settings.text, settings=settings.views
    )
    trouble = _trouble(survey)
    if survey.views and not trouble:
        return _title_over_scale(survey, ())
    captions = _captions(inside_chars, settings)
    unframed, peeled = sheet_frame(
        inside_ink, [caption.box for caption in captions], panel, settings
    )
    notes: list[str] = []
    if peeled:
        notes.append(
            f"left out as the vendor's sheet frame: {peeled} straight stroke(s) running round "
            "every caption, along the panel's outer edge"
        )
        again = survey_page_views(
            inside_chars, unframed, text=settings.text, settings=settings.views
        )
        if again.views and not _trouble(again):
            return _title_over_scale(again, tuple(notes), frame_peeled=True)
        if again.views or _trouble(again):
            trouble = _trouble(again)
    fallback = _views_by_heading_words(inside_chars, unframed, panel, settings, captions)
    if trouble:
        notes.append(
            "the title-over-scale finder left drawing out ("
            + "; ".join(str(note) for note in trouble)
            + "), so the panel was cut by its captions instead"
        )
    return ViewSurvey(fallback.views, (*notes, *fallback.notes))


def _trouble(survey: PageViewSurvey) -> list[PageNote]:
    """What `page_views` left out that makes its views untrustworthy here: a title it rejected, a
    drawing joined to the sheet's border, or ink running far below the titles."""
    return [
        note
        for note in survey.notes
        if note.kind == TITLE_NOT_VIEW
        or (
            note.kind == INK_OUTSIDE_VIEWS
            and (
                note.startswith("left out as the sheet's border")
                or "far below the view titles" in note
            )
        )
    ]


def _title_over_scale(
    survey: PageViewSurvey, notes: tuple[str, ...], *, frame_peeled: bool = False
) -> ViewSurvey:
    """`page_views`' views as vendor views. Where the views were found only once the sheet frame
    was peeled off (#1313), a view `page_views` reports not clearly separated (part of its drawing
    ran across to a neighbour and was left out of both, so its extent may be a piece of the
    drawing) is not cut apart: a section missing its overall dimension could have a part read as
    its depth."""
    views = tuple(
        VendorView(
            number=view.number,
            heading=" ".join(part for part in (view.bubble, view.title) if part),
            heading_box=view.label_box,
            extent=view.extent,
            separated=view.separated,
            reason=(
                view.reason
                if view.separated or not frame_peeled
                else f"{view.reason}; not cut apart: found once the sheet frame was peeled off, "
                "and part of its drawing may be left out: nothing on it is read"
            ),
            found_by="title over scale",
            cut_apart=view.separated or not frame_peeled,
        )
        for view in survey.views
    )
    return ViewSurvey(views, (*notes, *(str(note) for note in survey.notes)))


# --- the vendor's sheet frame --------------------------------------------------------------------

type _Segment = tuple[Decimal, Decimal, Decimal, Decimal]


def _segments(ink: PageInk) -> list[tuple[_Segment, int]]:
    """Every straight piece of the ink, each with where it came from: `-1 - n` for line `n`, or the
    curve's index (a curve is its segments, so a rectangle is four strokes and never one box)."""
    found: list[tuple[_Segment, int]] = [
        ((line.x0, line.y0, line.x1, line.y1), -1 - index) for index, line in enumerate(ink.lines)
    ]
    for index, curve in enumerate(ink.curves):
        points = list(curve.points)
        if curve.closed and len(points) > 2:
            points.append(points[0])
        if len(points) == 1:
            ((x, y),) = points
            found.append(((x, y, x, y), index))
        found.extend(
            ((first[0], first[1], second[0], second[1]), index)
            for first, second in pairwise(points)
        )
    return found


def _segment_box(segment: _Segment) -> Box:
    x0, y0, x1, y1 = segment
    return Box(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def sheet_frame(
    ink: PageInk, captions: Sequence[Box], panel: Box, settings: ViewFinderSettings
) -> tuple[PageInk, int]:
    """The panel's ink without the vendor's own sheet frame, and how many strokes were taken off.

    A vendor's pasted sheet often keeps its frame: a rectangle (or two, one inside the other) round
    the whole drawing area. Where a drawing touches the frame, `page_views` takes the drawing for
    the border and rejects it, and the caption cut gave the frame to one view, which then covered
    every other view. A frame stroke is a straight horizontal or vertical stroke that runs past
    every caption on the panel, on the far side of all of them, from one end of the panel's ink to
    the other, with no ink beyond it but other frame strokes (peeled from the outside in), and with
    both ends on other such strokes or on the panel's edge (where the paste cut the frame off), so
    a drawing whose edge the frame's going leaves outermost keeps it. A frame has a corner, so strokes are taken only when sides both across and up the panel
    are. A drawing's own long line has the drawing's other ink beyond it or past its ends, or ends
    on nothing of the kind; a drawing line drawn exactly on the frame cannot be told from it.
    """
    if not captions:
        return ink, 0
    around = captions[0]
    for box in captions[1:]:
        around = around.union(box)
    slack = settings.frame_slack_pt
    segments = _segments(ink)
    boxes = [_segment_box(segment) for segment, _source in segments]
    sides: dict[int, str] = {}
    for index, box in enumerate(boxes):
        if box.height <= slack and box.x0 <= around.x0 and box.x1 >= around.x1:
            if box.bottom < around.top:
                sides[index] = "top"
            elif box.top > around.bottom:
                sides[index] = "bottom"
        elif box.width <= slack and box.top <= around.top and box.bottom >= around.bottom:
            if box.x1 < around.x0:
                sides[index] = "left"
            elif box.x0 > around.x1:
                sides[index] = "right"
    if not sides:
        return ink, 0

    def ends_meet(index: int, among: Iterable[int], *, both: bool) -> bool:
        """Whether one end (or, with `both`, each end) of side `index` lies on the panel's edge
        or on one of the perpendicular sides `among`."""
        box = boxes[index]
        across = sides[index] in ("top", "bottom")
        ends = (
            ((box.x0, box.top), (box.x1, box.top))
            if across
            else ((box.x0, box.top), (box.x0, box.bottom))
        )
        others = [boxes[other] for other in among if (sides[other] in ("top", "bottom")) != across]
        met = [
            (across and (x - panel.x0 <= slack or panel.x1 - x <= slack))
            or (not across and (y - panel.top <= slack or panel.bottom - y <= slack))
            or any(
                edge.x0 - slack <= x <= edge.x1 + slack
                and edge.top - slack <= y <= edge.bottom + slack
                for edge in others
            )
            for x, y in ends
        ]
        return all(met) if both else any(met)

    sides = {index: side for index, side in sides.items() if ends_meet(index, sides, both=False)}
    if not sides:
        return ink, 0
    characters = [char.box for char in ink.characters]
    peeled: set[int] = set()
    grew = True
    while grew:
        grew = False
        rest = [box for index, box in enumerate(boxes) if index not in peeled] + characters
        if not rest:
            break
        left = min(box.x0 for box in rest)
        right = max(box.x1 for box in rest)
        top = min(box.top for box in rest)
        bottom = max(box.bottom for box in rest)
        outer: set[int] = set()
        for index, side in sides.items():
            box = boxes[index]
            if index in peeled:
                continue
            if side in ("top", "bottom"):
                outermost = (
                    box.top - top <= slack if side == "top" else bottom - box.bottom <= slack
                )
                across = box.x0 - left <= slack and right - box.x1 <= slack
            else:
                outermost = box.x0 - left <= slack if side == "left" else right - box.x1 <= slack
                across = box.top - top <= slack and bottom - box.bottom <= slack
            if outermost and across:
                outer.add(index)
        # A stroke is taken only where both its ends lie on the panel's edge or on frame-like
        # sides (strokes past every caption, on the far side of them all): never a drawing's own
        # edge that the frame's going has left outermost (#1313 review).
        for index in outer:
            if ends_meet(index, sides, both=True):
                peeled.add(index)
                grew = True
    # A frame has a corner: sides across and up the panel. Lone parallel lines are drawing.
    if {sides[index] in ("top", "bottom") for index in peeled} != {True, False}:
        return ink, 0
    lines_out = {-1 - segments[index][1] for index in peeled if segments[index][1] < 0}
    curves_hit = {segments[index][1] for index in peeled if segments[index][1] >= 0}
    kept_lines = [line for index, line in enumerate(ink.lines) if index not in lines_out]
    # What is left of a curve that lost a side stays, as plain strokes.
    kept_lines.extend(
        InkLine(*segment)
        for index, (segment, source) in enumerate(segments)
        if source in curves_hit and index not in peeled
    )
    return (
        replace(
            ink,
            lines=tuple(kept_lines),
            curves=tuple(
                curve for index, curve in enumerate(ink.curves) if index not in curves_hit
            ),
        ),
        len(peeled),
    )


# --- the caption cut -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Caption:
    text: str
    box: Box
    line: Decimal
    """The height of the caption's kind-word line: what "one caption tall" means."""


def _captions(chars: Sequence[TextChar], settings: ViewFinderSettings) -> list[_Caption]:
    """Each view's caption: a phrase naming a kind of view that is printed tall (at least
    `heading_height_ratio` times the panel's median phrase), with any second line right under it;
    or one printed right over a scale note, as `page_views` reads a title, with the note."""
    phrases = find_phrases(chars, settings.text)
    if not phrases:
        return []
    median = Decimal(statistics.median(phrase.height for phrase in phrases))
    tall = [
        phrase
        for phrase in phrases
        if phrase.orientation is Orientation.UPRIGHT
        and phrase.height >= settings.heading_height_ratio * median
    ]
    used: set[int] = set()
    captions: list[_Caption] = []
    for index, phrase in sorted(enumerate(tall), key=lambda pair: pair[1].box.top):
        if index in used or not kind_from_heading(phrase.text).words:
            continue
        used.add(index)
        text, box = phrase.text, phrase.box
        grew = True
        while grew:
            grew = False
            for other_index, other in enumerate(tall):
                if other_index in used:
                    continue
                gap = other.box.top - box.bottom
                if Decimal(
                    0
                ) <= gap <= settings.heading_line_em * phrase.height and other.box.overlaps_x(box):
                    used.add(other_index)
                    text, box = f"{text} {other.text}", box.union(other.box)
                    grew = True
        captions.append(_Caption(text, box, phrase.height))
    views = settings.views
    for scale in find_printed(chars, settings.text).scales:
        em = scale.box.height
        reach = Box(
            scale.box.x0 - views.title_reach_em * em,
            scale.box.top,
            scale.box.x1 + views.title_reach_em * em,
            scale.box.bottom,
        )
        above = [
            phrase
            for phrase in phrases
            if phrase.orientation is Orientation.UPRIGHT
            and kind_from_heading(phrase.text).words
            and Decimal(0) <= scale.box.top - phrase.box.bottom <= views.title_gap_em * em
            and phrase.box.overlaps_x(reach)
        ]
        if not above:
            continue
        title = max(above, key=lambda phrase: phrase.box.bottom)
        meeting = [index for index, caption in enumerate(captions) if caption.box.meets(title.box)]
        if meeting:
            # A tall caption's own scale note, printed under it, is part of its label: the row
            # of captions ends below it, so it never falls into the next view's part (#1313).
            for index in meeting:
                captions[index] = replace(captions[index], box=captions[index].box.union(scale.box))
            continue
        captions.append(_Caption(title.text, title.box.union(scale.box), title.height))
    return captions


def _rows(captions: Sequence[_Caption], settings: ViewFinderSettings) -> list[list[_Caption]]:
    """Captions in rows. Two captions are on one row when they overlap top to bottom, or when the
    gap between them is under `heading_row_em` caption heights: no drawing fits in a gap that small,
    so they are captions of one row printed at slightly different heights (#1313)."""
    rows: list[list[_Caption]] = []
    for caption in sorted(captions, key=lambda item: item.box.top):
        for row in rows:
            if any(
                caption.box.top - other.box.bottom
                < settings.heading_row_em * min(caption.line, other.line)
                and other.box.top - caption.box.bottom
                < settings.heading_row_em * min(caption.line, other.line)
                for other in row
            ):
                row.append(caption)
                break
        else:
            rows.append([caption])
    return [sorted(row, key=lambda item: item.box.centre_x) for row in rows]


def _gutter(
    pieces: Sequence[Box], left: Box, right: Box, top: Decimal, band: Decimal
) -> tuple[Decimal, int, Decimal]:
    """Where two captions' columns split: in the widest gap between their drawings crossed by the
    fewest pieces of ink standing in the row above the captions (#1313: halfway between the
    captions cut through a drawing whose caption is printed at one end of it). The gap is looked
    for across both captions' whole width, since a caption printed to one side of its drawing
    leaves the real gap partly over it; the split itself stays between the two captions. A narrow
    slit inside a hatch is never preferred to the wide gap between two drawings. Also says how many
    pieces cross the split and how wide the gap is, for the caller's check that the two parts are
    clearly apart."""
    low, high = left.x1, right.x0
    middle = (left.centre_x + right.centre_x) / _TWO
    if high <= low:
        return middle, 1, _ZERO
    start, end = left.x0, right.x1
    events: list[tuple[Decimal, int]] = []
    for box in pieces:
        if box.bottom < top or box.top >= band or box.x1 < start or box.x0 > end:
            continue
        events.append((max(box.x0, start), 1))
        events.append((min(box.x1, end), -1))
    events.sort(key=lambda event: (event[0], -event[1]))
    best: tuple[int, Decimal, Decimal, Decimal] | None = None
    depth, previous = 0, start
    for at, change in [*events, (end, 0)]:
        if at > previous and at > low and previous < high:
            here = min(max((previous + at) / _TWO, low), high)
            candidate = (depth, previous - at, abs(here - middle), here)
            if best is None or candidate[:3] < best[:3]:
                best = candidate
        depth += change
        previous = max(previous, at)
    if best is None:
        return middle, 1, _ZERO
    return best[3], best[0], -best[1]


@dataclass(frozen=True, slots=True)
class _Region:
    caption: _Caption
    box: Box
    band: tuple[Decimal, Decimal]
    """Top and bottom of the caption row: ink printed level with it could belong to the view above
    or to the one below, so it belongs to neither."""


def _segment_distance(segment: _Segment, box: Box) -> Decimal:
    """How far a straight stroke passes from a box (0 when it touches or crosses it), near enough:
    the least of its ends' and its middle's distances, and the box corners' to its own box."""
    x0, y0, x1, y1 = segment
    points = [(x0, y0), (x1, y1), ((x0 + x1) / _TWO, (y0 + y1) / _TWO)]

    def to_box(x: Decimal, y: Decimal) -> Decimal:
        return max(box.x0 - x, x - box.x1, box.top - y, y - box.bottom, Decimal(0))

    own = _segment_box(segment)
    corners = [(box.x0, box.top), (box.x1, box.top), (box.x0, box.bottom), (box.x1, box.bottom)]
    return min(
        *(to_box(x, y) for x, y in points),
        *(max(own.x0 - x, x - own.x1, own.top - y, y - own.bottom, Decimal(0)) for x, y in corners),
    )


def _gap(first: Box, second: Box) -> Decimal:
    return max(
        first.x0 - second.x1,
        second.x0 - first.x1,
        first.top - second.bottom,
        second.top - first.bottom,
        Decimal(0),
    )


def _within(inner: Box, outer: Box) -> bool:
    return (
        outer.x0 <= inner.x0
        and inner.x1 <= outer.x1
        and outer.top <= inner.top
        and inner.bottom <= outer.bottom
    )


def _views_by_heading_words(
    chars: Sequence[TextChar],
    ink: PageInk,
    panel: Box,
    settings: ViewFinderSettings,
    captions: Sequence[_Caption],
) -> ViewSurvey:
    """Cut a panel by its captions (no title over a scale note, or `page_views` left drawing out).

    Each caption owns the part of the panel above it: rows of captions split the panel top to
    bottom at each row's bottom, and captions on one row split it at the emptiest line between
    them. A stroke is a view's only when both its ends lie in that view's part; a character only
    when it lies wholly in it. What reaches past every part (below every caption, or past the
    panel's parts), runs from one part into another (not separated), is printed level with a row of
    captions, or is a label hanging on a stroke that runs into another view, is no view's. Of what
    a view owns, only the ink joined to its drawing (the drawing right above its caption, and what
    stands within `side_reach_em` of the panel's median phrase height of it) is its extent; notes,
    a legend or a table standing apart are left out. Every kind of left-out ink leaves a note.

    **Never wrongly** (#1313): a view is not cut apart, and nothing on it is read, when
    * its extent takes in readable ink that is not its drawing's (`_foreign`), or lettering printed
      as large as a caption other than its own (another caption, a legend's or a title block's);
    * it stands right above another row's caption (it may hold that caption's drawing);
    * a stroke from its drawing runs well into another view's part (the cut splits a drawing);
    * no clear gap, at least the joining reach wide and crossed by no ink, divides it from a
      neighbour on its row (code cannot tell whose the ink at the split is);
    * readable ink stands just beyond the joining reach of its drawing (too close to call: it may
      be the drawing's own dimension, left out).
    """
    if not captions:
        return ViewSurvey(
            (),
            (
                (
                    "no view found: no title over a scale note, and no caption naming a kind of "
                    "view is printed on this panel"
                ),
            ),
        )
    phrases = find_phrases(chars, settings.text)
    median = Decimal(statistics.median(phrase.height for phrase in phrases))
    reach = settings.views.side_reach_em * median
    join = settings.views.join_pt
    segments = [segment for segment, _source in _segments(ink)]
    stroke_boxes = [_segment_box(segment) for segment in segments]
    char_boxes = [char.box for char in ink.characters]
    size = settings.caption_size_fraction * min(caption.line for caption in captions)
    lettering = [
        phrase
        for phrase in phrases
        if phrase.orientation is Orientation.UPRIGHT
        and phrase.height >= size
        and sum(character.isalpha() for character in phrase.text)
        >= settings.views.title_minimum_letters
    ]
    rows = _rows(captions, settings)
    close: list[tuple[_Caption, _Caption]] = []
    regions: list[_Region] = []
    previous_bottom = panel.top
    for row in rows:
        row_top = min(caption.box.top for caption in row)
        row_bottom = max(caption.box.bottom for caption in row)
        gutters = [
            _gutter(stroke_boxes + char_boxes, left.box, right.box, previous_bottom, row_top)
            for left, right in pairwise(row)
        ]
        for (left, right), (_at, crossed, width) in zip(pairwise(row), gutters, strict=True):
            if crossed or width < reach:
                # No gap at least the joining reach wide, crossed by no ink, divides them: code
                # cannot tell which drawing the ink either side of the split belongs to.
                close.append((left, right))
        for position, caption in enumerate(row):
            left_edge = panel.x0 if position == 0 else gutters[position - 1][0]
            right_edge = panel.x1 if position == len(row) - 1 else gutters[position][0]
            regions.append(
                _Region(
                    caption,
                    Box(left_edge, previous_bottom, right_edge, row_bottom),
                    (row_top, row_bottom),
                )
            )
        previous_bottom = row_bottom

    def region_of(x: Decimal, y: Decimal) -> int | None:
        for index, region in enumerate(regions):
            if _inside(x, y, region.box):
                return index
        return None

    def level_with_captions(box: Box, index: int) -> bool:
        top, bottom = regions[index].band
        return top <= box.centre_y <= bottom

    caption_boxes = [caption.box for caption in captions]

    def in_caption(box: Box) -> bool:
        return any(_within(box, caption) for caption in caption_boxes)

    owned: dict[int, list[Box]] = {index: [] for index in range(len(regions))}
    crossing: dict[int, set[int]] = {index: set() for index in range(len(regions))}
    split: list[tuple[_Segment, int, int]] = []
    """Strokes running between two parts with more than a graze in each: the cut splits them."""
    loose: list[tuple[Box, bool]] = []
    """Ink that is no view's, each marked whether it is a character."""
    crossing_strokes: list[_Segment] = []
    level_with: dict[int, list[Box]] = {index: [] for index in range(len(regions))}
    outside = level = hanging = 0
    for segment, box in zip(segments, stroke_boxes, strict=True):
        if in_caption(box):
            continue
        ends = (region_of(segment[0], segment[1]), region_of(segment[2], segment[3]))
        if ends[0] is None or ends[1] is None:
            outside += 1
            loose.append((box, False))
        elif ends[0] != ends[1]:
            crossing[ends[0]].add(ends[1])
            crossing[ends[1]].add(ends[0])
            crossing_strokes.append(segment)
            if (
                min(
                    _clipped_length(segment, regions[ends[0]].box),
                    _clipped_length(segment, regions[ends[1]].box),
                )
                > join
            ):
                split.append((segment, ends[0], ends[1]))
        elif level_with_captions(box, ends[0]):
            level_with[ends[0]].append(box)
        else:
            owned[ends[0]].append(box)
    cell = Decimal(24)
    grid: dict[tuple[int, int], list[_Segment]] = defaultdict(list)
    for segment in crossing_strokes:
        grown = _segment_box(segment)
        for column in range(int((grown.x0 - join) // cell), int((grown.x1 + join) // cell) + 1):
            for row_at in range(
                int((grown.top - join) // cell), int((grown.bottom + join) // cell) + 1
            ):
                grid[(column, row_at)].append(segment)

    def near_crossing(box: Box) -> list[_Segment]:
        found: dict[_Segment, None] = {}
        for column in range(int(box.x0 // cell), int(box.x1 // cell) + 1):
            for row_at in range(int(box.top // cell), int(box.bottom // cell) + 1):
                found.update(dict.fromkeys(grid.get((column, row_at), ())))
        return list(found)

    for box in char_boxes:
        if in_caption(box):
            continue
        ends = (region_of(box.x0, box.top), region_of(box.x1, box.bottom))
        if ends[0] is None or ends[1] is None or ends[0] != ends[1]:
            outside += 1
            loose.append((box, True))
        elif level_with_captions(box, ends[0]):
            level_with[ends[0]].append(box)
        elif any(_segment_distance(segment, box) <= join for segment in near_crossing(box)):
            # A label on a leader that runs into another view's part: it may be that view's.
            hanging += 1
            loose.append((box, True))
        else:
            owned[ends[0]].append(box)

    # Ink level with a row of captions: what is joined to a caption (its bubble, underline and
    # scale note) is the caption's label; what is joined to the drawing above is that view's (a
    # wall drawn down beside its caption); anything else there (the next view's top dimension
    # printed beside the caption) could belong to the view above or to the one below: neither.
    labels: dict[int, Box] = {}
    label_ink: list[tuple[str, list[tuple[Box, bool]]]] = []
    """Per caption, the ink taken as its label besides the caption's own words."""
    level_ink: list[tuple[Box, bool]] = []
    char_set = set(char_boxes)
    digits = frozenset(char.box for char in ink.characters if char.text.isdigit())
    beside = settings.views.bubble_reach_em + settings.views.bubble_maximum_em
    for index, pieces in level_with.items():
        caption = regions[index].caption
        top, bottom = regions[index].band
        # A label is the caption's own line: its bubble, underline and scale note, no further to
        # either side than a bubble stands, nor above or below than a bubble is tall (`page_views`'
        # bubble reach and size). Ink joined to the caption that runs further (a dimension printed
        # on the caption's line) is no label, and nor is anything joined to it.
        tall = settings.views.bubble_maximum_em * caption.line
        zone = Box(
            caption.box.x0 - beside * caption.line,
            min(top, caption.box.top) - tall,
            caption.box.x1 + beside * caption.line,
            max(bottom, caption.box.bottom) + tall,
        )
        label = caption.box
        taken: list[tuple[Box, bool]] = []
        rest: list[Box] = []
        for group in _joined([*pieces, caption.box], join):
            members = [pieces[member] for member in group if member < len(pieces)]
            if len(pieces) in group and all(_within(box, zone) for box in members):
                label = _union_all([label, *members])
                taken.extend((box, box in char_set) for box in members)
            else:
                rest.extend(members)
        labels[index] = label
        if taken:
            label_ink.append((caption.text, taken))
        mine = owned[index]
        for group in _joined([*rest, *mine], join):
            members = [rest[member] for member in group if member < len(rest)]
            if any(member >= len(rest) for member in group):
                owned[index].extend(members)
            else:
                level += len(members)
                loose.extend((box, box in char_set) for box in members)
                level_ink.extend((box, box in char_set) for box in members)

    notes: list[str] = []
    if outside:
        notes.append(
            f"left out: {outside} piece(s) of ink lie below every caption, or reach past or across "
            "the captions' parts of the panel, so belong to no view"
        )
    if level:
        notes.append(
            f"left out: {level} piece(s) of ink printed level with a row of captions and not joined "
            "to the drawing above: they could belong to the view above or to the one below"
        )
    for text, taken in label_ink:
        numbers = sum(1 for box, is_char in taken if is_char and box in digits)
        notes.append(
            f"taken as the label of {text!r} and hidden in every picture: {len(taken)} piece(s) "
            + (
                "of ink on its line (its bubble, underline or scale note)"
                if not numbers
                else f"of ink on its line, {numbers} of them digits (a view's number on its "
                "bubble, or a dimension's number printed beside the caption)"
            )
        )
    if hanging:
        notes.append(
            f"left out: {hanging} character(s) of labels on leaders that run into another view's "
            "part of the panel"
        )

    built: list[tuple[int, Box]] = []
    near: dict[int, int] = {}
    apart = apart_pieces = 0
    ordered = sorted(
        range(len(regions)), key=lambda index: (regions[index].box.top, regions[index].box.x0)
    )
    for index in ordered:
        region = regions[index]
        caption = region.caption
        pieces = owned[index]
        if not pieces:
            notes.append(f"not a view: the caption {caption.text!r} has no drawing above it")
            continue
        clusters = [
            (_union_all(pieces[member] for member in group), [pieces[member] for member in group])
            for group in _joined(pieces, join)
        ]
        above = [
            cluster
            for cluster in clusters
            if cluster[0].overlaps_x(caption.box) and cluster[0].top < caption.box.top
        ]
        joined: list[tuple[Box, list[Box]]] = []
        if above:
            # The drawing right above the caption is the caption's, however far above it stands.
            joined.append(
                min(above, key=lambda cluster: max(caption.box.top - cluster[0].bottom, _ZERO))
            )
        waiting = [cluster for cluster in clusters if cluster not in joined]
        reached = [caption.box, *(cluster[0] for cluster in joined)]
        grew = True
        while grew:
            grew = False
            for cluster in list(waiting):
                if any(_gap(cluster[0], other) <= reach for other in reached):
                    joined.append(cluster)
                    reached.append(cluster[0])
                    waiting.remove(cluster)
                    grew = True
        apart += len(waiting)
        apart_pieces += sum(len(cluster[1]) for cluster in waiting)
        loose.extend((box, box in char_set) for cluster in waiting for box in cluster[1])
        if not joined:
            notes.append(f"not a view: the caption {caption.text!r} has no drawing joined to it")
            continue
        extent = _union_all(cluster[0] for cluster in joined)
        # Ink left out only just beyond the reach, and readable (a long stroke or a word): too close
        # to call. It may be the drawing's own dimension, so the cut may have left part out.
        unclear = sum(
            1
            for box, cluster_pieces in waiting
            if min(_gap(box, other[0]) for other in joined) <= _TWO * reach
            and _readable([(piece, piece in char_set) for piece in cluster_pieces], reach, settings)
        )
        built.append((index, extent))
        if unclear:
            near[index] = unclear
    if apart:
        notes.append(
            f"left out: {apart} piece(s) of ink standing apart beside a view (notes, a legend or a "
            f"table; {apart_pieces} strokes and characters)"
        )

    views: list[VendorView] = []
    for index, extent in built:
        region = regions[index]
        caption = region.caption
        held: list[str] = []
        foreign = _foreign(loose, extent, join=join, reach=reach, settings=settings)
        if foreign:
            held.append(
                f"its cut takes in {foreign} piece(s) of ink that are not its drawing's (a "
                "neighbour's labels or dimension, notes or a legend)"
            )
        for phrase in lettering:
            if _within(phrase.box, labels[index]) or not _inside(
                phrase.box.centre_x, phrase.box.centre_y, extent
            ):
                continue
            held.append(
                f"its cut takes in {phrase.text!r}, printed as large as a caption (another view's "
                "caption, a legend's or a title block's)"
            )
        for other_index, other in enumerate(regions):
            if other.band == region.band:
                continue
            below = labels[other_index]
            if below.overlaps_x(extent) and Decimal(0) <= below.top - extent.bottom <= reach:
                held.append(
                    f"its drawing stands right above the caption {other.caption.text!r}, whose "
                    "drawing it may hold"
                )
        # A stroke from this view's drawing well into another part: the cut splits a drawing.
        into = sorted(
            {
                repr(regions[second if first == index else first].caption.text)
                for segment, first, second in split
                if index in (first, second) and _segment_distance(segment, extent) <= join
            }
        )
        for first, second in close:
            if caption in (first, second):
                held.append(
                    "no clear gap divides it from "
                    + repr((second if caption is first else first).text)
                    + " on its row: code cannot tell which drawing the ink near the split belongs to"
                )
        # Ink on a caption row next to this cut that could be its own dimension: left out as
        # level with the captions (a number or a long stroke), or a number taken into a caption's
        # label (#1313 re-review: a dimension printed within a bubble's reach of a caption).
        level_near = _near_readable(
            level_ink, extent, join=join, reach=reach, characters=0, numbers=digits
        )
        if level_near:
            held.append(
                f"{level_near} piece(s) of ink printed level with a row of captions (a number or a "
                "long stroke) stand within twice the joining reach of its cut: they may be its own "
                "dimension"
            )
        for text, taken in label_ink:
            label_numbers = [(box, True) for box, is_char in taken if is_char and box in digits]
            label_near = _near_readable(
                label_numbers, extent, join=join, reach=reach, characters=0, numbers=digits
            )
            if label_near:
                held.append(
                    f"{label_near} digit(s) taken as the label of {text!r} stand within twice the "
                    "joining reach of its cut: they may be its own dimension's number"
                )
        if index in near:
            held.append(
                f"{near[index]} piece(s) of readable ink (a long stroke or a word) stand just beyond "
                "reach of its drawing: they may be its own dimension or label, so the cut may have "
                "left part of the drawing out"
            )
        if into:
            held.append(
                "a stroke (a dimension, a leader or an edge) runs from its drawing well into "
                + ", ".join(into)
                + ", so the cut splits a drawing"
            )
        others = sorted(regions[other].caption.text for other in crossing[index])
        separated = not others
        reason = (
            f"view {caption.text!r} found by its caption's words (no scale note is printed); "
            f"one of {len(captions)} caption(s) on the panel"
            + (
                ""
                if separated
                else "; not clearly separated: strokes cross into "
                + ", ".join(repr(text) for text in others)
            )
        )
        if held:
            reason += (
                "; not cut apart: " + "; ".join(dict.fromkeys(held)) + ": nothing on it is read"
            )
        views.append(
            VendorView(
                number=len(views) + 1,
                heading=caption.text,
                heading_box=labels[index],
                extent=extent,
                separated=separated,
                reason=reason,
                found_by="heading words",
                cut_apart=not held,
            )
        )
    if not views:
        notes.append("no view found: no caption on this panel has a drawing above it")
    return ViewSurvey(tuple(views), tuple(notes))


def _foreign(
    loose: Sequence[tuple[Box, bool]],
    extent: Box,
    *,
    join: Decimal,
    reach: Decimal,
    settings: ViewFinderSettings,
) -> int:
    """How many pieces of ink no view owns lie inside `extent` as something that could be read:
    joined into a group holding a word (`title_minimum_letters` characters or more) or a straight
    stroke at least `reach` long (a dimension line, a drawing's edge, a table's rule). A speck or a
    lone character (a section bubble's letter on a leader) is not counted."""
    inside = [(box, is_char) for box, is_char in loose if _within(box, extent)]
    if not inside:
        return 0
    count = 0
    for group in _joined([box for box, _is_char in inside], join):
        if _readable([inside[member] for member in group], reach, settings):
            count += len(group)
    return count


def _near_readable(
    ink: Sequence[tuple[Box, bool]],
    extent: Box,
    *,
    join: Decimal,
    reach: Decimal,
    characters: int,
    numbers: frozenset[Box],
) -> int:
    """How many pieces of `ink` stand within twice `reach` of `extent` in groups that could be
    read: holding `characters` characters or more (0: no number of characters is enough), a digit
    (one of `numbers`: a dimension's number), or a straight stroke at least `reach` long."""
    close = [(box, is_char) for box, is_char in ink if _gap(box, extent) <= _TWO * reach]
    count = 0
    for group in _joined([box for box, _is_char in close], join) if close else []:
        members = [close[member] for member in group]
        letters = sum(1 for _box, is_char in members if is_char)
        if (
            (characters and letters >= characters)
            or any(box in numbers for box, _is_char in members)
            or any(not is_char and max(box.width, box.height) >= reach for box, is_char in members)
        ):
            count += len(group)
    return count


def _readable(
    pieces: Sequence[tuple[Box, bool]], reach: Decimal, settings: ViewFinderSettings
) -> bool:
    """Whether a group of ink could be read as something: a word (`title_minimum_letters`
    characters or more) or a straight stroke at least `reach` long (a dimension line, an edge)."""
    letters = sum(1 for _box, is_char in pieces if is_char)
    return letters >= settings.views.title_minimum_letters or any(
        not is_char and max(box.width, box.height) >= reach for box, is_char in pieces
    )


def _clipped_length(segment: _Segment, box: Box) -> Decimal:
    """How long the part of a straight stroke inside `box` is (Liang-Barsky clipping), measured
    along its longer axis."""
    x0, y0, x1, y1 = segment
    dx, dy = x1 - x0, y1 - y0
    low, high = Decimal(0), Decimal(1)
    for step, room in (
        (-dx, x0 - box.x0),
        (dx, box.x1 - x0),
        (-dy, y0 - box.top),
        (dy, box.bottom - y0),
    ):
        if step == 0:
            if room < 0:
                return Decimal(0)
            continue
        at = room / step
        if step < 0:
            low = max(low, at)
        else:
            high = min(high, at)
        if low > high:
            return Decimal(0)
    return (high - low) * max(abs(dx), abs(dy))


def _union_all(boxes: Iterable[Box]) -> Box:
    result: Box | None = None
    for box in boxes:
        result = box if result is None else result.union(box)
    if result is None:
        raise ValueError("no boxes to join")
    return result


#: The architect reader's measured text and view settings (a vendor's view prints its title over
#: its scale note as an architect's does); captions are measured on AI Set 3's caption-only vendor
#: panels (2026-10-14): view captions are about three times the dimension text's height, and a
#: two-line caption's second line starts within a point of the first's bottom.
MEASURED_VIEW_SETTINGS: Final = ViewFinderSettings(
    text=MEASURED_ARCHITECT_SETTINGS.text,
    views=MEASURED_ARCHITECT_SETTINGS.views,
    heading_height_ratio=Decimal("1.5"),
    heading_line_em=Decimal("0.5"),
    # Measured on the depth answer key's pages (#1313, 2026-10-15): captions on one row printed at
    # different heights are 1-4 pt apart (about half a caption height); the next row's caption is
    # always several caption heights lower. Legend and title-block lettering is printed at the
    # captions' own height; a drawing's notes are smaller. A frame side is one straight stroke.
    heading_row_em=Decimal(1),
    caption_size_fraction=Decimal("0.9"),
    frame_slack_pt=Decimal(1),
)
